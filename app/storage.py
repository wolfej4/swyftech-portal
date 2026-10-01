"""Where uploaded files live: the container's disk, or any S3-compatible service.

New files go to S3 when S3_BUCKET and keys are set, otherwise to data/uploads. Each file
remembers where it was saved, so switching later doesn't break older files
(`portal-cli move-files-to-s3` moves them over).

Downloads always stream through the portal, which checks who's asking first. The S3
service never needs to be reachable from the internet.
"""
import logging
import mimetypes
import threading
from pathlib import Path
from typing import BinaryIO, Iterator

from . import config

log = logging.getLogger("portal.storage")

INLINE_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
CHUNK = 1024 * 1024

_client = None
_client_lock = threading.Lock()


class StorageError(Exception):
    """The file couldn't be saved or read."""


def backend() -> str:
    """Where new files go."""
    return "s3" if config.s3_enabled() else "local"


def content_type_for(filename: str) -> str:
    """Decide the type from the extension, never from what the browser claimed."""
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


# ---- S3 ----------------------------------------------------------------------

def _s3():
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config
            _client = boto3.client(
                "s3",
                endpoint_url=config.S3_ENDPOINT_URL or None,
                region_name=config.S3_REGION or "us-east-1",
                aws_access_key_id=config.S3_ACCESS_KEY_ID,
                aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
                verify=config.S3_VERIFY_TLS,
                config=Config(
                    s3={"addressing_style": "path" if config.S3_FORCE_PATH_STYLE else "auto"},
                    # Newer boto3 adds checksum headers by default, which several S3-compatible
                    # services reject. Only send them when an operation truly needs them.
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=60,
                ),
            )
        return _client


def _s3_key(key: str) -> str:
    return f"{config.S3_PREFIX}{key}"


# ---- local disk ----------------------------------------------------------------

def _local_path(key: str) -> Path:
    root = config.UPLOAD_DIR.resolve()
    path = (root / key).resolve()
    if root not in path.parents and path != root:
        raise StorageError("Bad file path")
    return path


# ---- public ----------------------------------------------------------------------

def save(fileobj: BinaryIO, key: str, content_type: str) -> str:
    """Store a file-like object under key. Returns the backend it went to ('local' or 's3')."""
    fileobj.seek(0)
    where = backend()
    try:
        if where == "s3":
            from botocore.exceptions import BotoCoreError, ClientError
            try:
                _s3().upload_fileobj(fileobj, config.S3_BUCKET, _s3_key(key), ExtraArgs={"ContentType": content_type})
            except (BotoCoreError, ClientError) as exc:
                raise StorageError(str(exc)) from exc
        else:
            path = _local_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("wb") as out:
                    while chunk := fileobj.read(CHUNK):
                        out.write(chunk)
            except OSError as exc:
                raise StorageError(str(exc)) from exc
    except StorageError:
        log.exception("Couldn't save %s to %s", key, where)
        raise
    return where


def open_stream(where: str, key: str) -> tuple[Iterator[bytes], int]:
    """(chunks, size) for a stored file."""
    if where == "s3":
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            obj = _s3().get_object(Bucket=config.S3_BUCKET, Key=_s3_key(key))
        except (BotoCoreError, ClientError) as exc:
            log.warning("Couldn't read %s from S3: %s", key, exc)
            raise StorageError(str(exc)) from exc
        body = obj["Body"]

        def chunks():
            try:
                yield from body.iter_chunks(CHUNK)
            finally:
                body.close()
        return chunks(), int(obj.get("ContentLength") or 0)

    path = _local_path(key)
    if not path.is_file():
        raise StorageError("File is missing from disk")

    def chunks():
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                yield chunk
    return chunks(), path.stat().st_size


def delete(where: str, key: str) -> None:
    try:
        if where == "s3":
            _s3().delete_object(Bucket=config.S3_BUCKET, Key=_s3_key(key))
        else:
            _local_path(key).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - a leftover file is better than a failed page
        log.exception("Couldn't delete %s from %s", key, where)


def check() -> str:
    """Write, read back and delete a small test file. Returns a plain description of where files go."""
    import io
    import uuid
    key = f"healthcheck/{uuid.uuid4().hex}.txt"
    payload = b"swyftech portal storage check"
    where = save(io.BytesIO(payload), key, "text/plain")
    chunks, _ = open_stream(where, key)
    if b"".join(chunks) != payload:
        raise StorageError("Read back different bytes than were written")
    delete(where, key)
    if where == "s3":
        return f"S3 bucket '{config.S3_BUCKET}' at {config.S3_ENDPOINT_URL or 'AWS'} (prefix '{config.S3_PREFIX}')"
    return f"local disk at {config.UPLOAD_DIR}"
