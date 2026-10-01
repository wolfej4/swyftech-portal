"""The SwyfTech side: manage clients, answer requests, bill, and publish documents and guides."""
import re
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from . import accounts, attachments, config, db, feedback, mailer, payments, snipeit, storage, visits
from .web import client_ip, flash, money, render, require_staff, templates, verify_csrf

router = APIRouter(prefix="/staff")
CSRF = [Depends(verify_csrf)]

ALLOWED_UPLOADS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".zip"}
PRIORITY_ORDER = "CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"


def _client_or_404(client_id: int):
    client = db.one("SELECT * FROM clients WHERE id = ?", (client_id,))
    if not client:
        raise HTTPException(status_code=404)
    return client


def _parse_money(value: str) -> int:
    cleaned = re.sub(r"[^\d.\-]", "", value or "")
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return 0


def _parse_qty(value: str) -> float:
    try:
        return max(0.0, round(float(value), 2))
    except ValueError:
        return 1.0


# ---- dashboard -------------------------------------------------------------

@router.get("")
def dashboard(request: Request):
    user = require_staff(request)
    tickets = db.all(
        f"SELECT t.*, c.name AS client_name FROM tickets t JOIN clients c ON c.id = t.client_id "
        f"WHERE t.status IN ('open','in_progress','waiting') ORDER BY {PRIORITY_ORDER}, t.updated_at DESC LIMIT 12"
    )
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")}
    unpaid = db.all("SELECT i.*, c.name AS client_name FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.status = 'sent' ORDER BY i.due_date")
    today = date.today().isoformat()
    outstanding = sum(db.invoice_total(i["id"]) for i in unpaid)
    overdue = [i for i in unpaid if i["due_date"] < today]
    month_start = date.today().replace(day=1).isoformat()
    paid_month = sum(db.invoice_total(r["id"]) for r in db.all("SELECT id FROM invoices WHERE status = 'paid' AND paid_at >= ?", (month_start,)))
    clients = db.one("SELECT COUNT(*) AS n FROM clients WHERE active = 1")["n"]
    upcoming_visits = db.all("SELECT v.*, c.name AS client_name FROM visits v JOIN clients c ON c.id = v.client_id "
                             "WHERE v.status IN ('requested','confirmed') AND v.end_utc >= ? ORDER BY v.start_utc LIMIT 6",
                             (visits.now_iso(),))
    to_confirm = sum(1 for v in upcoming_visits if v["status"] == "requested")
    ratings = feedback.stats(90)
    low = db.all("SELECT r.*, t.subject, c.name AS client_name FROM ticket_ratings r JOIN tickets t ON t.id = r.ticket_id "
                 "JOIN clients c ON c.id = t.client_id WHERE r.score = 1 ORDER BY r.updated_at DESC LIMIT 3")
    return render(request, "staff/dashboard.html", user=user, nav="s-home", tickets=tickets, counts=counts,
                  outstanding=outstanding, overdue=overdue, unpaid=unpaid, paid_month=paid_month, clients=clients,
                  upcoming_visits=upcoming_visits, to_confirm=to_confirm, ratings=ratings, low=low, V=visits)


# ---- clients ---------------------------------------------------------------

@router.get("/clients")
def clients(request: Request):
    user = require_staff(request)
    rows = db.all(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM users u WHERE u.client_id = c.id AND u.active = 1) AS people, "
        "(SELECT COUNT(*) FROM tickets t WHERE t.client_id = c.id AND t.status IN ('open','in_progress','waiting')) AS open_tickets "
        "FROM clients c ORDER BY c.active DESC, c.name"
    )
    balances = {r["id"]: sum(db.invoice_total(i["id"]) for i in db.all("SELECT id FROM invoices WHERE client_id = ? AND status = 'sent'", (r["id"],))) for r in rows}
    return render(request, "staff/clients.html", user=user, nav="s-clients", clients=rows, balances=balances)


