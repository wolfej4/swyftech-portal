"""SwyfTech client portal."""
import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, overrides, payments
from .routes_auth import router as auth_router
from .routes_client import router as client_router
from .routes_staff import router as staff_router
from .routes_visits import router as visits_router
from .routes_files import router as files_router
from .routes_reports import router as reports_router
from .routes_admin import router as admin_router
from .web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("portal")

if not config.SECRET_KEY:
    log.warning("SECRET_KEY is not set; using a temporary key. Everyone will be signed out on restart.")
    config.SECRET_KEY = secrets.token_urlsafe(48)

db.init()
overrides.load()

app = FastAPI(title="SwyfTech Client Portal", docs_url=None, redoc_url=None, openapi_url=None)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self' https://checkout.stripe.com"
)


MAX_BODY = (config.MAX_UPLOAD_MB * config.MAX_FILES_PER_MESSAGE + 2) * 1024 * 1024


@app.middleware("http")
async def security_headers(request: Request, call_next):
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        return PlainTextResponse(f"Upload too large. Files can be up to {config.MAX_UPLOAD_MB} MB each.", status_code=413)
    if not request.url.path.startswith("/static"):
        overrides.sync()  # pick up settings another worker just saved
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if config.SECURE_COOKIES:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if not request.url.path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="swyftech_portal",
    max_age=14 * 24 * 3600,  # upper limit; the session length setting is enforced in web.current_user
    same_site="lax",
    https_only=config.SECURE_COOKIES,
)

app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")
app.include_router(auth_router)
app.include_router(client_router)
app.include_router(staff_router)
app.include_router(visits_router)
app.include_router(files_router)
app.include_router(reports_router)
app.include_router(admin_router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (301, 302, 303, 307) and exc.headers and "Location" in exc.headers:
        if request.headers.get("hx-request"):
            return PlainTextResponse("", headers={"HX-Redirect": exc.headers["Location"]})
        return RedirectResponse(exc.headers["Location"], status_code=303)
    messages = {
        400: ("Something didn't go through", exc.detail if isinstance(exc.detail, str) and exc.detail != "Bad Request" else "Go back, refresh the page and try again."),
        403: ("You don't have access to this", "Your account can't open this page. An owner on your team can change what you can see."),
        404: ("We couldn't find that page", "It may have been moved or deleted. Head back to your dashboard."),
        413: ("That upload is too big", f"Files can be up to {config.MAX_UPLOAD_MB} MB each, {config.MAX_FILES_PER_MESSAGE} at a time."),
        503: ("That file can't be opened right now", "The file storage isn't answering. Try again in a few minutes."),
    }
    title, message = messages.get(exc.status_code, ("Something went wrong", "Try again in a moment."))
    return render(request, "error.html", title=title, message=message, status_code=exc.status_code)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not config.STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"error": "webhook not configured"}, status_code=503)
    payload = await request.body()
    try:
        payments.handle_webhook(payload, request.headers.get("stripe-signature", ""))
    except Exception:  # noqa: BLE001
        log.exception("Rejected Stripe webhook")
        return JSONResponse({"error": "invalid"}, status_code=400)
    return JSONResponse({"received": True})


@app.get("/healthz")
def health():
    db.one("SELECT 1")
    return PlainTextResponse("ok")
