"""Monthly client reports.

A report is computed from the portal's own records for one client and one calendar month
(in the business time zone). When it's sent, a frozen copy is saved, so the client always
sees exactly what you sent even if records change later.
"""
import json
import statistics
from datetime import date, datetime, time as dtime, timedelta, timezone

from . import config, db, mailer, snipeit
from .visits import TZ, iso, parse

UTC = timezone.utc


# ---- months ------------------------------------------------------------------

def last_month(today: date | None = None) -> str:
    first = (today or datetime.now(TZ).date()).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def valid_month(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m")
        return True
    except ValueError:
        return False


def bounds(month: str) -> tuple[datetime, datetime, date, date]:
    """(start UTC, end UTC, first local date, last local date)."""
    first = datetime.strptime(month, "%Y-%m").date()
    nxt = (first + timedelta(days=32)).replace(day=1)
    start = datetime.combine(first, dtime.min, tzinfo=TZ).astimezone(UTC)
    end = datetime.combine(nxt, dtime.min, tzinfo=TZ).astimezone(UTC)
    return start, end, first, nxt - timedelta(days=1)


def label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def recent_months(count: int = 12) -> list[str]:
    months, m = [], last_month()
    for _ in range(count):
        months.append(m)
        m = last_month(datetime.strptime(m, "%Y-%m").date())
    return months


# ---- formatting --------------------------------------------------------------

def duration(hours: float | None) -> str:
    if hours is None:
        return ""
    if hours < 1:
        return f"{max(1, round(hours * 60))} minutes"
    if hours < 24:
        h = round(hours)
        return "1 hour" if h == 1 else f"{h} hours"
    d = round(hours / 24, 1)
    d = int(d) if d == int(d) else d
    return "1 day" if d == 1 else f"{d} days"


def _hours(a: str, b: str) -> float:
    return (parse(b) - parse(a)).total_seconds() / 3600


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ---- the report --------------------------------------------------------------

def build(client_id: int, month: str) -> dict:
    client = db.one("SELECT * FROM clients WHERE id = ?", (client_id,))
    start, end, first_day, last_day = bounds(month)
    s, e = iso(start), iso(end)

    opened = db.all("SELECT * FROM tickets WHERE client_id = ? AND created_at >= ? AND created_at < ?", (client_id, s, e))
    resolved = db.all("SELECT * FROM tickets WHERE client_id = ? AND resolved_at >= ? AND resolved_at < ? ORDER BY resolved_at",
                      (client_id, s, e))
    still_open = db.one("SELECT COUNT(*) AS n FROM tickets WHERE client_id = ? AND created_at < ? AND "
                        "(resolved_at IS NULL OR resolved_at >= ?)", (client_id, e, e))["n"]

    # How quickly a person at SwyfTech first answered (internal notes don't count).
    first_replies = []
    for t in opened:
        row = db.one("SELECT MIN(m.created_at) AS first FROM ticket_messages m JOIN users u ON u.id = m.user_id "
                     "WHERE m.ticket_id = ? AND u.role = 'staff' AND m.internal = 0", (t["id"],))
        if row and row["first"]:
            first_replies.append(_hours(t["created_at"], row["first"]))
    fix_times = [_hours(t["created_at"], t["resolved_at"]) for t in resolved]

    ratings = db.all("SELECT r.*, t.subject FROM ticket_ratings r JOIN tickets t ON t.id = r.ticket_id "
                     "WHERE t.client_id = ? AND r.updated_at >= ? AND r.updated_at < ?", (client_id, s, e))
    visits = db.all("SELECT * FROM visits WHERE client_id = ? AND start_utc >= ? AND start_utc < ? "
                    "AND (status = 'completed' OR (status = 'confirmed' AND end_utc < ?)) ORDER BY start_utc",
                    (client_id, s, e, iso(datetime.now(UTC))))
    visit_hours = sum(_hours(v["start_utc"], v["end_utc"]) for v in visits)

    invoiced = sum(db.invoice_total(r["id"]) for r in db.all(
        "SELECT id FROM invoices WHERE client_id = ? AND status IN ('sent','paid') AND issue_date BETWEEN ? AND ?",
        (client_id, first_day.isoformat(), last_day.isoformat())))
    paid = sum(db.invoice_total(r["id"]) for r in db.all(
        "SELECT id FROM invoices WHERE client_id = ? AND status = 'paid' AND paid_at >= ? AND paid_at < ?", (client_id, s, e)))
    outstanding = sum(db.invoice_total(r["id"]) for r in db.all(
        "SELECT id FROM invoices WHERE client_id = ? AND status = 'sent'", (client_id,)))

    equipment = None
    if config.snipeit_enabled() and client["snipeit_company_id"]:
        try:
            devices = snipeit.assets(client["snipeit_company_id"])
            horizon = last_day + timedelta(days=90)
            ending = sorted((d for d in devices if d.warranty_expires and last_day < d.warranty_expires <= horizon),
                            key=lambda d: d.warranty_expires)
            equipment = {
                "count": len(devices),
                "expired": sum(1 for d in devices if d.warranty_expires and d.warranty_expires <= last_day),
                "ending": [{"name": d.name, "tag": d.tag, "who": d.assigned_name,
                            "date": d.warranty_expires.strftime("%b %-d, %Y")} for d in ending[:10]],
            }
        except snipeit.SnipeITError:
            equipment = None

    median_reply = statistics.median(first_replies) if first_replies else None
    median_fix = statistics.median(fix_times) if fix_times else None
    great = sum(1 for r in ratings if r["score"] == 3)

    # The plain-language opening, in the same voice as the dashboard.
    lines = []
    if not opened and not resolved:
        lines.append(f"A quiet month for {client['name']}: nothing needed fixing.")
    else:
        n = len(resolved)
        lines.append(f"We resolved {n} {'request' if n == 1 else 'requests'} for {client['name']}"
                     + (f" and usually replied within {duration(median_reply)}." if median_reply is not None else "."))
    if visits:
        lines.append(f"We were on site {len(visits)} {'time' if len(visits) == 1 else 'times'}, about {duration(visit_hours)} in all.")
    if ratings and great == len(ratings):
        lines.append("Your rating was Great." if great == 1 else f"All {great} ratings were Great.")
    elif great:
        lines.append(f"{great} of {len(ratings)} ratings {'was' if great == 1 else 'were'} Great.")
    if equipment and equipment["ending"]:
        k = len(equipment["ending"])
        lines.append(f"{k} {'device warranty ends' if k == 1 else 'device warranties end'} in the next 3 months.")

    return {
        "version": 1,
        "client": client["name"],
        "month": month,
        "month_label": label(month),
        "generated_at": iso(datetime.now(UTC)),
        "headline": lines,
        "requests": {
            "opened": len(opened),
            "resolved": len(resolved),
            "still_open": still_open,
            "urgent": sum(1 for t in opened if t["priority"] in ("urgent", "high")),
            "first_reply": duration(median_reply),
            "time_to_fix": duration(median_fix),
            "resolved_list": [{"id": t["id"], "subject": t["subject"]} for t in resolved[:25]],
            "resolved_more": max(0, len(resolved) - 25),
        },
        "ratings": {
            "total": len(ratings), "great": great,
            "okay": sum(1 for r in ratings if r["score"] == 2),
            "not_good": sum(1 for r in ratings if r["score"] == 1),
            "comments": [{"score": r["score"], "comment": r["comment"], "subject": r["subject"]} for r in ratings if r["comment"]][:10],
        },
        "visits": {
            "count": len(visits),
            "hours": duration(visit_hours) if visits else "",
            "list": [{"when": local_day(v["start_utc"]), "reason": v["reason"]} for v in visits],
        },
        "billing": {"invoiced": _money(invoiced), "paid": _money(paid), "outstanding": _money(outstanding),
                    "has_any": bool(invoiced or paid or outstanding)},
        "equipment": equipment,
    }


def local_day(value: str) -> str:
    return parse(value).astimezone(TZ).strftime("%a, %b %-d")


# ---- drafts and sending ------------------------------------------------------

def record(client_id: int, month: str):
    return db.one("SELECT * FROM monthly_reports WHERE client_id = ? AND month = ?", (client_id, month))


def view_data(client_id: int, month: str) -> tuple[dict, object]:
    """(report data, saved row). Sent reports show their frozen copy; drafts are built fresh."""
    row = record(client_id, month)
    if row and row["data_json"]:
        return json.loads(row["data_json"]), row
    return build(client_id, month), row


def save_note(client_id: int, month: str, note: str) -> None:
    db.run("INSERT INTO monthly_reports (client_id, month, note) VALUES (?,?,?) "
           "ON CONFLICT(client_id, month) DO UPDATE SET note = excluded.note", (client_id, month, note.strip()[:4000]))


def recipients(client_id: int) -> list[str]:
    return [r["email"] for r in db.all(
        "SELECT email FROM users WHERE client_id = ? AND active = 1 AND role = 'owner' AND password_hash IS NOT NULL",
        (client_id,))]


def send(client_id: int, month: str, sent_by: int | None) -> tuple[bool, list[str]]:
    """Freeze the numbers and email the client's owners. Returns (emailed, recipients)."""
    data = build(client_id, month)
    db.run("INSERT INTO monthly_reports (client_id, month, data_json, sent_at, sent_by) VALUES (?,?,?,?,?) "
           "ON CONFLICT(client_id, month) DO UPDATE SET data_json = excluded.data_json, sent_at = excluded.sent_at, "
           "sent_by = excluded.sent_by", (client_id, month, json.dumps(data), db.now(), sent_by))
    to = recipients(client_id)
    note = (record(client_id, month)["note"] or "").strip()
    body = (f"Here's your {data['month_label']} IT report from {config.BUSINESS_NAME}.\n\n"
            + "\n".join(data["headline"])
            + (f"\n\n{note}" if note else "")
            + f"\n\nSee the full report:\n{config.BASE_URL}/reports/{month}")
    emailed = mailer.send(to, f"Your {data['month_label']} IT report from {config.BUSINESS_NAME}", body)
    db.audit(sent_by, "report.sent", f"{data['client']} {month}")
    return emailed, to


def active_clients():
    return db.all("SELECT c.* FROM clients c WHERE c.active = 1 AND EXISTS "
                  "(SELECT 1 FROM users u WHERE u.client_id = c.id AND u.role = 'owner' AND u.active = 1) ORDER BY c.name")


def auto_send_enabled() -> bool:
    return db.get_setting("reports_auto_send", "0") == "1"