@router.post("/clients", dependencies=CSRF)
def client_create(request: Request, name: str = Form(...), phone: str = Form(""), address: str = Form(""),
                  owner_name: str = Form(""), owner_email: str = Form("")):
    user = require_staff(request)
    if not name.strip():
        flash(request, "Give the business a name.", "error")
        return RedirectResponse("/staff/clients", status_code=303)
    cid = db.run("INSERT INTO clients (name, phone, address, created_at) VALUES (?,?,?,?)",
                 (name.strip(), phone.strip(), address.strip(), db.now()))
    db.audit(user["id"], "client.created", name.strip(), client_ip(request))
    if owner_email.strip():
        try:
            _, link, emailed = accounts.invite(cid, owner_email, "owner", owner_name, user["name"] or config.BUSINESS_NAME)
            flash(request, f"Invite sent to {owner_email}." if emailed else f"Client added. Send this invite link to {owner_email}: {link}",
                  "ok" if emailed else "link")
        except accounts.InviteError as exc:
            flash(request, f"Client added, but the owner invite failed: {exc}", "error")
    else:
        flash(request, f"{name.strip()} added. Invite their first person below.")
    return RedirectResponse(f"/staff/clients/{cid}", status_code=303)


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int):
    user = require_staff(request)
    client = _client_or_404(client_id)
    people = db.all("SELECT * FROM users WHERE client_id = ? ORDER BY active DESC, role, name", (client_id,))
    tickets = db.all("SELECT * FROM tickets WHERE client_id = ? ORDER BY updated_at DESC LIMIT 10", (client_id,))
    invoices = db.all("SELECT * FROM invoices WHERE client_id = ? ORDER BY issue_date DESC, id DESC LIMIT 12", (client_id,))
    docs = db.all("SELECT * FROM documents WHERE client_id = ? ORDER BY uploaded_at DESC", (client_id,))
    snipe = {"enabled": config.snipeit_enabled(), "companies": [], "error": "", "summary": None, "attention": []}
    if snipe["enabled"]:
        try:
            snipe["companies"] = snipeit.companies()
            if client["snipeit_company_id"]:
                devices = snipeit.assets(client["snipeit_company_id"])
                snipe["summary"] = snipeit.summary(devices)
                snipe["attention"] = sorted((d for d in devices if d.warranty_state in ("soon", "expired")),
                                            key=lambda d: d.warranty_expires)[:8]
        except snipeit.SnipeITError as exc:
            snipe["error"] = str(exc)
    return render(request, "staff/client.html", user=user, nav="s-clients", client=client, people=people,
                  tickets=tickets, invoices=invoices, docs=docs, snipe=snipe)


@router.post("/clients/{client_id}/snipeit", dependencies=CSRF)
def client_link_snipeit(request: Request, client_id: int, company_id: int = Form(0)):
    user = require_staff(request)
    client = _client_or_404(client_id)
    taken = db.one("SELECT name FROM clients WHERE snipeit_company_id = ? AND id != ?", (company_id, client_id)) if company_id else None
    if taken:
        flash(request, f"That Snipe-IT company is already linked to {taken['name']}.", "error")
        return RedirectResponse(f"/staff/clients/{client_id}", status_code=303)
    db.run("UPDATE clients SET snipeit_company_id = ? WHERE id = ?", (company_id or None, client_id))
    snipeit.clear_cache(company_id or None)
    db.audit(user["id"], "client.snipeit_linked", f"{client['name']} -> {company_id or 'none'}", client_ip(request))
    flash(request, "Equipment linked. The client now has an Equipment page." if company_id else "Equipment unlinked.")
    return RedirectResponse(f"/staff/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}", dependencies=CSRF)
def client_update(request: Request, client_id: int, name: str = Form(...), phone: str = Form(""),
                  address: str = Form(""), notes: str = Form(""), active: str = Form("1")):
    user = require_staff(request)
    _client_or_404(client_id)
    db.run("UPDATE clients SET name = ?, phone = ?, address = ?, notes = ?, active = ? WHERE id = ?",
           (name.strip(), phone.strip(), address.strip(), notes.strip(), 1 if active == "1" else 0, client_id))
    db.audit(user["id"], "client.updated", name.strip(), client_ip(request))
    flash(request, "Client details saved.")
    return RedirectResponse(f"/staff/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/people", dependencies=CSRF)
