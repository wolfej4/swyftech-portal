"""Serving stored files. Every download passes a permission check here first."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from . import attachments, db, storage
from .web import client_ip, flash, require_staff, require_user, verify_csrf

router = APIRouter()
CSRF = [Depends(verify_csrf)]


def file_response(where: str, key: str, filename: str, content_type: str, inline: bool = False) -> StreamingResponse:
    try:
        chunks, size = storage.open_stream(where, key)
    except storage.StorageError:
        raise HTTPException(status_code=503)
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
    disposition = f'{"inline" if inline else "attachment"}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
    headers = {
        "Content-Disposition": disposition,
        # If someone uploads a file that a browser might treat as a web page, it can't run anything here.
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
    }
    if size:
        headers["Content-Length"] = str(size)
    return StreamingResponse(chunks, media_type=content_type, headers=headers)


@router.get("/attachments/{attachment_id}")
def attachment_download(request: Request, attachment_id: int, view: int = 0):
    user = require_user(request)
    row = db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    if not row:
        raise HTTPException(status_code=404)
    if user["role"] != "staff":
        from .routes_client import _ticket_scope  # same visibility rules as the request itself
        where, params = _ticket_scope(user)
        visible = db.one(f"SELECT 1 FROM tickets t WHERE t.id = ? AND {where}", (row["ticket_id"], *params))
        if not visible or row["internal"]:
            raise HTTPException(status_code=404)
    inline = bool(view) and attachments.is_image(row)
    if not inline:
        db.audit(user["id"], "attachment.download", f"#{row['ticket_id']} {row['filename']}", client_ip(request))
    return file_response(row["storage"], row["storage_key"], row["filename"], row["content_type"], inline=inline)


@router.post("/staff/attachments/{attachment_id}/delete", dependencies=CSRF)
def attachment_delete(request: Request, attachment_id: int):
    user = require_staff(request)
    row = db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    if not row:
        raise HTTPException(status_code=404)
    storage.delete(row["storage"], row["storage_key"])
    db.run("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    db.audit(user["id"], "attachment.deleted", f"#{row['ticket_id']} {row['filename']}", client_ip(request))
    flash(request, f"{row['filename']} deleted.")
    return RedirectResponse(f"/staff/tickets/{row['ticket_id']}", status_code=303)
