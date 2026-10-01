"""On-site visit scheduling.

Open times come from the weekly hours you set in the portal, minus:
  - visits already booked (plus a travel buffer on each side)
  - time off you've blocked
  - busy events on any calendars you link by ICS address (work schedule, personal calendar...)
"""
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import config, db

log = logging.getLogger("portal.visits")
TZ = ZoneInfo(config.TIMEZONE)
UTC = timezone.utc
ACTIVE = ("requested", "confirmed")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULTS = {
    "hours": {"0": ["09:00", "17:00"], "1": ["09:00", "17:00"], "2": ["09:00", "17:00"],
              "3": ["09:00", "17:00"], "4": ["09:00", "17:00"], "5": None, "6": None},
    "slot_minutes": 120,       # how long a visit is
    "step_minutes": 60,        # how often a visit can start
    "buffer_minutes": 30,      # travel time kept free before and after each visit
    "notice_hours": 24,        # earliest a client can book, from now
    "window_days": 21,         # how far ahead clients can book
    "max_per_day": 2,
    "require_confirmation": True,
    "calendar_urls": [],
}

STATUS_LABELS = {"requested": "Waiting for confirmation", "confirmed": "Confirmed",
                 "cancelled": "Cancelled", "completed": "Done"}
STAFF_STATUS_LABELS = {**STATUS_LABELS, "requested": "Needs confirming"}


# ---- time helpers ------------------------------------------------------------

def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(datetime.now(UTC))


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local(value: str | datetime) -> datetime:
    return (parse(value) if isinstance(value, str) else value).astimezone(TZ)


def when(start: str, end: str) -> str:
    """'Tue, Oct 6, 9:00 to 11:00 AM' in the business time zone."""
    s, e = local(start), local(end)
    fmt = lambda d: d.strftime("%-I:%M %p") if d.minute else d.strftime("%-I %p")
    same_half = s.strftime("%p") == e.strftime("%p")
    start_txt = fmt(s).rsplit(" ", 1)[0] if same_half else fmt(s)
    return f"{s.strftime('%a, %b %-d')}, {start_txt} to {fmt(e)}"


ZONE_NAMES = {"America/New_York": "Eastern", "America/Chicago": "Central", "America/Denver": "Mountain",
              "America/Phoenix": "Arizona", "America/Los_Angeles": "Pacific"}
ZONE_LABEL = ZONE_NAMES.get(config.TIMEZONE, config.TIMEZONE.split("/")[-1].replace("_", " "))


def span(start: str, end: str) -> str:
    """Time-off text: 'Sun, Oct 4, all day' or 'Mon, Oct 5, 1 to 5 PM' or a multi-day range."""
    s, e = local(start), local(end)
    whole = s.time() == dtime.min and e.strftime("%H:%M") in ("23:59", "00:00")
    if whole:
        last = e.date() - timedelta(days=1) if e.strftime("%H:%M") == "00:00" else e.date()
        if last <= s.date():
            return f"{s.strftime('%a, %b %-d')}, all day"
        return f"{s.strftime('%a, %b %-d')} to {last.strftime('%a, %b %-d')}, all day"
    if s.date() == e.date():
        return when(start, end)
    return f"{s.strftime('%a, %b %-d, %-I:%M %p')} to {e.strftime('%a, %b %-d, %-I:%M %p')}"


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


# ---- settings ----------------------------------------------------------------

def settings() -> dict:
    stored = json.loads(db.get_setting("visit_settings", "{}") or "{}")
    merged = {**DEFAULTS, **stored}
    merged["hours"] = {**DEFAULTS["hours"], **(stored.get("hours") or {})}
    return merged


def save_settings(values: dict) -> None:
    db.set_setting("visit_settings", json.dumps(values))
    _calendar_cache.clear()


def feed_key(rotate: bool = False) -> str:
    key = db.get_setting("visit_feed_key")
    if rotate or not key:
        key = secrets.token_urlsafe(24)
        db.set_setting("visit_feed_key", key)
    return key


# ---- linked calendars (ICS) --------------------------------------------------

_calendar_cache: dict[str, tuple[float, list, str]] = {}
_cal_lock = threading.Lock()
CALENDAR_CACHE_SECONDS = 600


