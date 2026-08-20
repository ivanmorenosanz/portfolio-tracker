from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

import pandas as pd
import requests
import finnhub

from config import (
    ASSET_NAME_ALIASES,
    FINNHUB_API_KEY,
    FT_FUND_SYMBOL_MAP,
    FUND_REFRESH_INTERVAL_SECONDS,
    PRICE_REFRESH_INTERVAL_SECONDS,
)
from database import SessionLocal
from models import Account, Asset, Holding

_finnhub_client = None
_finnhub_lock = threading.Lock()


def _get_finnhub_client():
    """Lazy-init Finnhub client (thread-safe)."""
    global _finnhub_client
    if _finnhub_client is None and FINNHUB_API_KEY:
        with _finnhub_lock:
            if _finnhub_client is None:
                _finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)
    return _finnhub_client


def fetch_latest_price(ticker: str) -> Optional[float]:
    """Fetch latest price from Finnhub (fast, reliable)."""
    client = _get_finnhub_client()

    # Strategy 1: Finnhub quote (real-time, most reliable)
    if client:
        try:
            quote = client.quote(ticker)
            if quote and quote.get("c") and float(quote["c"]) > 0:
                return float(quote["c"])
        except Exception as e:
            print(f"[PRICE] Finnhub quote failed for {ticker}: {e}", flush=True)
            pass

    # Strategy 2: Finnhub intraday (1-minute bars)
    if client:
        try:
            bars = client.stock_candles(ticker, "1", limit=10)
            if bars and bars.get("c"):
                closes = [p for p in bars["c"] if p and p > 0]
                if closes:
                    return float(closes[-1])
        except Exception as e:
            print(f"[PRICE] Finnhub candles failed for {ticker}: {e}", flush=True)
            pass

    # Strategy 3: Fallback to yfinance if Finnhub fails
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # Try fast_info first (doesn't download full history)
        try:
            price = t.fast_info.last_price
            if price is not None and float(price) > 0:
                return float(price)
        except Exception:
            pass
        # Try previous close
        try:
            price = t.fast_info.previous_close
            if price is not None and float(price) > 0:
                return float(price)
        except Exception:
            pass
    except Exception as e:
        print(f"[PRICE] Fallback yfinance failed for {ticker}: {e}", flush=True)
        pass

    # Strategy 4: FT delayed NAV fallback for mapped fund tickers
    try:
        return _download_prices_ft_fallback([ticker]).get(ticker)
    except Exception:
        pass

    return None


