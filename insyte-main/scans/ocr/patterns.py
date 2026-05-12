"""Compiled regex patterns and constants used by the OCR package.

Only patterns actively referenced by live code are kept here.
"""

import re

# ─── Address / Contact ────────────────────────────────────────────────────────

POSTCODE_PATTERN = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.IGNORECASE)

EMAIL_PATTERN = re.compile(
    r"\b([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})\b",
    re.IGNORECASE,
)

# UK phone number — matches 07xxx xxxxxx, 01xxx xxxxxx, +44 7xxx xxxxxx etc.
# Allows spaces, hyphens and brackets as separators.
PHONE_PATTERN = re.compile(
    r"\b((?:\+44|0)[\d][\d\s\-\(\)]{7,13}[\d])\b",
    re.IGNORECASE,
)

# ─── Noise / Filtering ────────────────────────────────────────────────────────

ADDRESS_NOISE_KEYWORDS: frozenset[str] = frozenset(
    {
        "mobile",
        "phone",
        "telephone",
        "email",
        "number",
        "contact",
        "text",
        "postcode",
    }
)

# Street-suffix hint used by address_looks_noisy
STREET_SUFFIX_RE = re.compile(
    r"^\d+[A-Za-z]?\s+|\b(street|st\.?|road|rd\.?|lane|ln\.?|avenue|ave\.?|"
    r"close|cl\.?|court|ct\.?|drive|dr\.?|way|place|pl\.?|flat|mount)\b",
    re.IGNORECASE,
)
