"""Everything a client's people see after signing in."""
import logging
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from . import accounts, attachments, config, db, feedback, mailer, payments, security, snipeit, storage, visits
from .web import (
    ROLE_HELP, can, client_ip, flash, money, render, require_client, require_perm, require_user,
    start_session, templates, verify_csrf,
)

log = logging.getLogger("portal.client")
router = APIRouter()
CSRF = [Depends(verify_csrf)]

URGENCY_CHOICES = [
    ("low", "It can wait a few days"),
    ("normal", "It's slowing us down"),
    ("high", "Someone can't work"),
    ("urgent", "The whole office is down"),
]


# ---- helpers ---------------------------------------------------------------

def _ticket_scope(user) -> tuple[str, tuple]:
    if can(user, "tickets.all"):
        return "t.client_id = ?", (user["client_id"],)
    return "t.client_id = ? AND t.created_by = ?", (user["client_id"], user["id"])


def _get_ticket(user, ticket_id: int):
    where, params = _ticket_scope(user)
    ticket = db.one(
        f"SELECT t.*, u.name AS author FROM tickets t JOIN users u ON u.id = t.created_by WHERE t.id = ? AND {where}",
        (ticket_id, *params),
    )
    if not ticket:
        raise HTTPException(status_code=404)
    return ticket


def _messages(ticket_id: int):
    return db.all(
        "SELECT m.*, u.name AS author, u.role AS author_role FROM ticket_messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.ticket_id = ? AND m.internal = 0 ORDER BY m.id",
        (ticket_id,),
    )


def _get_invoice(user, invoice_id: int):
    require_perm(user, "billing")
    inv = db.one("SELECT * FROM invoices WHERE id = ? AND client_id = ? AND status != 'draft'", (invoice_id, user["client_id"]))
    if not inv:
        raise HTTPException(status_code=404)
    return inv


def _equipment(user) -> tuple[list | None, bool]:
    """(assets this person may see, failed). Assets is None when the client isn't linked to Snipe-IT."""
    if not config.snipeit_enabled():
        return None, False
    row = db.one("SELECT snipeit_company_id FROM clients WHERE id = ?", (user["client_id"],))
    if not row or not row["snipeit_company_id"]:
        return None, False
    try:
        return snipeit.visible_to(user, snipeit.assets(row["snipeit_company_id"])), False
    except snipeit.SnipeITError:
        return [], True


def _notify_staff(subject: str, body: str) -> None:
    if config.STAFF_NOTIFY_EMAIL:
        mailer.send(config.STAFF_NOTIFY_EMAIL, subject, body)


# ---- home ------------------------------------------------------------------

@router.get("/")
def root(request: Request):
    user = require_user(request)
    return RedirectResponse("/staff" if user["role"] == "staff" else "/dashboard", status_code=303)


@router.get("/dashboard")
def dashboard(request: Request):
    user = require_client(request)
    client = db.one("SELECT * FROM clients WHERE id = ?", (user["client_id"],))
    where, params = _ticket_scope(user)
    open_tickets = db.all(
        f"SELECT t.* FROM tickets t WHERE {where} AND t.status IN ('open','in_progress','waiting') ORDER BY t.updated_at DESC",
        params,
    )
    waiting = [t for t in open_tickets if t["status"] == "waiting"]

    summary = []
    if waiting:
        summary.append(f"{len(waiting)} {'request needs' if len(waiting) == 1 else 'requests need'} a reply from you.")
    working = len(open_tickets) - len(waiting)
    if working:
        summary.append(f"We're working on {working} {'request' if working == 1 else 'requests'}.")

    unpaid, unpaid_total, overdue = [], 0, 0
    if can(user, "billing"):
        unpaid = db.all("SELECT * FROM invoices WHERE client_id = ? AND status = 'sent' ORDER BY due_date", (user["client_id"],))
        unpaid_total = sum(db.invoice_total(i["id"]) for i in unpaid)
        overdue = sum(1 for i in unpaid if i["due_date"] < date.today().isoformat())
        if overdue:
            summary.append(f"{overdue} {'invoice is' if overdue == 1 else 'invoices are'} past due.")
        elif unpaid:
            summary.append(f"{money(unpaid_total)} is due by {date.fromisoformat(unpaid[0]['due_date']).strftime('%b %-d')}.")
    if not summary:
        summary.append("Everything's quiet. No open requests.")

    vis = "" if can(user, "docs.billing") else "AND visibility = 'everyone'"
    recent_docs = db.all(
        f"SELECT * FROM documents WHERE (client_id = ? OR client_id IS NULL) {vis} ORDER BY uploaded_at DESC LIMIT 3",
        (user["client_id"],),
    )
    next_visit = None
    if can(user, "visits"):
        next_visit = db.one("SELECT * FROM visits WHERE client_id = ? AND status IN ('requested','confirmed') AND end_utc >= ? "
                            "ORDER BY start_utc LIMIT 1", (user["client_id"], visits.now_iso()))
    devices, devices_failed = _equipment(user)
    equipment = None if devices is None or devices_failed else snipeit.summary(devices)
    return render(request, "client/dashboard.html", user=user, nav="home", client=client, summary=summary,
                  open_tickets=open_tickets[:5], unpaid=unpaid, unpaid_total=unpaid_total, overdue=overdue,
                  recent_docs=recent_docs, equipment=equipment, next_visit=next_visit, V=visits)