def _make_session() -> requests.Session:
    """Return a requests session with browser-like headers."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def _download_prices(tickers: list[str]) -> dict[str, float]:
    """Batch fetch prices from Finnhub with efficient concurrency."""
    if not tickers:
        return {}

    prices: dict[str, float] = {}
    client = _get_finnhub_client()

    # If no Finnhub API key, use FT for mapped funds first, then yfinance.
    if not client:
        print("[PRICE] No Finnhub API key configured, using FT/yfinance fallbacks", flush=True)
        ft_prices = _download_prices_ft_fallback(tickers)
        prices.update(ft_prices)
        missing = [t for t in tickers if t not in prices]
        if missing:
            fallback_prices = _download_prices_yfinance_fallback(missing)
            prices.update(fallback_prices)
        return prices

    # Use ThreadPoolExecutor for parallel Finnhub requests (respects rate limit)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_finnhub_quote, client, ticker): ticker for ticker in tickers}

        for future in as_completed(futures):
            ticker = futures[future]
            try:
                price = future.result()
                if price is not None:
                    prices[ticker] = price
            except Exception as e:
                print(f"[PRICE] Failed to fetch {ticker}: {e}", flush=True)

    # Mapped mutual funds first: FT delayed NAV is more reliable than Yahoo
    # for 0P... fund codes (Yahoo data is often stale or missing for these).
    missing = [t for t in tickers if t not in prices]
    if missing:
        ft_prices = _download_prices_ft_fallback(missing)
        prices.update(ft_prices)

    # Fill any remaining gaps with the yfinance fallback.
    still_missing = [t for t in tickers if t not in prices]
    if still_missing:
        fallback_prices = _download_prices_yfinance_fallback(still_missing)
        prices.update(fallback_prices)

    return prices


def _fetch_finnhub_quote(client, ticker: str) -> Optional[float]:
    """Fetch a single ticker quote from Finnhub."""
    try:
        quote = client.quote(ticker)
        if quote and quote.get("c") and float(quote["c"]) > 0:
            return float(quote["c"])
    except Exception:
        pass
    return None


def _download_prices_yfinance_fallback(tickers: list[str]) -> dict[str, float]:
    """Fetch prices sequentially via yfinance fast_info with delay to avoid rate limits."""
    if not tickers:
        return {}
    prices: dict[str, float] = {}

    import yfinance as yf
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(2)  # avoid Yahoo Finance rate limiting
        try:
            fi = yf.Ticker(ticker).fast_info
            for attr in ("last_price", "previous_close"):
                try:
                    v = float(getattr(fi, attr))
                    if v > 0:
                        prices[ticker] = v
                        break
                except Exception:
                    pass
        except Exception as e:
            print(f"[PRICE] yfinance fast_info failed for {ticker}: {e}", flush=True)

    return prices


def _parse_ft_price_from_summary_html(html_text: str) -> Optional[float]:
    """Extract the fund price/NAV from FT summary markup."""
    match = re.search(
        r"Price\s*\([^)]*\)</span><span class=\"mod-ui-data-list__value\">([^<]+)</span>",
        html_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    raw = match.group(1).strip().replace(" ", "")
    normalized = raw.replace(",", "")
    try:
        value = float(normalized)
        if value > 0:
            return value
    except Exception:
        return None
    return None


def _download_prices_ft_fallback(tickers: list[str]) -> dict[str, float]:
    """Fetch delayed fund prices from FT for mapped fund tickers."""
    if not tickers:
        return {}

    session = _make_session()
    prices: dict[str, float] = {}

    for ticker in tickers:
        ft_symbol = FT_FUND_SYMBOL_MAP.get((ticker or "").upper())
        if not ft_symbol:
            continue
        url = f"https://markets.ft.com/data/funds/tearsheet/summary?s={quote_plus(ft_symbol)}"
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = session.get(url, timeout=25)
                if resp.ok:
                    price = _parse_ft_price_from_summary_html(resp.text)
                    if price is not None:
                        prices[ticker] = price
                        break
            except Exception as e:
                last_error = e
                if attempt == 0:
                    time.sleep(1)
                    continue
        if ticker not in prices and last_error is not None:
            print(f"[PRICE] FT fallback failed for {ticker}: {last_error}", flush=True)

    return prices


# ── per-asset price history (position detail) ────────────────────────────────
_history_cache: dict[str, tuple[float, list[tuple[str, float]]]] = {}
_HISTORY_CACHE_TTL = 1800.0  # seconds; NAV tables are daily, no need to re-fetch often


def _parse_ft_historical_table(html_text: str) -> list[tuple[str, float]]:
    """Parse FT's historical-prices table into [(date, close), ...] oldest-first.

    FT serves the last ~20 trading days server-side; the date-range filter is
    applied client-side via AJAX, so this is the reliable server-rendered slice.
    """
    rows: list[tuple[str, float]] = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html_text, flags=re.DOTALL):
        # Cells may contain nested <span>s (date + volume), so capture raw content
        # and strip tags afterwards.
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.DOTALL)
        if len(cells) < 5:
            continue
        date_match = re.search(r"([A-Z][a-z]+day), ([A-Z][a-z]+) (\d{1,2}), (\d{4})", cells[0])
        if not date_match:
            continue
        try:
            day = datetime.strptime(
                f"{date_match.group(1)}, {date_match.group(2)} {date_match.group(3)}, {date_match.group(4)}",
                "%A, %B %d, %Y",
            )
        except ValueError:
            continue
        close_raw = re.sub(r"<[^>]+>", "", cells[4]).strip().replace(",", "")
        try:
            close = float(close_raw)
        except ValueError:
            continue
        if close > 0:
            rows.append((day.strftime("%Y-%m-%d"), close))
    rows.reverse()  # FT lists newest first
    return rows


def _fetch_asset_history(ticker: str, asset_type: str) -> list[tuple[str, float]]:
    """Daily close series for a single ticker: FT NAV table for funds, yfinance otherwise."""
    key = (ticker or "").upper()
    if not key:
        return []
    cached = _history_cache.get(key)
    if cached and time.time() - cached[0] < _HISTORY_CACHE_TTL:
        return cached[1]

    series: list[tuple[str, float]] = []
    is_fund = (asset_type or "") == "fund" or key.startswith("0P") or key.endswith(".F")
    if is_fund:
        ft_symbol = FT_FUND_SYMBOL_MAP.get(key)
        if ft_symbol:
            try:
                resp = _make_session().get(
                    f"https://markets.ft.com/data/funds/tearsheet/historical?s={quote_plus(ft_symbol)}",
                    timeout=25,
                )
                if resp.ok:
                    series = _parse_ft_historical_table(resp.text)
            except Exception as e:
                print(f"[HISTORY] FT failed for {key}: {e}", flush=True)
    if len(series) < 2:
        try:
            import yfinance as yf
            hist = yf.Ticker(key).history(period="1y", interval="1d", auto_adjust=True, actions=False)
            if hist is not None and not hist.empty:
                series = [
                    (d.strftime("%Y-%m-%d"), float(v))
                    for d, v in zip(hist.index, hist["Close"])
                    if v == v  # drop NaN
                ]
        except Exception as e:
            print(f"[HISTORY] yfinance failed for {key}: {e}", flush=True)

    _history_cache[key] = (time.time(), series)
    return series


def _refresh_single_ticker(asset_id: int, ticker: str) -> None:
    prices = _download_prices([ticker])
    price = prices.get(ticker)
    if price is not None:
        with SessionLocal() as db:
            asset = db.get(Asset, asset_id)
            if asset:
                asset.last_price = price
                asset.last_updated = datetime.utcnow()
                db.commit()


def _is_asset_refresh_due(asset: Asset, now_utc: datetime) -> bool:
    if asset.last_updated is None:
        return True
    if asset.asset_type == "fund":
        return (now_utc - asset.last_updated).total_seconds() >= FUND_REFRESH_INTERVAL_SECONDS
    return (now_utc - asset.last_updated).total_seconds() >= PRICE_REFRESH_INTERVAL_SECONDS


def _fetch_open_price(ticker: str) -> Optional[float]:
    """Return today's opening price for ticker via Finnhub daily candle, fallback to current price."""
    client = _get_finnhub_client()
    if client:
        try:
            import time as _time
            now_ts = int(_time.time())
            from_ts = now_ts - 86400  # yesterday → today window
            bars = client.stock_candles(ticker, "D", from_ts, now_ts)
            if bars and bars.get("s") == "ok" and bars.get("o"):
                opens = [p for p in bars["o"] if p and p > 0]
                if opens:
                    return float(opens[-1])
        except Exception as e:
            print(f"[AUTO] open price fetch failed for {ticker}: {e}", flush=True)
    return None


