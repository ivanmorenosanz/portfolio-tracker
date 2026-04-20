#!/usr/bin/env python3
"""
Daily Portfolio Tracker — Agente de Inversiones Profesional
"""
import json, logging, os, sys, time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic, httpx, yaml, yfinance as yf
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

def load_config(path="config.yaml"):
    with open(path) as f: cfg = yaml.safe_load(f)
    log.info(f"Config · tickers: {cfg['tickers']} · modelo: {cfg.get('model')}")
    return cfg

def fetch_market_context():
    symbols = {"S&P 500":"^GSPC","Nasdaq":"^IXIC","Dow":"^DJI","VIX":"^VIX","Gold":"GC=F","Crude Oil":"CL=F","10Y Yield":"^TNX","USD/EUR":"EURUSD=X"}
    ctx = {}
    for name, sym in symbols.items():
        try:
            hist = yf.Ticker(sym).history(period="2d")
            if hist.empty: continue
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else curr
            if curr <= 0: continue
            ctx[name] = {"value": round(curr,2), "change_pct": round(((curr-prev)/prev)*100,2) if prev else 0.0}
        except Exception as e: log.warning(f"Mercado {sym}: {e}")
    return ctx

def fetch_stock(ticker, retries=4):
    for attempt in range(1, retries+1):
        try:
            t = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="60d")
            if hist.empty: raise ValueError("Historial vacío")
            current = float(hist["Close"].iloc[-1])
            if current <= 0: raise ValueError(f"Precio inválido: {current}")
            prev = float(hist["Close"].iloc[-2]) if len(hist)>1 else current
            chg_pct = round(((current-prev)/prev)*100,2) if prev else 0.0
            sma20 = round(hist["Close"].tail(20).mean(),2) if len(hist)>=20 else None
            sma50 = round(hist["Close"].tail(50).mean(),2) if len(hist)>=50 else None
            vol = info.get("volume") or info.get("regularMarketVolume",0)
            avg_vol = info.get("averageVolume",0)
            vol_ratio = round(vol/avg_vol,2) if avg_vol and avg_vol>0 else None
            high52 = info.get("fiftyTwoWeekHigh")
            low52 = info.get("fiftyTwoWeekLow")
            earnings_dates = info.get("earningsDate")
            next_earnings = ""
            if earnings_dates:
                if isinstance(earnings_dates,(list,tuple)) and len(earnings_dates)>0: next_earnings = str(earnings_dates[0])
                else: next_earnings = str(earnings_dates)
            result = {
                "ticker": ticker, "short_name": info.get("shortName",ticker),
                "sector": info.get("sector",""), "industry": info.get("industry",""), "country": info.get("country",""),
                "price": round(current,2), "prev_close": round(prev,2), "change_pct": chg_pct,
                "sma20": sma20, "sma50": sma50,
                "above_sma20": (current>sma20) if sma20 else None,
                "above_sma50": (current>sma50) if sma50 else None,
                "vol_ratio": vol_ratio,
                "high_52w": high52, "low_52w": low52,
                "pct_from_high": round(((current-high52)/high52)*100,1) if high52 else None,
                "pct_from_low": round(((current-low52)/low52)*100,1) if low52 else None,
                "market_cap_b": round((info.get("marketCap",0) or 0)/1e9,1),
                "enterprise_val_b": round((info.get("enterpriseValue",0) or 0)/1e9,1),
                "pe_ttm": info.get("trailingPE"), "pe_fwd": info.get("forwardPE"),
                "peg": info.get("pegRatio"), "ps_ratio": info.get("priceToSalesTrailing12Months"),
                "pb_ratio": info.get("priceToBook"), "ev_ebitda": info.get("enterpriseToEbitda"),
                "ev_revenue": info.get("enterpriseToRevenue"),
                "revenue_b": round((info.get("totalRevenue",0) or 0)/1e9,2),
                "revenue_growth": info.get("revenueGrowth"), "earnings_growth": info.get("earningsGrowth"),
                "gross_margins": info.get("grossMargins"), "operating_margins": info.get("operatingMargins"),
                "profit_margins": info.get("profitMargins"), "roe": info.get("returnOnEquity"),
                "roa": info.get("returnOnAssets"), "debt_equity": info.get("debtToEquity"),
                "current_ratio": info.get("currentRatio"),
                "fcf_b": round((info.get("freeCashflow",0) or 0)/1e9,2),
                "cash_b": round((info.get("totalCash",0) or 0)/1e9,2),
                "target_mean": info.get("targetMeanPrice"), "target_high": info.get("targetHighPrice"),
                "target_low": info.get("targetLowPrice"), "analyst_count": info.get("numberOfAnalystOpinions",0),
                "recommendation": info.get("recommendationKey","n/a"),
                "beta": info.get("beta"), "dividend_yield": info.get("dividendYield"),
                "next_earnings": next_earnings, "short_ratio": info.get("shortRatio"),
                "institutional_pct": info.get("heldPercentInstitutions"),
            }
            log.info(f"  {ticker} ✓  ${result['price']}  {'+' if chg_pct>=0 else ''}{chg_pct}%")
            return result
        except Exception as exc:
            log.warning(f"  {ticker} intento {attempt}/{retries}: {exc}")
            if attempt < retries: time.sleep(2**attempt)
    log.error(f"  {ticker}: todos los intentos fallaron")
    return {"ticker": ticker, "error": "datos no disponibles"}

