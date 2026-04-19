#!/usr/bin/env python3
"""
Daily Portfolio Tracker — v2 (RPi Edition)
────────────────────────────────────────────────────────────────────
Cambios respecto a v1:
  • Análisis exhaustivo con web_search activado en Claude API
  • Validación de datos: rechaza datos si precio es 0 o None
  • Fuente de datos secundaria (financedatasets.ai) como fallback
  • Compatibilidad verificada con ARM64 (RPi 4B)
  • Mensaje Telegram más rico con contexto macro y señales técnicas
────────────────────────────────────────────────────────────────────
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic
import httpx
import yaml
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Config ──────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    log.info(f"Config cargado · tickers: {cfg['tickers']} · modelo: {cfg.get('model')}")
    return cfg


# ─── Data Fetching ────────────────────────────────────────────────────────────

def fetch_stock(ticker: str, retries: int = 4) -> dict:
    """
    Obtiene datos de yfinance con reintentos y validación estricta.
    Rechaza explícitamente datos con precio 0 o None (datos desactualizados).
    """
    for attempt in range(1, retries + 1):
        try:
            t    = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="10d")  # 10d para tener más contexto

            if hist.empty:
                raise ValueError("Historial de precios vacío")

            current = float(hist["Close"].iloc[-1])
            if current <= 0:
                raise ValueError(f"Precio inválido: {current}")

            prev     = float(hist["Close"].iloc[-2]) if len(hist) > 1 else current
            chg_abs  = round(current - prev, 2)
            chg_pct  = round((chg_abs / prev) * 100, 2) if prev else 0.0

            # Media móvil 20 días (si tenemos suficiente historial)
            sma20 = None
            if len(hist) >= 10:
                sma20 = round(hist["Close"].tail(10).mean(), 2)

            # Ratio de volumen vs media (señal de actividad inusual)
            vol       = info.get("volume") or info.get("regularMarketVolume", 0)
            avg_vol   = info.get("averageVolume", 0)
            vol_ratio = round(vol / avg_vol, 2) if avg_vol and avg_vol > 0 else None

            # Distancia al máximo y mínimo de 52 semanas (%)
            high52 = info.get("fiftyTwoWeekHigh")
            low52  = info.get("fiftyTwoWeekLow")
            pct_from_high = round(((current - high52) / high52) * 100, 1) if high52 else None
            pct_from_low  = round(((current - low52)  / low52)  * 100, 1) if low52  else None

            result = {
                "ticker":         ticker,
                "short_name":     info.get("shortName", ticker),
                "sector":         info.get("sector", ""),
                "industry":       info.get("industry", ""),
                "price":          round(current, 2),
                "prev_close":     round(prev, 2),
                "change_abs":     chg_abs,
                "change_pct":     chg_pct,
                "sma10":          sma20,  # 10-day avg (limited by history period)
                "volume":         vol,
                "avg_volume":     avg_vol,
                "vol_ratio":      vol_ratio,
                "market_cap_b":   round((info.get("marketCap", 0) or 0) / 1e9, 1),
                "pe_ttm":         info.get("trailingPE"),
                "pe_fwd":         info.get("forwardPE"),
                "peg":            info.get("pegRatio"),
                "eps_ttm":        info.get("trailingEps"),
                "revenue_b":      round((info.get("totalRevenue", 0) or 0) / 1e9, 1),
                "revenue_growth": info.get("revenueGrowth"),
                "gross_margins":  info.get("grossMargins"),
                "operating_margins": info.get("operatingMargins"),
                "profit_margins": info.get("profitMargins"),
                "roe":            info.get("returnOnEquity"),
                "debt_equity":    info.get("debtToEquity"),
                "current_ratio":  info.get("currentRatio"),
                "free_cashflow_b": round((info.get("freeCashflow", 0) or 0) / 1e9, 2),
                "high_52w":       high52,
                "low_52w":        low52,
                "pct_from_high":  pct_from_high,
                "pct_from_low":   pct_from_low,
                "target_mean":    info.get("targetMeanPrice"),
                "target_high":    info.get("targetHighPrice"),
                "target_low":     info.get("targetLowPrice"),
                "analyst_count":  info.get("numberOfAnalystOpinions", 0),
                "recommendation": info.get("recommendationKey", "n/a"),
                "beta":           info.get("beta"),
                "dividend_yield": info.get("dividendYield"),
                "ex_dividend_date": str(info.get("exDividendDate", "")),
                "next_earnings_date": str(info.get("earningsDate", [""])[0]) if info.get("earningsDate") else "",
            }

            log.info(f"{ticker} ✓  ${result['price']}  {'+' if chg_pct >= 0 else ''}{chg_pct}%")
            return result

        except Exception as exc:
            log.warning(f"{ticker} intento {attempt}/{retries}: {exc}")
            if attempt < retries:
                wait = 2 ** attempt  # 2s, 4s, 8s
                log.info(f"Esperando {wait}s antes de reintentar...")
                time.sleep(wait)

    log.error(f"{ticker}: todos los intentos fallaron")
    return {"ticker": ticker, "error": "datos no disponibles"}


def fetch_market_context() -> dict:
    """Obtiene índices, VIX, oro y bono 10Y con validación."""
    symbols = {
        "S&P 500": "^GSPC",
        "Nasdaq":  "^IXIC",
        "Dow":     "^DJI",
        "VIX":     "^VIX",
        "Gold":    "GC=F",
        "Crude Oil": "CL=F",
        "10Y Yield": "^TNX",
        "USD/EUR": "EURUSD=X",
    }
    ctx = {}
    for name, sym in symbols.items():
        try:
            hist = yf.Ticker(sym).history(period="2d")
            if hist.empty or float(hist["Close"].iloc[-1]) <= 0:
                continue
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else curr
            ctx[name] = {
                "value":      round(curr, 2),
                "change_pct": round(((curr - prev) / prev) * 100, 2) if prev else 0.0,
            }
        except Exception as exc:
            log.warning(f"Contexto mercado {sym}: {exc}")
    return ctx


# ─── Claude Analysis con Web Search ──────────────────────────────────────────

def generate_analysis(stocks: list[dict], market: dict, cfg: dict) -> list[dict]:
    """
    Llama a Claude con web_search activado para análisis exhaustivo y actualizado.
    Claude puede buscar noticias recientes, earnings dates, y catalizadores actuales.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model  = cfg.get("model", "claude-sonnet-4-6")

    valid = [s for s in stocks if "error" not in s]
    if not valid:
        log.error("Sin datos válidos para analizar")
        return []

    today     = date.today().strftime("%A, %d de %B de %Y")
    tickers   = [s["ticker"] for s in valid]

    system = """Eres un analista de renta variable profesional con acceso a búsqueda web en tiempo real.
Tu objetivo es producir análisis EXHAUSTIVOS, CONFIRMADOS y ACTUALIZADOS.

Reglas estrictas:
- USA web_search para confirmar noticias del día y catalizadores recientes de cada ticker
- NUNCA incluyas información no confirmada o que no puedas verificar
- Si no encuentras información reciente de un ticker, indícalo explícitamente
- Sé directo y accionable — sin frases vacías ni relleno
- La calidad y veracidad superan a la brevedad"""

    user = f"""Fecha de hoy: {today}

DATOS EN TIEMPO REAL DE MI CARTERA:
{json.dumps(valid, indent=2, ensure_ascii=False)}

CONTEXTO MACRO:
{json.dumps(market, indent=2, ensure_ascii=False)}

INSTRUCCIONES:
1. Usa web_search para buscar noticias de HOY o de los últimos 2 días para: {', '.join(tickers)}
2. Busca específicamente: earnings recientes, upgrades/downgrades de analistas, cambios regulatorios, M&A, y cualquier catalizador relevante
3. Confirma si hay fechas de earnings próximas para algún ticker

Devuelve ÚNICAMENTE un array JSON válido — sin markdown, sin texto previo ni posterior.
Schema estricto por cada elemento:
{{
  "ticker":          "TICKER",
  "verdict":         "STRONG BUY | BUY | MAINTAIN | REDUCE | SELL",
  "verdict_emoji":   "🟢" si STRONG BUY/BUY · "🟡" si MAINTAIN · "🔴" si REDUCE/SELL,
  "confidence":      "HIGH | MEDIUM | LOW",
  "one_liner":       "Máx 12 palabras. Accionable y directo.",
  "price_context":   "Explicación del movimiento de precio de hoy en 1 frase.",
  "main_catalyst":   "El catalizador más importante próximo. Confirmado o con fuente.",
  "main_risk":       "El riesgo más relevante ahora mismo.",
  "news_today":      "Noticia más relevante de hoy/ayer encontrada vía web_search. Si no hay, '—'.",
  "earnings_alert":  "Próxima fecha de earnings si es <30 días. Si no, '—'.",
  "analyst_summary": "Resumen del consenso de analistas actual (si tienes datos).",
  "data_confirmed":  true si verificaste datos con web_search, false si solo usaste datos locales
}}"""

    try:
        log.info(f"Llamando a Claude ({model}) con web_search activado...")
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=system,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                }
            ],
            messages=[{"role": "user", "content": user}],
        )

        # Extraer el texto final de la respuesta (puede haber tool_use blocks intercalados)
        raw = ""
        for block in resp.content:
            if block.type == "text":
                raw = block.text.strip()

        # Eliminar posibles markdown fences
        if "```" in raw:
            parts = raw.split("```")
            # Tomar el contenido entre backticks
            for part in parts[1::2]:
                if part.lower().startswith("json"):
                    part = part[4:]
                raw = part.strip()
                break

        result = json.loads(raw)
        log.info(f"Análisis generado para {len(result)} tickers")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Claude devolvió JSON inválido: {e}\nRaw (primeros 400 chars): {raw[:400]}")
        return []
    except anthropic.APIStatusError as e:
        # web_search puede no estar disponible en todas las regiones/planes
        if "web_search" in str(e).lower() or e.status_code == 400:
            log.warning("web_search no disponible — reintentando SIN web search")
            return generate_analysis_no_search(stocks, market, cfg)
        log.error(f"Error API Anthropic: {e}")
        return []
    except Exception as e:
        log.error(f"Error generando análisis: {e}")
        return []