def _fetch_busy(url: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    import icalendar
    import recurring_ical_events

    fetch_url = url.replace("webcal://", "https://", 1)
    req = Request(fetch_url, headers={"User-Agent": "SwyfTech-Portal"})
    with urlopen(req, timeout=10) as resp:
        cal = icalendar.Calendar.from_ical(resp.read())
    busy = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
            continue  # marked "free"
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s = ev.decoded("DTSTART")
        e = ev.decoded("DTEND") if ev.get("DTEND") else None
        if not isinstance(s, datetime):  # all-day event
            s_dt = datetime.combine(s, dtime.min, tzinfo=TZ)
            e_dt = datetime.combine(e, dtime.min, tzinfo=TZ) if e else s_dt + timedelta(days=1)
        else:
            s_dt = s if s.tzinfo else s.replace(tzinfo=TZ)
            e_dt = (e if e.tzinfo else e.replace(tzinfo=TZ)) if isinstance(e, datetime) else s_dt + timedelta(hours=1)
        busy.append((s_dt.astimezone(UTC), e_dt.astimezone(UTC)))
    return busy


def calendar_busy(start: datetime, end: datetime) -> tuple[list, list[str]]:
    """(busy periods, problems) across all linked calendars. A failing calendar doesn't block booking."""
    busy, problems = [], []
    for url in settings()["calendar_urls"]:
        now = time.monotonic()
        with _cal_lock:
            hit = _calendar_cache.get(url)
        if hit and now - hit[0] < CALENDAR_CACHE_SECONDS:
            busy += hit[1]
            if hit[2]:
                problems.append(hit[2])
            continue
        try:
            periods, problem = _fetch_busy(url, start, end), ""
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            periods, problem = [], f"Couldn't read a linked calendar ({url[:40]}...): {exc}"
            log.warning(problem)
        with _cal_lock:
            _calendar_cache[url] = (now, periods, problem)
        busy += periods
        if problem:
            problems.append(problem)
    return busy, problems


# ---- open times --------------------------------------------------------------

@dataclass
class Slot:
    start: datetime
    end: datetime

    @property
    def value(self) -> str:
        return iso(self.start)

    @property
    def label(self) -> str:
        s, e = self.start.astimezone(TZ), self.end.astimezone(TZ)
        f = lambda d: d.strftime("%-I:%M %p").replace(":00", "")
        return f"{f(s)} to {f(e)}"


def _blocked(start: datetime, end: datetime, ignore_visit: int | None = None) -> list[tuple[datetime, datetime]]:
    cfg = settings()
    pad = timedelta(minutes=cfg["buffer_minutes"])
    rows = db.all(f"SELECT id, start_utc, end_utc FROM visits WHERE status IN {ACTIVE} AND end_utc > ? AND start_utc < ?",
                  (iso(start - pad), iso(end + pad)))
    blocked = [(parse(r["start_utc"]) - pad, parse(r["end_utc"]) + pad) for r in rows if r["id"] != ignore_visit]
    blocked += [(parse(r["start_utc"]), parse(r["end_utc"]))
                for r in db.all("SELECT * FROM time_off WHERE end_utc > ? AND start_utc < ?", (iso(start), iso(end)))]
    return blocked


def open_slots(now: datetime | None = None) -> list[tuple[date, list[Slot]]]:
    """Bookable times grouped by local day, soonest first."""
    cfg = settings()
    now = now or datetime.now(UTC)
    earliest = now + timedelta(hours=cfg["notice_hours"])
    length, step = timedelta(minutes=cfg["slot_minutes"]), timedelta(minutes=max(15, cfg["step_minutes"]))
    first_day = now.astimezone(TZ).date()
    last_day = first_day + timedelta(days=cfg["window_days"])
    range_start = datetime.combine(first_day, dtime.min, tzinfo=TZ)
    range_end = datetime.combine(last_day + timedelta(days=1), dtime.min, tzinfo=TZ)

    blocked = _blocked(range_start, range_end)
    cal_busy, _ = calendar_busy(range_start, range_end)
    blocked += cal_busy

    per_day: dict[date, int] = {}
    for r in db.all(f"SELECT start_utc FROM visits WHERE status IN {ACTIVE} AND start_utc >= ?", (iso(range_start),)):
        d = local(r["start_utc"]).date()
        per_day[d] = per_day.get(d, 0) + 1

    days = []
    day = first_day
    while day <= last_day:
        hours = cfg["hours"].get(str(day.weekday()))
        if hours and per_day.get(day, 0) < cfg["max_per_day"]:
            open_t = datetime.combine(day, dtime.fromisoformat(hours[0]), tzinfo=TZ)
            close_t = datetime.combine(day, dtime.fromisoformat(hours[1]), tzinfo=TZ)
            slots, t = [], open_t
            while t + length <= close_t:
                s, e = t.astimezone(UTC), (t + length).astimezone(UTC)
                if s >= earliest and not any(overlaps(s, e, b0, b1) for b0, b1 in blocked):
                    slots.append(Slot(s, e))
                t += step
            if slots:
                days.append((day, slots))
        day += timedelta(days=1)
    return days


def calendar_weeks(open_days: list[tuple[date, list[Slot]]], now: datetime | None = None) -> list[list[dict]]:
    """Monday-to-Sunday weeks covering today through the booking window, for a date picker.
    Each cell: {'date', 'in_range', 'open'} where open means at least one bookable time."""
    cfg = settings()
    first = (now or datetime.now(UTC)).astimezone(TZ).date()
    last = first + timedelta(days=cfg["window_days"])
    available = {d for d, _ in open_days}
    day = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    weeks, week = [], []
    while day <= end:
        week.append({"date": day, "in_range": first <= day <= last, "open": day in available})
        if len(week) == 7:
            weeks.append(week)
            week = []
        day += timedelta(days=1)
    return weeks


def months_label(weeks: list[list[dict]]) -> str:
    """'October' or 'October and November', from the dates clients can book."""
    names = []
    for week in weeks:
        for cell in week:
            name = cell["date"].strftime("%B %Y")
            if cell["in_range"] and name not in names:
                names.append(name)
    if len(names) > 1 and len({n.split()[1] for n in names}) == 1:
        names = [n.split()[0] for n in names[:-1]] + [names[-1]]
    return " and ".join(names)


def slots_on(open_days: list[tuple[date, list[Slot]]], day: date | None) -> list[Slot] | None:
    """Times for one day, or None if the day isn't bookable."""
    return next((slots for d, slots in open_days if d == day), None) if day else None


def parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class SlotTaken(Exception):
    pass


def book(client_id: int, user_id: int, start_value: str, *, ticket_id: int | None, location: str,
         onsite_contact: str, reason: str, status: str) -> int:
    """Book a slot from open_slots(). Re-checks inside a write lock so two people can't grab the same time."""
    cfg = settings()
    try:
        start = parse(start_value)
    except ValueError as exc:
        raise SlotTaken from exc
    if not any(s.value == iso(start) for _, slots in open_slots() for s in slots):
        raise SlotTaken
    end = start + timedelta(minutes=cfg["slot_minutes"])
    pad = timedelta(minutes=cfg["buffer_minutes"])
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        clash = conn.execute(f"SELECT 1 FROM visits WHERE status IN {ACTIVE} AND start_utc < ? AND end_utc > ?",
                             (iso(end + pad), iso(start - pad))).fetchone()
        if clash:
            conn.rollback()
            raise SlotTaken
        ts = db.now()
        cur = conn.execute(
            "INSERT INTO visits (client_id, ticket_id, booked_by, start_utc, end_utc, status, location, onsite_contact, "
            "reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (client_id, ticket_id, user_id, iso(start), iso(end), status, location, onsite_contact, reason, ts, ts))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def staff_conflicts(start: datetime, end: datetime, ignore_visit: int | None = None) -> list[str]:
    """Plain-language warnings when staff schedule outside the normal rules. Staff can still go ahead."""
    notes = []
    if any(overlaps(start, end, b0, b1) for b0, b1 in _blocked(start, end, ignore_visit)):
        notes.append("it overlaps another visit, its travel buffer, or your time off")
    busy, _ = calendar_busy(start - timedelta(days=1), end + timedelta(days=1))
    if any(overlaps(start, end, b0, b1) for b0, b1 in busy):
        notes.append("your linked calendar shows you busy then")
    return notes


# ---- calendar files (ICS) ----------------------------------------------------

def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, raw = [], line.encode("utf-8")
    while len(raw) > 75:
        cut = 75 if not out else 74
        while cut and (raw[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n ".join(out)


def ics(rows, name: str, for_staff: bool) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    host = config.BASE_URL.split("//")[-1].split("/")[0] or "portal"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SwyfTech//Client Portal//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(name)}"]
    for v in rows:
        fmt = lambda value: parse(value).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        who = v["client_name"] if for_staff else config.BUSINESS_NAME
        desc = v["reason"] or ""
        if v["onsite_contact"]:
            desc += f"\nAsk for: {v['onsite_contact']}"
        link = f"{config.BASE_URL}/staff/visits" if for_staff else f"{config.BASE_URL}/visits/{v['id']}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:visit-{v['id']}@{host}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{fmt(v['start_utc'])}",
            f"DTEND:{fmt(v['end_utc'])}",
            f"SUMMARY:{_esc(('On-site: ' + who) if for_staff else (config.BUSINESS_NAME + ' on-site visit'))}",
            f"LOCATION:{_esc(v['location'])}",
            f"DESCRIPTION:{_esc(desc.strip() + chr(10) + link)}",
            f"STATUS:{'CANCELLED' if v['status'] == 'cancelled' else 'CONFIRMED' if v['status'] in ('confirmed', 'completed') else 'TENTATIVE'}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