def client_invite(request: Request, client_id: int, email: str = Form(...), name: str = Form(""), role: str = Form("member")):
    user = require_staff(request)
    _client_or_404(client_id)
    if role not in ("owner", "billing", "member"):
        role = "member"
    try:
        _, link, emailed = accounts.invite(client_id, email, role, name, user["name"] or config.BUSINESS_NAME)
        flash(request, f"Invite sent to {email}." if emailed else f"Send this invite link to {email}: {link}", "ok" if emailed else "link")
        db.audit(user["id"], "user.invited", f"{email} as {role}", client_ip(request))
    except accounts.InviteError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/staff/clients/{client_id}", status_code=303)


@router.post("/users/{user_id}", dependencies=CSRF)
def user_action(request: Request, user_id: int, action: str = Form(...), role: str = Form("")):
    staff = require_staff(request)
    person = db.one("SELECT * FROM users WHERE id = ? AND role != 'staff'", (user_id,))
    if not person:
        raise HTTPException(status_code=404)
    label = person["name"] or person["email"]
    if action == "role" and role in ("owner", "billing", "member"):
        db.run("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        flash(request, f"{label} is now {role}.")
    elif action == "reset_mfa":
        db.run("UPDATE users SET totp_secret = NULL, totp_enabled = 0, backup_codes = '[]', session_version = session_version + 1 WHERE id = ?", (user_id,))
        flash(request, f"{label} will set up a new authenticator app the next time they sign in.")
    elif action == "disable":
        db.run("UPDATE users SET active = 0, session_version = session_version + 1 WHERE id = ?", (user_id,))
        flash(request, f"{label} can no longer sign in.")
    elif action == "enable":
        db.run("UPDATE users SET active = 1, failed_attempts = 0, locked_until = NULL WHERE id = ?", (user_id,))
        flash(request, f"{label} can sign in again.")
    elif action == "unlock":
        db.run("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (user_id,))
        flash(request, f"{label} is unlocked.")
    elif action == "resend":
        link, emailed = accounts.send_invite(user_id, staff["name"] or config.BUSINESS_NAME)
        flash(request, f"Invite sent again to {person['email']}." if emailed else f"Send this invite link to {person['email']}: {link}",
              "ok" if emailed else "link")
    db.audit(staff["id"], f"user.{action}", person["email"], client_ip(request))
    return RedirectResponse(f"/staff/clients/{person['client_id']}", status_code=303)


# ---- tickets ---------------------------------------------------------------

@router.get("/tickets")
def tickets(request: Request, show: str = "active", client_id: int = 0):
    user = require_staff(request)
    states = {"active": "('open','in_progress','waiting')", "waiting": "('waiting')", "done": "('resolved','closed')"}.get(show, "('open','in_progress','waiting')")
    where, params = f"t.status IN {states}", ()
    if client_id:
        where += " AND t.client_id = ?"
        params = (client_id,)
    rows = db.all(
        f"SELECT t.*, c.name AS client_name, u.name AS author FROM tickets t JOIN clients c ON c.id = t.client_id "
        f"JOIN users u ON u.id = t.created_by WHERE {where} ORDER BY {PRIORITY_ORDER}, t.updated_at DESC LIMIT 200",
        params,
    )
    all_clients = db.all("SELECT id, name FROM clients ORDER BY name")
    return render(request, "staff/tickets.html", user=user, nav="s-tickets", tickets=rows, show=show,
                  client_id=client_id, all_clients=all_clients)


def _staff_ticket(ticket_id: int):
    ticket = db.one(
        "SELECT t.*, c.name AS client_name, c.phone AS client_phone, u.name AS author, u.email AS author_email "
        "FROM tickets t JOIN clients c ON c.id = t.client_id JOIN users u ON u.id = t.created_by WHERE t.id = ?",
        (ticket_id,),
    )
    if not ticket:
        raise HTTPException(status_code=404)
    return ticket


@router.get("/tickets/{ticket_id}")
def ticket_detail(request: Request, ticket_id: int):
    user = require_staff(request)
    ticket = _staff_ticket(ticket_id)
    messages = db.all(
        "SELECT m.*, u.name AS author, u.role AS author_role FROM ticket_messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.ticket_id = ? ORDER BY m.id",
        (ticket_id,),
    )
    ticket_visits = db.all("SELECT * FROM visits WHERE ticket_id = ? AND status != 'cancelled' ORDER BY start_utc DESC", (ticket_id,))
    return render(request, "staff/ticket.html", user=user, nav="s-tickets", ticket=ticket, messages=messages,
                  atts=attachments.for_ticket(ticket_id, include_internal=True), accept=attachments.ACCEPT_ATTR,
                  rating=feedback.get(ticket_id), SCORES=feedback.SCORES, ticket_visits=ticket_visits, V=visits)


@router.post("/tickets/{ticket_id}/reply", dependencies=CSRF)
def ticket_reply(request: Request, ticket_id: int, body: str = Form(""), internal: str = Form(""),
                 status: str = Form("in_progress"), files: list[UploadFile | str] = File(default=[])):
    from .routes_client import reply_fragment
    user = require_staff(request)
    ticket = _staff_ticket(ticket_id)
    body, files = body.strip(), attachments.real_files(files)
    is_internal = internal == "1"
    if status not in ("open", "in_progress", "waiting", "resolved", "closed") or is_internal:
        status = ticket["status"]
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
        return RedirectResponse(f"/staff/tickets/{ticket_id}", status_code=303)

    ts = db.now()
    mid = db.run("INSERT INTO ticket_messages (ticket_id, user_id, body, internal, created_at) VALUES (?,?,?,?,?)",
                 (ticket_id, user["id"], body, int(is_internal), ts))
    db.run("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (status, ts, ticket_id))
    saved, save_error = 0, ""
    try:
        saved = attachments.save_all(files, ticket, mid, user["id"], internal=is_internal)
    except storage.StorageError as exc:
        save_error = f"Reply saved, but the files couldn't be stored: {exc}"
    if not is_internal:
        headline = {"waiting": "We need a reply from you", "resolved": "Your request is resolved"}.get(status, "New reply on your request")
        rate = ("\n" + feedback.email_block(ticket_id, ticket["created_by"])) if status == "resolved" else ""
        note = f"\n\n({saved} file{'s' if saved != 1 else ''} attached in the portal)" if saved else ""
        mailer.send(ticket["author_email"], f"[#{ticket_id}] {headline}: {ticket['subject']}",
                    f"{user['name'] or config.BUSINESS_NAME} wrote:\n\n{body or '(see the attached files)'}{note}\n\n"
                    f"Reply or follow along here:\n{config.BASE_URL}/tickets/{ticket_id}\n{rate}")
    if request.headers.get("hx-request"):
        msg = db.one("SELECT m.*, u.name AS author, u.role AS author_role FROM ticket_messages m JOIN users u ON u.id = m.user_id "
                     "WHERE m.id = ?", (mid,))
        return reply_fragment(request, _staff_ticket(ticket_id), user, msg, error=save_error,
                              atts=attachments.for_ticket(ticket_id, include_internal=True))
    if save_error:
        flash(request, save_error, "warn")
    return RedirectResponse(f"/staff/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/update", dependencies=CSRF)
def ticket_update(request: Request, ticket_id: int, status: str = Form(...), priority: str = Form(...)):
    user = require_staff(request)
    ticket = _staff_ticket(ticket_id)
    if status in ("open", "in_progress", "waiting", "resolved", "closed") and priority in ("low", "normal", "high", "urgent"):
        db.run("UPDATE tickets SET status = ?, priority = ?, updated_at = ? WHERE id = ?", (status, priority, db.now(), ticket_id))
        if status == "resolved" and ticket["status"] != "resolved":
            mailer.send(ticket["author_email"], f"[#{ticket_id}] Your request is resolved: {ticket['subject']}",
                        f"We've marked your request as resolved. If anything still isn't right, reply here and it will reopen:\n"
                        f"{config.BASE_URL}/tickets/{ticket_id}\n\n{feedback.email_block(ticket_id, ticket['created_by'])}")
        db.audit(user["id"], "ticket.updated", f"#{ticket_id} {status}/{priority}", client_ip(request))
        flash(request, "Request updated.")
    return RedirectResponse(f"/staff/tickets/{ticket_id}", status_code=303)


# ---- invoices --------------------------------------------------------------

@router.get("/invoices")
def invoices(request: Request, show: str = "open"):
    user = require_staff(request)
    where = {"open": "i.status IN ('draft','sent')", "paid": "i.status = 'paid'", "all": "1=1"}.get(show, "i.status IN ('draft','sent')")
    rows = db.all(f"SELECT i.*, c.name AS client_name FROM invoices i JOIN clients c ON c.id = i.client_id WHERE {where} "
                  "ORDER BY CASE i.status WHEN 'draft' THEN 0 WHEN 'sent' THEN 1 ELSE 2 END, i.due_date DESC, i.id DESC LIMIT 300")
    all_clients = db.all("SELECT id, name FROM clients WHERE active = 1 ORDER BY name")
    return render(request, "staff/invoices.html", user=user, nav="s-invoices", invoices=rows, show=show, all_clients=all_clients)


@router.post("/invoices", dependencies=CSRF)
def invoice_create(request: Request, client_id: int = Form(...)):
    user = require_staff(request)
    _client_or_404(client_id)
    today = date.today()
    iid = db.run(
        "INSERT INTO invoices (client_id, number, issue_date, due_date, created_at) VALUES (?,?,?,?,?)",
        (client_id, db.next_invoice_number(), today.isoformat(), (today + timedelta(days=config.DEFAULT_NET_DAYS)).isoformat(), db.now()),
    )
    db.audit(user["id"], "invoice.created", str(iid), client_ip(request))
    return RedirectResponse(f"/staff/invoices/{iid}", status_code=303)


def _staff_invoice(invoice_id: int):
    inv = db.one("SELECT i.*, c.name AS client_name FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.id = ?", (invoice_id,))
    if not inv:
        raise HTTPException(status_code=404)
    return inv


def _items(invoice_id: int):
    return db.all("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id", (invoice_id,))


@router.get("/invoices/{invoice_id}")
def invoice_detail(request: Request, invoice_id: int):
    user = require_staff(request)
    inv = _staff_invoice(invoice_id)
    client = db.one("SELECT * FROM clients WHERE id = ?", (inv["client_id"],))
    return render(request, "staff/invoice.html", user=user, nav="s-invoices", inv=inv, client=client, items=_items(invoice_id))


def _items_fragment(request: Request, inv):
    return templates.TemplateResponse(request, "partials/invoice_items_edit.html",
                                      {"inv": inv, "items": _items(inv["id"]), "csrf": request.session.get("csrf", "")})


@router.post("/invoices/{invoice_id}/items", dependencies=CSRF)
def invoice_add_item(request: Request, invoice_id: int, description: str = Form(...), quantity: str = Form("1"), unit_price: str = Form("0")):
    require_staff(request)
    inv = _staff_invoice(invoice_id)
    if inv["status"] == "draft" and description.strip():
        pos = db.one("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM invoice_items WHERE invoice_id = ?", (invoice_id,))["p"]
        db.run("INSERT INTO invoice_items (invoice_id, description, quantity, unit_cents, position) VALUES (?,?,?,?,?)",
               (invoice_id, description.strip(), _parse_qty(quantity), _parse_money(unit_price), pos))
    if request.headers.get("hx-request"):
        return _items_fragment(request, inv)
    return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/items/{item_id}/delete", dependencies=CSRF)
def invoice_delete_item(request: Request, invoice_id: int, item_id: int):
    require_staff(request)
    inv = _staff_invoice(invoice_id)
    if inv["status"] == "draft":
        db.run("DELETE FROM invoice_items WHERE id = ? AND invoice_id = ?", (item_id, invoice_id))
    if request.headers.get("hx-request"):
        return _items_fragment(request, inv)
    return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/details", dependencies=CSRF)
def invoice_details(request: Request, invoice_id: int, issue_date: str = Form(...), due_date: str = Form(...), notes: str = Form("")):
    require_staff(request)
    inv = _staff_invoice(invoice_id)
    try:
        issue, due = date.fromisoformat(issue_date), date.fromisoformat(due_date)
    except ValueError:
        flash(request, "Dates need to look like 2026-10-01.", "error")
        return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)
    if inv["status"] in ("draft", "sent"):
        db.run("UPDATE invoices SET issue_date = ?, due_date = ?, notes = ? WHERE id = ?", (issue.isoformat(), due.isoformat(), notes.strip(), invoice_id))
        flash(request, "Invoice details saved.")
    return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/send", dependencies=CSRF)
def invoice_send(request: Request, invoice_id: int):
    user = require_staff(request)
    inv = _staff_invoice(invoice_id)
    total = db.invoice_total(invoice_id)
    if inv["status"] != "draft":
        return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)
    if total <= 0:
        flash(request, "Add at least one line with an amount before sending.", "error")
        return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)
    db.run("UPDATE invoices SET status = 'sent' WHERE id = ?", (invoice_id,))
    payers = [r["email"] for r in db.all("SELECT email FROM users WHERE client_id = ? AND active = 1 AND role IN ('owner','billing')", (inv["client_id"],))]
    pay_line = "You can pay online by card or bank account" if config.stripe_enabled() else "You can view it"
    emailed = mailer.send(
        payers, f"Invoice {inv['number']} from {config.BUSINESS_NAME}: {money(total)}",
        f"Hi,\n\nInvoice {inv['number']} for {money(total)} is ready. It's due {date.fromisoformat(inv['due_date']).strftime('%B %-d, %Y')}.\n\n"
        f"{pay_line} here:\n{config.BASE_URL}/invoices/{invoice_id}\n\nThank you!",
    )
    db.audit(user["id"], "invoice.sent", inv["number"], client_ip(request))
    if emailed:
        flash(request, f"Invoice sent to {', '.join(payers)}.")
    elif not payers:
        flash(request, "Invoice is live in the portal, but this client has no Owner or Billing person to email yet.", "warn")
    else:
        flash(request, "Invoice is live in the portal. Email isn't set up, so let the client know it's there.", "warn")
    return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/mark-paid", dependencies=CSRF)
