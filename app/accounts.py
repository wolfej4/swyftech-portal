"""Inviting people into the portal (used by both SwyfTech staff and client owners)."""
from . import config, db, mailer, security

INVITE_HOURS = 24 * 7


class InviteError(Exception):
    pass


def invite(client_id: int | None, email: str, role: str, name: str, invited_by: str,
           staff_admin: bool = False) -> tuple[int, str, bool]:
    """Create (or re-invite) a user. Returns (user_id, invite_link, emailed)."""
    email = email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise InviteError("That doesn't look like an email address.")
    existing = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if existing:
        if existing["password_hash"] or existing["client_id"] != client_id:
            raise InviteError("Someone with that email already has a portal account.")
        user_id = existing["id"]
        db.run("UPDATE users SET role = ?, name = ?, active = 1, staff_admin = ? WHERE id = ?",
               (role, name.strip() or existing["name"], int(staff_admin), user_id))
    else:
        user_id = db.run(
            "INSERT INTO users (client_id, email, name, role, staff_admin, created_at) VALUES (?,?,?,?,?,?)",
            (client_id, email, name.strip(), role, int(staff_admin), db.now()),
        )
    return (user_id, *send_invite(user_id, invited_by))


def send_invite(user_id: int, invited_by: str) -> tuple[str, bool]:
    user = db.one("SELECT u.*, c.name AS client_name FROM users u LEFT JOIN clients c ON c.id = u.client_id WHERE u.id = ?", (user_id,))
    token = security.issue_token(user_id, "invite", hours=INVITE_HOURS)
    link = f"{config.BASE_URL}/invite/{token}"
    if user["role"] == "staff":
        intro = (f"{invited_by} added you to the {config.BUSINESS_NAME} staff portal. It's where SwyfTech "
                 "answers client requests, schedules visits and keeps client records.")
    else:
        intro = (f"{invited_by} invited you to the {config.BUSINESS_NAME} client portal for {user['client_name']}. "
                 "It's where you can ask for IT help, follow along on requests, and see invoices and documents.")
    emailed = mailer.send(
        user["email"],
        f"You're invited to the {config.BUSINESS_NAME} {'staff' if user['role'] == 'staff' else 'client'} portal",
        f"Hi {user['name'] or 'there'},\n\n{intro}\n\n"
        f"Set up your account here (the link works for 7 days):\n\n{link}\n\n"
        "You'll choose a password and connect an authenticator app on your phone, "
        "such as Microsoft Authenticator or Google Authenticator. It takes about two minutes.",
    )
    return link, emailed


RESET_HOURS_ADMIN = 24


def send_reset(user_id: int) -> tuple[str, bool]:
    """A password reset link made by SwyfTech staff for someone who's locked out. Returns (link, emailed)."""
    user = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    token = security.issue_token(user_id, "reset", hours=RESET_HOURS_ADMIN)
    link = f"{config.BASE_URL}/reset/{token}"
    emailed = mailer.send(
        user["email"], f"Reset your {config.BUSINESS_NAME} portal password",
        f"Hi {user['name'] or 'there'},\n\n{config.BUSINESS_NAME} sent you a link to choose a new password "
        f"(it works for {RESET_HOURS_ADMIN} hours):\n\n{link}\n\nYour authenticator app stays connected.",
    )
    db.run("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (user_id,))
    return link, emailed
