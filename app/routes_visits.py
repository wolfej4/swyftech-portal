"""On-site visits: clients book open times; SwyfTech confirms, reschedules and syncs to a calendar."""
import secrets
from datetime import date, datetime, time as dtime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import config, db, mailer, visits
from .web import (
    client_ip, require_admin, flash, render, require_client, require_perm, require_staff, templates, verify_csrf,
)

router = APIRouter()
CSRF = [Depends(verify_csrf)]
UTC = timezone.utc


def _now() -> str:
    return visits.iso(datetime.now(UTC))


def _visit_row(visit_id: int):
    return db.one(
        "SELECT v.*, c.name AS client_name, c.phone AS client_phone, u.name AS booker_name, u.email AS booker_email, "
        "t.subject AS ticket_subject FROM visits v JOIN clients c ON c.id = v.client_id "
        "JOIN users u ON u.id = v.booked_by LEFT JOIN tickets t ON t.id = v.ticket_id WHERE v.id = ?",
        (visit_id,),
    )


def _client_visit(user, visit_id: int):
    require_perm(user, "visits")
    v = _visit_row(visit_id)
    if not v or v["client_id"] != user["client_id"]:
        raise HTTPException(status_code=404)
    return v


def _client_contacts(client_id: int) -> list[str]:
    return [r["email"] for r in db.all(
        "SELECT email FROM users WHERE client_id = ? AND active = 1 AND role IN ('owner','billing')", (client_id,))]


def _tell_client(v, subject: str, body: str) -> None:
    """Email whoever booked it; if SwyfTech booked it, email the client's Owner and Billing people."""
    booker = db.one("SELECT role, email, active FROM users WHERE id = ?", (v["booked_by"],))
    if booker and booker["role"] != "staff" and booker["active"]:
        to = [booker["email"]]
    else:
        to = _client_contacts(v["client_id"])
    mailer.send(to, subject, body + f"\n\nDetails and calendar file:\n{config.BASE_URL}/visits/{v['id']}")


# ---- client side -------------------------------------------------------------

@router.get("/visits")
def client_visits(request: Request):
    user = require_client(request)
    require_perm(user, "visits")
    upcoming = db.all("SELECT * FROM visits WHERE client_id = ? AND status IN ('requested','confirmed') AND end_utc >= ? "
                      "ORDER BY start_utc", (user["client_id"], _now()))
    past = db.all("SELECT * FROM visits WHERE client_id = ? AND (status IN ('cancelled','completed') OR end_utc < ?) "
                  "ORDER BY start_utc DESC LIMIT 15", (user["client_id"], _now()))
    return render(request, "client/visits.html", user=user, nav="visits", upcoming=upcoming, past=past, V=visits)


def _new_form(request: Request, user, status_code: int = 200, day: str = "", **extra):
    client = db.one("SELECT * FROM clients WHERE id = ?", (user["client_id"],))
    open_tickets = db.all("SELECT id, subject FROM tickets WHERE client_id = ? AND status IN ('open','in_progress','waiting') "
                          "ORDER BY updated_at DESC", (user["client_id"],))
    open_days = visits.open_slots()
    chosen = visits.parse_day(day)
    return render(request, "client/visit_new.html", user=user, nav="visits", V=visits, open_days=open_days,
                  weeks=visits.calendar_weeks(open_days), chosen=chosen, slots=visits.slots_on(open_days, chosen),
                  cfg_visits=visits.settings(), client=client, open_tickets=open_tickets, status_code=status_code, **extra)


@router.get("/visits/new")
def client_visit_new(request: Request, ticket: int = 0, day: str = ""):
    user = require_client(request)
    require_perm(user, "visits")
    return _new_form(request, user, day=day, form={"ticket_id": ticket})


@router.get("/visits/new/times")
def client_visit_times(request: Request, day: str = ""):
    """The time list for one date, swapped into the booking form when a date is picked."""
    user = require_client(request)
    require_perm(user, "visits")
    chosen = visits.parse_day(day)
    open_days = visits.open_slots()
    # A bare fragment, rendered without render() so it doesn't use up any pending flash messages.
    return templates.TemplateResponse(request, "partials/visit_times.html",
                                      {"V": visits, "chosen": chosen, "slots": visits.slots_on(open_days, chosen), "form": {}})


