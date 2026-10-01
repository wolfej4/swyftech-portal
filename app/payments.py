"""Stripe Checkout. Card and bank details never touch the portal; Stripe hosts the payment page."""
import logging

import stripe

from . import config, db, mailer

log = logging.getLogger("portal.payments")


def _client():
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def create_checkout(invoice, payer_email: str) -> str:
    """Create a Checkout Session for the invoice balance and return its URL."""
    total = db.invoice_total(invoice["id"])
    s = _client()
    session = s.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": total,
                "product_data": {"name": f"Invoice {invoice['number']}", "description": config.BUSINESS_NAME},
            },
            "quantity": 1,
        }],
        customer_email=payer_email,
        client_reference_id=str(invoice["id"]),
        metadata={"invoice_id": str(invoice["id"]), "invoice_number": invoice["number"]},
        payment_intent_data={
            "description": f"{config.BUSINESS_NAME} invoice {invoice['number']}",
            "metadata": {"invoice_id": str(invoice["id"]), "invoice_number": invoice["number"]},
        },
        success_url=f"{config.BASE_URL}/invoices/{invoice['id']}/paid?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{config.BASE_URL}/invoices/{invoice['id']}",
    )
    return session.url


def mark_paid(invoice_id: int, method: str, ref: str, actor_id: int | None = None) -> bool:
    """Mark an invoice paid once. Returns True only the first time."""
    changed = db.run(
        "UPDATE invoices SET status = 'paid', paid_at = ?, payment_method = ?, payment_ref = ? "
        "WHERE id = ? AND status = 'sent'",
        (db.now(), method, ref, invoice_id),
    )
    if not changed:
        return False
    inv = db.one("SELECT i.*, c.name AS client_name FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.id = ?", (invoice_id,))
    db.audit(actor_id, "invoice.paid", f"{inv['number']} via {method} {ref}".strip())
    if config.STAFF_NOTIFY_EMAIL:
        amount = db.invoice_total(invoice_id) / 100
        mailer.send(
            config.STAFF_NOTIFY_EMAIL,
            f"Payment received: {inv['number']} ({inv['client_name']})",
            f"{inv['client_name']} paid invoice {inv['number']} for ${amount:,.2f} via {method}.\n"
            f"{config.BASE_URL}/staff/invoices/{invoice_id}",
        )
    return True


def _plain(obj) -> dict:
    """Stripe objects aren't dicts in newer libraries; convert so .get() works."""
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def _settle_session(session) -> bool:
    """Apply a completed Checkout Session if it's paid and the amount matches."""
    session = _plain(session)
    if session.get("payment_status") != "paid":
        return False
    invoice_id = int((session.get("metadata") or {}).get("invoice_id") or session.get("client_reference_id") or 0)
    if not invoice_id:
        return False
    expected = db.invoice_total(invoice_id)
    if session.get("amount_total") != expected:
        log.warning("Amount mismatch for invoice %s: paid %s, expected %s", invoice_id, session.get("amount_total"), expected)
        return False
    return mark_paid(invoice_id, "Stripe", session.get("payment_intent") or session.get("id"))


def confirm_return(session_id: str, invoice_id: int) -> bool:
    """Called when the client lands back on the portal after paying, in case the webhook is slow."""
    session = _plain(_client().checkout.Session.retrieve(session_id))
    if str((session.get("metadata") or {}).get("invoice_id")) != str(invoice_id):
        return False
    _settle_session(session)
    row = db.one("SELECT status FROM invoices WHERE id = ?", (invoice_id,))
    return bool(row and row["status"] == "paid")


def handle_webhook(payload: bytes, signature: str) -> None:
    event = stripe.Webhook.construct_event(payload, signature, config.STRIPE_WEBHOOK_SECRET)
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        _settle_session(event["data"]["object"])
