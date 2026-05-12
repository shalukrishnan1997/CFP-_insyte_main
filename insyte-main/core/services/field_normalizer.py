"""Field normalizer for multi-charity form label resolution.

This module provides :class:`FieldNormalizer`, which builds the label-to-field
mapping used during Document AI entity extraction.  The design separates two
distinct concerns:

**Universal NLP types** (always detected automatically by Document AI)
    ``person``, ``address``, ``phone``, ``email`` — these are semantic entity
    types returned by the Form Parser's NLP pipeline regardless of the form
    layout or which charity printed it.  They require *no* per-charity config.

**Charity-specific form labels** (stored per-client in the database)
    Labels like ``"i am a uk taxpayer"`` or ``"i would like to purchase £"``
    vary between charities.  They are stored as a ``form_field_mapping`` JSON
    field on the :class:`~core.models.Client` record and can be managed by
    staff in the Django admin without any code change or deployment.

The resolved map is produced by merging these two layers:

.. code-block:: text

    _UNIVERSAL_NLP_MAP  (hardcoded — never charity-specific)
            +
    client.form_field_mapping  (per-charity DB config — wins on conflict)
            =
    field_map  (used by OCRExtractor._apply_entity_overrides)

Onboarding a new charity with a different form layout is therefore a pure admin
task: open the Client record → paste the JSON mapping → save.  No code change,
no deployment.

JSON format for ``Client.form_field_mapping``::

    {
        "full name":                  "donor_name",
        "supporter":                  "donor_name",
        "i would like to purchase £": "amount",
        "donation amount":            "amount",
        "i am a uk taxpayer":         "gift_aid",
        "tick if you pay uk tax":     "gift_aid",
        "post code":                  "postcode",
        "email us":                   "email_consent",
        "telephone":                  "phone",
        "text":                       "sms_consent"
    }

Keys are normalised to lowercase + underscores at build time so they match the
normalised entity type keys produced by Document AI.

Canonical field names (values in the mapping)
----------------------------------------------
donor_name, donor_first_name, donor_last_name, title,
amount, gift_aid, donation_date, processed_payment_date (bank/reconciliation only),
payment_method, urn,
postcode, county, city, address_line1, address_line2, address_block,
phone, email,
contact_consent, email_consent, sms_consent, phone_consent, post_consent,
cheque_number, cheque_date, sort_code, account_number,
card_last_four, card_expiry_date, card_holder_name,
caf_voucher_number, caf_amount, postal_order_number
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_IGNORED_LABEL_TOKENS: frozenset[str] = frozenset(
    {
        "your",
        "please",
        "optional",
        "option",
    }
)

# ---------------------------------------------------------------------------
# Universal NLP entity types
# ---------------------------------------------------------------------------
# These are emitted by Google Document AI's NLP pipeline regardless of the
# form's layout.  They are charity-agnostic and must never be removed.
# Any charity-specific label overrides are applied on top of this base.
_UNIVERSAL_NLP_MAP: dict[str, str] = {
    # ── Document AI NLP structural entity types ──────────────────────────────
    # person + address are reliably tied to the form-fill region and work
    # across all charities automatically.
    "person": "donor_name",
    # address → special sentinel; _apply_entity_overrides calls
    # _parse_address_block to fan it out into address_line1/city/postcode.
    "address": "address_block",
    #
    # NOTE: "phone" and "email" NLP entity types are intentionally absent.
    # NLP detection is document-wide — it finds the charity's own printed
    # helpline/email rather than the donor's written data.
    # However, form-field LABELS like "Telephone:" or "Email address:" are
    # safe because they identify a specific labeled input box on the form.
    # These are included below and filtered by entity.source in
    # _apply_entity_overrides (source="form_field" allowed, source="nlp" blocked).
    #
    # ── Common phone field label variants (form-field labels only) ────────────
    "telephone": "phone",
    "tel": "phone",
    "mobile": "phone",
    "mobile_number": "phone",
    "phone_number": "phone",
    "your_telephone": "phone",
    "your_mobile": "phone",
    "your_phone_number": "phone",
    "daytime_telephone": "phone",
    "contact_number": "phone",
    # ── Common email field label variants ────────────────────────────────────
    # On UK donation forms "Email" alone is nearly always a consent tick box,
    # NOT a text input.  Only multi-word labels like "Email address:" indicate
    # an actual address field.  This prevents checkbox symbols being stored
    # as the donor's email address.
    "email_address": "email",
    "your_email": "email",
    "your_email_address": "email",
    "e_mail": "email",
    "e_mail_address": "email",
    # Stand-alone "email" label → consent tick box
    "email": "email_consent",
    # Stand-alone "phone" label → phone consent tick box on UK donation forms.
    # Forms that use "Phone" as a text-input label should override this via
    # Client.form_field_mapping (e.g. map "phone" → "phone" in the DB).
    "phone": "phone_consent",
    # ── SMS / Text consent ────────────────────────────────────────────────────
    # "Text" is the near-universal UK donation-form label for the SMS/text
    # consent tick box (Keeping in touch section).
    "text": "sms_consent",
    #
    # ── Amount ───────────────────────────────────────────────────────────────
    "donation_amount": "amount",
    "amount": "amount",
    "gift_amount": "amount",
    "donation": "amount",
    "amount_donated": "amount",
    "total_amount": "amount",
    "total_donation": "amount",
    "enclosed_amount": "amount",
    "i_enclose": "amount",
    "i_would_like_to_donate": "amount",
    "i_would_like_to_purchase": "amount",
    # ── Gift Aid ─────────────────────────────────────────────────────────────
    "gift_aid": "gift_aid",
    "gift_aid_declaration": "gift_aid",
    "giftaid": "gift_aid",
    "gift_aid_tick": "gift_aid",
    "i_am_a_uk_taxpayer": "gift_aid",
    "uk_taxpayer": "gift_aid",
    # ── Donor name ───────────────────────────────────────────────────────────
    "donor_name": "donor_name",
    "name": "donor_name",
    "full_name": "donor_name",
    "your_name": "donor_name",
    "name_of_donor": "donor_name",
    "supporter": "donor_name",
    "supporter_name": "donor_name",
    "donor": "donor_name",
    # Split-name layouts are common on more structured donation forms.
    "first_name": "donor_first_name",
    "firstname": "donor_first_name",
    "forename": "donor_first_name",
    "given_name": "donor_first_name",
    "christian_name": "donor_first_name",
    "last_name": "donor_last_name",
    "lastname": "donor_last_name",
    "surname": "donor_last_name",
    "family_name": "donor_last_name",
    # ── Title / salutation ────────────────────────────────────────────────────
    "title": "title",
    "salutation": "title",
    "prefix": "title",
    "mr_mrs_ms": "title",
    "mr_mrs_miss": "title",
    "mr_mrs_miss_ms": "title",
    "mr_mrs_ms_dr": "title",
    "mr_mrs_miss_ms_dr": "title",
    "mr_mrs_miss_ms_dr_rev": "title",
    # Some forms use "Initial" as the title/prefix field
    "initial": "title",
    # ── URN / reference number ────────────────────────────────────────────────
    "urn": "urn",
    "supporter_id": "urn",
    "reference": "urn",
    "reference_number": "urn",
    "ref": "urn",
    "donor_ref": "urn",
    "form_number": "urn",
    "our_reference": "urn",
    # ── Address ──────────────────────────────────────────────────────────────
    "address_line_1": "address_line1",
    "address_line1": "address_line1",
    "address_1": "address_line1",
    "address1": "address_line1",
    "street_address": "address_line1",
    "address_line_2": "address_line2",
    "address_line2": "address_line2",
    "address_2": "address_line2",
    "address2": "address_line2",
    "town": "city",
    "town_city": "city",
    "city_town": "city",
    "locality": "city",
    "postcode": "postcode",
    "post_code": "postcode",
    "zip_postcode": "postcode",
    "county": "county",
    "county_region": "county",
    # ── Date (donation) ───────────────────────────────────────────────────────
    "donation_date": "donation_date",
    "date": "donation_date",
    # NOTE: Some labels are intentionally narrow — banks stamp "processed"/received
    # dates that must not impersonate supporter donation dates.
    "signature_date": "donation_date",
    "gift_aid_date": "donation_date",
    # ── Bank / reconciliation dates (distinct from supporter donation intent) ─
    # "payment_date" is widely used by banks for stamping — must not overwrite the
    # supporter-facing donation_date. Stored separately for QA transparency only.
    "payment_date": "processed_payment_date",
    "processed_date": "processed_payment_date",
    "process_date": "processed_payment_date",
    "date_processed": "processed_payment_date",
    "bank_date": "processed_payment_date",
    "date_received_by_bank": "processed_payment_date",
    "date_received_by_us": "processed_payment_date",
    # ── Payment method ───────────────────────────────────────────────────────
    "payment_method": "payment_method",
    "payment_type": "payment_method",
    "payment": "payment_method",
    "method_of_payment": "payment_method",
    "how_would_you_like_to_pay": "payment_method",
    "please_tick_payment_method": "payment_method",
    # Typical multi-option UK appeal slips (checkbox captions)
    "please_debit_my_visa_mastercard": "payment_method",
    "please_debit_my_visa_mc": "payment_method",
    "i_enclose_a_cheque_postal_order": "payment_method",
    "enclose_a_cheque_postal_order": "payment_method",
    # ── Cheque / postal order ─────────────────────────────────────────────────
    "cheque_number": "cheque_number",
    "cheque_no": "cheque_number",
    "cheque_no_": "cheque_number",
    "check_number": "cheque_number",
    "cheque_postal_order": "cheque_number",
    "cheque_postal_order_number": "cheque_number",
    "cheque_p_o_no": "cheque_number",
    "cheque_or_po_number": "cheque_number",
    "cheque_date": "cheque_date",
    "date_of_cheque": "cheque_date",
    "cheque_dated": "cheque_date",
    # ── Bank / Direct Debit ───────────────────────────────────────────────────
    "sort_code": "sort_code",
    "account_number": "account_number",
    "bank_account_number": "account_number",
    # ── Card ──────────────────────────────────────────────────────────────────
    "card_last_four": "card_last_four",
    "card_last_4": "card_last_four",
    "last_4_digits": "card_last_four",
    "last_four_digits": "card_last_four",
    "card_last_4_digits": "card_last_four",
    # Form Parser often labels the masked card number field as "card_number"
    "card_number": "card_last_four",
    "credit_card_number_last_4_digits": "card_last_four",
    "card_expiry": "card_expiry_date",
    "card_expiry_date": "card_expiry_date",
    "expiry_date": "card_expiry_date",
    "expiry_mm_yyyy": "card_expiry_date",
    "expiry_mm_yy": "card_expiry_date",
    "expiry_date_mm_yyyy": "card_expiry_date",
    "expiry": "card_expiry_date",
    "exp_date": "card_expiry_date",
    "valid_until": "card_expiry_date",
    "valid_thru": "card_expiry_date",
    "card_holder": "card_holder_name",
    "card_holder_name": "card_holder_name",
    "cardholder_name": "card_holder_name",
    "name_on_card": "card_holder_name",
    "card_name": "card_holder_name",
    # ── CAF voucher ───────────────────────────────────────────────────────────
    "caf_voucher_number": "caf_voucher_number",
    "caf_voucher": "caf_voucher_number",
    "caf_voucher_no": "caf_voucher_number",
    "caf_cheque_number": "caf_voucher_number",
    "caf_amount": "caf_amount",
    "caf_voucher_amount": "caf_amount",
    # ── Postal order ──────────────────────────────────────────────────────────
    "postal_order_number": "postal_order_number",
    "postal_order_no": "postal_order_number",
    "postal_order": "postal_order_number",
    "po_number": "postal_order_number",
    "p_o_number": "postal_order_number",
    "postal_order_date": "postal_order_date",
    "date_of_postal_order": "postal_order_date",
    # ── Consent ───────────────────────────────────────────────────────────────
    "contact_consent": "contact_consent",
    "email_consent": "email_consent",
    "sms_consent": "sms_consent",
    "phone_consent": "phone_consent",
    "post_consent": "post_consent",
}


class FieldNormalizer:
    """Builds the entity-type → canonical-field mapping for a given charity.

    Usage::

        from core.services.field_normalizer import FieldNormalizer

        field_map = FieldNormalizer.build_mapping(client)
        canonical_field = field_map.get(entity_type_key)
    """

    @staticmethod
    def _normalise_key(key: str) -> str:
        """Normalise a label key: lowercase, replace spaces/hyphens with underscores.

        Args:
            key: Raw label text (e.g. ``"I am a UK taxpayer"``).

        Returns:
            Normalised key (e.g. ``"i_am_a_uk_taxpayer"``).
        """
        lowered = key.lower().strip()
        collapsed = re.sub(r"[^\w]+", "_", lowered)
        return re.sub(r"_+", "_", collapsed).strip("_")

    @staticmethod
    def _label_aliases(normalised_key: str) -> set[str]:
        """Return safe alias variants for a normalized field label.

        Args:
            normalised_key: Normalized label key.

        Returns:
            Set of alias keys that should resolve to the same canonical field.
        """
        aliases = {normalised_key}
        tokens = [token for token in normalised_key.split("_") if token]
        filtered_tokens = [
            token for token in tokens if token not in _IGNORED_LABEL_TOKENS
        ]
        if filtered_tokens and filtered_tokens != tokens:
            aliases.add("_".join(filtered_tokens))
        return aliases

    @staticmethod
    def _register_mapping_entry(
        merged: dict[str, str],
        raw_key: str,
        canonical_field: str,
    ) -> bool:
        """Register a normalized key and its safe aliases.

        Args:
            merged: Mapping updated in place.
            raw_key: Raw label or normalized key.
            canonical_field: Canonical extraction field.

        Returns:
            ``True`` when at least one key was registered.
        """
        normalised_key = FieldNormalizer._normalise_key(raw_key)
        if not normalised_key:
            return False

        for alias_key in FieldNormalizer._label_aliases(normalised_key):
            merged[alias_key] = canonical_field
        return True

    @staticmethod
    def resolve_field(
        field_map: dict[str, str],
        raw_key: str,
    ) -> str | None:
        """Resolve a raw OCR label to a canonical field name.

        Args:
            field_map: Normalized field map.
            raw_key: Raw or normalized OCR label.

        Returns:
            Canonical field name when a direct or alias match exists.
        """
        normalised_key = FieldNormalizer._normalise_key(raw_key)
        for alias_key in FieldNormalizer._label_aliases(normalised_key):
            canonical_field = field_map.get(alias_key)
            if canonical_field is not None:
                return canonical_field
        return None

    @staticmethod
    def find_unmapped_labels(
        entities: list[Any],
        client: Any | None,
    ) -> list[str]:
        """Return distinct form-field labels that do not map to canonical fields.

        Args:
            entities: OCR entities from Document AI.
            client: Optional client instance for per-charity mapping resolution.

        Returns:
            Sorted distinct list of unmapped form-field labels.
        """
        field_map = FieldNormalizer.build_mapping(client)
        unmapped_labels: set[str] = set()
        for entity in entities:
            if getattr(entity, "source", "nlp") != "form_field":
                continue
            raw_label = str(getattr(entity, "type_", "") or "").strip()
            if not raw_label:
                continue
            if FieldNormalizer.resolve_field(field_map, raw_label) is not None:
                continue
            unmapped_labels.add(raw_label)
        return sorted(unmapped_labels)

    @staticmethod
    def _merge_client_mapping(
        merged: dict[str, str],
        raw_mapping: dict[str, Any],
    ) -> int:
        """Merge client mapping entries into the resolved field map.

        Args:
            merged: Base mapping to update in place.
            raw_mapping: Raw mapping from the Client model.

        Returns:
            Number of valid client entries merged.
        """
        client_entries = 0
        for raw_key, canonical_field in raw_mapping.items():
            if not isinstance(canonical_field, str):
                continue
            if FieldNormalizer._register_mapping_entry(
                merged,
                raw_key,
                canonical_field,
            ):
                client_entries += 1
        return client_entries

    @staticmethod
    def build_mapping(client: Any | None) -> dict[str, str]:
        """Build the merged label → canonical field mapping.

        Starts with the universal NLP base, then overlays the per-client DB
        configuration.  Client-specific entries take precedence.

        Args:
            client: Optional :class:`~core.models.Client` instance.  When
                ``None`` the universal map is returned unchanged.

        Returns:
            Dict mapping normalised entity-type keys to canonical field names.
        """
        merged: dict[str, str] = {}
        for raw_key, canonical_field in _UNIVERSAL_NLP_MAP.items():
            FieldNormalizer._register_mapping_entry(merged, raw_key, canonical_field)

        if client is None:
            return merged

        raw_mapping: Any = getattr(client, "form_field_mapping", None) or {}
        if not isinstance(raw_mapping, dict):
            logger.warning(
                "Client '%s' has invalid form_field_mapping (expected dict, got %s). "
                "Falling back to universal map.",
                getattr(client, "name", client),
                type(raw_mapping).__name__,
            )
            return merged

        client_entries = FieldNormalizer._merge_client_mapping(merged, raw_mapping)

        if client_entries:
            logger.debug(
                "FieldNormalizer: loaded %d client-specific labels for '%s'.",
                client_entries,
                getattr(client, "name", client),
            )

        return merged