@router.post("/visits", dependencies=CSRF)
def client_visit_book(request: Request, slot: str = Form(""), reason: str = Form(""), location: str = Form(""),
                      onsite_contact: str = Form(""), ticket_id: int = Form(0)):
    user = require_client(request)
    require_perm(user, "visits")
    form = {"slot": slot, "reason": reason, "location": location, "onsite_contact": onsite_contact, "ticket_id": ticket_id}
    try:
        picked_day = visits.local(slot).date().isoformat() if slot else ""
    except ValueError:
        picked_day = ""
    ticket = db.one("SELECT id, subject FROM tickets WHERE id = ? AND client_id = ?", (ticket_id, user["client_id"])) if ticket_id else None
    if not slot:
        return _new_form(request, user, 400, form=form, error="Pick a date, then a time for the visit.")
    if not reason.strip() and not ticket:
        return _new_form(request, user, 400, day=picked_day, form=form,
                         error="Tell us what the visit is for, or pick the request it's about.")
    cfg = visits.settings()
    status = "requested" if cfg["require_confirmation"] else "confirmed"
    try:
        vid = visits.book(user["client_id"], user["id"], slot, ticket_id=ticket["id"] if ticket else None,
                          location=location.strip()[:300], onsite_contact=onsite_contact.strip()[:120],
                          reason=reason.strip()[:2000], status=status)
    except visits.SlotTaken:
        form["slot"] = ""
        return _new_form(request, user, 409, day=picked_day, form=form,
                         error="Someone just booked that time. Pick another one below.")
    v = _visit_row(vid)
    db.audit(user["id"], "visit.booked", f"#{vid} {v['start_utc']}", client_ip(request))
    about = v["reason"] or f"Request #{ticket['id']}: {ticket['subject']}"
    if config.STAFF_NOTIFY_EMAIL:
        mailer.send(config.STAFF_NOTIFY_EMAIL,
                    f"{'Visit request' if status == 'requested' else 'Visit booked'}: {v['client_name']}, {visits.when(v['start_utc'], v['end_utc'])}",
                    f"{user['name']} booked an on-site visit.\nWhen: {visits.when(v['start_utc'], v['end_utc'])}\n"
                    f"Where: {v['location'] or '(not given)'}\nAsk for: {v['onsite_contact'] or '(not given)'}\nAbout: {about}\n\n"
                    f"{config.BASE_URL}/staff/visits")
    if status == "requested":
        flash(request, "Visit requested. We'll confirm the time by email, usually within a business day.")
    else:
        flash(request, "Visit booked. It's on our calendar.")
    return RedirectResponse(f"/visits/{vid}", status_code=303)


@router.get("/visits/{visit_id}")
def client_visit_detail(request: Request, visit_id: int):
    user = require_client(request)
    v = _client_visit(user, visit_id)
    upcoming = v["status"] in ("requested", "confirmed") and v["end_utc"] >= _now()
    return render(request, "client/visit.html", user=user, nav="visits", v=v, V=visits, upcoming=upcoming)


@router.post("/visits/{visit_id}/cancel", dependencies=CSRF)
def client_visit_cancel(request: Request, visit_id: int, reason: str = Form("")):
    user = require_client(request)
    v = _client_visit(user, visit_id)
    if v["status"] in ("requested", "confirmed") and v["start_utc"] > _now():
        db.run("UPDATE visits SET status = 'cancelled', cancel_reason = ?, updated_at = ? WHERE id = ?",
               (f"Cancelled by {user['name']}" + (f": {reason.strip()[:500]}" if reason.strip() else ""), db.now(), visit_id))
        db.audit(user["id"], "visit.cancelled", f"#{visit_id}", client_ip(request))
        if config.STAFF_NOTIFY_EMAIL:
            mailer.send(config.STAFF_NOTIFY_EMAIL, f"Visit cancelled: {v['client_name']}, {visits.when(v['start_utc'], v['end_utc'])}",
                        f"{user['name']} cancelled the visit.\nReason: {reason.strip() or '(none given)'}")
        flash(request, "Visit cancelled. The time is free for someone else.")
    return RedirectResponse(f"/visits/{visit_id}", status_code=303)


