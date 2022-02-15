"""Utilities to help the ad server communicate with Stripe."""
from django.conf import settings


STRIPE_DASHBOARD_URL = "https://dashboard.stripe.com/"


def get_invoice_url(invoice_id):
    """Get an invoice URL for an invoice ID."""
    url = STRIPE_DASHBOARD_URL
    if settings.DEBUG:
        url += "test/"  # pragma: no cover
    url += f"invoices/{invoice_id}"
    return url
