"""Passwords, authenticator-app codes, backup codes and emailed link tokens."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from . import config, db

_hasher = PasswordHasher()

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 12


# ---- passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    if not stored:
        return False
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str, confirm: str, email: str = "") -> str | None:
    """Return a plain-language reason the password can't be used, or None."""
    if password != confirm:
        return "The two passwords don't match. Type the same password in both boxes."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters. A short phrase like 'blue truck sunny dock' works well."
    if email and password.lower() == email.lower():
        return "Your password can't be your email address."
    return None


# ---- lockout ---------------------------------------------------------------

def is_locked(user) -> bool:
    if not user["locked_until"]:
        return False
    return datetime.fromisoformat(user["locked_until"]) > datetime.now(timezone.utc)


def record_failure(user) -> None:
    attempts = user["failed_attempts"] + 1
    locked = None
    if attempts >= MAX_FAILED_ATTEMPTS:
        locked = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
        attempts = 0
    db.run("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (attempts, locked, user["id"]))


def clear_failures(user_id: int) -> None:
    db.run("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (user_id,))


# ---- authenticator app (TOTP) ----------------------------------------------

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    issuer = config.BUSINESS_NAME.replace(" LLC", "")
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return img.to_string(encoding="unicode")


def check_totp(secret: str, code: str, last_step: int = 0) -> int | None:
    """Return the matching time step if the code is valid and unused, else None."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    totp = pyotp.TOTP(secret)
    current = int(datetime.now(timezone.utc).timestamp()) // totp.interval
    for step in (current - 1, current, current + 1):
        if step <= last_step:
            continue  # block replay of a code that was already used
        if secrets.compare_digest(totp.generate_otp(step), code):
            return step
    return None


# ---- backup codes ----------------------------------------------------------

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalise_code(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def new_backup_codes(count: int = 8) -> tuple[list[str], str]:
    """Return (codes to show once, JSON of hashes to store)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    hashes = [_digest(_normalise_code(c)) for c in codes]
    return codes, json.dumps(hashes)


def use_backup_code(user, code: str) -> bool:
    hashes = json.loads(user["backup_codes"] or "[]")
    digest = _digest(_normalise_code(code))
    if digest not in hashes:
        return False
    hashes.remove(digest)
    db.run("UPDATE users SET backup_codes = ? WHERE id = ?", (json.dumps(hashes), user["id"]))
    return True


def backup_codes_left(user) -> int:
    return len(json.loads(user["backup_codes"] or "[]"))


# ---- emailed one-time links ------------------------------------------------

def issue_token(user_id: int, purpose: str, hours: int) -> str:
    db.run("UPDATE tokens SET used = 1 WHERE user_id = ? AND purpose = ? AND used = 0", (user_id, purpose))
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    db.run(
        "INSERT INTO tokens (user_id, purpose, token_hash, expires_at) VALUES (?,?,?,?)",
        (user_id, purpose, _digest(token), expires),
    )
    return token


def find_token(token: str, purpose: str):
    row = db.one(
        "SELECT t.*, u.email, u.name, u.active FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND t.purpose = ? AND t.used = 0",
        (_digest(token), purpose),
    )
    if not row or not row["active"]:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    return row


def spend_token(token_id: int) -> None:
    db.run("UPDATE tokens SET used = 1 WHERE id = ?", (token_id,))