def fetch_news(tickers, max_per_ticker=5):
    result = {}
    now_ts = time.time()
    for tk in tickers:
        try:
            raw_news = yf.Ticker(tk).news or []
            items = []
            for art in raw_news[:max_per_ticker]:
                pub_ts = art.get("content",{}).get("pubDate") or art.get("providerPublishTime",0)
                if isinstance(pub_ts,str):
                    try:
                        dt = datetime.fromisoformat(pub_ts.replace("Z","+00:00"))
                        pub_ts = dt.timestamp()
                    except: pub_ts = 0
                age_h = round((now_ts-float(pub_ts))/3600,1) if pub_ts else None
                title = art.get("content",{}).get("title") or art.get("title","")
                publisher = art.get("content",{}).get("provider",{}).get("displayName") or art.get("publisher","")
                if title: items.append({"title":title,"publisher":publisher,"age_hours":age_h})
            result[tk] = items
            log.info(f"  Noticias {tk}: {len(items)} artículos")
        except Exception as e:
            log.warning(f"  Noticias {tk}: {e}")
            result[tk] = []
    return result

SYSTEM_PROMPT = """Eres un gestor de carteras senior con 20 años de experiencia en renta variable global.
Tu especialidad es el análisis fundamental profundo combinado con contexto macroeconómico.
Tu único objetivo es maximizar el retorno ajustado al riesgo para el inversor.

PRINCIPIOS INQUEBRANTABLES:
1. Sé brutalmente honesto — ni optimismo gratuito ni pesimismo injustificado
2. Cada recomendación debe estar respaldada por datos concretos de los proporcionados
3. Distingue claramente entre certezas y probabilidades
4. El riesgo importa tanto como el retorno potencial
5. Una recomendación vaga no tiene valor — sé específico y accionable
6. Nunca inventes datos, fechas o noticias que no estén en los datos proporcionados"""

