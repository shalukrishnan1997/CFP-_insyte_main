"""OCRExtractor — extracts donation fields from Document AI Form Parser output.

Form Parser returns labelled key-value pairs directly, so no regex baseline
is needed.  Entities are mapped to internal field names via
``_ENTITY_FIELD_MAP`` in :class:`OCREntityOverridesMixin`.
"""

import logging
import re
from decimal import Decimal
from typing import Any

from scans.ocr.entity_overrides import OCREntityOverridesMixin
from scans.ocr.field_extractors import parse_address_block
from scans.ocr.patterns import EMAIL_PATTERN, PHONE_PATTERN, POSTCODE_PATTERN
from scans.ocr.postcode_lookup import lookup_postcode

from . import donor_matching as DM

logger = logging.getLogger(__name__)


class OCRExtractor(OCREntityOverridesMixin):
    """Extract structured donation data from Document AI Form Parser output.

    Form Parser returns key-value pairs directly — no regex needed.
    All methods are static; no instance state required.

    Public entry points:

    * :meth:`extract_from_document_ai` — maps Form Parser entities to fields.
    * :meth:`match_donor` — resolves an extracted URN to a ``Donor`` record.
    """

    @staticmethod
    def _empty_result(raw_text: str = "") -> dict[str, Any]:
        """Return a blank extraction result dict with all fields at defaults.

        Args:
            raw_text: Raw OCR text stored for debugging / QA display.

        Returns:
            Dict with all expected extraction keys set to empty/None defaults.
        """
        return {
            "urn": "",
            "urn_confidence": 0.0,
            "amount": "",
            "amount_confidence": 0.0,
            "gift_aid": None,
            "gift_aid_confidence": 0.0,
            # QA template reads ed.gift_aid_consent — kept in sync with gift_aid
            "gift_aid_consent": None,
            "gift_aid_consent_confidence": 0.0,
            "payment_method": "",
            "payment_method_confidence": 0.0,
            "donation_date": "",
            "donation_date_confidence": 0.0,
            "processed_payment_date": "",
            "processed_payment_date_confidence": 0.0,
            "appeal_code": "",
            "package_code": "",
            "donor_name": "",
            "donor_name_confidence": 0.0,
            "donor_first_name": "",
            "donor_first_name_confidence": 0.0,
            "donor_last_name": "",
            "donor_last_name_confidence": 0.0,
            "title": "",
            "title_confidence": 0.0,
            # Cheque
            "cheque_number": "",
            "cheque_number_confidence": 0.0,
            "cheque_date": "",
            "cheque_date_confidence": 0.0,
            # Bank / Direct Debit
            "sort_code": "",
            "sort_code_confidence": 0.0,
            "account_number": "",
            "account_number_confidence": 0.0,
            # Card
            "card_last_four": "",
            "card_last_four_confidence": 0.0,
            "card_expiry_date": "",
            "card_expiry_date_confidence": 0.0,
            "card_holder_name": "",
            "card_holder_name_confidence": 0.0,
            # CAF Voucher
            "caf_voucher_number": "",
            "caf_voucher_number_confidence": 0.0,
            "caf_amount": "",
            "caf_amount_confidence": 0.0,
            # Postal Order
            "postal_order_number": "",
            "postal_order_number_confidence": 0.0,
            "postal_order_date": "",
            "postal_order_date_confidence": 0.0,
            # Address
            "postcode": "",
            "postcode_confidence": 0.0,
            "postcode_valid": None,
            "address_line1": "",
            "address_line1_confidence": 0.0,
            "address_line2": "",
            "address_line2_confidence": 0.0,
            "city": "",
            "city_confidence": 0.0,
            "county": "",
            "county_confidence": 0.0,
            # Contact
            "phone": "",
            "phone_confidence": 0.0,
            "email": "",
            "email_confidence": 0.0,
            # Consent
            "contact_consent": None,
            "contact_consent_confidence": 0.0,
            "email_consent": None,
            "email_consent_confidence": 0.0,
            "sms_consent": None,
            "sms_consent_confidence": 0.0,
            "phone_consent": None,
            "phone_consent_confidence": 0.0,
            "post_consent": None,
            "post_consent_confidence": 0.0,
            "raw_text": raw_text[:5000],
        }

    # ── Checkbox symbols used in UK donation form consent rows ────────────────
    _CHECKBOX_FILLED: frozenset[str] = frozenset("☑☒✓✔◉●✗")
    _CHECKBOX_EMPTY: frozenset[str] = frozenset("☐○□")

    # Consent labels → field names for raw-text inline checkbox fallback.
    # Ordered so earlier patterns take priority (email before generic text).
    _CONSENT_LABEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bemail\b", re.IGNORECASE), "email_consent"),
        (re.compile(r"\bphone\b", re.IGNORECASE), "phone_consent"),
        (re.compile(r"\btext\b|\bsms\b", re.IGNORECASE), "sms_consent"),
    ]

    _TITLE_PREFIXES: frozenset[str] = frozenset(
        {"mr", "mrs", "ms", "miss", "dr", "prof", "rev", "sir", "lady", "mx"}
    )
    _ADDRESS_NOISE_PREFIXES: tuple[str, ...] = (
        "step ",
        "signature",
        "date",
        "please ",
        "please complete",
        "by completing",
        "i would like",
        "to ",
        "gift aid",
        "card number",
        "expiry date",
    )
    _STREET_HINT_PATTERN = re.compile(
        r"\b(street|st\b|road|rd\b|lane|ln\b|avenue|ave\b|close|cl\b|way|drive|dr\b|court|ct\b|place|pl\b|crescent|cres\b|terrace|terr\b|gardens|grove|park)\b",
        re.IGNORECASE,
    )
    _POSTCODE_LIKE_LINE_PATTERN = re.compile(
        r"^[A-Z0-9]{2,4}\s+[A-Z0-9]{2,4}$", re.IGNORECASE
    )
    _PAYMENT_DATE_PATTERN = re.compile(
        r"\bdate\s*[:\-]?\s*(\d{1,2}\s*[/-]\s*\d{1,2}\s*[/-]\s*\d{2,4})\b",
        re.IGNORECASE,
    )
    _DATE_CONTEXT_REJECT = re.compile(
        r"\b(?:processed|receiv(?:ed|ing)|bank\s+stamp|office\s+use|for\s+bank|credits?)"
        r"\b",
        re.IGNORECASE,
    )
    _CURRENCY_AMOUNT_PATTERN = re.compile(
        r"[£€]\s*(\d{1,6}(?:\s*[.-]\s*\d{2})?)\b",
        re.IGNORECASE,
    )
    _CHEQUE_AMOUNT_PATTERN = re.compile(r"\b(\d{1,6}\s*-\s*\d{2})\b")
    _OPT_OUT_NOISE_PATTERN = re.compile(
        r"do\s+not\s+want\s+to\s+receive\s+a\s+thank\s+you\s+letter",
        re.IGNORECASE,
    )

    @staticmethod
    def _clean_raw_text_lines(raw_text: str) -> list[str]:
        """Return non-empty OCR lines with collapsed internal whitespace.

        Args:
            raw_text: Raw OCR text from Document AI.

        Returns:
            Cleaned non-empty lines in original order.
        """
        return [
            re.sub(r"\s+", " ", line).strip()
            for line in raw_text.splitlines()
            if line.strip()
        ]

    @staticmethod
    def _normalise_name_line(line: str) -> str:
        """Extract a plausible donor name from a raw OCR line.

        Args:
            line: Single OCR text line.

        Returns:
            Normalised donor name or an empty string when the line is not a
            credible name candidate.
        """
        cleaned = re.sub(r"\s+", " ", line).strip(" :-")
        if not cleaned or any(char.isdigit() for char in cleaned):
            return ""

        label_match = re.match(
            r"^(?:name|full name|supporter|donor)\s*[:\-]\s*(.+)$",
            cleaned,
            re.IGNORECASE,
        )
        if label_match:
            cleaned = label_match.group(1).strip()

        parts = [part.rstrip(".") for part in cleaned.split()]
        if len(parts) < 2:
            return ""

        first_part = parts[0].lower()
        if first_part in OCRExtractor._TITLE_PREFIXES:
            remaining = parts[1:]
            if len(remaining) >= 2 and all(
                token[:1].isalpha() and token[0].isupper() for token in remaining
            ):
                return cleaned

        return ""

    @staticmethod
    def _looks_like_address_noise(line: str) -> bool:
        """Return whether a raw OCR line is clearly not part of an address.

        Args:
            line: Single OCR text line.

        Returns:
            ``True`` when the line looks like an instruction or section label.
        """
        lowered = line.lower()
        return lowered.startswith(OCRExtractor._ADDRESS_NOISE_PREFIXES)

    @staticmethod
    def _looks_like_address_line(line: str) -> bool:
        """Return whether a raw OCR line looks like a postal address line.

        Args:
            line: Single OCR text line.

        Returns:
            ``True`` when the line resembles a street-address line.
        """
        if not line:
            return False
        return bool(
            re.match(r"^\d+[A-Za-z]?(?:[\/-]\d+)?\s+", line)
            or OCRExtractor._STREET_HINT_PATTERN.search(line)
        )

    @staticmethod
    def _extract_postcode_like_value(line: str) -> str:
        """Return a postcode or postcode-like token from an isolated OCR line.

        Args:
            line: Single OCR text line.

        Returns:
            Canonical postcode text when the line looks like a postcode, else
            an empty string.
        """
        postcode_match = POSTCODE_PATTERN.search(line)
        if postcode_match:
            return postcode_match.group(1).upper().strip()

        cleaned = re.sub(r"\s+", " ", line).strip().upper()
        if OCRExtractor._POSTCODE_LIKE_LINE_PATTERN.match(cleaned):
            parts = cleaned.split()
            if len(parts) != 2:
                return ""
            if not all(
                any(char.isalpha() for char in part)
                and any(char.isdigit() for char in part)
                for part in parts
            ):
                return ""
            return cleaned
        return ""

    @staticmethod
    def _extract_payment_date_from_raw_text(raw_text: str) -> str:
        """Return a donor-visible date hint from unstructured OCR paragraphs.

        Rejects snippets that resemble bank/office processing stamps so we do not
        treat reconciliation dates as the supporter-facing donation_date.
        """
        for match in OCRExtractor._PAYMENT_DATE_PATTERN.finditer(raw_text):
            start_window = match.start()
            # Keep the window tight so unrelated bank stamps three lines earlier do
            # not suppress the donor-visible Date: anchor on this page segment.
            snippet = raw_text[max(0, start_window - 48) : start_window + 36]
            if OCRExtractor._DATE_CONTEXT_REJECT.search(snippet):
                logger.debug(
                    "Skipping OCR Date: match in processed/banking context (%r)",
                    snippet[:90],
                )
                continue
            return re.sub(r"\s+", "", match.group(1))
        return ""

    @staticmethod
    def _extract_amount_near_donation_phrases(raw_text: str) -> str:
        """Recover amounts handwritten near ``donate``/``gift`` phrasing."""

        snippet_patterns = (
            # "I would like to donate (£) 36" — tolerate newline debris
            re.compile(
                r"(?:i\s+)?would\s+like\s+to\s+donate[^\d£]{0,80}?£?\s*"
                r"(\d{1,6}(?:\.\d{1,2})?)",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"(?:i\s+would\s+like\s+to\s+gift(?:\s+aid)?)[^\d£]{0,80}?£?\s*"
                r"(\d{1,6}(?:\.\d{1,2})?)",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"(?:donation|donate(?:\s+amount)?)[^\n£]{0,40}?£\s*"
                r"(\d{1,6}(?:\.\d{1,2})?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:gift(?:\s+amount)?)[^\n£]{0,40}?£\s*"
                r"(\d{1,6}(?:\.\d{1,2})?)",
                re.IGNORECASE,
            ),
        )

        for pattern in snippet_patterns:
            match = pattern.search(raw_text)
            if not match:
                continue
            coerced = OCRExtractor._coerce_amount(match.group(1))
            if coerced is not None:
                return coerced

        keyword_lines = ("donate", "donation", "gift aid", "i enclos", "enclose")

        lines = OCRExtractor._clean_raw_text_lines(raw_text)
        for line in lines:
            lowered = line.lower()
            if "date of birth" in lowered:
                continue
            if any(keyword in lowered for keyword in keyword_lines):
                symbol = re.search(
                    r"£\s*(\d{1,6}(?:\.\d{1,2})?)", line, flags=re.IGNORECASE
                )
                if symbol:
                    coerced = OCRExtractor._coerce_amount(symbol.group(1))
                    if coerced is not None:
                        return coerced
                lone = re.search(r"\b(\d{1,4}(?:\.\d{1,2})?)\s*$", line.strip())
                if lone:
                    coerced = OCRExtractor._coerce_amount(lone.group(1))
                    if (
                        coerced is not None
                        and Decimal(coerced) >= Decimal("1.00")
                        and Decimal(coerced) <= Decimal("99999")
                    ):
                        return coerced

        return ""

    @staticmethod
    def _extract_amount_from_raw_text(raw_text: str) -> str:
        """Return a GBP amount string from raw OCR text when present."""
        lines = OCRExtractor._clean_raw_text_lines(raw_text)

        for line in lines:
            if "date" in line.lower():
                continue
            match = OCRExtractor._CURRENCY_AMOUNT_PATTERN.search(line)
            if not match:
                continue
            coerced = OCRExtractor._coerce_amount(match.group(1))
            if coerced is not None:
                return coerced

        for line in lines:
            if "date" in line.lower() or line.count("-") > 1:
                continue
            match = OCRExtractor._CHEQUE_AMOUNT_PATTERN.search(line)
            if not match:
                continue
            coerced = OCRExtractor._coerce_amount(match.group(1))
            if coerced is not None:
                return coerced

        if re.search(r"\bpay\b", raw_text, re.IGNORECASE) and re.search(
            r"\bpounds?\b", raw_text, re.IGNORECASE
        ):
            for line in lines:
                lowered = line.lower()
                if any(
                    marker in lowered for marker in ("date", "bank", "pay", "pound")
                ):
                    continue
                if "/" in line or "-" in line:
                    continue
                digit_groups = re.findall(r"\d+", line)
                if len(digit_groups) != 1:
                    continue
                if len(re.findall(r"[A-Za-z]+", line)) > 1:
                    continue
                coerced = OCRExtractor._coerce_amount(digit_groups[0])
                if coerced is not None:
                    return coerced
        return ""

    @staticmethod
    def _contact_section(raw_text: str) -> str:
        """Return the likely donor contact-details section from OCR text."""
        start_match = re.search(
            r"step\s*4\s*:\s*your contact details",
            raw_text,
            re.IGNORECASE,
        )
        if not start_match:
            return ""
        start = start_match.end()
        end_match = re.search(r"step\s*5\s*:", raw_text[start:], re.IGNORECASE)
        end = (
            start + end_match.start() if end_match else min(len(raw_text), start + 500)
        )
        return raw_text[start:end]

    @staticmethod
    def _clean_contact_fallback_value(value: str) -> str:
        """Return a cleaned contact fallback candidate."""
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _extract_email_from_contact_section(raw_text: str) -> str:
        """Return a donor email only from the contact-details section."""
        contact_section = OCRExtractor._contact_section(raw_text)
        if not contact_section:
            return ""
        match = EMAIL_PATTERN.search(contact_section)
        if not match:
            return ""
        return match.group(1).lower()

    @staticmethod
    def _extract_phone_from_contact_section(raw_text: str) -> str:
        """Return a donor phone number only from the contact-details section."""
        contact_section = OCRExtractor._contact_section(raw_text)
        if not contact_section:
            return ""
        for raw_match in PHONE_PATTERN.finditer(contact_section):
            candidate = OCRExtractor._clean_contact_fallback_value(raw_match.group(1))
            coerced = OCRExtractor._coerce_phone(candidate)
            if coerced is not None:
                return coerced
        return ""

    @staticmethod
    def _extract_name_and_address_from_raw_text(raw_text: str) -> dict[str, str]:
        """Extract donor name and address fields from a postcode-adjacent text block.

        Args:
            raw_text: Full OCR text from the Document AI response.

        Returns:
            Dict containing any recovered ``donor_name`` and address fields.
        """
        lines = OCRExtractor._clean_raw_text_lines(raw_text)
        candidate_indices = [
            index
            for index, line in enumerate(lines)
            if OCRExtractor._extract_postcode_like_value(line)
        ]
        if not candidate_indices:
            return {}

        for postcode_index in reversed(candidate_indices):
            block_lines: list[str] = []
            for index in range(max(0, postcode_index - 6), postcode_index + 1):
                line = lines[index]
                if OCRExtractor._looks_like_address_noise(line):
                    continue
                block_lines.append(line)

            if not block_lines:
                continue

            donor_name = ""
            for line in block_lines[:-1]:
                candidate = OCRExtractor._normalise_name_line(line)
                if candidate:
                    donor_name = candidate
                    break

            relevant_lines = block_lines
            if donor_name and donor_name in block_lines:
                donor_index = block_lines.index(donor_name)
                relevant_lines = block_lines[donor_index + 1 :]

            first_address_index = next(
                (
                    index
                    for index, line in enumerate(relevant_lines)
                    if OCRExtractor._looks_like_address_line(line)
                ),
                None,
            )
            if first_address_index is None:
                continue

            address_lines = relevant_lines[first_address_index:]
            if not address_lines:
                continue

            postcode_line = address_lines[-1]
            postcode_value = OCRExtractor._extract_postcode_like_value(postcode_line)
            if not postcode_value:
                continue

            pre_postcode_lines = address_lines[:-1]
            has_street_line = any(
                OCRExtractor._looks_like_address_line(line)
                for line in pre_postcode_lines
            )
            if not has_street_line:
                continue

            merged_city_line = ""
            if (
                postcode_value == postcode_line.strip().upper()
                and len(address_lines) >= 2
            ):
                city_candidate = address_lines[-2]
                if not OCRExtractor._looks_like_address_line(city_candidate):
                    merged_city_line = city_candidate
                    address_lines = [
                        *address_lines[:-2],
                        f"{city_candidate} {postcode_line}",
                    ]

            parsed = parse_address_block("\n".join(address_lines))
            parsed_line1 = str(parsed.get("address_line1", "") or "").strip()
            if not parsed_line1 or not OCRExtractor._looks_like_address_line(
                parsed_line1
            ):
                continue

            result: dict[str, str] = {}
            if donor_name:
                result["donor_name"] = donor_name
            if not parsed.get("postcode"):
                parsed["postcode"] = postcode_value
            if merged_city_line and not parsed.get("city"):
                parsed["city"] = merged_city_line.title()
                if str(parsed.get("address_line2", "") or "").strip() == (
                    f"{merged_city_line} {postcode_value}"
                ):
                    parsed["address_line2"] = ""
            for key in ("address_line1", "address_line2", "city", "postcode"):
                value = str(parsed.get(key, "") or "").strip()
                if value:
                    result[key] = value
            return result

        return {}

    @staticmethod
    def _apply_raw_text_fallbacks(result: dict[str, Any], raw_text: str) -> None:
        """Fill extraction gaps from raw OCR text when Form Parser missed fields.

        Applies two fallbacks:

        1. **Postcode** — regex-scans ``raw_text`` when no postcode was written
           by form-field entities, then validates each candidate via
           postcodes.io.  The first valid postcode wins and city/county are
           back-filled from the API response.

        2. **Consent checkboxes** — detects inline ☑/☐ marks adjacent to
           ``Email``, ``Phone``, and ``Text`` labels in the "Keeping in touch"
           section.  Form Parser sees this horizontal row as a single
           unstructured block and never produces separate key-value entities
           for it, so we parse the raw text directly.

        Args:
            result: Extraction result dict (mutated in place).
            raw_text: Full OCR text from the Document AI response.
        """
        # ── 0. Donor identity + address block fallback ───────────────────
        if not result.get("donor_name") or not result.get("address_line1"):
            name_and_address = OCRExtractor._extract_name_and_address_from_raw_text(
                raw_text
            )
            if not result.get("donor_name") and name_and_address.get("donor_name"):
                result["donor_name"] = name_and_address["donor_name"]
                result["donor_name_confidence"] = 0.60
                logger.debug(
                    "Donor name raw-text fallback: %r",
                    name_and_address["donor_name"],
                )

            for field_name in ("address_line1", "address_line2", "city", "postcode"):
                if result.get(field_name) or not name_and_address.get(field_name):
                    continue
                result[field_name] = name_and_address[field_name]
                if f"{field_name}_confidence" in result:
                    result[f"{field_name}_confidence"] = 0.60
                logger.debug(
                    "Address raw-text fallback: %s = %r",
                    field_name,
                    name_and_address[field_name],
                )

        # ── 1. Postcode regex fallback ─────────────────────────────────────
        if not result.get("postcode"):
            best_effort: str | None = (
                None  # first regex candidate; used when API is down
            )
            for raw_pc in POSTCODE_PATTERN.findall(raw_text):
                pc_clean = raw_pc.upper().replace(" ", "")
                postcode = f"{pc_clean[:-3]} {pc_clean[-3:]}"
                if best_effort is None:
                    best_effort = postcode
                enriched = lookup_postcode(postcode)
                if enriched is None:
                    # Network error — try next candidate
                    continue
                if enriched:
                    # Confirmed valid postcode via postcodes.io
                    result["postcode"] = postcode
                    result["postcode_confidence"] = 0.70
                    if not result.get("city") and enriched.get("city"):
                        result["city"] = enriched["city"]
                        logger.debug(
                            "Postcode fallback filled city: %r", enriched["city"]
                        )
                    if not result.get("county") and enriched.get("county"):
                        result["county"] = enriched["county"]
                        logger.debug(
                            "Postcode fallback filled county: %r", enriched["county"]
                        )
                    logger.debug("Postcode raw-text fallback: %r", postcode)
                    break

            # If API was unreachable for every candidate, use the first regex
            # match at reduced confidence (no city/county enrichment possible).
            if not result.get("postcode") and best_effort:
                result["postcode"] = best_effort
                result["postcode_confidence"] = 0.50
                logger.debug("Postcode raw-text fallback (no API): %r", best_effort)

        # ── 2. Inline consent checkbox parsing ────────────────────────────
        # UK donation forms print the consent row as:
        #   "☑ Email  ☐ Phone  ◉ Text"
        # Form Parser cannot produce key-value pairs from horizontal rows
        # like this, so we detect checkbox symbols immediately adjacent to
        # the label words in the "Keeping in touch" section.
        # Prefer "tick accordingly" — it appears immediately before the
        # checkbox row.  Fall back to the broader "Keeping in touch" heading
        # which may be hundreds of chars before the actual checkboxes.
        section_m = re.search(r"tick[\s,]*accordingly", raw_text, re.IGNORECASE)
        if not section_m:
            section_m = re.search(
                r"keeping in touch|we.{0,10}d love to keep",
                raw_text,
                re.IGNORECASE,
            )
        consent_section = (
            raw_text[section_m.start() : section_m.start() + 300]
            if section_m
            else raw_text[-800:]  # last 800 chars if no marker found
        )

        for label_re, field in OCRExtractor._CONSENT_LABEL_PATTERNS:
            if result.get(field) is not None:
                # Already set by a form-field entity — don't overwrite
                continue
            for m in label_re.finditer(consent_section):
                # Only look for a checkbox symbol on the SAME LINE as the label.
                # Splitting on \n prevents mistakenly attributing a symbol from
                # the preceding or following line (e.g. "Phone\n☑ Text").
                prefix_raw = consent_section[max(0, m.start() - 6) : m.start()]
                # Take only the part after the last newline (same line as label)
                same_line_prefix = prefix_raw.split("\n")[-1].strip()
                check_char = same_line_prefix[-1] if same_line_prefix else ""

                if check_char not in (
                    OCRExtractor._CHECKBOX_FILLED | OCRExtractor._CHECKBOX_EMPTY
                ):
                    # Fall back to symbol immediately AFTER the label word,
                    # but only up to the next newline (same line).
                    suffix_raw = consent_section[m.end() : m.end() + 6]
                    same_line_suffix = suffix_raw.split("\n")[0].strip()
                    check_char = same_line_suffix[0] if same_line_suffix else ""

                if check_char in OCRExtractor._CHECKBOX_FILLED:
                    result[field] = True
                    result[f"{field}_confidence"] = 0.65
                    logger.debug("Consent raw-text fallback: %s = True", field)
                    break
                elif check_char in OCRExtractor._CHECKBOX_EMPTY:
                    result[field] = False
                    result[f"{field}_confidence"] = 0.65
                    logger.debug("Consent raw-text fallback: %s = False", field)
                    break

        # ── 3. Email raw-text fallback ─────────────────────────────────────
        # If no form-field entity produced a donor email, scan raw OCR text.
        # Writing an email address on the form implies email consent.
        if not result.get("donation_date"):
            payment_date = OCRExtractor._extract_payment_date_from_raw_text(raw_text)
            if payment_date:
                result["donation_date"] = payment_date
                result["donation_date_confidence"] = 0.60
                logger.debug("Payment date raw-text fallback: %r", payment_date)

        if not result.get("amount"):
            amount = OCRExtractor._extract_amount_from_raw_text(raw_text)
            if not amount:
                amount = OCRExtractor._extract_amount_near_donation_phrases(raw_text)
            if amount:
                result["amount"] = amount
                result["amount_confidence"] = 0.60
                logger.debug("Amount raw-text fallback: %r", amount)

        if not result.get("email"):
            candidate = OCRExtractor._extract_email_from_contact_section(raw_text)
            if candidate:
                result["email"] = candidate
                result["email_confidence"] = 0.65
                logger.debug("Email raw-text fallback: %r", candidate)
                if result.get("email_consent") is None:
                    result["email_consent"] = True
                    result["email_consent_confidence"] = 0.65
                    logger.debug(
                        "Email consent inferred True from raw-text email fallback"
                    )

        # ── 4. Phone raw-text fallback ─────────────────────────────────────
        # If no form-field entity produced a donor phone number, scan raw OCR
        # text for a UK number.  Writing a number implies phone consent.
        if not result.get("phone"):
            coerced = OCRExtractor._extract_phone_from_contact_section(raw_text)
            if coerced:
                result["phone"] = coerced
                result["phone_confidence"] = 0.60
                logger.debug("Phone raw-text fallback: %r", coerced)
                if result.get("phone_consent") is None:
                    result["phone_consent"] = True
                    result["phone_consent_confidence"] = 0.60
                    logger.debug(
                        "Phone consent inferred True from raw-text phone fallback"
                    )

    @staticmethod
    def _synthesise_donor_name_from_parts(result: dict[str, Any]) -> None:
        """Build donor_name from split-name fields when a full name is absent.

        Args:
            result: Extraction result dict (mutated in place).
        """
        if result.get("donor_name"):
            return

        first_name = str(result.get("donor_first_name", "") or "").strip()
        last_name = str(result.get("donor_last_name", "") or "").strip()
        parts = [part for part in (first_name, last_name) if part]
        if not parts:
            return

        result["donor_name"] = " ".join(parts)
        name_part_confidences = [
            float(result.get("donor_first_name_confidence", 0.0) or 0.0),
            float(result.get("donor_last_name_confidence", 0.0) or 0.0),
        ]
        valid_confidences = [
            confidence for confidence in name_part_confidences if confidence > 0
        ]
        if valid_confidences:
            result["donor_name_confidence"] = round(
                sum(valid_confidences) / len(valid_confidences),
                4,
            )

    @staticmethod
    def match_donor(urn: str, campaign: Any) -> dict[str, Any]:
        """Resolve an extracted URN to a Donor record.

        Searches the campaign data file first, then falls back to the house
        file.

        Args:
            urn: Extracted URN string.
            campaign: Campaign model instance.

        Returns:
            Dict with ``donor``, ``data_file_donor``, ``source``,
            ``donor_name``.
        """
        return DM.match_donor(urn, campaign)

    @staticmethod
    def extract_from_document_ai(
        doc_ai_result: Any,
        campaign: Any,
        qr_data: dict[str, str] | None = None,
        known_urns: list[str] | None = None,
        client: Any = None,
    ) -> dict[str, Any]:
        """Extract donation fields from a Document AI Form Parser result.

        Form Parser already returns labelled key-value pairs.  This method:

        1. Starts from a blank result dict.
        2. For cold records (no QR), attempts URN resolution from OCR text.
        3. Maps all Form Parser entities to result fields via
           ``_apply_entity_overrides``.
        4. Injects QR identifiers at confidence 1.0 for warm records.

        Args:
            doc_ai_result: Result from ``DocumentAIService.process_image_bytes``.
            campaign: Campaign model instance.
            qr_data: Parsed QR dict (warm record) or ``None`` (cold record).
            known_urns: Pre-loaded URN list (avoids repeated DB hit).
            client: Optional Client instance for per-charity entity mapping.

        Returns:
            Extracted fields dict.
        """
        result = OCRExtractor._empty_result(doc_ai_result.full_text)

        # For cold records, attempt URN resolution from OCR text against
        # known campaign URNs before entity overrides run.
        if not qr_data:
            urn, urn_conf = DM.extract_urn(
                doc_ai_result.full_text, campaign, known_urns=known_urns
            )
            if urn:
                result["urn"] = urn
                result["urn_confidence"] = urn_conf

        # Map Form Parser key-value entities → result fields
        _client = client or getattr(campaign, "client", None)
        OCRExtractor._apply_entity_overrides(
            result, doc_ai_result.entities, client=_client
        )

        # Fill remaining gaps from raw OCR text (postcode regex + inline
        # consent checkboxes that Form Parser cannot produce as key-value pairs)
        OCRExtractor._apply_raw_text_fallbacks(result, doc_ai_result.full_text)
        OCRExtractor._synthesise_donor_name_from_parts(result)

        # Keep gift_aid_consent in sync with gift_aid
        result["gift_aid_consent"] = result["gift_aid"]
        result["gift_aid_consent_confidence"] = result["gift_aid_confidence"]

        # QR warm record: override identifiers at maximum confidence
        if qr_data:
            result["appeal_code"] = qr_data["appeal_code"]
            result["package_code"] = qr_data.get("package_code", "")
            result["urn"] = qr_data["urn"]
            result["urn_confidence"] = 1.0

        return result
