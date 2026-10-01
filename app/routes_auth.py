"""Sign in with a password plus an authenticator-app code. MFA is required for everyone."""
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from . import config, db, mailer, security
from .web import client_ip, current_user, flash, redirect, render, start_session, verify_csrf

router = APIRouter()
CSRF = [Depends(verify_csrf)]
_DUMMY_HASH = security.hash_password(secrets.token_hex(16))


def _home_for(user) -> str:
    return "/staff" if user["role"] == "staff" else "/dashboard"


def _pending_user(request: Request):
    uid = request.session.get("pending_uid")
    if not uid:
        raise redirect("/login")
    user = db.one("SELECT * FROM users WHERE id = ? AND active = 1", (uid,))
    if not user:
        request.session.clear()
        raise redirect("/login")
    return user


def _begin_second_step(request: Request, user) -> RedirectResponse:
    csrf = request.session.get("csrf") or secrets.token_urlsafe(24)
    request.session.clear()
    request.session["pending_uid"] = user["id"]
    request.session["csrf"] = csrf
    return RedirectResponse("/login/code" if user["totp_enabled"] else "/setup-mfa", status_code=303)


def _finish_login(request: Request, user) -> RedirectResponse:
    security.clear_failures(user["id"])
    db.run("UPDATE users SET last_login = ? WHERE id = ?", (db.now(), user["id"]))
    db.audit(user["id"], "login", "", client_ip(request))
    start_session(request, user)
    return RedirectResponse(_home_for(user), status_code=303)


# ---- sign in ---------------------------------------------------------------

@router.get("/login")
def login_page(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse(_home_for(user), status_code=303)
    return render(request, "auth/login.html")


@router.post("/login", dependencies=CSRF)
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = db.one("SELECT * FROM users WHERE email = ?", (email.strip(),))
    if user and security.is_locked(user):
        return render(request, "auth/login.html", error="Too many tries. For your security, sign-in is paused for 15 minutes.", email=email, status_code=429)
    if not user:
        security.verify_password(_DUMMY_HASH, password)  # keep timing similar
    ok = bool(user and user["active"] and security.verify_password(user["password_hash"], password))
    if ok and user["client_id"]:
        client = db.one("SELECT active FROM clients WHERE id = ?", (user["client_id"],))
        ok = bool(client and client["active"])
    if not ok:
        if user:
            security.record_failure(user)
        return render(request, "auth/login.html", error="That email and password don't match. Check for typos, or reset your password below.", email=email, status_code=401)
    return _begin_second_step(request, user)


@router.get("/login/code")
def code_page(request: Request):
    user = _pending_user(request)
    if not user["totp_enabled"]:
        return RedirectResponse("/setup-mfa", status_code=303)
    return render(request, "auth/mfa.html", user=None, pending=user)


@router.post("/login/code", dependencies=CSRF)
def code_submit(request: Request, code: str = Form(...)):
    user = _pending_user(request)
    if security.is_locked(user):
        request.session.clear()
        flash(request, "Too many tries. Sign-in is paused for 15 minutes.", "error")
        return RedirectResponse("/login", status_code=303)
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) == 6 and len(code.strip()) <= 7:
        step = security.check_totp(user["totp_secret"], digits, user["totp_last_step"])
        if step:
            db.run("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
            return _finish_login(request, user)
    elif security.use_backup_code(user, code):
        fresh = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
        resp = _finish_login(request, fresh)
        left = security.backup_codes_left(fresh)
        flash(request, f"You used a backup code. You have {left} left. You can make new ones under Account.", "warn")
        return resp
    security.record_failure(user)
    return render(request, "auth/mfa.html", user=None, pending=user, error="That code didn't work. Codes change every 30 seconds, so use the newest one.", status_code=401)


# ---- authenticator enrollment ---------------------------------------------

@router.get("/setup-mfa")
def mfa_setup_page(request: Request):
    user = _pending_user(request)
    if user["totp_enabled"]:
        return RedirectResponse("/login/code", status_code=303)
    secret = request.session.get("totp_setup") or security.new_totp_secret()
    request.session["totp_setup"] = secret
    uri = security.totp_uri(secret, user["email"])
    return render(request, "auth/mfa_setup.html", user=None, pending=user, qr=security.qr_svg(uri),
                  secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))


@router.post("/setup-mfa", dependencies=CSRF)
def mfa_setup_submit(request: Request, code: str = Form(...)):
    user = _pending_user(request)
    secret = request.session.get("totp_setup")
    if user["totp_enabled"] or not secret:
        return RedirectResponse("/setup-mfa", status_code=303)
    step = security.check_totp(secret, code)
    if not step:
        uri = security.totp_uri(secret, user["email"])
        return render(request, "auth/mfa_setup.html", user=None, pending=user, qr=security.qr_svg(uri),
                      secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                      error="That code didn't match. Make sure your phone's clock is set automatically, then try the newest code.",
                      status_code=400)
    codes, hashes = security.new_backup_codes()
    db.run("UPDATE users SET totp_secret = ?, totp_enabled = 1, totp_last_step = ?, backup_codes = ? WHERE id = ?",
           (secret, step, hashes, user["id"]))
    request.session.pop("totp_setup", None)
    db.audit(user["id"], "mfa.enrolled", "", client_ip(request))
    return render(request, "auth/backup_codes.html", user=None, codes=codes, action="/setup-mfa/done")