_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "CHF": "₣",
    "JPY": "¥",
}


def _sym(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency.upper(), currency)


def get_effective_price(asset: Asset) -> float:
    if asset.ticker and asset.last_price is not None:
        return asset.last_price
    if asset.asset_type == "cash":
        return 1.0
    if asset.manual_price is not None:
        return asset.manual_price
    return 0.0


def _asset_source_url(ticker: Optional[str]) -> tuple[str, str]:
    """Return (url, source_label) for a ticker's price source.

    Mapped funds (0P... codes) are priced from FT's delayed NAV feed, so the
    link points to the FT tearsheet instead of Yahoo Finance.
    """
    key = (ticker or "").strip().upper()
    if not key:
        return "", ""
    ft_symbol = FT_FUND_SYMBOL_MAP.get(key)
    if ft_symbol:
        return (
            f"https://markets.ft.com/data/funds/tearsheet/summary?s={quote_plus(ft_symbol)}",
            "FT",
        )
    return (f"https://finance.yahoo.com/quote/{quote_plus(key)}", "Yahoo Finance")


def _compute_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("inf"))
    rsi = 100 - (100 / (1 + rs))
    val = float(rsi.iloc[-1])
    return val if val == val else 50.0  # guard NaN


def _cluster_levels(levels: list[float], threshold: float = 0.02) -> list[float]:
    if not levels:
        return []
    lvs = sorted(set(levels))
    clusters: list[list[float]] = [[lvs[0]]]
    for lv in lvs[1:]:
        if abs(lv - clusters[-1][-1]) / clusters[-1][-1] < threshold:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    return [sum(c) / len(c) for c in clusters]