# ---- requests (tickets) ----------------------------------------------------

@router.get("/tickets")
def tickets(request: Request, show: str = "open"):
    user = require_client(request)
    where, params = _ticket_scope(user)
    states = "('open','in_progress','waiting')" if show == "open" else "('resolved','closed')"
    rows = db.all(
        f"SELECT t.*, u.name AS author FROM tickets t JOIN users u ON u.id = t.created_by "
        f"WHERE {where} AND t.status IN {states} ORDER BY t.updated_at DESC",
        params,
    )
    return render(request, "client/tickets.html", user=user, nav="tickets", tickets=rows, show=show)


@router.get("/tickets/new")
def ticket_new(request: Request, asset: str = ""):
    user = require_client(request)
    devices, _ = _equipment(user)
    return render(request, "client/ticket_new.html", user=user, nav="tickets", urgency=URGENCY_CHOICES,
                  devices=devices or [], asset=asset)


@router.post("/tickets", dependencies=CSRF)
def ticket_create(request: Request, subject: str = Form(...), body: str = Form(...), priority: str = Form("normal"),
                  asset: str = Form(""), files: list[UploadFile | str] = File(default=[])):
    user = require_client(request)
    subject, body = subject.strip()[:200], body.strip()
    if priority not in dict(URGENCY_CHOICES):
        priority = "normal"
    devices, _ = _equipment(user) if asset else (None, False)
    device = next((d for d in devices or [] if d.tag == asset), None)  # only devices this person can see
    files = attachments.real_files(files)
    error = "" if subject and body else "Add a short summary and a few details so we know where to start."
    if not error:
        try:
            attachments.check(files)
        except attachments.Rejected as exc:
            error = f"{exc} Your request hasn't been sent yet."
    if error:
        if devices is None:
            devices, _ = _equipment(user)
        return render(request, "client/ticket_new.html", user=user, nav="tickets", urgency=URGENCY_CHOICES, error=error,
                      subject=subject, body=body, priority=priority, devices=devices or [], asset=asset, status_code=400)
    ts = db.now()
    tid = db.run(
        "INSERT INTO tickets (client_id, created_by, subject, priority, asset_id, asset_tag, asset_label, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (user["client_id"], user["id"], subject, priority,
         device.id if device else None, device.tag if device else "", device.label if device else "", ts, ts),
    )
    mid = db.run("INSERT INTO ticket_messages (ticket_id, user_id, body, created_at) VALUES (?,?,?,?)", (tid, user["id"], body, ts))
    saved = 0
    try:
        saved = attachments.save_all(files, {"id": tid, "client_id": user["client_id"]}, mid, user["id"], internal=False)
    except storage.StorageError:
        flash(request, "Your request was sent, but the files couldn't be saved. Try attaching them in a reply.", "warn")
    client = db.one("SELECT name FROM clients WHERE id = ?", (user["client_id"],))
    if saved:
        body += f"\n\n({saved} file{'s' if saved != 1 else ''} attached)"
    device_line = f"Device: {device.label}\n{device.snipeit_url}\n" if device else ""
    _notify_staff(f"[#{tid}] {'URGENT: ' if priority == 'urgent' else ''}{subject} ({client['name']})",
                  f"{user['name']} ({user['email']}) opened a request.\nUrgency: {dict(URGENCY_CHOICES)[priority]}\n{device_line}\n{body}\n\n"
                  f"{config.BASE_URL}/staff/tickets/{tid}")
    flash(request, f"Request #{tid} sent. We'll reply here and by email.")
    return RedirectResponse(f"/tickets/{tid}", status_code=303)