def generate_analysis(stocks, market, cfg, news=None):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = cfg.get("model","claude-sonnet-4-6")
    valid = [s for s in stocks if "error" not in s]
    if not valid: return []
    today = date.today().strftime("%A, %d de %B de %Y")

    news_block = ""
    if news:
        lines = []
        for tk, articles in news.items():
            if not articles: lines.append(f"  {tk}: sin titulares"); continue
            lines.append(f"  {tk}:")
            for a in articles:
                age = f"{a['age_hours']}h" if a.get("age_hours") is not None else "?"
                lines.append(f"    [{age}] {a['title']}  ({a['publisher']})")
        news_block = "\n\nNOTICIAS RECIENTES (Yahoo Finance):\n" + "\n".join(lines)

    macro_signals = []
    vix = market.get("VIX",{}).get("value")
    if vix:
        if vix < 15: macro_signals.append(f"VIX {vix} — mercado en complacencia")
        elif vix < 25: macro_signals.append(f"VIX {vix} — volatilidad moderada")
        else: macro_signals.append(f"VIX {vix} — ALTA volatilidad, mercado en miedo")
    yield10 = market.get("10Y Yield",{}).get("value")
    if yield10:
        if yield10 > 4.5: macro_signals.append(f"Bono 10Y al {yield10}% — tipos altos, presión en múltiplos")
        elif yield10 > 3.5: macro_signals.append(f"Bono 10Y al {yield10}% — tipos moderados")
        else: macro_signals.append(f"Bono 10Y al {yield10}% — tipos bajos, favorable renta variable")
    sp_chg = market.get("S&P 500",{}).get("change_pct",0)
    macro_signals.append(f"S&P 500 hoy {'+' if sp_chg>=0 else ''}{sp_chg:.2f}%")
    macro_context = " · ".join(macro_signals)

    user = f"""Fecha: {today}
Contexto macro: {macro_context}

DATOS COMPLETOS DE MERCADO:
{json.dumps(market, indent=2, ensure_ascii=False)}

POSICIONES A ANALIZAR:
{json.dumps(valid, indent=2, ensure_ascii=False)}
{news_block}

─────────────────────────────────────────────────────────
INSTRUCCIONES DE ANÁLISIS

Para cada ticker, produce un informe de inversión profesional.
Cubre OBLIGATORIAMENTE:

1. SITUACIÓN ACTUAL: ¿Qué explica el movimiento de precio de hoy?
2. VALORACIÓN: ¿Está caro, justo o barato? Compara métricas con sector. Calcula upside al target de analistas.
3. FUNDAMENTALES: Calidad del negocio — crecimiento, márgenes, ROE, FCF. ¿Mejorando o deteriorando?
4. MACRO Y SECTOR: ¿Cómo afecta el entorno actual a esta posición?
5. CATALIZADORES: Eventos próximos que puedan mover el precio.
6. RIESGOS CONCRETOS: Los 2-3 riesgos más específicos con probabilidad e impacto.
7. RECOMENDACIÓN: Explícita. ¿Comprar más, mantener, reducir, vender?
   - Si COMPRAR: precio máximo de entrada + precio objetivo
   - Si MANTENER: qué evento cambiaría la visión
   - Si VENDER/REDUCIR: por qué ahora y stop loss referencia

Devuelve ÚNICAMENTE un array JSON válido. Sin markdown, sin texto previo.
Schema:
[
  {{
    "ticker": "TICKER",
    "verdict": "COMPRAR | MANTENER | REDUCIR | VENDER",
    "verdict_emoji": "🟢 COMPRAR · 🟡 MANTENER · 🔴 REDUCIR o VENDER",
    "conviction": "ALTA | MEDIA | BAJA",
    "horizon": "Corto plazo (<3m) | Medio plazo (3-12m) | Largo plazo (>1 año)",
    "situacion_hoy": "2-3 frases sobre estado actual y movimiento de hoy.",
    "valoracion": {{
      "vista_general": "¿Caro, justo o barato? Argumento concreto.",
      "upside_analistasPct": null_o_numero,
      "pe_vs_sector": "Comparativa P/E vs sector.",
      "conclusion": "¿Merece la valoración el nivel de crecimiento esperado?"
    }},
    "fundamentales": {{
      "fortalezas": ["punto 1", "punto 2"],
      "debilidades": ["punto 1", "punto 2"],
      "tendencia": "MEJORANDO | ESTABLE | DETERIORANDO"
    }},
    "macro_impacto": "Cómo afecta el entorno macro a esta posición.",
    "catalizadores": [
      {{"evento": "descripción", "plazo": "fecha o período", "impacto_esperado": "alcista/bajista/neutro"}}
    ],
    "riesgos": [
      {{"riesgo": "descripción concreta", "probabilidad": "alta/media/baja", "impacto": "alto/medio/bajo"}}
    ],
    "recomendacion": {{
      "accion": "Recomendación explícita en 1 frase.",
      "precio_entrada_max": null_o_numero,
      "precio_objetivo": null_o_numero,
      "stop_loss_referencia": null_o_numero,
      "razonamiento": "2-3 frases argumentando con datos concretos."
    }},
    "noticias_clave": "Titular más relevante de los proporcionados, o —.",
    "alerta_earnings": "Fecha earnings si es en <30 días, o —."
  }}
]"""

    try:
        log.info(f"Análisis profundo con Claude ({model})...")
        resp = client.messages.create(model=model, max_tokens=8000, system=SYSTEM_PROMPT,
                                      messages=[{"role":"user","content":user}])
        raw = resp.content[0].text.strip()
        if "```" in raw:
            for part in raw.split("```")[1::2]:
                raw = part[4:].strip() if part.lower().startswith("json") else part.strip()
                break
        result = json.loads(raw)
        log.info(f"Análisis para {len(result)} tickers ✓")
        return result
    except json.JSONDecodeError as e:
        log.error(f"JSON inválido: {e}\nRaw: {raw[:500]}")
        return []
    except anthropic.APIStatusError as e:
        log.error(f"API Anthropic ({e.status_code}): {e.message}")
        return []
    except Exception as e:
        log.error(f"Error análisis: {e}")
        return []

