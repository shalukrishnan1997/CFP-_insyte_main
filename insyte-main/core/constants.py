"""Core constants for the INSYTE DMS application.

Centralised constants used across the project. Import from here
instead of defining local duplicates.
"""

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
CURRENCY_CODE: str = "GBP"
"""ISO 4217 currency code used throughout the system."""

CURRENCY_SYMBOL: str = "£"
"""Display symbol for the default currency."""

# ---------------------------------------------------------------------------
# OCR Processing
# ---------------------------------------------------------------------------
MIN_OCR_CONFIDENCE: float = 0.30
"""Minimum OCR confidence score (0.0-1.0) below which a donation is auto-flagged."""
