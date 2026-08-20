from fastapi.templating import Jinja2Templates

from config import APPS, CURRENT_APP_ID, TEMPLATES_DIR, request_root_path

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["trim_zeros"] = lambda v: f"{v:.4f}".rstrip("0").rstrip(".")
# Expose the app's URL prefix so templates can build correct links when the
# app is served behind a reverse proxy under a path (e.g. /Portfolio).
# Call it with the request: {{ base_path(request) }}
templates.env.globals["base_path"] = request_root_path
# Apps launcher data (the cross-app "portfolio of apps" popup).
templates.env.globals["apps_list"] = APPS
templates.env.globals["current_app_id"] = CURRENT_APP_ID