@router.get("/tickets/{ticket_id}")
def ticket_detail(request: Request, ticket_id: int):
    user = require_client(request)
    ticket = _get_ticket(user, ticket_id)
    can_rate = ticket["created_by"] == user["id"] and ticket["status"] in ("resolved", "closed")
    visit_rows = db.all("SELECT * FROM visits WHERE ticket_id = ? AND status IN ('requested','confirmed') AND end_utc >= ? "
                        "ORDER BY start_utc", (ticket_id, visits.now_iso()))
    return render(request, "client/ticket.html", user=user, nav="tickets", ticket=ticket, messages=_messages(ticket_id),
                  atts=attachments.for_ticket(ticket_id, include_internal=False), accept=attachments.ACCEPT_ATTR,
                  rating=feedback.get(ticket_id), can_rate=can_rate, SCORES=feedback.SCORES,
                  ticket_visits=visit_rows if can(user, "visits") else [], V=visits)


@router.post("/tickets/{ticket_id}/rate", dependencies=CSRF)
def ticket_rate(request: Request, ticket_id: int, score: int = Form(...), comment: str = Form("")):
    user = require_client(request)
    ticket = _get_ticket(user, ticket_id)
    if ticket["created_by"] != user["id"] or ticket["status"] not in ("resolved", "closed"):
        raise HTTPException(status_code=403)
    feedback.record(ticket, user["id"], score, comment)
    if comment.strip():
        flash(request, "Thanks for the note. It goes straight to us.")
    elif score == 1:
        flash(request, "Thanks for telling us. Add a note below, or reply to reopen the request so we can fix it.", "warn")
    else:
        flash(request, "Thanks for the feedback!")
    return RedirectResponse(f"/tickets/{ticket_id}#rating", status_code=303)


# ---- rating from an email link (no sign-in) --------------------------------

