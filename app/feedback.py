"""One-click ratings when a request is resolved.

Email links carry a signed token, so the person doesn't need to sign in. The link opens a page
with their choice already selected and one button to send it. Email security scanners (Microsoft
Defender and others) open every link in a message, so a link that saved the rating on its own
would record votes nobody made.
"""
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config, db, mailer

SCORES = {3: "Great", 2: "Okay", 1: "Not good"}
TOKEN_DAYS = 30


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ticket-rating")


def make_token(ticket_id: int, user_id: int) -> str:
    return _serializer().dumps({"t": ticket_id, "u": user_id})


def read_token(token: str) -> tuple[int, int] | None:
    try:
        data = _serializer().loads(token, max_age=TOKEN_DAYS * 86400)
        return int(data["t"]), int(data["u"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def email_block(ticket_id: int, user_id: int) -> str:
    """Text added to the 'resolved' email."""
    token = make_token(ticket_id, user_id)
    base = f"{config.BASE_URL}/rate/{token}"
    return ("How did we do? Pick one:\n"
            f"  Great:    {base}?score=3\n"
            f"  Okay:     {base}?score=2\n"
            f"  Not good: {base}?score=1\n")


def get(ticket_id: int):
    return db.one("SELECT * FROM ticket_ratings WHERE ticket_id = ?", (ticket_id,))


def record(ticket, user_id: int, score: int, comment: str) -> None:
    """Save (or change) the rating for a request and tell SwyfTech right away about a bad one."""
    if score not in SCORES:
        return
    comment = comment.strip()[:2000]
    before = get(ticket["id"])
    ts = db.now()
    db.run(
        "INSERT INTO ticket_ratings (ticket_id, user_id, score, comment, created_at, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(ticket_id) DO UPDATE SET user_id = excluded.user_id, score = excluded.score, "
        "comment = CASE WHEN excluded.comment != '' THEN excluded.comment ELSE ticket_ratings.comment END, "
        "updated_at = excluded.updated_at",
        (ticket["id"], user_id, score, comment, ts, ts),
    )
    db.audit(user_id, "ticket.rated", f"#{ticket['id']} {SCORES[score]}")
    changed = not before or before["score"] != score or (comment and comment != before["comment"])
    if score == 1 and changed and config.STAFF_NOTIFY_EMAIL:
        client = db.one("SELECT name FROM clients WHERE id = ?", (ticket["client_id"],))
        mailer.send(config.STAFF_NOTIFY_EMAIL, f"[#{ticket['id']}] Rated 'Not good' by {client['name']}",
                    f"Request: {ticket['subject']}\nComment: {comment or '(none)'}\n\n{config.BASE_URL}/staff/tickets/{ticket['id']}")


def stats(days: int = 90) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.all("SELECT score FROM ticket_ratings WHERE updated_at >= ?", (cutoff,))
    total = len(rows)
    great = sum(1 for r in rows if r["score"] == 3)
    return {"total": total, "great": great, "bad": sum(1 for r in rows if r["score"] == 1),
            "great_pct": round(100 * great / total) if total else None}
