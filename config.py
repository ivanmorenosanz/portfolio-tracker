from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import re
import secrets

from passlib.context import CryptContext

MADRID_TZ = ZoneInfo("Europe/Madrid")


def _to_madrid(dt: datetime) -> datetime:
    """Convert a naive UTC datetime to an aware Madrid local datetime."""
    return dt.replace(tzinfo=timezone.utc).astimezone(MADRID_TZ)


def _in_market_hours(dt_madrid: datetime) -> bool:
    """True if dt falls on a weekday inside extended trading hours (08:30–22:15 Madrid)."""
    if dt_madrid.weekday() >= 5:  # Sat=5, Sun=6
        return False
    minutes = dt_madrid.hour * 60 + dt_madrid.minute
    return 8 * 60 + 30 <= minutes <= 22 * 60 + 15


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR = STATIC_DIR / "record_attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
MAX_ATTACHMENT_SIZE = 8 * 1024 * 1024  # 8 MB
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".pdf", ".txt", ".csv", ".xlsx", ".docx",
}

# ── load .env file if present ─────────────────────────────────────────────────
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

ROOT_PATH = os.getenv("ROOT_PATH", "").rstrip("/")  # optional fallback prefix (usually empty; the reverse proxy sets it per request)


def request_root_path(request) -> str:
    """Return the URL prefix the browser needs for this request.

    The reverse proxy (Caddy) strips the path prefix before proxying and tells
    us about it via the X-Portfolio-Prefix header, so links/redirects can be
    generated correctly. Empty for direct/LAN access (no header).
    """
    return (request.headers.get("x-portfolio-prefix") or ROOT_PATH).rstrip("/")


# ── apps launcher (cross-app "portfolio of apps" popup) ───────────────────────
# Each web app exposes a popup that links to the others. Keep this list in sync
# with the APPS array in FitTracker/index.html.
APPS = [
    {
        "id": "portfolio",
        "name": "Portfolio Pi",
        "tagline": "Inversiones, liquidez y gastos",
        "desc": "Cartera, efectivo, movimientos recurrentes y datos de mercado.",
        "icon": "📈",
        "color": "#0f6a63",
        "path": "/Portfolio",
        "port": 8000,
    },
    {
        "id": "fittracker",
        "name": "FitTracker",
        "tagline": "Gimnasio, peso y nutrición",
        "desc": "Entrenos, peso corporal, pasos y nutrición diaria.",
        "icon": "💪",
        "color": "#2563eb",
        "path": "/FitTracker",
        "port": 8001,
    },
]
CURRENT_APP_ID = "portfolio"


DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'portfolio.db'}")
REFRESH_HOUR = int(os.getenv("REFRESH_HOUR", "6"))
REFRESH_MINUTE = int(os.getenv("REFRESH_MINUTE", "0"))
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
PRICE_REFRESH_INTERVAL_SECONDS = int(os.getenv("PRICE_REFRESH_INTERVAL_SECONDS", str(60 * 60)))
FUND_REFRESH_INTERVAL_SECONDS = int(os.getenv("FUND_REFRESH_INTERVAL_SECONDS", str(60 * 60)))
SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("SNAPSHOT_INTERVAL_SECONDS", str(60 * 60)))


