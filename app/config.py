"""Settings, read from environment variables or the .env file next to the app."""
import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    env_file = pathlib.Path(os.getenv("PORTAL_ENV_FILE", BASE_DIR / ".env"))
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "portal.db"

SECRET_KEY = os.getenv("SECRET_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SECURE_COOKIES = _bool("SECURE_COOKIES", BASE_URL.startswith("https://"))
TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "SwyfTech LLC")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")
TAGLINE = os.getenv("TAGLINE", "IT support for businesses across Okaloosa, Santa Rosa, Walton and Escambia counties.")
SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "")
STAFF_NOTIFY_EMAIL = os.getenv("STAFF_NOTIFY_EMAIL", SUPPORT_EMAIL)

INVOICE_PREFIX = os.getenv("INVOICE_PREFIX", "SWY")
DEFAULT_NET_DAYS = int(os.getenv("DEFAULT_NET_DAYS", "15"))
PAYMENT_TERMS_NOTE = os.getenv(
    "PAYMENT_TERMS_NOTE", "Payment is due by the date shown. Thank you for your business."
)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SUPPORT_EMAIL)
SMTP_TLS = os.getenv("SMTP_TLS", "starttls").lower()  # starttls | ssl | none

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Snipe-IT (optional): shows each client their equipment and lets them pick a device on requests.
SNIPEIT_URL = os.getenv("SNIPEIT_URL", "").rstrip("/")
SNIPEIT_API_TOKEN = os.getenv("SNIPEIT_API_TOKEN", "")
SNIPEIT_VERIFY_TLS = _bool("SNIPEIT_VERIFY_TLS", True)
SNIPEIT_CACHE_MINUTES = int(os.getenv("SNIPEIT_CACHE_MINUTES", "5"))
SNIPEIT_WARRANTY_WARN_DAYS = int(os.getenv("SNIPEIT_WARRANTY_WARN_DAYS", "60"))

# S3-compatible storage (optional). Leave S3_BUCKET blank to keep files on the container's disk.
# Works with Garage, SeaweedFS, Backblaze B2, Cloudflare R2, AWS S3 and others.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").rstrip("/")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_PREFIX = os.getenv("S3_PREFIX", "swyftech-portal/").lstrip("/")
S3_FORCE_PATH_STYLE = _bool("S3_FORCE_PATH_STYLE", bool(S3_ENDPOINT_URL))
S3_VERIFY_TLS = _bool("S3_VERIFY_TLS", True)

# Sign-in rules (also editable under Admin > Sign-in and security)
MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "12"))
MAX_FAILED_ATTEMPTS = int(os.getenv("MAX_FAILED_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(os.getenv("LOCKOUT_MINUTES", "15"))

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_FILES_PER_MESSAGE = int(os.getenv("MAX_FILES_PER_MESSAGE", "5"))
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))


def smtp_enabled() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def s3_enabled() -> bool:
    return bool(S3_BUCKET and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY)


def snipeit_enabled() -> bool:
    return bool(SNIPEIT_URL and SNIPEIT_API_TOKEN)