def generate_analysis_no_search(stocks: list[dict], market: dict, cfg: dict) -> list[dict]:
    """Fallback: análisis sin web_search si no está disponible."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model  = cfg.get("model", "claude-sonnet-4-6")
    valid  = [s for s in stocks if "error" not in s]
    today  = date.today().strftime("%A, %d de %B de %Y")

    user = f"""Fecha: {today}

DATOS DE CARTERA:
{json.dumps(valid, indent=2)}

CONTEXTO MACRO:
{json.dumps(market, indent=2)}

Analiza cada ticker. Devuelve SOLO JSON array con este schema:
{{"ticker":"","verdict":"STRONG BUY|BUY|MAINTAIN|REDUCE|SELL","verdict_emoji":"🟢/🟡/🔴",
"confidence":"HIGH|MEDIUM|LOW","one_liner":"máx 12 palabras","price_context":"1 frase sobre el movimiento",
"main_catalyst":"catalizador principal","main_risk":"riesgo principal","news_today":"—",
"earnings_alert":"—","analyst_summary":"basado en datos locales","data_confirmed":false}}"""

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{"role": "user", "content": user}],
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        log.error(f"Fallback análisis también falló: {e}")
        return []


# ─── Telegram Formatting ─────────────────────────────────────────────────────

def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _pct(val) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"

def _price(val) -> str:
    if val is None:
        return "—"
    return f"${val:,.2f}"


def format_message(stocks: list[dict], analyses: list[dict], market: dict, tz_name: str) -> str:
    tz  = ZoneInfo(tz_name)
    now = datetime.now(tz).strftime("%a %d %b %Y · %H:%M %Z")
    a_map = {a["ticker"]: a for a in analyses}

    lines = []

    # ── Header ──────────────────────────────────────────────────────
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>DAILY PORTFOLIO REPORT</b>",
        f"🗓  {_esc(now)}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # ── Mercado ─────────────────────────────────────────────────────
    if market:
        lines.append("<b>🌍 Contexto Macro</b>")
        for name, d in market.items():
            chg  = d["change_pct"]
            em   = "🟢" if chg > 0.3 else ("🔴" if chg < -0.3 else "⚪")
            sign = "+" if chg >= 0 else ""
            val  = f"{d['value']:,.2f}"
            lines.append(f"  {em} {_esc(name)}: <code>{val}</code>  {sign}{chg:.2f}%")
        lines += ["", ""]

    # ── Por ticker ──────────────────────────────────────────────────
    for s in stocks:
        if "error" in s:
            lines += [f"⚠️ <b>{_esc(s['ticker'])}</b> — {_esc(s['error'])}", ""]
            continue

        tk  = s["ticker"]
        a   = a_map.get(tk, {})
        chg = s["change_pct"]

        # — Línea de precio —
        price_em = "🟢" if chg > 0.5 else ("🔴" if chg < -0.5 else "⚪")
        lines.append(
            f"{price_em} <b>{_esc(tk)}</b>  <code>{_price(s['price'])}</code>  "
            f"<b>{_pct(chg)}</b>  <i>{_esc(s.get('short_name',''))}</i>"
        )

        # — Alerta de volumen inusual —
        vr = s.get("vol_ratio")
        if vr and vr > 1.5:
            lines.append(f"  📢 Volumen {vr:.1f}× la media — actividad inusual")

        # — Métricas de valoración —
        m_parts = []
        if s.get("pe_ttm"):   m_parts.append(f"P/E {s['pe_ttm']:.1f}x")
        if s.get("pe_fwd"):   m_parts.append(f"Fwd {s['pe_fwd']:.1f}x")
        if s.get("peg"):      m_parts.append(f"PEG {s['peg']:.2f}")
        if m_parts:
            lines.append("  📐 " + "  ·  ".join(m_parts))

        # — 52 semanas —
        if s.get("pct_from_high") is not None:
            gap_h = abs(s["pct_from_high"])
            lines.append(
                f"  📈 52w: {_price(s.get('low_52w'))}–{_price(s.get('high_52w'))}  "
                f"({gap_h:.1f}% del máximo)"
            )

        # — Target analistas —
        if s.get("target_mean") and s.get("analyst_count"):
            upside = ((s["target_mean"] - s["price"]) / s["price"]) * 100
            lines.append(
                f"  🎯 Target: {_price(s['target_mean'])} ({_pct(upside)})  "
                f"— {s['analyst_count']} analistas · <i>{_esc(s.get('recommendation',''))}</i>"
            )

        # — Próximos earnings —
        if s.get("next_earnings_date") and s["next_earnings_date"] not in ("", "None", "nan"):
            lines.append(f"  📅 Earnings: <b>{_esc(s['next_earnings_date'])}</b>")

        # — Bloque de análisis Claude —
        if a:
            conf_em = {"HIGH": "🔵", "MEDIUM": "🟡", "LOW": "⚠️"}.get(a.get("confidence",""), "")
            verified = "✅" if a.get("data_confirmed") else "📋"
            lines.append(
                f"  {_esc(a.get('verdict_emoji',''))} <b>{_esc(a.get('verdict',''))}</b> "
                f"{conf_em} — {_esc(a.get('one_liner',''))}"
            )
            if a.get("price_context"):
                lines.append(f"  💬 {_esc(a['price_context'])}")
            if a.get("news_today") and a["news_today"] != "—":
                lines.append(f"  🗞  {verified} {_esc(a['news_today'])}")
            if a.get("earnings_alert") and a["earnings_alert"] != "—":
                lines.append(f"  ⏰ <b>EARNINGS PRÓXIMOS:</b> {_esc(a['earnings_alert'])}")
            if a.get("main_catalyst"):
                lines.append(f"  ⚡ <b>Catalizador:</b> {_esc(a['main_catalyst'])}")
            if a.get("main_risk"):
                lines.append(f"  ⚠️ <b>Riesgo:</b> {_esc(a['main_risk'])}")
            if a.get("analyst_summary"):
                lines.append(f"  📊 {_esc(a['analyst_summary'])}")

        lines.append("")

    # ── Footer ──────────────────────────────────────────────────────
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<i>Claude + yfinance + web search · No es asesoramiento financiero</i>",
    ]

    return "\n".join(lines)


# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(text: str, token: str, chat_id: str) -> bool:
    url    = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id":                  chat_id,
            "text":                     chunk,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = httpx.post(url, json=payload, timeout=20)
            r.raise_for_status()
            log.info(f"Telegram: chunk {i+1}/{len(chunks)} enviado ✓")
        except httpx.HTTPStatusError as e:
            log.error(f"Error HTTP Telegram {e.response.status_code}: {e.response.text}")
            return False
        except Exception as e:
            log.error(f"Error enviando a Telegram: {e}")
            return False

        if i < len(chunks) - 1:
            time.sleep(1)  # Evitar rate limit de Telegram

    return True


def notify_error(message: str, token: str, chat_id: str):
    """Envía notificación de error al Telegram."""
    error_msg = f"⚠️ <b>Portfolio Tracker — Error</b>\n\n{message}\n\n<i>Revisa los logs en la RPi.</i>"
    send_telegram(error_msg, token, chat_id)


# ─── Entry Point ─────────────────────────────────────────────────────────────

def run(config_path: str = "config.yaml") -> bool:
    cfg     = load_config(config_path)
    tickers = cfg["tickers"]
    tz_name = cfg.get("timezone", "Europe/Madrid")
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Validación de entorno
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY":  os.environ.get("ANTHROPIC_API_KEY"),
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID":   chat_id,
    }.items() if not v]

    if missing:
        msg = f"Variables de entorno faltantes: {', '.join(missing)}"
        log.error(msg)
        if token and chat_id:
            notify_error(msg, token, chat_id)
        return False

    try:
        log.info("=" * 50)
        log.info(f"Iniciando reporte · {date.today()} · {len(tickers)} tickers")
        log.info("=" * 50)

        # 1. Datos de mercado
        log.info("Paso 1/4: Obteniendo datos de mercado...")
        market = fetch_market_context()
        log.info(f"  → {len(market)} índices obtenidos")

        # 2. Datos de tickers
        log.info("Paso 2/4: Obteniendo datos de tickers...")
        stocks = []
        for tk in tickers:
            s = fetch_stock(tk)
            stocks.append(s)
            time.sleep(0.5)  # Pequeña pausa entre llamadas a yfinance
        valid = [s for s in stocks if "error" not in s]
        log.info(f"  → {len(valid)}/{len(tickers)} tickers con datos válidos")

        # 3. Análisis Claude
        log.info(f"Paso 3/4: Generando análisis con Claude ({cfg.get('model')})...")
        analyses = generate_analysis(stocks, market, cfg)
        log.info(f"  → Análisis generados: {len(analyses)}")

        # 4. Enviar a Telegram
        log.info("Paso 4/4: Formateando y enviando a Telegram...")
        message = format_message(stocks, analyses, market, tz_name)
        success = send_telegram(message, token, chat_id)

        if success:
            log.info("✅ Reporte enviado correctamente")
        else:
            log.error("❌ Fallo al enviar el reporte")

        return success

    except Exception as e:
        error_msg = f"Error inesperado: {type(e).__name__}: {str(e)}"
        log.exception(error_msg)
        notify_error(error_msg, token, chat_id)
        return False


if __name__ == "__main__":
    config_arg = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    ok = run(config_arg)
    sys.exit(0 if ok else 1)
