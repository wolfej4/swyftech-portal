"""Monthly reports: SwyfTech reviews and sends; client Owners and Billing people read them."""
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import db, reports
from .web import client_ip, flash, render, require_admin, require_client, require_perm, verify_csrf

router = APIRouter()
CSRF = [Depends(verify_csrf)]


def _check_month(month: str) -> None:
    if not reports.valid_month(month):
        raise HTTPException(status_code=404)


def _author(row) -> str:
    if row and row["sent_by"]:
        u = db.one("SELECT name FROM users WHERE id = ?", (row["sent_by"],))
        if u and u["name"]:
            return u["name"]
    return ""


# ---- client side -------------------------------------------------------------

@router.get("/reports")
def client_reports(request: Request):
    user = require_client(request)
    require_perm(user, "reports")
    rows = db.all("SELECT * FROM monthly_reports WHERE client_id = ? AND sent_at IS NOT NULL ORDER BY month DESC",
                  (user["client_id"],))
    items = [{"month": r["month"], "label": reports.label(r["month"]),
              "headline": json.loads(r["data_json"])["headline"][0]} for r in rows]
    return render(request, "client/reports.html", user=user, nav="reports", items=items)


@router.get("/reports/{month}")
def client_report(request: Request, month: str):
    user = require_client(request)
    require_perm(user, "reports")
    _check_month(month)
    row = reports.record(user["client_id"], month)
    if not row or not row["sent_at"]:
        raise HTTPException(status_code=404)
    return render(request, "client/report.html", user=user, nav="reports", r=json.loads(row["data_json"]),
                  note=row["note"], author=_author(row))


# ---- staff side --------------------------------------------------------------

@router.get("/staff/reports")
def staff_reports(request: Request, month: str = ""):
    user = require_admin(request)
    month = month if reports.valid_month(month) else reports.last_month()
    rows = []
    for c in db.all("SELECT * FROM clients WHERE active = 1 ORDER BY name"):
        data, rec = reports.view_data(c["id"], month)
        rows.append({"client": c, "data": data, "rec": rec, "owners": reports.recipients(c["id"])})
    unsent = sum(1 for r in rows if r["owners"] and not (r["rec"] and r["rec"]["sent_at"]))
    return render(request, "staff/reports.html", user=user, nav="s-reports", month=month, month_label=reports.label(month),
                  months=[(mo, reports.label(mo)) for mo in reports.recent_months()], rows=rows, unsent=unsent, auto_send=reports.auto_send_enabled())


@router.post("/staff/reports/settings", dependencies=CSRF)
def staff_reports_settings(request: Request, auto_send: str = Form(""), month: str = Form("")):
    user = require_admin(request)
    db.set_setting("reports_auto_send", "1" if auto_send == "1" else "0")
    db.audit(user["id"], "reports.auto_send", auto_send or "0", client_ip(request))
    flash(request, "Reports will be sent automatically on the 1st." if auto_send == "1"
          else "You'll get an email on the 1st to review reports before they go out.")
    return RedirectResponse(f"/staff/reports?month={month}" if reports.valid_month(month) else "/staff/reports", status_code=303)


@router.post("/staff/reports/{month}/send-all", dependencies=CSRF)
def staff_reports_send_all(request: Request, month: str):
    user = require_admin(request)
    _check_month(month)
    sent = 0
    for c in reports.active_clients():
        rec = reports.record(c["id"], month)
        if not (rec and rec["sent_at"]):
            reports.send(c["id"], month, user["id"])
            sent += 1
    flash(request, f"Sent {sent} {'report' if sent == 1 else 'reports'} for {reports.label(month)}." if sent else "Nothing left to send.")
    return RedirectResponse(f"/staff/reports?month={month}", status_code=303)


@router.get("/staff/reports/{client_id}/{month}")
def staff_report(request: Request, client_id: int, month: str):
    user = require_admin(request)
    _check_month(month)
    client = db.one("SELECT * FROM clients WHERE id = ?", (client_id,))
    if not client:
        raise HTTPException(status_code=404)
    data, rec = reports.view_data(client_id, month)
    return render(request, "staff/report.html", user=user, nav="s-reports", client=client, month=month, r=data, rec=rec,
                  note=rec["note"] if rec else "", author=_author(rec) or user["name"], owners=reports.recipients(client_id))


@router.post("/staff/reports/{client_id}/{month}/note", dependencies=CSRF)
def staff_report_note(request: Request, client_id: int, month: str, note: str = Form("")):
    require_admin(request)
    _check_month(month)
    reports.save_note(client_id, month, note)
    flash(request, "Note saved.")
    return RedirectResponse(f"/staff/reports/{client_id}/{month}", status_code=303)


@router.post("/staff/reports/{client_id}/{month}/send", dependencies=CSRF)
def staff_report_send(request: Request, client_id: int, month: str, note: str = Form(None)):
    user = require_admin(request)
    _check_month(month)
    if note is not None:
        reports.save_note(client_id, month, note)
    emailed, to = reports.send(client_id, month, user["id"])
    if not to:
        flash(request, "Saved for the client to see, but there's no Owner to email yet.", "warn")
    elif emailed:
        flash(request, f"Report sent to {', '.join(to)}.")
    else:
        flash(request, "Report saved. Email isn't set up, so let the owner know it's in the portal under Reports.", "warn")
    return RedirectResponse(f"/staff/reports/{client_id}/{month}", status_code=303)