def _find_support_resistance(hist: pd.DataFrame, n: int = 3) -> dict:
    """Return nearest support/resistance levels using swing high/low detection."""
    highs = hist["High"].values.astype(float)
    lows = hist["Low"].values.astype(float)
    current = float(hist["Close"].iloc[-1])

    res_raw: list[float] = []
    sup_raw: list[float] = []
    for i in range(n, len(highs) - n):
        if all(highs[i] >= highs[i - j] for j in range(1, n + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, n + 1)):
            res_raw.append(highs[i])
        if all(lows[i] <= lows[i - j] for j in range(1, n + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, n + 1)):
            sup_raw.append(lows[i])

    resistances = _cluster_levels(res_raw)
    supports = _cluster_levels(sup_raw)

    sup_below = [s for s in supports if s < current * 0.9995]
    res_above = [r for r in resistances if r > current * 1.0005]

    return {
        "nearest_support": max(sup_below) if sup_below else None,
        "nearest_resistance": min(res_above) if res_above else None,
        "all_supports": sorted(sup_below)[-4:],
        "all_resistances": sorted(res_above)[:4],
    }


def _fetch_analysis_history(ticker: str) -> pd.DataFrame:
    """Return ~1y of daily OHLCV for technical analysis.

    Finnhub candles are tried first — the same provider used for quotes — so the
    analysis no longer depends on Yahoo's rate-limited history endpoint. yfinance
    remains the fallback for tickers Finnhub's free tier doesn't cover (non-US
    symbols, funds, etc.).
    """
    client = _get_finnhub_client()
    if client:
        try:
            end_ts = int(time.time())
            start_ts = end_ts - 400 * 24 * 60 * 60  # ~13 months of daily bars
            candles = client.stock_candles(ticker, "D", start_ts, end_ts)
            if candles and candles.get("s") == "ok":
                timestamps = candles.get("t") or []
                if len(timestamps) >= 20:
                    df = pd.DataFrame(
                        {
                            "Open": candles.get("o") or [],
                            "High": candles.get("h") or [],
                            "Low": candles.get("l") or [],
                            "Close": candles.get("c") or [],
                            "Volume": candles.get("v") or [],
                        },
                        index=pd.to_datetime(timestamps, unit="s"),
                    )
                    df = df[df["Close"].notna() & (df["Close"] > 0)]
                    if len(df) >= 20:
                        return df
        except Exception as e:
            print(f"[ANALYSIS] Finnhub candles failed for {ticker}: {e}", flush=True)

    import yfinance as yf
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            hist = yf.Ticker(ticker).history(
                period="1y", interval="1d", auto_adjust=True, actions=False
            )
            if hist is not None and not hist.empty:
                return hist
            return pd.DataFrame()
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return pd.DataFrame()