def _parse_key_value_map(raw: str, *, uppercase_value: bool = False) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for part in (raw or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key_raw, value_raw = item.split("=", 1)
        key = key_raw.strip().upper()
        value = value_raw.strip()
        if uppercase_value:
            value = value.upper()
        if key and value:
            mapping[key] = value
    return mapping


FT_FUND_SYMBOL_MAP: dict[str, str] = {
    # Yahoo fund tickers (0P...) are often unavailable in Finnhub and can be
    # rate-limited in Yahoo APIs. FT provides delayed NAV for these funds.
    "0P00000G12.F": "IE0032620787:EUR",   # Vanguard US 500 Stock Index (U.S. 500)
    "0P0001XF3Z.F": "IE000QAZP7L2:EUR",   # iShares Emerging Markets Index Fund (IE) S Acc EUR
    "0P0001AN9H.F": "IE00BDRK7L36:EUR",   # iShares Europe Index Fund (IE) D Acc EUR
    "0P0001AF4X.F": "LU1372006947:EUR",   # Cobas Selection Fund P Acc EUR
    "0P0001CH1E.F": "LU1694789535:EUR",   # DNCA Invest Alpha Bonds B EUR
    "0P0000ZMZS.F": "IE00BF2ZVB54:EUR",   # Wellington Global Health Care Equity Fund EUR D Ac
}
FT_FUND_SYMBOL_MAP.update(_parse_key_value_map(os.getenv("FT_FUND_SYMBOL_MAP", ""), uppercase_value=True))


ASSET_NAME_ALIASES: dict[str, str] = {
    # Friendly commercial names for the fund tickers. Yahoo has no names for
    # these 0P… codes, so the alias is used instead of the raw ticker.
    # Overridable via the ASSET_NAME_ALIASES env var (TICKER=Name;TICKER=Name).
    "0P0001XF3Z.F": "EmergMkts",
    "0P00000G12.F": "S&P 500",
    "0P0001AN9H.F": "iShares Europa",
    "0P0001CH1E.F": "DNCA Alpha Bonds",
    "0P0000ZMZS.F": "Wellington Health Care",
}
ASSET_NAME_ALIASES.update(_parse_key_value_map(os.getenv("ASSET_NAME_ALIASES", "")))

# ── secret key (persistent across restarts) ───────────────────────────────────
_DATA_DIR = DATA_DIR
RECOVERY_CODE_FILE = _DATA_DIR / ".recovery_key"


def _load_or_create_secret_key() -> str:
    key_file = _DATA_DIR / ".secret_key"
    try:
        if key_file.exists():
            return key_file.read_text().strip()
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        key_file.write_text(key)
        return key
    except OSError:
        return secrets.token_hex(32)


def _load_or_create_recovery_code() -> str:
    try:
        if RECOVERY_CODE_FILE.exists():
            return RECOVERY_CODE_FILE.read_text().strip()
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        code = secrets.token_urlsafe(24)
        RECOVERY_CODE_FILE.write_text(code)
        return code
    except OSError:
        return secrets.token_urlsafe(24)


SECRET_KEY = os.getenv("SECRET_KEY") or _load_or_create_secret_key()
RECOVERY_CODE = os.getenv("RECOVERY_CODE") or _load_or_create_recovery_code()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── expense categorisation ────────────────────────────────────────────────────
_EXPENSE_CATEGORIES = [
    ("Vivienda", ["alquiler", "hipoteca", "renta", "comunidad", "seguro hogar", "seguro casa"]),
    ("Suministros", ["luz", "electricidad", "agua", "gas", "internet", "wifi", "fibra", "teléfono", "telefono", "móvil", "movil", "energía", "energia", "endesa", "iberdrola", "naturgy"]),
    ("Alimentación", ["supermercado", "mercado", "compra", "comida", "alimentación", "alimentacion", "restaurante", "café", "cafe", "bar", "mercadona", "carrefour", "lidl", "aldi", "glovo", "deliveroo", "uber eats"]),
    ("Transporte", ["gasolina", "combustible", "diésel", "diesel", "coche", "transporte", "taxi", "uber", "cabify", "tren", "metro", "bus", "autobús", "autobus", "peaje", "aparcamiento", "parking", "renfe", "blablacar", "repostaje", "itv"]),
    ("Salud", ["farmacia", "médico", "medico", "doctor", "salud", "dentista", "óptica", "optica", "clínica", "clinica", "seguro médico", "seguro medico", "seguro salud", "psicólogo", "psicologo", "fisio"]),
    ("Suscripciones", ["suscripción", "suscripcion", "netflix", "spotify", "disney", "hbo", "prime", "youtube", "icloud", "dropbox", "cuota", "membresía", "membresia", "patreon"]),
    ("Ocio", ["cine", "teatro", "concierto", "viaje", "hotel", "vuelo", "ocio", "juego", "steam", "libro", "museo", "festival", "avión", "avion"]),
    ("Educación", ["curso", "formación", "formacion", "libros", "colegio", "universidad", "clases", "academia", "máster", "master", "idiomas"]),
    ("Ropa", ["ropa", "calzado", "zapatos", "zapatillas", "moda", "vestir", "zara", "primark", "h&m"]),
    ("Hogar", ["muebles", "electrodomésticos", "electrodomestico", "decoración", "decoracion", "ikea", "limpieza", "ferretería", "ferreteria", "jardín", "jardin"]),
    ("Tecnología", ["ordenador", "portátil", "portatil", "software", "hardware", "iphone", "samsung", "electrónica", "electronica", "gadget", "teclado", "ratón", "raton", "monitor"]),
    ("Mascotas", ["mascota", "perro", "gato", "veterinario", "pienso"]),
    ("Finanzas", ["banco", "comisión", "comision", "intereses", "impuestos", "hacienda", "seguro", "tarjeta", "préstamo", "prestamo", "multa", "gestoría", "gestoria", "notaría", "notaria"]),
]

_EXPENSE_CATEGORY_NAMES = [name for name, _ in _EXPENSE_CATEGORIES] + ["Otros"]


def _categorize_expense(text: str) -> str:
    t = (text or "").lower()
    for category, keywords in _EXPENSE_CATEGORIES:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", t):
                return category
    return "Otros"
