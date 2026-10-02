"""Request helpers shared by every route: who's signed in, what they may do, and page rendering."""
import secrets
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import markdown as md
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import config, db

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
_tz = ZoneInfo(config.TIMEZONE)

ROLE_LABELS = {
    "owner": "Owner",
    "billing": "Billing",
    "member": "Team member",
    "staff": "SwyfTech staff",
}
def _role_help() -> dict[str, str]:
    equipment = config.snipeit_enabled()
    return {
        "owner": "Everything, including billing, on-site visits" + (", equipment" if equipment else "") + " and managing your team",
        "billing": "Their own requests, plus invoices, payments, contracts, on-site visits"
                   + (" and the equipment list" if equipment else ""),
        "member": "Their own requests" + (", their own devices," if equipment else "") + " and how-to guides",
    }


ROLE_HELP = _role_help()
STATUS_LABELS = {
    "open": "Received",
    "in_progress": "Being worked on",
    "waiting": "Waiting on you",
    "resolved": "Resolved",
    "closed": "Closed",
}
STAFF_STATUS_LABELS = {**STATUS_LABELS, "open": "New", "waiting": "Waiting on client"}
PRIORITY_LABELS = {"low": "Low", "normal": "Normal", "high": "High", "urgent": "Urgent"}
DOC_CATEGORIES = ["Agreement", "Proposal", "Policy", "Network & equipment", "Report", "Other"]

# What each client role is allowed to do.
PERMISSIONS = {
    "owner": {"tickets.all", "billing", "docs.billing", "team", "visits", "reports"},
    "billing": {"billing", "docs.billing", "visits", "reports"},
    "member": set(),
    # SwyfTech staff: Technicians get this; Admins also get STAFF_ADMIN_EXTRA.
    "staff": {"tickets.all", "docs.billing", "team", "staff", "visits"},
}
STAFF_ADMIN_EXTRA = {"admin", "money", "billing", "reports"}
STAFF_ROLE_LABELS = {1: "Admin", 0: "Technician"}
STAFF_ROLE_HELP = {
    1: "Everything, including invoices, reports, visit hours and Admin settings",
    0: "Requests, clients, visits, documents and guides. No billing, reports or settings",
}


def can(user, perm: str) -> bool:
    if not user:
        return False
    perms = PERMISSIONS.get(user["role"], set())
    if user["role"] == "staff" and user["staff_admin"]:
        perms = perms | STAFF_ADMIN_EXTRA
    return perm in perms


# ---- session & auth --------------------------------------------------------

def redirect(url: str) -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": url})


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.one("SELECT * FROM users WHERE id = ? AND active = 1", (uid,))
    if not user or user["session_version"] != request.session.get("sv"):
        request.session.clear()
        return None
    # Session length is checked here (not just by the cookie) so changing it under Admin applies at once.
    started = request.session.get("iat", 0)
    if time.time() - started > config.SESSION_HOURS * 3600:
        request.session.clear()
        return None
    if user["client_id"]:
        client = db.one("SELECT active FROM clients WHERE id = ?", (user["client_id"],))
        if not client or not client["active"]:
            request.session.clear()
            return None
    return user


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise redirect("/login")
    return user


def require_client(request: Request):
    user = require_user(request)
    if user["role"] == "staff":
        raise redirect("/staff")
    return user


def require_staff(request: Request):
    user = require_user(request)
    if user["role"] != "staff":
        raise HTTPException(status_code=404)
    return user


def require_admin(request: Request):
    """SwyfTech staff with the Admin role."""
    user = require_staff(request)
    if not user["staff_admin"]:
        raise HTTPException(status_code=403)
    return user


def require_perm(user, perm: str) -> None:
    if not can(user, perm):
        raise HTTPException(status_code=403)


def start_session(request: Request, user) -> None:
    request.session.clear()
    request.session["uid"] = user["id"]
    request.session["sv"] = user["session_version"]
    request.session["csrf"] = secrets.token_urlsafe(24)
    request.session["iat"] = int(time.time())


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")


# ---- CSRF ------------------------------------------------------------------

def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency for every form POST."""
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(status_code=400, detail="Your session expired. Go back, refresh the page and try again.")


# ---- flash messages --------------------------------------------------------

def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flashes", []).append({"kind": kind, "text": message})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flashes", [])


# ---- rendering -------------------------------------------------------------

def render(request: Request, template: str, status_code: int = 200, **context):
    user = context.pop("user", None) or current_user(request)
    context.update(
        request=request,
        user=user,
        csrf=csrf_token(request),
        flashes=pop_flashes(request),
        cfg=config,
        can=lambda perm: can(user, perm),
        nav=context.get("nav", ""),
    )
    if user and user["client_id"] and "company" not in context:
        row = db.one("SELECT name, snipeit_company_id FROM clients WHERE id = ?", (user["client_id"],))
        context["company"] = row["name"] if row else ""
        context["equipment_on"] = bool(row and row["snipeit_company_id"] and config.snipeit_enabled())
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def money(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def local_dt(value: str | None, fmt: str = "%b %-d, %Y at %-I:%M %p") -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value)
    return dt.astimezone(_tz).strftime(fmt)


def nice_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).strftime("%b %-d, %Y")
    except ValueError:
        return value


def ago(value: str | None) -> str:
    if not value:
        return ""
    delta = datetime.now(_tz) - datetime.fromisoformat(value).astimezone(_tz)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days < 7:
        return "yesterday" if days == 1 else f"{days} days ago"
    return nice_date(value)


def render_markdown(text: str) -> Markup:
    return Markup(md.markdown(text or "", extensions=["fenced_code", "tables", "sane_lists"]))


def filesize(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def invoice_state(inv) -> str:
    """Display state, including 'overdue' which is computed rather than stored."""
    if inv["status"] == "sent" and inv["due_date"] < date.today().isoformat():
        return "overdue"
    return inv["status"]


INVOICE_STATE_LABELS = {"draft": "Draft", "sent": "Due", "overdue": "Overdue", "paid": "Paid", "void": "Void"}

templates.env.filters.update(
    money=money, local_dt=local_dt, nice_date=nice_date, ago=ago,
    markdown=render_markdown, filesize=filesize,
)
templates.env.globals.update(
    ROLE_LABELS=ROLE_LABELS, ROLE_HELP=ROLE_HELP, STAFF_ROLE_LABELS=STAFF_ROLE_LABELS, STAFF_ROLE_HELP=STAFF_ROLE_HELP, STATUS_LABELS=STATUS_LABELS, STAFF_STATUS_LABELS=STAFF_STATUS_LABELS,
    PRIORITY_LABELS=PRIORITY_LABELS, DOC_CATEGORIES=DOC_CATEGORIES,
    invoice_state=invoice_state, INVOICE_STATE_LABELS=INVOICE_STATE_LABELS,
    invoice_total=db.invoice_total,
)