def invoice_mark_paid(request: Request, invoice_id: int, method: str = Form("Check"), reference: str = Form("")):
    user = require_staff(request)
    if payments.mark_paid(invoice_id, method.strip()[:40] or "Other", reference.strip()[:80], user["id"]):
        flash(request, "Marked as paid.")
    return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/void", dependencies=CSRF)
def invoice_void(request: Request, invoice_id: int):
    user = require_staff(request)
    inv = _staff_invoice(invoice_id)
    if inv["status"] == "draft":
        db.run("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        flash(request, f"Draft {inv['number']} deleted.")
        return RedirectResponse("/staff/invoices", status_code=303)
    if inv["status"] == "sent":
        db.run("UPDATE invoices SET status = 'void' WHERE id = ?", (invoice_id,))
        db.audit(user["id"], "invoice.void", inv["number"], client_ip(request))
        flash(request, f"{inv['number']} is void. The client will see it as void.")
    return RedirectResponse(f"/staff/invoices/{invoice_id}", status_code=303)


# ---- documents -------------------------------------------------------------

@router.get("/documents")
def documents(request: Request, client_id: int = 0):
    user = require_staff(request)
    rows = db.all("SELECT d.*, c.name AS client_name FROM documents d LEFT JOIN clients c ON c.id = d.client_id "
                  "ORDER BY d.uploaded_at DESC LIMIT 300")
    all_clients = db.all("SELECT id, name FROM clients WHERE active = 1 ORDER BY name")
    return render(request, "staff/documents.html", user=user, nav="s-docs", docs=rows, all_clients=all_clients, preselect=client_id)


@router.post("/documents", dependencies=CSRF)
async def document_upload(request: Request, title: str = Form(...), category: str = Form("Other"),
                          visibility: str = Form("everyone"), client_id: int = Form(0), upload: UploadFile = File(...)):
    user = require_staff(request)
    back = f"/staff/clients/{client_id}" if client_id else "/staff/documents"
    ext = ("." + upload.filename.rsplit(".", 1)[-1].lower()) if upload.filename and "." in upload.filename else ""
    if ext not in ALLOWED_UPLOADS:
        flash(request, f"That file type isn't allowed. Use one of: {', '.join(sorted(ALLOWED_UPLOADS))}.", "error")
        return RedirectResponse(back, status_code=303)
    if client_id:
        _client_or_404(client_id)
    size = upload.size if upload.size is not None else len(await upload.read())
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        flash(request, f"That file is over {config.MAX_UPLOAD_MB} MB.", "error")
        return RedirectResponse(back, status_code=303)
    stored = f"documents/{uuid.uuid4().hex}{ext}"
    try:
        where = storage.save(upload.file, stored, storage.content_type_for(upload.filename))
    except storage.StorageError as exc:
        flash(request, f"The file couldn't be stored: {exc}", "error")
        return RedirectResponse(back, status_code=303)
    safe_name = re.sub(r"[^\w.\- ]", "_", upload.filename)[:120]
    db.run(
        "INSERT INTO documents (client_id, title, category, visibility, filename, stored_name, size_bytes, uploaded_at, storage) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (client_id or None, title.strip() or safe_name, category if category else "Other",
         visibility if visibility in ("everyone", "billing") else "everyone", safe_name, stored, size, db.now(), where),
    )
    db.audit(user["id"], "document.uploaded", title.strip(), client_ip(request))
    flash(request, f"{title.strip() or safe_name} uploaded.")
    return RedirectResponse(back, status_code=303)


