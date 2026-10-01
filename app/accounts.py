"""Inviting people into the portal (used by both SwyfTech staff and client owners)."""
from . import config, db, mailer, security

INVITE_HOURS = 24 * 7


class InviteError(Exception):
    pass


def invite(client_id: int | None, email: str, role: str, name: str, invited_by: str) -> tuple[int, str, bool]:
    """Create (or re-invite) a user. Returns (user_id, invite_link, emailed)."""
    email = email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise InviteError("That doesn't look like an email address.")
    existing = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if existing:
        if existing["password_hash"] or existing["client_id"] != client_id:
            raise InviteError("Someone with that email already has a portal account.")
        user_id = existing["id"]
        db.run("UPDATE users SET role = ?, name = ?, active = 1 WHERE id = ?", (role, name.strip() or existing["name"], user_id))
    else:
        user_id = db.run(
            "INSERT INTO users (client_id, email, name, role, created_at) VALUES (?,?,?,?,?)",
            (client_id, email, name.strip(), role, db.now()),
        )
    return (user_id, *send_invite(user_id, invited_by))


def send_invite(user_id: int, invited_by: str) -> tuple[str, bool]:
    user = db.one("SELECT u.*, c.name AS client_name FROM users u LEFT JOIN clients c ON c.id = u.client_id WHERE u.id = ?", (user_id,))
    token = security.issue_token(user_id, "invite", hours=INVITE_HOURS)
    link = f"{config.BASE_URL}/invite/{token}"
    company = user["client_name"] or config.BUSINESS_NAME
    emailed = mailer.send(
        user["email"],
        f"You're invited to the {config.BUSINESS_NAME} client portal",
        f"Hi {user['name'] or 'there'},\n\n{invited_by} invited you to the {config.BUSINESS_NAME} client portal for {company}. "
        "It's where you can ask for IT help, follow along on requests, and see invoices and documents.\n\n"
        f"Set up your account here (the link works for 7 days):\n\n{link}\n\n"
        "You'll choose a password and connect an authenticator app on your phone, "
        "such as Microsoft Authenticator or Google Authenticator. It takes about two minutes.",
    )
    return link, emailed