@router.get("/visits/{visit_id}/calendar.ics")
def client_visit_ics(request: Request, visit_id: int):
    user = require_client(request)
    v = _client_visit(user, visit_id)
    return Response(visits.ics([v], f"{config.BUSINESS_NAME} visit", for_staff=False), media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="swyftech-visit-{visit_id}.ics"'})


# ---- staff side --------------------------------------------------------------

def _parse_local(day: str, at: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), dtime.fromisoformat(at), tzinfo=visits.TZ).astimezone(UTC)


@router.get("/staff/visits")
def staff_visits(request: Request, client_id: int = 0, ticket_id: int = 0):
    user = require_staff(request)
    cutoff = visits.iso(datetime.now(UTC) - timedelta(hours=12))
    upcoming = db.all(
        "SELECT v.*, c.name AS client_name, u.name AS booker_name, t.subject AS ticket_subject FROM visits v "
        "JOIN clients c ON c.id = v.client_id JOIN users u ON u.id = v.booked_by LEFT JOIN tickets t ON t.id = v.ticket_id "
        "WHERE v.status IN ('requested','confirmed') AND v.end_utc >= ? "
        "ORDER BY CASE v.status WHEN 'requested' THEN 0 ELSE 1 END, v.start_utc", (cutoff,))
    past = db.all("SELECT v.*, c.name AS client_name FROM visits v JOIN clients c ON c.id = v.client_id "
                  "WHERE v.status IN ('cancelled','completed') OR v.end_utc < ? ORDER BY v.start_utc DESC LIMIT 20", (cutoff,))
    all_clients = db.all("SELECT id, name, address FROM clients WHERE active = 1 ORDER BY name")
    return render(request, "staff/visits.html", user=user, nav="s-visits", upcoming=upcoming, past=past, V=visits,
                  all_clients=all_clients, preselect=client_id, ticket_id=ticket_id,
                  default_minutes=visits.settings()["slot_minutes"], today=date.today().isoformat())


@router.post("/staff/visits", dependencies=CSRF)
def staff_visit_create(request: Request, client_id: int = Form(...), day: str = Form(...), at: str = Form(...),
                       minutes: int = Form(120), location: str = Form(""), onsite_contact: str = Form(""),
                       reason: str = Form(""), ticket_id: int = Form(0)):
    user = require_staff(request)
    client = db.one("SELECT * FROM clients WHERE id = ?", (client_id,))
    if not client:
        raise HTTPException(status_code=404)
    try:
        start = _parse_local(day, at)
    except ValueError:
        flash(request, "Pick a date and start time.", "error")
        return RedirectResponse("/staff/visits", status_code=303)
    end = start + timedelta(minutes=max(15, min(minutes, 12 * 60)))
    warnings = visits.staff_conflicts(start, end)
    ticket = db.one("SELECT id FROM tickets WHERE id = ? AND client_id = ?", (ticket_id, client_id)) if ticket_id else None
    ts = db.now()
    vid = db.run(
        "INSERT INTO visits (client_id, ticket_id, booked_by, start_utc, end_utc, status, location, onsite_contact, reason, "
        "created_at, updated_at) VALUES (?,?,?,?,?, 'confirmed', ?,?,?,?,?)",
        (client_id, ticket["id"] if ticket else None, user["id"], visits.iso(start), visits.iso(end),
         location.strip() or client["address"], onsite_contact.strip(), reason.strip(), ts, ts))
    v = _visit_row(vid)
    _tell_client(v, f"On-site visit scheduled: {visits.when(v['start_utc'], v['end_utc'])}",
                 f"{config.BUSINESS_NAME} scheduled an on-site visit.\nWhen: {visits.when(v['start_utc'], v['end_utc'])}\n"
                 f"Where: {v['location'] or '(your office)'}\nAbout: {v['reason'] or '(see your request)'}")
    db.audit(user["id"], "visit.scheduled", f"#{vid} {client['name']}", client_ip(request))
    flash(request, f"Visit scheduled for {client['name']}." + (f" Heads up: {' and '.join(warnings)}." if warnings else ""),
          "warn" if warnings else "ok")
    return RedirectResponse("/staff/visits", status_code=303)


