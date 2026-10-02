"""Plain-text email. If SMTP isn't set up, messages are logged and skipped."""
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config

log = logging.getLogger("portal.mail")


def _send(to: list[str], subject: str, body: str, raise_errors: bool = False) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr((config.BUSINESS_NAME, config.SMTP_FROM))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=config.SMTP_FROM.split("@")[-1] or None)
    if config.SUPPORT_EMAIL:
        msg["Reply-To"] = config.SUPPORT_EMAIL
    signature = f"\n\n-- \n{config.BUSINESS_NAME}"
    if config.SUPPORT_PHONE:
        signature += f"\n{config.SUPPORT_PHONE}"
    signature += f"\n{config.BASE_URL}"
    msg.set_content(body + signature)

    try:
        if config.SMTP_TLS == "ssl":
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ssl.create_default_context(), timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
            if config.SMTP_TLS == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        log.info("Sent '%s' to %s", subject, to)
    except Exception:  # noqa: BLE001 - email must never break a page
        log.exception("Could not send '%s' to %s", subject, to)
        if raise_errors:
            raise


def send(to: list[str] | str, subject: str, body: str) -> bool:
    """Queue an email. Returns False when SMTP isn't configured so callers can show a link instead."""
    recipients = [to] if isinstance(to, str) else [r for r in to if r]
    if not recipients:
        return False
    if not config.smtp_enabled():
        log.info("SMTP not configured; skipped '%s' to %s", subject, recipients)
        return False
    threading.Thread(target=_send, args=(recipients, subject, body), daemon=True).start()
    return True


def send_test(to: str) -> str | None:
    """Send right away and report what went wrong, in plain words. None means it worked."""
    if not config.smtp_enabled():
        return "Add a server and a From address first."
    try:
        _send([to], f"Test email from the {config.BUSINESS_NAME} portal",
              "This is a test. If you're reading it, the portal can send email.", raise_errors=True)
        return None
    except smtplib.SMTPAuthenticationError as exc:
        hint = ""
        if "office365" in config.SMTP_HOST or "outlook" in config.SMTP_HOST:
            hint = (" For Microsoft 365, SMTP AUTH must be turned on for this mailbox, and Microsoft is retiring "
                    "basic-auth SMTP. A sending service like SMTP2GO or Postmark avoids this.")
        return f"The server didn't accept the username or password ({exc.smtp_code}).{hint}"
    except smtplib.SMTPSenderRefused as exc:
        return f"The server won't send as {config.SMTP_FROM}. The From address usually has to match the login. ({exc.smtp_code})"
    except (TimeoutError, OSError) as exc:
        return f"Couldn't connect to {config.SMTP_HOST}:{config.SMTP_PORT}. Check the server, port and security setting. ({exc})"
    except smtplib.SMTPException as exc:
        return f"The server refused the message: {exc}"
