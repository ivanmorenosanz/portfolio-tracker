from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from auth import router as auth_router
from config import STATIC_DIR
from database import ensure_schema
import scheduler as scheduler_module
import services
from routes import router as main_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        print("[STARTUP] Ensuring schema...", flush=True)
        ensure_schema()
        print("[STARTUP] Adding scheduler jobs...", flush=True)
        scheduler_module.configure_scheduler()
        print("[STARTUP] Starting scheduler...", flush=True)
        scheduler_module.scheduler.start()
        print(f"[STARTUP] Scheduler running: {scheduler_module.scheduler.running}", flush=True)
        print("[STARTUP] Running initial auto-contributions...", flush=True)
        services.run_auto_contributions()
        print("[STARTUP] Running initial scheduled expenses...", flush=True)
        services.run_scheduled_expenses()
        print("[STARTUP] Running initial scheduled incomes...", flush=True)
        services.run_scheduled_incomes()
        print("[STARTUP] Running initial scheduled transfers...", flush=True)
        services.run_scheduled_transfers()
        print("[STARTUP] Startup complete!", flush=True)
    except Exception as e:
        print(f"[STARTUP] ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()

    yield

    # Shutdown
    if scheduler_module.scheduler.running:
        scheduler_module.scheduler.shutdown()
    print("[SHUTDOWN] Portfolio app closed.", flush=True)


app = FastAPI(title="Portfolio Pi", lifespan=lifespan)
# SessionMiddleware is GONE: auth is now driven by the JWT cookie issued by the
# shared `auth/` service (see auth/auth_client.py). There's no Starlette session
# to keep, drop the middleware.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def prefix_redirects(request: Request, call_next):
    """Prepend the reverse-proxy path prefix (e.g. /Portfolio) to redirects.

    Caddy strips /Portfolio before proxying, so the app generates unprefixed
    Location headers; this middleware re-adds the prefix (only when the proxy
    says it stripped one, via X-Portfolio-Prefix) so the browser ends up on the
    correct URL. Direct/LAN requests have no header and are untouched.

    Exception: paths owned by the SHARED auth service (/login, /logout,
    /api/auth/*) are served at the bare path on the same domain — adding the
    /Portfolio/ prefix would route them back to this app. They must stay
    unprefixed so Caddy's matching reverse_proxy rules can deliver them
    to the auth service.
    """
    prefix = (request.headers.get("x-portfolio-prefix") or "").rstrip("/")
    response = await call_next(request)
    location = response.headers.get("location")
    if not (prefix and location and location.startswith("/") and not location.startswith("//")):
        return response

    # Don't rewrite paths that belong to the shared auth service.
    AUTH_OWNED_PREFIXES = ("/login", "/logout", "/api/auth/")
    if any(location.startswith(p) for p in AUTH_OWNED_PREFIXES):
        return response

    # Skip if it already has the prefix or is the root alias.
    if location == prefix or location.startswith(prefix + "/"):
        return response

    response.headers["location"] = prefix + location
    return response

app.include_router(auth_router)
app.include_router(main_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