@router.post("/staff/visits/{visit_id}", dependencies=CSRF)
def staff_visit_action(request: Request, visit_id: int, action: str = Form(...), reason: str = Form(""),
                       day: str = Form(""), at: str = Form(""), minutes: int = Form(0), staff_notes: str = Form("")):
    user = require_staff(request)
    v = _visit_row(visit_id)
    if not v:
        raise HTTPException(status_code=404)
    when_txt = visits.when(v["start_utc"], v["end_utc"])
    if action == "confirm" and v["status"] == "requested":
        db.run("UPDATE visits SET status = 'confirmed', updated_at = ? WHERE id = ?", (db.now(), visit_id))
        _tell_client(v, f"Visit confirmed: {when_txt}", f"Your on-site visit is confirmed.\nWhen: {when_txt}\nWhere: {v['location'] or '(your office)'}")
        flash(request, "Visit confirmed. The client was emailed.")
    elif action == "cancel" and v["status"] in ("requested", "confirmed"):
        note = reason.strip()[:500]
        db.run("UPDATE visits SET status = 'cancelled', cancel_reason = ?, updated_at = ? WHERE id = ?",
               (f"Cancelled by {config.BUSINESS_NAME}" + (f": {note}" if note else ""), db.now(), visit_id))
        _tell_client(v, f"Visit cancelled: {when_txt}",
                     f"We had to cancel the on-site visit for {when_txt}." + (f"\nReason: {note}" if note else "")
                     + "\n\nBook another time in the portal, or reply and we'll find one together.")
        flash(request, "Visit cancelled. The client was emailed.")
    elif action == "reschedule" and v["status"] in ("requested", "confirmed"):
        try:
            start = _parse_local(day, at)
        except ValueError:
            flash(request, "Pick a new date and start time.", "error")
            return RedirectResponse("/staff/visits", status_code=303)
        length = timedelta(minutes=minutes) if minutes else visits.parse(v["end_utc"]) - visits.parse(v["start_utc"])
        end = start + length
        warnings = visits.staff_conflicts(start, end, ignore_visit=visit_id)
        db.run("UPDATE visits SET start_utc = ?, end_utc = ?, status = 'confirmed', updated_at = ? WHERE id = ?",
               (visits.iso(start), visits.iso(end), db.now(), visit_id))
        new_txt = visits.when(visits.iso(start), visits.iso(end))
        _tell_client(v, f"Visit moved to {new_txt}", f"Your on-site visit has moved.\nWas: {when_txt}\nNow: {new_txt}")
        flash(request, f"Moved to {new_txt}. The client was emailed." + (f" Heads up: {' and '.join(warnings)}." if warnings else ""),
              "warn" if warnings else "ok")
    elif action == "complete" and v["status"] == "confirmed":
        db.run("UPDATE visits SET status = 'completed', updated_at = ? WHERE id = ?", (db.now(), visit_id))
        flash(request, "Marked done.")
    elif action == "notes":
        db.run("UPDATE visits SET staff_notes = ?, updated_at = ? WHERE id = ?", (staff_notes.strip(), db.now(), visit_id))
        flash(request, "Notes saved.")
    db.audit(user["id"], f"visit.{action}", f"#{visit_id}", client_ip(request))
    return RedirectResponse("/staff/visits", status_code=303)


@router.get("/staff/visit-hours")
def staff_visit_hours(request: Request):
    user = require_admin(request)
    cfg = visits.settings()
    upcoming_off = db.all("SELECT * FROM time_off WHERE end_utc >= ? ORDER BY start_utc", (_now(),))
    start = datetime.now(UTC)
    _, problems = visits.calendar_busy(start, start + timedelta(days=cfg["window_days"] + 1))
    preview = visits.open_slots()[:5]
    return render(request, "staff/visit_hours.html", user=user, nav="s-visits", cfg_visits=cfg, V=visits,
                  time_off=upcoming_off, problems=problems, preview=preview,
                  feed_url=f"{config.BASE_URL}/feeds/visits/{visits.feed_key()}.ics")


