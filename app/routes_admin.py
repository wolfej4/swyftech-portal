"""Admin: settings that used to need .env edits, and SwyfTech staff accounts. Admins only."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import accounts, config, db, mailer, overrides
from .web import client_ip, flash, render, require_admin, start_session, verify_csrf

router = APIRouter(prefix="/staff/admin")
CSRF = [Depends(verify_csrf)]

# name: (label, hint, input type, extra) where extra is (min, max) for numbers or choices for selects
FIELDS = {
    "business": [
        ("BUSINESS_NAME", "Business name", "On invoices, emails, reports and the sign-in page", "text", None),
        ("SUPPORT_EMAIL", "Support email", "Shown to clients and used as the reply-to address on emails", "email", None),
        ("SUPPORT_PHONE", "Support phone", "Shown for urgent problems and on invoices", "text", None),
        ("TAGLINE", "Tagline", "The line under the logo on the sign-in page", "text", None),
        ("PAYMENT_TERMS_NOTE", "Invoice footer", "Printed at the bottom of every invoice", "text", None),
        ("DEFAULT_NET_DAYS", "Invoices due after", "Days after the issue date", "number", (0, 120)),
    ],
    "email": [
        ("SMTP_HOST", "Server", "For example smtp.office365.com or mail.smtp2go.com", "text", None),
        ("SMTP_PORT", "Port", "Usually 587 for STARTTLS or 465 for SSL", "number", (1, 65535)),
        ("SMTP_TLS", "Security", "", "select", [("starttls", "STARTTLS (port 587)"), ("ssl", "SSL/TLS (port 465)"), ("none", "None (only on a trusted network)")]),
        ("SMTP_USER", "Username", "Often the full email address", "text", None),
        ("SMTP_PASSWORD", "Password", "Leave blank to keep the saved password. Stored encrypted.", "password", None),
        ("SMTP_FROM", "Send from", "The address emails come from. Many servers require it to match the username.", "email", None),
        ("STAFF_NOTIFY_EMAIL", "Send SwyfTech alerts to", "New requests, replies, payments, bookings and bad ratings", "email", None),
    ],
    "security": [
        ("MIN_PASSWORD_LENGTH", "Shortest password allowed", "Characters. Applies the next time someone sets a password.", "number", (8, 64)),
        ("MAX_FAILED_ATTEMPTS", "Wrong tries before a pause", "Password or code attempts in a row", "number", (3, 20)),
        ("LOCKOUT_MINUTES", "Pause length", "Minutes before that account can try again", "number", (5, 240)),
        ("SESSION_HOURS", "Stay signed in for", "Hours. After this, people sign in again. Applies right away.", "number", (1, 336)),
    ],
}
TITLES = {"business": "Business details", "email": "Email", "security": "Sign-in and security"}


def _form_page(request: Request, user, section: str, status_code: int = 200, **extra):
    return render(request, f"admin/{section}.html", user=user, nav="s-admin", tab=section, title=TITLES.get(section, "Staff"),
                  fields=FIELDS.get(section, []), overridden=overrides.overridden(), env=overrides.env_value,
                  status_code=status_code, **extra)


def _validate(section: str, form) -> tuple[dict, dict]:
    values, errors = {}, {}
    for name, label, _hint, kind, extra in FIELDS[section]:
        raw = str(form.get(name, "")).strip()
        if kind == "number":
            try:
                n = int(raw)
            except ValueError:
                errors[name] = "Enter a whole number."
                continue
            lo, hi = extra
            if not lo <= n <= hi:
                errors[name] = f"Pick a number from {lo} to {hi}."
                continue
            values[name] = n
        elif kind == "select":
            if raw not in {v for v, _ in extra}:
                errors[name] = "Pick one of the options."
                continue
            values[name] = raw
        else:
            if kind == "email" and raw and ("@" not in raw or "." not in raw.split("@")[-1]):
                errors[name] = "That doesn't look like an email address."
                continue
            if name == "BUSINESS_NAME" and not raw:
                errors[name] = "The business needs a name."
                continue
            values[name] = raw
    return values, errors


@router.get("")
def admin_home(request: Request):
    require_admin(request)
    return RedirectResponse("/staff/admin/business", status_code=303)


@router.get("/{section}")
def settings_page(request: Request, section: str):
    user = require_admin(request)
    if section == "staff":
        return _staff_page(request, user)
    if section not in FIELDS:
        raise HTTPException(status_code=404)
    return _form_page(request, user, section)


@router.post("/{section}", dependencies=CSRF)
async def settings_save(request: Request, section: str):
    user = require_admin(request)
    if section not in FIELDS:
        raise HTTPException(status_code=404)
    form = await request.form()
    values, errors = _validate(section, form)
    if errors:
        return _form_page(request, user, section, 400, errors=errors, posted=dict(form))
    changed = overrides.save(values, user["id"])
    flash(request, "Saved." if changed else "Nothing changed.")
    return RedirectResponse(f"/staff/admin/{section}", status_code=303)


@router.post("/reset/{name}", dependencies=CSRF)
def settings_reset(request: Request, name: str, section: str = Form("business")):
    user = require_admin(request)
    if name not in overrides.EDITABLE:
        raise HTTPException(status_code=404)
    overrides.reset(name, user["id"])
    flash(request, "Back to the value in .env.")
    return RedirectResponse(f"/staff/admin/{section if section in FIELDS else 'business'}", status_code=303)


@router.post("/email/test", dependencies=CSRF)
def email_test(request: Request, to: str = Form(...)):
    user = require_admin(request)
    problem = mailer.send_test(to.strip())
    db.audit(user["id"], "email.test", f"{to} {'ok' if not problem else 'failed'}", client_ip(request))
    if problem:
        flash(request, f"The test email didn't go out. {problem}", "error")
    else:
        flash(request, f"Test email sent to {to}. If it isn't in the inbox in a minute, check spam.")
    return RedirectResponse("/staff/admin/email", status_code=303)


@router.post("/security/sign-out-everyone", dependencies=CSRF)
def sign_out_everyone(request: Request):
    user = require_admin(request)
    db.run("UPDATE users SET session_version = session_version + 1")
    db.audit(user["id"], "sessions.revoked_all", "", client_ip(request))
    start_session(request, db.one("SELECT * FROM users WHERE id = ?", (user["id"],)))  # keep you signed in
    flash(request, "Everyone else has been signed out, on every device.")
    return RedirectResponse("/staff/admin/security", status_code=303)


# ---- SwyfTech staff ----------------------------------------------------------

def _staff_page(request: Request, user, status_code: int = 200, **extra):
    people = db.all("SELECT * FROM users WHERE role = 'staff' ORDER BY active DESC, staff_admin DESC, name")
    return render(request, "admin/staff.html", user=user, nav="s-admin", tab="staff", title="SwyfTech staff",
                  people=people, status_code=status_code, **extra)


def _admin_count() -> int:
    return db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'staff' AND staff_admin = 1 AND active = 1")["n"]


@router.post("/staff/invite", dependencies=CSRF)
def staff_invite(request: Request, email: str = Form(...), name: str = Form(""), access: str = Form("0")):
    user = require_admin(request)
    try:
        uid, link, emailed = accounts.invite(None, email, "staff", name, user["name"] or config.BUSINESS_NAME,
                                             staff_admin=access == "1")
    except accounts.InviteError as exc:
        return _staff_page(request, user, 400, error=str(exc), form={"email": email, "name": name, "access": access})
    db.audit(user["id"], "staff.invited", f"{email} {'admin' if access == '1' else 'technician'}", client_ip(request))
    flash(request, f"Invite sent to {email}." if emailed else f"Send this invite link to {email} (works for 7 days): {link}",
          "ok" if emailed else "link")
    return RedirectResponse("/staff/admin/staff", status_code=303)


@router.post("/staff/{person_id}", dependencies=CSRF)
def staff_action(request: Request, person_id: int, action: str = Form(...), access: str = Form("")):
    user = require_admin(request)
    person = db.one("SELECT * FROM users WHERE id = ? AND role = 'staff'", (person_id,))
    if not person:
        raise HTTPException(status_code=404)
    label = person["name"] or person["email"]
    back = RedirectResponse("/staff/admin/staff", status_code=303)
    if person["id"] == user["id"] and action in ("access", "disable", "reset_mfa"):
        flash(request, "You can't change your own access here. Ask another Admin, or use portal-cli in the container.", "error")
        return back
    losing_admin = person["staff_admin"] and person["active"] and (action == "disable" or (action == "access" and access != "1"))
    if losing_admin and _admin_count() <= 1:
        flash(request, "There has to be at least one active Admin.", "error")
        return back

    if action == "access" and access in ("0", "1"):
        db.run("UPDATE users SET staff_admin = ? WHERE id = ?", (int(access), person_id))
        flash(request, f"{label} is now {'an Admin' if access == '1' else 'a Technician'}.")
    elif action == "reset_mfa":
        db.run("UPDATE users SET totp_secret = NULL, totp_enabled = 0, backup_codes = '[]', session_version = session_version + 1 "
               "WHERE id = ?", (person_id,))
        flash(request, f"{label} will set up a new authenticator app the next time they sign in.")
    elif action == "reset_link" and person["password_hash"]:
        link, emailed = accounts.send_reset(person_id)
        flash(request, f"Password reset link emailed to {person['email']}." if emailed
              else f"Send this password reset link to {person['email']} (works for 24 hours): {link}", "ok" if emailed else "link")
    elif action == "resend" and not person["password_hash"]:
        link, emailed = accounts.send_invite(person_id, user["name"] or config.BUSINESS_NAME)
        flash(request, f"Invite sent again to {person['email']}." if emailed
              else f"Send this invite link to {person['email']} (works for 7 days): {link}", "ok" if emailed else "link")
    elif action == "disable":
        db.run("UPDATE users SET active = 0, session_version = session_version + 1 WHERE id = ?", (person_id,))
        flash(request, f"{label} can no longer sign in.")
    elif action == "enable":
        db.run("UPDATE users SET active = 1, failed_attempts = 0, locked_until = NULL WHERE id = ?", (person_id,))
        flash(request, f"{label} can sign in again.")
    elif action == "unlock":
        db.run("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (person_id,))
        flash(request, f"{label} is unlocked.")
    db.audit(user["id"], f"staff.{action}", person["email"], client_ip(request))
    return back