def _rating_target(token: str):
    found = feedback.read_token(token)
    if not found:
        return None, None
    ticket_id, user_id = found
    ticket = db.one("SELECT * FROM tickets WHERE id = ? AND created_by = ?", (ticket_id, user_id))
    person = db.one("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,))
    return (ticket, person) if ticket and person else (None, None)


@router.get("/rate/{token}")
def rate_page(request: Request, token: str, score: int = 0):
    ticket, person = _rating_target(token)
    if not ticket:
        return render(request, "auth/message.html", title="This rating link has expired",
                      message="Rating links work for 30 days. You can still rate the request from the portal.",
                      link=("/login", "Sign in"))
    current = feedback.get(ticket["id"])
    return render(request, "client/rate.html", user=None, ticket=ticket, person=person, token=token,
                  score=score if score in feedback.SCORES else (current["score"] if current else 0),
                  SCORES=feedback.SCORES, done=False)


@router.post("/rate/{token}", dependencies=CSRF)
def rate_submit(request: Request, token: str, score: int = Form(...), comment: str = Form("")):
    ticket, person = _rating_target(token)
    if not ticket:
        return RedirectResponse(f"/rate/{token}", status_code=303)
    feedback.record(ticket, person["id"], score, comment)
    return render(request, "client/rate.html", user=None, ticket=ticket, person=person, token=token,
                  score=score, SCORES=feedback.SCORES, done=True)


def reply_fragment(request: Request, ticket, viewer, msg=None, error: str = "", atts=None):
    """htmx answer to a reply: the new message (if any), the updated status pill, and any error."""
    resp = templates.TemplateResponse(request, "partials/reply_result.html",
                                      {"m": msg, "ticket": ticket, "viewer": viewer, "added": msg is not None,
                                       "error": error, "atts": atts or {}, "csrf": request.session.get("csrf", "")})
    if msg is not None and not error:
        resp.headers["X-Form-Reset"] = "1"  # only clear the form when the reply went through
    return resp


@router.post("/tickets/{ticket_id}/reply", dependencies=CSRF)
def ticket_reply(request: Request, ticket_id: int, body: str = Form(""), files: list[UploadFile | str] = File(default=[])):
    user = require_client(request)
    ticket = _get_ticket(user, ticket_id)
    body, files = body.strip(), attachments.real_files(files)
    error = ""
    try:
        attachments.check(files)
    except attachments.Rejected as exc:
        error = str(exc)
    if not error and not body and not files:
        error = "Write a reply or attach a file."
    if error:
        if request.headers.get("hx-request"):
            return reply_fragment(request, ticket, user, error=error)
        flash(request, error, "error")
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)

    ts = db.now()
    new_status = {"waiting": "in_progress", "resolved": "open", "closed": "open"}.get(ticket["status"], ticket["status"])
    mid = db.run("INSERT INTO ticket_messages (ticket_id, user_id, body, created_at) VALUES (?,?,?,?)", (ticket_id, user["id"], body, ts))
    db.run("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (new_status, ts, ticket_id))
    saved, save_error = 0, ""
    try:
        saved = attachments.save_all(files, ticket, mid, user["id"], internal=False)
    except storage.StorageError:
        save_error = "Your reply was sent, but the files couldn't be saved. Try attaching them again in a minute."
    note = f"\n({saved} file{'s' if saved != 1 else ''} attached)" if saved else ""
    _notify_staff(f"[#{ticket_id}] New reply: {ticket['subject']}",
                  f"{user['name']} replied:\n\n{body or '(files only)'}{note}\n\n{config.BASE_URL}/staff/tickets/{ticket_id}")
    if request.headers.get("hx-request"):
        msg = db.one("SELECT m.*, u.name AS author, u.role AS author_role FROM ticket_messages m JOIN users u ON u.id = m.user_id "
                     "WHERE m.id = ?", (mid,))
        return reply_fragment(request, _get_ticket(user, ticket_id), user, msg, error=save_error,
                              atts=attachments.for_ticket(ticket_id, include_internal=False))
    if save_error:
        flash(request, save_error, "warn")
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/resolve", dependencies=CSRF)
def ticket_resolve(request: Request, ticket_id: int):
    user = require_client(request)
    ticket = _get_ticket(user, ticket_id)
    if ticket["status"] not in ("resolved", "closed"):
        db.run("UPDATE tickets SET status = 'resolved', updated_at = ? WHERE id = ?", (db.now(), ticket_id))
        _notify_staff(f"[#{ticket_id}] Marked resolved by client", f"{user['name']} marked '{ticket['subject']}' as resolved.")
        flash(request, "Marked as resolved. Reply any time to reopen it. How did we do? Rate it below.")
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ---- invoices --------------------------------------------------------------

@router.get("/invoices")
def invoices(request: Request):
    user = require_client(request)
    require_perm(user, "billing")
    rows = db.all("SELECT * FROM invoices WHERE client_id = ? AND status != 'draft' ORDER BY issue_date DESC, id DESC", (user["client_id"],))
    return render(request, "client/invoices.html", user=user, nav="invoices", invoices=rows)


@router.get("/invoices/{invoice_id}")
def invoice_detail(request: Request, invoice_id: int):
    user = require_client(request)
    inv = _get_invoice(user, invoice_id)
    client = db.one("SELECT * FROM clients WHERE id = ?", (inv["client_id"],))
    items = db.all("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id", (invoice_id,))
    return render(request, "client/invoice.html", user=user, nav="invoices", inv=inv, client=client, items=items)


@router.post("/invoices/{invoice_id}/pay", dependencies=CSRF)
def invoice_pay(request: Request, invoice_id: int):
    user = require_client(request)
    inv = _get_invoice(user, invoice_id)
    if inv["status"] != "sent":
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    if not config.stripe_enabled():
        flash(request, "Online payment isn't available yet. Contact us to pay by check or bank transfer.", "warn")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    try:
        url = payments.create_checkout(inv, user["email"])
    except Exception:  # noqa: BLE001
        log.exception("Stripe checkout failed for invoice %s", invoice_id)
        flash(request, "The payment page couldn't open. Wait a minute and try again, or contact us.", "error")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    db.audit(user["id"], "invoice.checkout_started", inv["number"], client_ip(request))
    return RedirectResponse(url, status_code=303)


@router.get("/invoices/{invoice_id}/paid")
def invoice_paid(request: Request, invoice_id: int, session_id: str = ""):
    user = require_client(request)
    _get_invoice(user, invoice_id)
    paid = False
    if session_id and config.stripe_enabled():
        try:
            paid = payments.confirm_return(session_id, invoice_id)
        except Exception:  # noqa: BLE001
            log.exception("Could not confirm Stripe session for invoice %s", invoice_id)
    if paid:
        flash(request, "Payment received. Thank you! Stripe will email you a receipt.")
    else:
        flash(request, "Thanks! Your payment is processing. This page will show Paid once it clears.", "warn")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


# ---- equipment (Snipe-IT) --------------------------------------------------

@router.get("/equipment")
def equipment(request: Request, q: str = ""):
    user = require_client(request)
    devices, failed = _equipment(user)
    if devices is None:
        raise HTTPException(status_code=404)
    totals = snipeit.summary(devices)
    term = q.strip().lower()
    if term:
        devices = [d for d in devices if term in " ".join(
            (d.name, d.tag, d.model, d.manufacturer, d.serial, d.assigned_name, d.location, d.category)).lower()]
    grouped: dict[str, list] = {}
    for d in devices:
        grouped.setdefault(d.category, []).append(d)
    return render(request, "client/equipment.html", user=user, nav="equipment", grouped=grouped, totals=totals,
                  failed=failed, q=q, sees_all=user["role"] in ("owner", "billing"))


# ---- documents -------------------------------------------------------------

def _visible_docs_sql(user) -> tuple[str, tuple]:
    vis = "" if can(user, "docs.billing") else " AND visibility = 'everyone'"
    return f"(client_id = ? OR client_id IS NULL){vis}", (user["client_id"],)


@router.get("/documents")
def documents(request: Request):
    user = require_client(request)
    where, params = _visible_docs_sql(user)
    rows = db.all(f"SELECT * FROM documents WHERE {where} ORDER BY category, uploaded_at DESC", params)
    grouped: dict[str, list] = {}
    for d in rows:
        grouped.setdefault(d["category"], []).append(d)
    return render(request, "client/documents.html", user=user, nav="documents", grouped=grouped)


@router.get("/documents/{doc_id}/download")
def document_download(request: Request, doc_id: int):
    user = require_user(request)
    if user["role"] == "staff":
        doc = db.one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    else:
        where, params = _visible_docs_sql(user)
        doc = db.one(f"SELECT * FROM documents WHERE id = ? AND {where}", (doc_id, *params))
    if not doc:
        raise HTTPException(status_code=404)
    db.audit(user["id"], "document.download", doc["title"], client_ip(request))
    from .routes_files import file_response
    return file_response(doc["storage"], doc["stored_name"], doc["filename"], storage.content_type_for(doc["filename"]))


# ---- how-to guides ---------------------------------------------------------

@router.get("/help")
def help_index(request: Request, q: str = ""):
    user = require_user(request)
    if q.strip():
        like = f"%{q.strip()}%"
        rows = db.all("SELECT * FROM kb_articles WHERE published = 1 AND (title LIKE ? OR summary LIKE ? OR body_md LIKE ?) ORDER BY title",
                      (like, like, like))
    else:
        rows = db.all("SELECT * FROM kb_articles WHERE published = 1 ORDER BY title")
    return render(request, "client/help.html", user=user, nav="help", articles=rows, q=q)


@router.get("/help/{slug}")
def help_article(request: Request, slug: str):
    user = require_user(request)
    article = db.one("SELECT * FROM kb_articles WHERE slug = ? AND (published = 1 OR ? = 'staff')", (slug, user["role"]))
    if not article:
        raise HTTPException(status_code=404)
    return render(request, "client/help_article.html", user=user, nav="help", article=article)


# ---- team (owners) ---------------------------------------------------------

def _team(user):
    return db.all("SELECT * FROM users WHERE client_id = ? ORDER BY active DESC, role, name", (user["client_id"],))


def _owner_count(client_id: int) -> int:
    return db.one("SELECT COUNT(*) AS n FROM users WHERE client_id = ? AND role = 'owner' AND active = 1", (client_id,))["n"]


@router.get("/team")
def team(request: Request):
    user = require_client(request)
    require_perm(user, "team")
    return render(request, "client/team.html", user=user, nav="team", people=_team(user))


@router.post("/team/invite", dependencies=CSRF)
def team_invite(request: Request, email: str = Form(...), name: str = Form(""), role: str = Form("member")):
    user = require_client(request)
    require_perm(user, "team")
    if role not in ("owner", "billing", "member"):
        role = "member"
    try:
        _, link, emailed = accounts.invite(user["client_id"], email, role, name, user["name"] or user["email"])
    except accounts.InviteError as exc:
        return render(request, "client/team.html", user=user, nav="team", people=_team(user), error=str(exc),
                      form={"email": email, "name": name, "role": role}, status_code=400)
    db.audit(user["id"], "team.invite", f"{email} as {role}", client_ip(request))
    if emailed:
        flash(request, f"Invite sent to {email}.")
        return RedirectResponse("/team", status_code=303)
    return render(request, "client/team.html", user=user, nav="team", people=_team(user), invite_link=link, invite_email=email)


@router.post("/team/{member_id}", dependencies=CSRF)
def team_update(request: Request, member_id: int, action: str = Form(...), role: str = Form("")):
    user = require_client(request)
    require_perm(user, "team")
    member = db.one("SELECT * FROM users WHERE id = ? AND client_id = ?", (member_id, user["client_id"]))
    if not member:
        raise HTTPException(status_code=404)
    if member["id"] == user["id"]:
        flash(request, "You can't change your own access. Ask another owner or contact us.", "error")
        return RedirectResponse("/team", status_code=303)
    leaving_owner = member["role"] == "owner" and member["active"] and (action == "disable" or (action == "role" and role != "owner"))
    if leaving_owner and _owner_count(user["client_id"]) <= 1:
        flash(request, "Your business needs at least one owner.", "error")
        return RedirectResponse("/team", status_code=303)
    if action == "role" and role in ("owner", "billing", "member"):
        db.run("UPDATE users SET role = ? WHERE id = ?", (role, member_id))
        flash(request, f"{member['name'] or member['email']} can now see: {ROLE_HELP[role].lower()}.")
    elif action == "disable":
        db.run("UPDATE users SET active = 0, session_version = session_version + 1 WHERE id = ?", (member_id,))
        flash(request, f"{member['name'] or member['email']} can no longer sign in.")
    elif action == "enable":
        db.run("UPDATE users SET active = 1 WHERE id = ?", (member_id,))
        flash(request, f"{member['name'] or member['email']} can sign in again.")
    elif action == "resend" and not member["password_hash"]:
        link, emailed = accounts.send_invite(member_id, user["name"] or user["email"])
        flash(request, f"Invite sent again to {member['email']}." if emailed else f"Share this invite link with {member['email']}: {link}")
    db.audit(user["id"], f"team.{action}", f"{member['email']} {role}".strip(), client_ip(request))
    return RedirectResponse("/team", status_code=303)


# ---- account (everyone, including staff) -----------------------------------

@router.get("/account")
def account(request: Request):
    user = require_user(request)
    return render(request, "account.html", user=user, nav="account", codes_left=security.backup_codes_left(user))


@router.post("/account/password", dependencies=CSRF)
def account_password(request: Request, current: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    user = require_user(request)
    error = None
    if not security.verify_password(user["password_hash"], current):
        error = "Your current password isn't right."
    else:
        error = security.password_problem(password, confirm, user["email"])
    if error:
        return render(request, "account.html", user=user, nav="account", codes_left=security.backup_codes_left(user),
                      pw_error=error, status_code=400)
    db.run("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
           (security.hash_password(password), user["id"]))
    db.audit(user["id"], "password.changed", "", client_ip(request))
    start_session(request, db.one("SELECT * FROM users WHERE id = ?", (user["id"],)))
    flash(request, "Password changed. You've been signed out everywhere else.")
    return RedirectResponse("/account", status_code=303)


@router.post("/account/backup-codes", dependencies=CSRF)
def account_backup_codes(request: Request, code: str = Form(...)):
    user = require_user(request)
    step = security.check_totp(user["totp_secret"], code, user["totp_last_step"])
    if not step:
        return render(request, "account.html", user=user, nav="account", codes_left=security.backup_codes_left(user),
                      codes_error="That code didn't work. Use the newest code from your authenticator app.", status_code=400)
    codes, hashes = security.new_backup_codes()
    db.run("UPDATE users SET backup_codes = ?, totp_last_step = ? WHERE id = ?", (hashes, step, user["id"]))
    db.audit(user["id"], "mfa.backup_codes_regenerated", "", client_ip(request))
    return render(request, "auth/backup_codes.html", user=user, codes=codes, action=None)