@router.post("/staff/visit-hours", dependencies=CSRF)
async def staff_visit_hours_save(request: Request):
    user = require_admin(request)
    form = await request.form()
    hours = {}
    for i in range(7):
        on, start, end = form.get(f"on{i}"), form.get(f"start{i}", ""), form.get(f"end{i}", "")
        try:
            ok = on and dtime.fromisoformat(start) < dtime.fromisoformat(end)
        except ValueError:
            ok = False
        hours[str(i)] = [start, end] if ok else None

    def num(name, lo, hi, default):
        try:
            return max(lo, min(hi, int(form.get(name, default))))
        except (TypeError, ValueError):
            return default

    d = visits.DEFAULTS
    urls = [u.strip() for u in str(form.get("calendar_urls", "")).splitlines()
            if u.strip().startswith(("https://", "http://", "webcal://"))]
    visits.save_settings({
        "hours": hours,
        "slot_minutes": num("slot_minutes", 30, 480, d["slot_minutes"]),
        "step_minutes": num("step_minutes", 15, 240, d["step_minutes"]),
        "buffer_minutes": num("buffer_minutes", 0, 240, d["buffer_minutes"]),
        "notice_hours": num("notice_hours", 0, 24 * 14, d["notice_hours"]),
        "window_days": num("window_days", 1, 90, d["window_days"]),
        "max_per_day": num("max_per_day", 1, 12, d["max_per_day"]),
        "require_confirmation": form.get("require_confirmation") == "1",
        "calendar_urls": urls[:5],
    })
    db.audit(user["id"], "visits.settings", "", client_ip(request))
    flash(request, "Visit hours saved.")
    return RedirectResponse("/staff/visit-hours", status_code=303)


@router.post("/staff/visit-hours/time-off", dependencies=CSRF)
def staff_time_off_add(request: Request, start_day: str = Form(...), end_day: str = Form(""), start_at: str = Form(""),
                       end_at: str = Form(""), note: str = Form("")):
    require_admin(request)
    try:
        start = _parse_local(start_day, start_at or "00:00")
        end = _parse_local(end_day or start_day, end_at or "23:59")
    except ValueError:
        flash(request, "Dates need to look like 2026-10-12.", "error")
        return RedirectResponse("/staff/visit-hours", status_code=303)
    if end <= start:
        flash(request, "The end needs to be after the start.", "error")
        return RedirectResponse("/staff/visit-hours", status_code=303)
    db.run("INSERT INTO time_off (start_utc, end_utc, note) VALUES (?,?,?)", (visits.iso(start), visits.iso(end), note.strip()[:200]))
    flash(request, "Time off added. Clients can't book during it.")
    return RedirectResponse("/staff/visit-hours", status_code=303)


@router.post("/staff/visit-hours/time-off/{off_id}/delete", dependencies=CSRF)
def staff_time_off_delete(request: Request, off_id: int):
    require_admin(request)
    db.run("DELETE FROM time_off WHERE id = ?", (off_id,))
    flash(request, "Time off removed.")
    return RedirectResponse("/staff/visit-hours", status_code=303)


@router.post("/staff/visit-hours/feed", dependencies=CSRF)
def staff_feed_rotate(request: Request):
    user = require_admin(request)
    visits.feed_key(rotate=True)
    db.audit(user["id"], "visits.feed_rotated", "", client_ip(request))
    flash(request, "New calendar link made. The old one stopped working, so update it in your calendar app.", "warn")
    return RedirectResponse("/staff/visit-hours", status_code=303)


@router.get("/feeds/visits/{key}.ics")
def staff_feed(key: str):
    """Private subscription feed for your calendar app. The long random key is the password."""
    if not secrets.compare_digest(key, visits.feed_key()):
        raise HTTPException(status_code=404)
    since = visits.iso(datetime.now(UTC) - timedelta(days=60))
    rows = db.all("SELECT v.*, c.name AS client_name FROM visits v JOIN clients c ON c.id = v.client_id "
                  "WHERE v.end_utc >= ? AND v.status != 'cancelled' ORDER BY v.start_utc", (since,))
    return Response(visits.ics(rows, "SwyfTech visits", for_staff=True), media_type="text/calendar")