@router.post("/setup-mfa/done", dependencies=CSRF)
def mfa_setup_done(request: Request):
    user = _pending_user(request)
    if not user["totp_enabled"]:
        return RedirectResponse("/setup-mfa", status_code=303)
    return _finish_login(request, user)


# ---- invites & password reset ---------------------------------------------

@router.get("/invite/{token}")
def invite_page(request: Request, token: str):
    row = security.find_token(token, "invite")
    if not row:
        return render(request, "auth/message.html", title="This invite link has expired",
                      message="Invite links work for 7 days and only once. Ask the person who invited you to send a new one.")
    return render(request, "auth/set_password.html", user=None, mode="invite", row=row, token=token)


@router.post("/invite/{token}", dependencies=CSRF)
def invite_submit(request: Request, token: str, name: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    row = security.find_token(token, "invite")
    if not row:
        return RedirectResponse(f"/invite/{token}", status_code=303)
    problem = security.password_problem(password, confirm, row["email"])
    if problem or not name.strip():
        return render(request, "auth/set_password.html", user=None, mode="invite", row=row, token=token,
                      error=problem or "Add your name so SwyfTech knows who's asking.", name=name, status_code=400)
    db.run("UPDATE users SET name = ?, password_hash = ? WHERE id = ?", (name.strip(), security.hash_password(password), row["user_id"]))
    security.spend_token(row["id"])
    db.audit(row["user_id"], "invite.accepted", "", client_ip(request))
    user = db.one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    return _begin_second_step(request, user)


@router.get("/forgot")
def forgot_page(request: Request):
    return render(request, "auth/forgot.html")


@router.post("/forgot", dependencies=CSRF)
def forgot_submit(request: Request, email: str = Form(...)):
    if not config.smtp_enabled():
        contact = config.SUPPORT_PHONE or config.SUPPORT_EMAIL or "SwyfTech"
        return render(request, "auth/message.html", title="Contact us to reset your password",
                      message=f"Password reset emails aren't turned on yet. Reach out to {contact} and we'll send you a reset link.")
    user = db.one("SELECT * FROM users WHERE email = ? AND active = 1 AND password_hash IS NOT NULL", (email.strip(),))
    if user:
        token = security.issue_token(user["id"], "reset", hours=2)
        mailer.send(user["email"], "Reset your SwyfTech portal password",
                    f"Hi {user['name'] or 'there'},\n\nSomeone asked to reset the password for your SwyfTech client portal account. "
                    f"If that was you, set a new password here (the link works for 2 hours):\n\n{config.BASE_URL}/reset/{token}\n\n"
                    "If you didn't ask for this, you can ignore this email. Your password won't change.")
        db.audit(user["id"], "password.reset_requested", "", client_ip(request))
    return render(request, "auth/message.html", title="Check your email",
                  message="If that address has a portal account, a reset link is on its way. It works for 2 hours.")


@router.get("/reset/{token}")
def reset_page(request: Request, token: str):
    row = security.find_token(token, "reset")
    if not row:
        return render(request, "auth/message.html", title="This reset link has expired",
                      message="Reset links work for 2 hours and only once. Request a new one from the sign-in page.", link=("/forgot", "Request a new link"))
    return render(request, "auth/set_password.html", user=None, mode="reset", row=row, token=token)


@router.post("/reset/{token}", dependencies=CSRF)
def reset_submit(request: Request, token: str, password: str = Form(...), confirm: str = Form(...)):
    row = security.find_token(token, "reset")
    if not row:
        return RedirectResponse(f"/reset/{token}", status_code=303)
    problem = security.password_problem(password, confirm, row["email"])
    if problem:
        return render(request, "auth/set_password.html", user=None, mode="reset", row=row, token=token, error=problem, status_code=400)
    db.run("UPDATE users SET password_hash = ?, session_version = session_version + 1, failed_attempts = 0, locked_until = NULL WHERE id = ?",
           (security.hash_password(password), row["user_id"]))
    security.spend_token(row["id"])
    db.audit(row["user_id"], "password.reset", "", client_ip(request))
    request.session.clear()
    flash(request, "Password updated. Sign in with your new password.")
    return RedirectResponse("/login", status_code=303)


@router.post("/logout", dependencies=CSRF)
def logout(request: Request):
    request.session.clear()
    flash(request, "You're signed out.")
    return RedirectResponse("/login", status_code=303)