def _esc(t): return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def _pct(v,d=2): return "—" if v is None else f"{'+' if v>=0 else ''}{v:.{d}f}%"
def _price(v): return "—" if v is None else f"${v:,.2f}"
def _ratio(v,s="x"): return "—" if v is None else f"{v:.1f}{s}"

def format_message(stocks, analyses, market, tz_name):
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz).strftime("%a %d %b %Y · %H:%M %Z")
    a_map = {a["ticker"]:a for a in analyses}
    lines = []

    lines += ["━━━━━━━━━━━━━━━━━━━━━━━━━","📊 <b>INFORME DE CARTERA</b>",f"🗓  {_esc(now)}","━━━━━━━━━━━━━━━━━━━━━━━━━",""]
    lines.append("<b>🌍 Mercado Global</b>")
    for name, d in market.items():
        chg = d["change_pct"]
        em = "🟢" if chg>0.3 else ("🔴" if chg<-0.3 else "⚪")
        lines.append(f"  {em} {_esc(name)}: <code>{d['value']:,.2f}</code>  {_pct(chg)}")
    lines += ["",""]

    for s in stocks:
        if "error" in s:
            lines += [f"⚠️ <b>{_esc(s['ticker'])}</b> — datos no disponibles",""]
            continue
        tk = s["ticker"]
        a = a_map.get(tk, {})
        chg = s["change_pct"]
        em = "🟢" if chg>0.5 else ("🔴" if chg<-0.5 else "⚪")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{em} <b>{_esc(tk)}</b>  <code>{_price(s['price'])}</code>  <b>{_pct(chg)}</b>  <i>{_esc(s.get('short_name',''))}</i>")
        lines.append(f"  <i>{_esc(s.get('sector',''))} · {_esc(s.get('country',''))}</i>")

        if s.get("vol_ratio") and s["vol_ratio"]>1.8:
            lines.append(f"  📢 Volumen {s['vol_ratio']:.1f}x la media — actividad inusual")

        lines += ["","  <b>📐 Valoración</b>"]
        row1 = []
        if s.get("pe_ttm"): row1.append(f"P/E {_ratio(s['pe_ttm'])}")
        if s.get("pe_fwd"): row1.append(f"Fwd {_ratio(s['pe_fwd'])}")
        if s.get("peg"): row1.append(f"PEG {s['peg']:.2f}")
        if s.get("ev_ebitda"): row1.append(f"EV/EBITDA {_ratio(s['ev_ebitda'])}")
        if row1: lines.append("  "+"  ·  ".join(row1))
        if s.get("pct_from_high") is not None:
            lines.append(f"  52w: {_price(s.get('low_52w'))} – {_price(s.get('high_52w'))}  ({abs(s['pct_from_high']):.1f}% del máx)")
        if s.get("target_mean") and s.get("analyst_count"):
            upside = ((s["target_mean"]-s["price"])/s["price"])*100
            lines.append(f"  🎯 Consenso: {_price(s['target_mean'])}  ({_pct(upside,1)})  ·  {s['analyst_count']} analistas  ·  <i>{_esc(s.get('recommendation',''))}</i>")

        lines += ["","  <b>📈 Fundamentales</b>"]
        row2 = []
        if s.get("revenue_growth") is not None: row2.append(f"Rev {_pct(s['revenue_growth']*100,1)}")
        if s.get("profit_margins") is not None: row2.append(f"Margen neto {_pct(s['profit_margins']*100,1)}")
        if s.get("roe") is not None: row2.append(f"ROE {_pct(s['roe']*100,1)}")
        if s.get("fcf_b"): row2.append(f"FCF ${s['fcf_b']:.1f}B")
        if row2: lines.append("  "+"  ·  ".join(row2))

        if s.get("sma20") and s.get("sma50"):
            if s.get("above_sma20") and s.get("above_sma50"):
                lines.append("  🟢 Por encima de SMA20 y SMA50 — tendencia alcista")
            elif not s.get("above_sma20") and not s.get("above_sma50"):
                lines.append("  🔴 Por debajo de SMA20 y SMA50 — tendencia bajista")
            elif s.get("above_sma50"):
                lines.append("  🟡 Por encima SMA50, por debajo SMA20 — consolidación")
            else:
                lines.append("  🟡 Por encima SMA20, por debajo SMA50 — rebote")

        if s.get("next_earnings") and s["next_earnings"] not in ("","None","nan","NaT"):
            lines.append(f"  📅 Próximos earnings: <b>{_esc(s['next_earnings'])}</b>")

        if not a:
            lines += ["","  <i>Análisis no disponible</i>",""]
            continue

        lines.append("")
        conv_em = {"ALTA":"🔵","MEDIA":"🟡","BAJA":"⚠️"}.get(a.get("conviction",""),"")
        lines.append(f"  {a.get('verdict_emoji','')} <b>{_esc(a.get('verdict',''))}</b>  {conv_em} Convicción {_esc(a.get('conviction',''))}  ·  {_esc(a.get('horizon',''))}")
        if a.get("situacion_hoy"):
            lines.append(f"  💬 {_esc(a['situacion_hoy'])}")

        val_b = a.get("valoracion",{})
        if val_b:
            lines += ["","  <b>💡 Valoración</b>"]
            if val_b.get("vista_general"): lines.append(f"  {_esc(val_b['vista_general'])}")
            if val_b.get("pe_vs_sector"): lines.append(f"  {_esc(val_b['pe_vs_sector'])}")
            if val_b.get("conclusion"): lines.append(f"  <i>{_esc(val_b['conclusion'])}</i>")

        fund = a.get("fundamentales",{})
        if fund:
            lines += ["","  <b>📊 Fundamentales</b>"]
            tend_em = {"MEJORANDO":"🟢","ESTABLE":"🟡","DETERIORANDO":"🔴"}.get(fund.get("tendencia",""),"")
            lines.append(f"  Tendencia: {tend_em} {_esc(fund.get('tendencia',''))}")
            for f_str in fund.get("fortalezas",[]): lines.append(f"  ✅ {_esc(f_str)}")
            for d_str in fund.get("debilidades",[]): lines.append(f"  ⚡ {_esc(d_str)}")

        if a.get("macro_impacto"):
            lines += ["",f"  <b>🌐 Macro:</b> {_esc(a['macro_impacto'])}"]

        cats = a.get("catalizadores",[])
        if cats:
            lines += ["","  <b>⚡ Catalizadores</b>"]
            for c in cats[:3]:
                imp_em = {"alcista":"🟢","bajista":"🔴","neutro":"⚪"}.get(c.get("impacto_esperado","").lower(),"")
                lines.append(f"  {imp_em} {_esc(c.get('evento',''))}  <i>{_esc(c.get('plazo',''))}</i>")

        risks = a.get("riesgos",[])
        if risks:
            lines += ["","  <b>⚠️ Riesgos</b>"]
            for r in risks[:3]:
                prob_em = {"alta":"🔴","media":"🟡","baja":"🟢"}.get(r.get("probabilidad","").lower(),"⚪")
                lines.append(f"  {prob_em} {_esc(r.get('riesgo',''))}  [prob: {_esc(r.get('probabilidad','?'))} · impacto: {_esc(r.get('impacto','?'))}]")

        if a.get("noticias_clave") and a["noticias_clave"]!="—":
            lines += ["",f"  🗞  {_esc(a['noticias_clave'])}"]
        if a.get("alerta_earnings") and a["alerta_earnings"]!="—":
            lines.append(f"  ⏰ <b>EARNINGS:</b> {_esc(a['alerta_earnings'])}")

        rec = a.get("recomendacion",{})
        if rec:
            lines += ["","  ┌─ <b>RECOMENDACIÓN DE INVERSIÓN</b>",f"  │  {_esc(rec.get('accion',''))}"]
            if rec.get("precio_entrada_max"): lines.append(f"  │  💰 Entrada máx: <code>{_price(rec['precio_entrada_max'])}</code>")
            if rec.get("precio_objetivo"): lines.append(f"  │  🎯 Precio objetivo: <code>{_price(rec['precio_objetivo'])}</code>")
            if rec.get("stop_loss_referencia"): lines.append(f"  │  🛑 Stop ref: <code>{_price(rec['stop_loss_referencia'])}</code>")
            if rec.get("razonamiento"): lines.append(f"  └─ <i>{_esc(rec['razonamiento'])}</i>")

        lines.append("")

    lines += ["━━━━━━━━━━━━━━━━━━━━━━━━━","<i>Análisis por Claude · No es asesoramiento financiero</i>"]
    return "\n".join(lines)

