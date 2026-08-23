from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from config import APPS, CURRENT_APP_ID, TEMPLATES_DIR, request_root_path
from device import device_from_request
from i18n import catalog_for, translate

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["trim_zeros"] = lambda v: f"{v:.4f}".rstrip("0").rstrip(".")
# Expose the app's URL prefix so templates can build correct links when the
# app is served behind a reverse proxy under a path (e.g. /Portfolio).
# Call it with the request: {{ base_path(request) }}
templates.env.globals["base_path"] = request_root_path
# Server-side device detection ('mobile' | 'tablet' | 'desktop'), available to
# templates as {{ device_of(request) }} — used to set a data-device attribute
# so the layout can adapt per device on top of Tailwind breakpoints.
templates.env.globals["device_of"] = device_from_request
# Apps launcher data (the cross-app "portfolio of apps" popup).
templates.env.globals["apps_list"] = APPS
templates.env.globals["current_app_id"] = CURRENT_APP_ID
# i18n: {{ t('key') }} and {{ t('key', name=value) }} resolve against the
# `language` context variable (see `current_language(request)` in routes.py).
@pass_context
def _t(ctx, key, **kwargs):
    lang = ctx.get("language") or "es"
    return translate(key, lang, **kwargs)

templates.env.globals["t"] = _t
# Flat {key: text} dict for one language, for injecting `I18N` into client JS.
templates.env.globals["i18n_dict"] = catalog_for
