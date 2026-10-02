"""Settings changed under Admin.

.env holds the defaults. Anything saved in the portal is stored in the database and laid over
the config module, so every page and email picks it up. Both web workers notice a change on
their next request (a tiny version check), so nothing needs restarting.

Secrets (the SMTP password) are encrypted with a key derived from SECRET_KEY, so a copy of
the database on its own, like a backup, doesn't reveal them.
"""
import base64
import hashlib
import logging
import threading

from cryptography.fernet import Fernet, InvalidToken

from . import config, db

log = logging.getLogger("portal.settings")

# name -> (type, is_secret)
EDITABLE: dict[str, tuple[type, bool]] = {
    "BUSINESS_NAME": (str, False), "SUPPORT_EMAIL": (str, False), "SUPPORT_PHONE": (str, False),
    "TAGLINE": (str, False), "PAYMENT_TERMS_NOTE": (str, False), "DEFAULT_NET_DAYS": (int, False),
    "STAFF_NOTIFY_EMAIL": (str, False),
    "SMTP_HOST": (str, False), "SMTP_PORT": (int, False), "SMTP_TLS": (str, False),
    "SMTP_USER": (str, False), "SMTP_PASSWORD": (str, True), "SMTP_FROM": (str, False),
    "MIN_PASSWORD_LENGTH": (int, False), "MAX_FAILED_ATTEMPTS": (int, False), "LOCKOUT_MINUTES": (int, False),
    "SESSION_HOURS": (int, False),
}
PREFIX = "cfg:"

_env_defaults = {name: getattr(config, name) for name in EDITABLE}
_lock = threading.Lock()
_loaded_version = None


def _fernet() -> Fernet:
    key = hashlib.sha256(("portal-settings:" + config.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _decode(name: str, raw: str):
    kind, secret = EDITABLE[name]
    if secret:
        try:
            raw = _fernet().decrypt(raw.encode()).decode()
        except InvalidToken:
            log.warning("Couldn't decrypt saved %s (SECRET_KEY changed?). Using the .env value.", name)
            return _env_defaults[name]
    return kind(raw) if kind is int else raw


def load() -> None:
    """Apply .env defaults, then any portal-saved values, to the config module."""
    global _loaded_version
    with _lock:
        saved = {r["key"][len(PREFIX):]: r["value"] for r in db.all("SELECT key, value FROM settings WHERE key LIKE ?", (PREFIX + "%",))}
        for name in EDITABLE:
            value = _env_defaults[name]
            if name in saved:
                try:
                    value = _decode(name, saved[name])
                except ValueError:
                    log.warning("Ignoring bad saved value for %s", name)
            setattr(config, name, value)
        _loaded_version = db.get_setting("cfg_version", "0")


def sync() -> None:
    """Cheap check on each request: reload if another worker saved settings."""
    if db.get_setting("cfg_version", "0") != _loaded_version:
        load()


def _bump() -> None:
    db.set_setting("cfg_version", str(int(db.get_setting("cfg_version", "0")) + 1))


def save(values: dict, actor_id: int | None) -> list[str]:
    """Store changed values. Returns the names that changed. Secrets left blank keep their current value."""
    changed = []
    for name, value in values.items():
        kind, secret = EDITABLE[name]
        if secret and value == "":
            continue
        if kind is int:
            value = int(value)
        if value == getattr(config, name):
            continue
        raw = _fernet().encrypt(str(value).encode()).decode() if secret else str(value)
        db.set_setting(PREFIX + name, raw)
        changed.append(name)
    if changed:
        _bump()
        load()
        db.audit(actor_id, "settings.changed", ", ".join(changed))
    return changed


def reset(name: str, actor_id: int | None) -> None:
    """Go back to the .env value."""
    db.run("DELETE FROM settings WHERE key = ?", (PREFIX + name,))
    _bump()
    load()
    db.audit(actor_id, "settings.reset", name)


def overridden() -> set[str]:
    return {r["key"][len(PREFIX):] for r in db.all("SELECT key FROM settings WHERE key LIKE ?", (PREFIX + "%",))}


def env_value(name: str):
    return _env_defaults[name]
