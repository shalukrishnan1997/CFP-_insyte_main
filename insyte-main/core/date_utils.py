"""Centralised date parsing utilities for INSYTE DMS.

All date parsing in the project should use these functions instead of
implementing ad-hoc ``strptime`` loops in individual views or services.

The format priority order is: ISO → UK → named-month variants.
US-style ``%m/%d/%Y`` is intentionally excluded because it is ambiguous
with UK ``%d/%m/%Y`` and this is a UK-focused system (GBP).
"""

from datetime import date, datetime, timedelta

from django.utils.dateparse import parse_date as django_parse_date

# Ordered: try most specific / unambiguous formats first.
_DATE_FORMATS: list[str] = [
    "%d/%m/%Y",  # UK slash: 24/02/2026
    "%d/%m/%y",  # UK slash short year: 24/02/26
    "%d-%m-%Y",  # UK dash:  24-02-2026
    "%d-%m-%y",  # UK dash short year:  24-02-26
    "%Y/%m/%d",  # ISO slash: 2026/02/24
    "%B %d, %Y",  # Named US: February 24, 2026
    "%d %B %Y",  # Named UK: 24 February 2026
    "%b. %d, %Y",  # Abbreviated: Feb. 24, 2026
    "%b %d, %Y",  # Abbreviated no dot: Feb 24, 2026
]


def parse_date(value: str | None) -> date | None:
    """Parse a date string trying ISO first, then common UK/named formats.

    Args:
        value: A date string, or ``None`` / empty string.

    Returns:
        A ``datetime.date`` on success, or ``None`` if parsing fails.
    """
    if not value:
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    # 1. Try Django's ISO-8601 parser (handles YYYY-MM-DD and variants)
    result = django_parse_date(cleaned)
    if result is not None:
        return result

    # 2. Try strptime with common formats
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue

    return None


def parse_date_with_default(
    value: str | None,
    *,
    default: date | None = None,
    default_offset_days: int = 0,
) -> date:
    """Parse a date string, falling back to a default value.

    Useful for request parameters where a missing/invalid value should
    default to today (or an offset from today).

    Args:
        value: A date string, or ``None`` / empty string.
        default: Explicit fallback date.  Takes priority over *default_offset_days*.
        default_offset_days: Days offset from today used as fallback when
            *default* is ``None``.  Negative values go into the past.

    Returns:
        Parsed date or the computed default.  Never returns ``None``.
    """
    result = parse_date(value)
    if result is not None:
        return result

    if default is not None:
        return default

    from django.utils import timezone

    return (timezone.now() + timedelta(days=default_offset_days)).date()