@router.post("/documents/{doc_id}/delete", dependencies=CSRF)
def document_delete(request: Request, doc_id: int):
    user = require_staff(request)
    doc = db.one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(status_code=404)
    storage.delete(doc["storage"], doc["stored_name"])
    db.run("DELETE FROM documents WHERE id = ?", (doc_id,))
    db.audit(user["id"], "document.deleted", doc["title"], client_ip(request))
    flash(request, f"{doc['title']} deleted.")
    referer = request.headers.get("referer", "")
    return RedirectResponse(referer if referer.startswith(config.BASE_URL) else "/staff/documents", status_code=303)


# ---- how-to guides ---------------------------------------------------------

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "guide"
    base, n = slug, 2
    while db.one("SELECT 1 FROM kb_articles WHERE slug = ?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    return slug


@router.get("/guides")
def guides(request: Request):
    user = require_staff(request)
    rows = db.all("SELECT * FROM kb_articles ORDER BY published DESC, title")
    return render(request, "staff/guides.html", user=user, nav="s-guides", articles=rows)


@router.get("/guides/new")
def guide_new(request: Request):
    user = require_staff(request)
    return render(request, "staff/guide_edit.html", user=user, nav="s-guides", article=None)


@router.post("/guides", dependencies=CSRF)
def guide_create(request: Request, title: str = Form(...), summary: str = Form(""), body_md: str = Form(""), published: str = Form("")):
    user = require_staff(request)
    aid = db.run("INSERT INTO kb_articles (title, slug, summary, body_md, published, updated_at) VALUES (?,?,?,?,?,?)",
                 (title.strip(), _slugify(title), summary.strip(), body_md, 1 if published else 0, db.now()))
    db.audit(user["id"], "guide.created", title.strip(), client_ip(request))
    flash(request, "Guide saved.")
    return RedirectResponse(f"/staff/guides/{aid}", status_code=303)


@router.get("/guides/{article_id}")
def guide_edit(request: Request, article_id: int):
    user = require_staff(request)
    article = db.one("SELECT * FROM kb_articles WHERE id = ?", (article_id,))
    if not article:
        raise HTTPException(status_code=404)
    return render(request, "staff/guide_edit.html", user=user, nav="s-guides", article=article)


@router.post("/guides/{article_id}", dependencies=CSRF)
def guide_update(request: Request, article_id: int, title: str = Form(...), summary: str = Form(""), body_md: str = Form(""),
                 published: str = Form(""), action: str = Form("save")):
    user = require_staff(request)
    if action == "delete":
        db.run("DELETE FROM kb_articles WHERE id = ?", (article_id,))
        flash(request, "Guide deleted.")
        return RedirectResponse("/staff/guides", status_code=303)
    db.run("UPDATE kb_articles SET title = ?, summary = ?, body_md = ?, published = ?, updated_at = ? WHERE id = ?",
           (title.strip(), summary.strip(), body_md, 1 if published else 0, db.now(), article_id))
    db.audit(user["id"], "guide.updated", title.strip(), client_ip(request))
    flash(request, "Guide saved.")
    return RedirectResponse(f"/staff/guides/{article_id}", status_code=303)
