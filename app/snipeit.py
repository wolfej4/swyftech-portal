"""Read-only Snipe-IT connection.

Each portal client is linked to one Snipe-IT company. The portal reads that company's
assets through the API and never writes anything back. Results are cached briefly so
pages stay fast and Snipe-IT isn't hit on every click.
"""
import html
import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import config

log = logging.getLogger("portal.snipeit")

PAGE_SIZE = 500          # Snipe-IT's default API maximum per page
TIMEOUT_SECONDS = 8
FAILURE_BACKOFF = 60     # after an error, wait this long before trying Snipe-IT again

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


class SnipeITError(Exception):
    """Snipe-IT couldn't be reached or refused the request."""


@dataclass
class Asset:
    id: int
    tag: str
    name: str
    model: str
    manufacturer: str
    category: str
    serial: str
    status: str
    status_meta: str
    assigned_type: str      # 'user', 'location', 'asset' or ''
    assigned_name: str
    assigned_email: str
    location: str
    warranty_expires: date | None
    purchase_date: date | None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.tag})" if self.tag else self.name

    @property
    def snipeit_url(self) -> str:
        return f"{config.SNIPEIT_URL}/hardware/{self.id}"

    @property
    def warranty_state(self) -> str:
        """'ok', 'soon', 'expired' or 'none'."""
        if not self.warranty_expires:
            return "none"
        days = (self.warranty_expires - date.today()).days
        if days < 0:
            return "expired"
        if days <= config.SNIPEIT_WARRANTY_WARN_DAYS:
            return "soon"
        return "ok"

    @property
    def warranty_text(self) -> str:
        state = self.warranty_state
        if state == "none":
            return "No warranty on file"
        when = self.warranty_expires.strftime("%b %-d, %Y")
        return {"ok": f"Warranty until {when}", "soon": f"Warranty ends {when}", "expired": f"Warranty ended {when}"}[state]


# ---- HTTP ------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    if config.SNIPEIT_VERIFY_TLS:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{config.SNIPEIT_URL}/api/v1{path}"
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={
        "Authorization": f"Bearer {config.SNIPEIT_API_TOKEN}",
        "Accept": "application/json",
        "User-Agent": "SwyfTech-Portal",
    })
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise SnipeITError(f"Snipe-IT answered {exc.code} for {path}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SnipeITError(f"Couldn't reach Snipe-IT: {exc}") from exc
    except ValueError as exc:
        raise SnipeITError("Snipe-IT sent something that isn't JSON. Check SNIPEIT_URL.") from exc
    # Snipe-IT reports many errors as HTTP 200 with a status field.
    if isinstance(data, dict) and data.get("status") == "error":
        raise SnipeITError(f"Snipe-IT error: {data.get('messages')}")
    return data


def _get_all(path: str, params: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = _get(path, {**(params or {}), "limit": PAGE_SIZE, "offset": offset})
        batch = page.get("rows") or []
        rows.extend(batch)
        total = int(page.get("total") or 0)
        offset += len(batch)
        if not batch or offset >= total:
            return rows


def _cached(key: str, loader, minutes: int):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit:
        stamp, value = hit
        if isinstance(value, SnipeITError) and now - stamp < FAILURE_BACKOFF:
            raise value
        if not isinstance(value, SnipeITError) and now - stamp < minutes * 60:
            return value
    try:
        value = loader()
    except SnipeITError as exc:
        log.warning("%s", exc)
        with _lock:
            _cache[key] = (now, exc)
        raise
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache(company_id: int | None = None) -> None:
    with _lock:
        if company_id is None:
            _cache.clear()
        else:
            _cache.pop(f"assets:{company_id}", None)


# ---- parsing ---------------------------------------------------------------

def _text(value) -> str:
    """Snipe-IT HTML-escapes strings in API responses; undo that so Jinja escapes them once."""
    return html.unescape(value).strip() if isinstance(value, str) else ""


def _name(obj) -> str:
    return _text(obj.get("name")) if isinstance(obj, dict) else _text(obj)


def _date(obj) -> date | None:
    raw = obj.get("date") if isinstance(obj, dict) else obj
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _parse(row: dict) -> Asset:
    status = row.get("status") or row.get("status_label") or {}
    assigned = row.get("assigned_to") or {}
    model = _name(row.get("model"))
    return Asset(
        id=int(row.get("id") or 0),
        tag=_text(row.get("asset_tag")),
        name=_text(row.get("name")) or model or "Unnamed device",
        model=model,
        manufacturer=_name(row.get("manufacturer")),
        category=_name(row.get("category")) or "Other",
        serial=_text(row.get("serial")),
        status=_text(status.get("name")),
        status_meta=_text(status.get("status_meta")),
        assigned_type=_text(assigned.get("type")),
        assigned_name=_text(assigned.get("name")),
        assigned_email=_text(assigned.get("email")).lower(),
        location=_name(row.get("location")),
        warranty_expires=_date(row.get("warranty_expires")),
        purchase_date=_date(row.get("purchase_date")),
    )


# ---- public ----------------------------------------------------------------

def companies() -> list[dict]:
    """[{'id': 3, 'name': 'Gulf Breeze Dental', 'assets': 14}, ...] for linking clients."""
    def load():
        rows = _get_all("/companies", {"sort": "name", "order": "asc"})
        return [{"id": int(r["id"]), "name": _text(r.get("name")), "assets": int(r.get("assets_count") or 0)} for r in rows]
    return _cached("companies", load, minutes=1)


def assets(company_id: int) -> list[Asset]:
    """Every non-archived asset for a Snipe-IT company, sorted by category then name."""
    def load():
        rows = _get_all("/hardware", {"company_id": company_id, "sort": "name", "order": "asc"})
        parsed = [_parse(r) for r in rows]
        # Snipe-IT should already filter by company; double-check so one client never sees another's gear.
        parsed = [a for r, a in zip(rows, parsed) if int((r.get("company") or {}).get("id") or 0) == company_id]
        parsed = [a for a in parsed if a.status_meta != "archived"]
        return sorted(parsed, key=lambda a: (a.category.lower(), a.name.lower()))
    return _cached(f"assets:{company_id}", load, minutes=config.SNIPEIT_CACHE_MINUTES)


def visible_to(user, all_assets: list[Asset]) -> list[Asset]:
    """Owners, Billing and staff see everything. Team members see devices checked out to
    them (matched by email) plus shared devices checked out to a location, like printers."""
    if user["role"] in ("owner", "billing", "staff"):
        return all_assets
    email = (user["email"] or "").lower()
    return [a for a in all_assets
            if (a.assigned_type == "user" and a.assigned_email and a.assigned_email == email)
            or a.assigned_type == "location"]


def summary(items: list[Asset]) -> dict:
    return {
        "count": len(items),
        "soon": sum(1 for a in items if a.warranty_state == "soon"),
        "expired": sum(1 for a in items if a.warranty_state == "expired"),
    }