# ── provider call caches (avoid Yahoo/Finnhub rate limits) ──────────────────
_search_cache: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_CACHE_TTL = 1800.0  # 30 min: symbol search results change rarely
_price_cache: dict[str, tuple[float, dict]] = {}
_PRICE_CACHE_TTL = 300.0  # 5 min: quote endpoint
_analysis_cache: dict[str, tuple[float, dict]] = {}
_ANALYSIS_CACHE_TTL = 3600.0  # 1h: technical analysis on 1y daily data


def yahoo_symbol_search(search_text: str):
    key = (search_text or "").strip().lower()
    if not key:
        return []
    cached = _search_cache.get(key)
    if cached and time.time() - cached[0] < _SEARCH_CACHE_TTL:
        return cached[1]
    try:
        response = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": search_text, "quotesCount": 12, "newsCount": 0, "enableFuzzyQuery": True},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("quotes", []):
            symbol = item.get("symbol")
            if not symbol:
                continue
            results.append({
                "symbol": symbol,
                "shortname": item.get("shortname") or item.get("longname") or symbol,
                "exchange": item.get("exchange") or "",
                "quoteType": item.get("quoteType") or "",
            })
        _search_cache[key] = (time.time(), results)
        return results
    except Exception:
        # Don't cache failures: a transient rate limit shouldn't poison results
        # for the next 30 minutes.
        return []


def _is_generic_asset_name(name: Optional[str], ticker: Optional[str]) -> bool:
    """True when the current asset name looks like a technical identifier."""
    cleaned_name = (name or "").strip()
    cleaned_ticker = (ticker or "").strip().upper()
    if not cleaned_name:
        return True
    if cleaned_ticker and cleaned_name.upper() == cleaned_ticker:
        return True
    # Names using only uppercase/digits/punctuation are usually instrument codes.
    return bool(re.fullmatch(r"[A-Z0-9.\-_/=]+", cleaned_name))


def _lookup_descriptive_name(ticker: str) -> Optional[str]:
    """Resolve a friendly instrument name from Yahoo search results."""
    normalized = ticker.strip().upper()
    if not normalized:
        return None

    alias = ASSET_NAME_ALIASES.get(normalized)
    if alias:
        return alias

    results = yahoo_symbol_search(normalized)
    exact = next(
        (row for row in results if (row.get("symbol") or "").strip().upper() == normalized),
        None,
    )

    candidates = [exact] if exact else []
    candidates.extend(results)
    for candidate in candidates:
        if not candidate:
            continue
        name = (candidate.get("shortname") or "").strip()
        if name and name.upper() != normalized:
            return name
    return None


def _enrich_generic_asset_names(db: Session, user_id: int) -> None:
    """Replace generic ticker-like names with descriptive names for this user's assets."""
    assets = (
        db.query(Asset)
        .join(Holding, Holding.asset_id == Asset.id)
        .join(Account, Holding.account_id == Account.id)
        .filter(
            Account.user_id == user_id,
            Asset.asset_type != "cash",
            Asset.ticker.isnot(None),
        )
        .all()
    )

    cache: dict[str, Optional[str]] = {}
    seen_asset_ids: set[int] = set()
    changed = False

    for asset in assets:
        if asset.id in seen_asset_ids:
            continue
        seen_asset_ids.add(asset.id)

        if not _is_generic_asset_name(asset.name, asset.ticker):
            continue

        ticker = (asset.ticker or "").strip().upper()
        if not ticker:
            continue

        if ticker not in cache:
            cache[ticker] = _lookup_descriptive_name(ticker)

        descriptive_name = cache[ticker]
        if descriptive_name and descriptive_name != asset.name:
            asset.name = descriptive_name[:120]
            changed = True

    if changed:
        db.commit()
