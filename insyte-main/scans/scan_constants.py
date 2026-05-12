"""Module-level constants and helpers shared by scan models."""

from typing import Any

from django.db.models import Q

PAYMENT_METHOD_CHOICES = [
    ("card", "Card"),
    ("direct_debit", "Direct Debit"),
    ("cash", "Cash"),
    ("caf", "CAF Voucher"),
    ("cheque", "Cheque"),
    ("postal_order", "Postal Order"),
    ("non_financial", "Non Financial/No Payment"),
]
PAYMENT_METHOD_LABELS = dict(PAYMENT_METHOD_CHOICES)

SCAN_FORM_TYPE_SIMPLEX = "simplex"
SCAN_FORM_TYPE_DUPLEX = "duplex"
SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT = "simplex_with_payment"
SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT = "duplex_with_payment"
SCAN_FORM_TYPE_MIXED_MAIL = "mixed_mail"

SCAN_FORM_TYPE_CHOICES = [
    (SCAN_FORM_TYPE_SIMPLEX, "Simplex"),
    (SCAN_FORM_TYPE_DUPLEX, "Duplex"),
    (SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT, "Simplex + Payment Doc"),
    (SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT, "Duplex + Payment Doc"),
    (SCAN_FORM_TYPE_MIXED_MAIL, "Legacy Mixed Mail / Patch T"),
]

SCAN_FORM_TYPE_PAGES: dict[str, int | None] = {
    SCAN_FORM_TYPE_SIMPLEX: 1,
    SCAN_FORM_TYPE_DUPLEX: 2,
    SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT: 2,
    SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT: 4,
    SCAN_FORM_TYPE_MIXED_MAIL: None,
}

_NON_PAYMENT_LAYOUTS = (
    SCAN_FORM_TYPE_SIMPLEX,
    SCAN_FORM_TYPE_DUPLEX,
)
_PAYMENT_DOC_LAYOUTS = (
    SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT,
    SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT,
)
_LEGACY_MIXED_MAIL_LAYOUTS = (SCAN_FORM_TYPE_MIXED_MAIL,)
_NON_PAYMENT_AND_MIXED_LAYOUTS = _NON_PAYMENT_LAYOUTS + _LEGACY_MIXED_MAIL_LAYOUTS
_PAYMENT_DOC_AND_MIXED_LAYOUTS = _PAYMENT_DOC_LAYOUTS + _LEGACY_MIXED_MAIL_LAYOUTS

_NON_PAYMENT_METHODS = (
    "card",
    "direct_debit",
    "cash",
    "non_financial",
)
_PAYMENT_DOC_METHODS = (
    "cheque",
    "caf",
    "postal_order",
)

PAYMENT_METHOD_TO_SCAN_FORM_TYPES: dict[str, tuple[str, ...]] = {
    "card": _NON_PAYMENT_AND_MIXED_LAYOUTS,
    "direct_debit": _NON_PAYMENT_AND_MIXED_LAYOUTS,
    "cash": _NON_PAYMENT_AND_MIXED_LAYOUTS,
    "non_financial": _NON_PAYMENT_AND_MIXED_LAYOUTS,
    "cheque": _PAYMENT_DOC_AND_MIXED_LAYOUTS,
    "caf": _PAYMENT_DOC_AND_MIXED_LAYOUTS,
    "postal_order": _PAYMENT_DOC_AND_MIXED_LAYOUTS,
}

# Payment methods whose donor forms must not be sent to Google Document AI.
# Card / direct_debit: sensitive PAN / sort-code + account-number live inline on
# the donor form; extracting them via OCR defeats the manual-redaction gate.
# CAF voucher / postal order: the amount and reference live on the separate
# payment document, so OCR of the donor form adds little value. For these
# methods the pipeline still decodes the QR code (PCI-safe) and runs donor
# matching, but skips Document AI text extraction and leaves donation/payment
# fields blank for QA to fill in.
PAYMENT_METHODS_WITHOUT_DOCUMENT_AI: tuple[str, ...] = (
    "card",
    "direct_debit",
    "caf",
    "postal_order",
)


def _coerce_scan_request_bool(value: Any) -> bool:
    """Return a stable boolean for scan batch request values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _scan_batch_layout_compatibility_q() -> Q:
    """Return the DB-level layout compatibility rule for scan batches."""
    return Q(
        payment_method__in=_NON_PAYMENT_METHODS,
        scan_form_type__in=_NON_PAYMENT_AND_MIXED_LAYOUTS,
    ) | Q(
        payment_method__in=_PAYMENT_DOC_METHODS,
        scan_form_type__in=_PAYMENT_DOC_AND_MIXED_LAYOUTS,
    )
