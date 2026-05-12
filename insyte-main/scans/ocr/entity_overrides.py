"""Document AI entity override/coercion mixin for OCR extraction.

Isolates Document AI entity-mapping and coercion logic so the main extractor
class stays focused on orchestration.
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from core.services.field_normalizer import FieldNormalizer
from scans.ocr.field_extractors import address_looks_noisy, parse_address_block
from scans.ocr.patterns import EMAIL_PATTERN
from scans.ocr.postcode_lookup import lookup_postcode

logger = logging.getLogger(__name__)


class OCREntityOverridesMixin:
    """Mixin containing Document AI entity override/coercion helpers."""

    _GIFT_AID_TRUE = frozenset(
        {"yes", "true", "1", "y", "x", "tick", "ticked", "checked", "☑", "✔", "✗", "☒"}
    )
    _BOOL_TRUE = frozenset({"yes", "true", "1"})
    _BOOL_FALSE = frozenset({"no", "false", "0"})
    _CONSENT_FIELDS = frozenset(
        {
            "contact_consent",
            "email_consent",
            "sms_consent",
            "phone_consent",
            "post_consent",
        }
    )

    @classmethod
    def _contact_section_text(cls, result: dict[str, Any]) -> str:
        """Return the Step 4 contact-details slice from stored raw OCR text."""
        raw_text = str(result.get("raw_text", "") or "")
        contact_section = getattr(cls, "_contact_section", None)
        if not raw_text or not callable(contact_section):
            return ""
        return str(contact_section(raw_text) or "")

    @classmethod
    def _contact_section_contains_email(
        cls,
        result: dict[str, Any],
        email: str,
    ) -> bool:
        """Return whether an email appears inside the donor contact section."""
        raw_text = str(result.get("raw_text", "") or "")
        if not raw_text:
            return True
        contact_section = cls._contact_section_text(result).lower()
        return bool(contact_section and email.lower() in contact_section)

    @classmethod
    def _contact_section_contains_phone(
        cls,
        result: dict[str, Any],
        phone: str,
    ) -> bool:
        """Return whether a phone number appears inside the donor contact section."""
        raw_text = str(result.get("raw_text", "") or "")
        if not raw_text:
            return True
        contact_section = cls._contact_section_text(result)
        contact_digits = re.sub(r"\D+", "", contact_section)
        phone_digits = re.sub(r"\D+", "", phone)
        return bool(contact_digits and phone_digits and phone_digits in contact_digits)

    @classmethod
    def _apply_entity_overrides(
        cls,
        result: dict[str, Any],
        entities: list[Any],
        client: Any = None,
    ) -> None:
        """Apply Document AI entity values to the extraction result dict.

        Args:
            result: Extraction result dict (mutated in place).
            entities: Document AI entities.
            client: Optional Client instance for per-charity mapping overrides.
        """
        field_map = FieldNormalizer.build_mapping(client)

        _nlp_contact_block: frozenset[str] = frozenset({"phone", "email"})
        _nlp_address_skip_subfields: frozenset[str] = frozenset({"city", "postcode"})

        for entity in entities:
            field_name = FieldNormalizer.resolve_field(field_map, entity.type_)
            if field_name is None:
                continue

            if (
                field_name in _nlp_contact_block
                and getattr(entity, "source", "nlp") == "nlp"
            ):
                continue

            if field_name == "address_block":
                raw_addr = entity.normalised_value or entity.mention_text
                if raw_addr:
                    parsed = parse_address_block(raw_addr)
                    addr_conf = round(entity.confidence, 4)
                    is_nlp = getattr(entity, "source", "nlp") == "nlp"
                    if is_nlp and addr_conf < 0.40:
                        logger.debug(
                            "Skipping low-confidence NLP address entity: %r (conf %.2f)",
                            raw_addr,
                            addr_conf,
                        )
                        continue
                    if not is_nlp and address_looks_noisy(parsed):
                        logger.debug(
                            "Skipping noisy form_field address entity: %r",
                            raw_addr,
                        )
                        continue
                    for sub_key, sub_val in parsed.items():
                        if is_nlp and sub_key in _nlp_address_skip_subfields:
                            continue
                        existing = float(result.get(f"{sub_key}_confidence", 0.0))
                        if addr_conf > existing and sub_val:
                            result[sub_key] = sub_val
                            if f"{sub_key}_confidence" in result:
                                result[f"{sub_key}_confidence"] = addr_conf
                            logger.debug(
                                "Address entity (%s): %s = %r (conf %.2f)",
                                "form_field" if not is_nlp else "nlp",
                                sub_key,
                                sub_val,
                                addr_conf,
                            )

                    # Validate postcode via postcodes.io and enrich city/county.
                    # lookup_postcode returns:
                    #   populated dict  → valid postcode, enrich city/county
                    #   {}              → confirmed 404, OCR misread; clear it
                    #   None            → network error; leave postcode unchanged
                    postcode_in_result = result.get("postcode", "")
                    if postcode_in_result:
                        enriched = lookup_postcode(postcode_in_result)
                        if enriched is not None and not enriched:
                            # Confirmed invalid (HTTP 404) — OCR misread the postcode
                            logger.info(
                                "Invalid postcode %r from OCR — clearing",
                                postcode_in_result,
                            )
                            result["postcode"] = ""
                            result["postcode_confidence"] = 0.0
                        elif enriched:
                            # Valid — fill city/county only if not already populated
                            if not result.get("city") and enriched.get("city"):
                                result["city"] = enriched["city"]
                                logger.debug(
                                    "Postcode lookup filled city: %r", enriched["city"]
                                )
                            if not result.get("county") and enriched.get("county"):
                                result["county"] = enriched["county"]
                                logger.debug(
                                    "Postcode lookup filled county: %r",
                                    enriched["county"],
                                )
                continue

            raw_value: str = entity.normalised_value or entity.mention_text
            if not raw_value:
                continue

            entity_confidence: float = entity.confidence
            existing_confidence: float = float(
                result.get(f"{field_name}_confidence", 0.0)
            )

            # ── Consent-labelled fields that may contain real contact data ──
            # When a form field is labelled just "Email" or "Phone", the label
            # is mapped to email_consent / phone_consent.  BUT if the donor
            # actually wrote their email address or phone number in that field,
            # Document AI will return the written value (not a checkbox symbol).
            # Detect this by attempting real-value coercion first; if it
            # succeeds, store it as the contact field AND infer consent = True.
            if field_name == "email_consent":
                coerced_email = cls._coerce_email(raw_value)
                if coerced_email is not None:
                    if not cls._contact_section_contains_email(result, coerced_email):
                        logger.debug(
                            "Skipping consent-labelled email outside contact section: %r",
                            coerced_email,
                        )
                        continue
                    # Donor wrote their email address — capture it and infer consent
                    existing_email_conf = float(result.get("email_confidence", 0.0))
                    if entity_confidence > existing_email_conf:
                        result["email"] = coerced_email
                        result["email_confidence"] = round(entity_confidence, 4)
                        logger.debug(
                            "Email extracted from consent-labelled field: %r (conf %.2f)",
                            coerced_email,
                            entity_confidence,
                        )
                    if result.get("email_consent") is None:
                        result["email_consent"] = True
                        result["email_consent_confidence"] = round(entity_confidence, 4)
                        logger.debug(
                            "Email consent inferred True from written email address"
                        )
                    continue

            if field_name == "phone_consent":
                coerced_phone = cls._coerce_phone(raw_value)
                if coerced_phone is not None:
                    if not cls._contact_section_contains_phone(result, coerced_phone):
                        logger.debug(
                            "Skipping consent-labelled phone outside contact section: %r",
                            coerced_phone,
                        )
                        continue
                    # Donor wrote their phone number — capture it and infer consent
                    existing_phone_conf = float(result.get("phone_confidence", 0.0))
                    if entity_confidence > existing_phone_conf:
                        result["phone"] = coerced_phone
                        result["phone_confidence"] = round(entity_confidence, 4)
                        logger.debug(
                            "Phone extracted from consent-labelled field: %r (conf %.2f)",
                            coerced_phone,
                            entity_confidence,
                        )
                    if result.get("phone_consent") is None:
                        result["phone_consent"] = True
                        result["phone_consent_confidence"] = round(entity_confidence, 4)
                        logger.debug(
                            "Phone consent inferred True from written phone number"
                        )
                    continue

            entity_label_raw = str(getattr(entity, "type_", "") or "")
            parsed_value = cls._coerce_entity_value(
                field_name, raw_value, entity_label=entity_label_raw
            )
            if parsed_value is None:
                continue

            if field_name == "email" and not cls._contact_section_contains_email(
                result, str(parsed_value)
            ):
                logger.debug(
                    "Skipping email entity outside contact section: %r",
                    parsed_value,
                )
                continue

            if field_name == "phone" and not cls._contact_section_contains_phone(
                result, str(parsed_value)
            ):
                logger.debug(
                    "Skipping phone entity outside contact section: %r",
                    parsed_value,
                )
                continue

            if field_name == "amount":
                try:
                    parsed_dec = Decimal(str(parsed_value))
                    existing_raw = result.get("amount", "")
                    existing_dec = (
                        Decimal(str(existing_raw)) if existing_raw else Decimal("0")
                    )
                    if (
                        getattr(entity, "source", "nlp") == "form_field"
                        and parsed_dec >= Decimal("10")
                        and existing_dec < Decimal("10")
                    ):
                        entity_confidence = max(
                            entity_confidence, existing_confidence + 0.0001
                        )
                except InvalidOperation:
                    pass

            if entity_confidence <= existing_confidence:
                continue

            result[field_name] = parsed_value
            if f"{field_name}_confidence" in result:
                result[f"{field_name}_confidence"] = round(entity_confidence, 4)

            logger.debug(
                "Entity override: %s = %r (conf %.2f)",
                field_name,
                parsed_value,
                entity_confidence,
            )

    @classmethod
    def _coerce_gift_aid(cls, raw: str) -> bool | None:
        """Coerce a raw string to a gift aid boolean.

        Args:
            raw: Lower-cased raw string from the Document AI entity.

        Returns:
            True, False, or None if indeterminate.
        """
        if raw in cls._GIFT_AID_TRUE:
            return True
        if raw in cls._BOOL_FALSE:
            return False
        return None

    @classmethod
    def _coerce_amount(cls, raw: str) -> str | None:
        """Coerce a raw string to a validated GBP amount string.

        Args:
            raw: Raw amount string (may include £ prefix or commas).

        Returns:
            Decimal string (e.g. ``"25.00"``) or ``None`` if invalid.
        """
        clean = raw.replace(",", "").strip()
        clean = re.sub(r"(?<=\d)(?:-|\u2013)(?=\d{2}\b)", ".", clean)
        amount_match = re.search(r"£?\s*([\d]+(?:\.\d{1,2})?)", clean)
        clean = amount_match.group(1) if amount_match else clean.lstrip("£").strip()
        try:
            val = Decimal(clean)
            if Decimal("0.01") <= val <= Decimal("999999.99"):
                return str(val)
        except InvalidOperation:
            pass
        return None

    @classmethod
    def _coerce_phone(cls, raw: str) -> str | None:
        """Coerce a raw string into a UK-friendly phone value.

        Args:
            raw: Raw phone candidate.

        Returns:
            Sanitised phone string or ``None``.
        """
        compact = re.sub(r"[^\d+]", "", raw)
        if compact.startswith("+44") and len(re.sub(r"\D", "", compact)) >= 12:
            return raw.strip()
        digits_only = re.sub(r"\D", "", raw)
        if 10 <= len(digits_only) <= 12 and digits_only.startswith("0"):
            return raw.strip()
        return None

    @classmethod
    def _coerce_email(cls, raw: str) -> str | None:
        """Coerce a raw string into an email value.

        Args:
            raw: Raw email candidate.

        Returns:
            Lowercased email or ``None``.
        """
        match = EMAIL_PATTERN.search(raw)
        if match:
            return match.group(1).lower()
        return None

    @classmethod
    def _coerce_payment_method_value(cls, raw: str, *, entity_label: str) -> str | None:
        """Map a checkbox / typed payment-option field to canonical payment_method.

        Returns ``None`` when the OCR value is ambiguous or negative so the
        scan batch ``payment_method`` remains authoritative — Document AI labels
        like "Please debit my Visa/Mastercard" often encode *only* yes/no ticks.
        """
        text = raw.strip()
        lowered = text.lower()

        gift_true = getattr(cls, "_GIFT_AID_TRUE", frozenset())
        bool_false = getattr(cls, "_BOOL_FALSE", frozenset())

        norm_label = FieldNormalizer._normalise_key(entity_label)

        def _looks_like_checkbox_no() -> bool:
            return lowered in bool_false | {"unchecked", "off", "☐"}

        if _looks_like_checkbox_no():
            return None

        # Explicit method words in the *value* (not just "yes").
        synonyms: tuple[tuple[str, str], ...] = (
            ("postal_order", "postal_order"),
            ("postal order", "postal_order"),
            ("credit card", "card"),
            ("debit card", "card"),
            ("visa", "card"),
            ("mastercard", "card"),
            ("american express", "card"),
            ("amex", "card"),
            ("direct debit", "direct_debit"),
            ("caf", "caf"),
            ("cheque", "cheque"),
            ("cash", "cash"),
        )
        hay = lowered.replace("\n", " ")
        for needle, method in synonyms:
            if needle in hay:
                return method

        tick_true = bool(lowered in gift_true or lowered in {"selected", "ticked"})

        debit_card_hint = ("visa" in norm_label and "mastercard" in norm_label) or (
            "debit" in norm_label and "visa" in norm_label
        )
        debit_card_hint = debit_card_hint or (
            ("please_debit" in norm_label or "debit_my" in norm_label)
            and any(t in norm_label for t in ("visa", "mastercard"))
        )

        if tick_true:
            if debit_card_hint:
                return "card"
            if "postal_order" in norm_label or (
                "postal" in norm_label and "order" in norm_label
            ):
                return "postal_order"
            if "cheque" in norm_label or "enclose" in norm_label:
                return "cheque"
            # Tick with no discriminative label — preserve batch inference.
            return None

        return None

    @classmethod
    def _coerce_entity_value(
        cls, field_name: str, raw: str, *, entity_label: str = ""
    ) -> Any:
        """Coerce a raw entity string to the correct Python type for a field.

        Args:
            field_name: Internal result-dict field name (e.g. ``"gift_aid"``).
            raw: Raw string value from the Document AI entity.

        Returns:
            Coerced value appropriate for the field, or ``None`` if the value
            cannot be parsed.
        """
        raw = raw.strip()

        if field_name == "payment_method":
            return cls._coerce_payment_method_value(raw, entity_label=entity_label)

        if field_name == "gift_aid":
            return cls._coerce_gift_aid(raw.lower())

        if field_name == "amount":
            return cls._coerce_amount(raw)

        if field_name == "phone":
            return cls._coerce_phone(raw)

        if field_name == "email":
            return cls._coerce_email(raw)

        if field_name == "card_last_four":
            # Form Parser may return "**** **** **** 1234" or "XXXX 1234" — extract last 4
            digits = re.sub(r"\D", "", raw)
            return digits[-4:] if len(digits) >= 4 else (digits if digits else None)

        if field_name in cls._CONSENT_FIELDS:
            lower = raw.lower()
            if lower in cls._BOOL_TRUE:
                return True
            if lower in cls._BOOL_FALSE:
                return False
            return None

        return raw if raw else None