def send_telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0,len(text),4000)]
    for i, chunk in enumerate(chunks):
        try:
            r = httpx.post(url, json={"chat_id":chat_id,"text":chunk,"parse_mode":"HTML","disable_web_page_preview":True}, timeout=20)
            r.raise_for_status()
            log.info(f"Telegram: chunk {i+1}/{len(chunks)} ✓")
        except Exception as e:
            log.error(f"Telegram error: {e}")
            return False
        if i < len(chunks)-1: time.sleep(1)
    return True

def notify_error(message, token, chat_id):
    send_telegram(f"⚠️ <b>Portfolio Tracker — Error</b>\n\n{message}\n\n<i>Revisa los logs en la RPi.</i>", token, chat_id)

def run(config_path="config.yaml"):
    cfg = load_config(config_path)
    tickers = cfg["tickers"]
    tz_name = cfg.get("timezone","Europe/Madrid")
    token = os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID","")

    missing = [k for k,v in {"ANTHROPIC_API_KEY":os.environ.get("ANTHROPIC_API_KEY"),"TELEGRAM_BOT_TOKEN":token,"TELEGRAM_CHAT_ID":chat_id}.items() if not v]
    if missing:
        msg = f"Variables faltantes: {', '.join(missing)}"
        log.error(msg)
        if token and chat_id: notify_error(msg, token, chat_id)
        return False

    try:
        log.info("="*50)
        log.info(f"Iniciando · {date.today()} · {len(tickers)} tickers")
        log.info("="*50)

        log.info("Paso 1/4: Mercado global...")
        market = fetch_market_context()
        log.info(f"  → {len(market)} índices")

        log.info("Paso 2/4: Datos de tickers (60d histórico + fundamentales)...")
        stocks = []
        for tk in tickers:
            stocks.append(fetch_stock(tk))
            time.sleep(0.5)
        valid = [s for s in stocks if "error" not in s]
        log.info(f"  → {len(valid)}/{len(tickers)} válidos")

        log.info("Paso 3/4: Noticias recientes...")
        news = fetch_news([s["ticker"] for s in valid], max_per_ticker=5)

        log.info("Paso 4/4: Análisis profundo con Claude...")
        analyses = generate_analysis(stocks, market, cfg, news=news)
        log.info(f"  → {len(analyses)} análisis generados")

        message = format_message(stocks, analyses, market, tz_name)
        success = send_telegram(message, token, chat_id)
        log.info("✅ Informe enviado" if success else "❌ Error al enviar")
        return success

    except Exception as e:
        msg = f"Error: {type(e).__name__}: {e}"
        log.exception(msg)
        notify_error(msg, token, chat_id)
        return False

if __name__ == "__main__":
    config_arg = sys.argv[1] if len(sys.argv)>1 else "config.yaml"
    sys.exit(0 if run(config_arg) else 1)