"""Files attached to requests: screenshots, photos of error messages, exports, PDFs."""
import re
import uuid

from fastapi import UploadFile

from . import config, db, storage

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".txt", ".log", ".csv",
           ".doc", ".docx", ".xls", ".xlsx", ".pptx", ".zip", ".eml", ".msg"}
ACCEPT_ATTR = ",".join(sorted(ALLOWED))


class Rejected(Exception):
    """A file can't be accepted. The message is shown to the person."""


def _ext(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if name and "." in name else ""


def _size(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def real_files(files) -> list[UploadFile]:
    """When nothing was picked, browsers send an empty file part (or an empty text field). Ignore those."""
    return [f for f in (files or []) if hasattr(f, "filename") and hasattr(f, "file") and f.filename]


def check(files: list[UploadFile]) -> None:
    """Validate every file before saving any, so a request never ends up half-attached."""
    if len(files) > config.MAX_FILES_PER_MESSAGE:
        raise Rejected(f"Attach up to {config.MAX_FILES_PER_MESSAGE} files at a time.")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    for f in files:
        if _ext(f.filename) not in ALLOWED:
            raise Rejected(f"{f.filename} can't be attached. Photos, PDFs, Office files, text files and zips are fine.")
        if _size(f) > limit:
            raise Rejected(f"{f.filename} is over {config.MAX_UPLOAD_MB} MB. Try a smaller photo or a zip.")
        if _size(f) == 0:
            raise Rejected(f"{f.filename} is empty.")


def save_all(files: list[UploadFile], ticket, message_id: int, user_id: int, internal: bool) -> int:
    """Store already-checked files and link them to a message. Returns how many were saved."""
    saved = 0
    for f in files:
        ext = _ext(f.filename)
        key = f"attachments/{ticket['client_id']}/{ticket['id']}/{uuid.uuid4().hex}{ext}"
        ctype = storage.content_type_for(f.filename)
        where = storage.save(f.file, key, ctype)
        safe_name = re.sub(r"[^\w.\- ()]", "_", f.filename)[:150]
        db.run(
            "INSERT INTO attachments (ticket_id, message_id, client_id, uploaded_by, filename, content_type, size_bytes, "
            "storage, storage_key, internal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ticket["id"], message_id, ticket["client_id"], user_id, safe_name, ctype, _size(f), where, key, int(internal), db.now()),
        )
        saved += 1
    return saved


def for_ticket(ticket_id: int, include_internal: bool) -> dict[int, list]:
    """{message_id: [attachment rows]} for rendering a thread."""
    rows = db.all("SELECT * FROM attachments WHERE ticket_id = ? " + ("" if include_internal else "AND internal = 0 ") + "ORDER BY id",
                  (ticket_id,))
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["message_id"], []).append(r)
    return grouped


def is_image(row) -> bool:
    return _ext(row["filename"]) in storage.INLINE_IMAGES
