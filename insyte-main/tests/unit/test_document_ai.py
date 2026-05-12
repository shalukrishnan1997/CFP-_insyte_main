"""Unit tests for DocumentAIService, OCRExtractor (Document AI path), and _run_document_ai.

Covers:
- DocumentAIService.is_configured / _get_processor_name / process_image_bytes
- OCRExtractor._coerce_gift_aid / _coerce_amount / _coerce_entity_value
- OCRExtractor._apply_entity_overrides
- OCRExtractor.extract_from_document_ai
- ScanProcessingService._run_document_ai

Google Cloud SDK calls are fully mocked via sys.modules patching so no real
credentials or protobuf-compatible Python runtime is required.
"""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import django
import pytest

django.setup()

from scans.document_ai import (  # noqa: E402
    DocumentAIEntity,
    DocumentAIResult,
    DocumentAIService,
)
from scans.ocr import OCRExtractor  # noqa: E402
from tests.factories import CampaignFactory  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _entity(
    type_: str,
    text: str,
    confidence: float = 0.90,
    norm: str = "",
    source: str = "nlp",
) -> DocumentAIEntity:
    """Build a DocumentAIEntity for test setup."""
    return DocumentAIEntity(
        type_=type_,
        mention_text=text,
        confidence=confidence,
        normalised_value=norm,
        source=source,
    )


def _doc_ai_result(
    full_text: str = "",
    entities: list[DocumentAIEntity] | None = None,
    confidence: float = 0.90,
) -> DocumentAIResult:
    """Build a DocumentAIResult for test setup."""
    return DocumentAIResult(
        full_text=full_text,
        entities=entities or [],
        confidence=confidence,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DocumentAIService — is_configured
# ─────────────────────────────────────────────────────────────────────────────


class TestDocumentAIServiceIsConfigured:
    """Tests for DocumentAIService.is_configured."""

    def _client(self, processor_id: str = "abc123") -> Any:
        c = SimpleNamespace()
        c.document_ai_processor_id = processor_id
        return c

    @patch("scans.document_ai.settings")
    def test_configured_when_project_and_processor_set(
        self, mock_settings: Any
    ) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        assert DocumentAIService.is_configured(self._client("abc123")) is True

    @patch("scans.document_ai.settings")
    def test_not_configured_when_no_project_id(self, mock_settings: Any) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = ""
        assert DocumentAIService.is_configured(self._client("abc123")) is False

    @patch("scans.document_ai.settings")
    def test_not_configured_when_no_processor_id(self, mock_settings: Any) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        assert DocumentAIService.is_configured(self._client("")) is False

    @patch("scans.document_ai.settings")
    def test_not_configured_when_both_empty(self, mock_settings: Any) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = ""
        assert DocumentAIService.is_configured(self._client("")) is False


# ─────────────────────────────────────────────────────────────────────────────
# DocumentAIService — _get_processor_name
# ─────────────────────────────────────────────────────────────────────────────


class TestDocumentAIServiceGetProcessorName:
    """Tests for DocumentAIService._get_processor_name."""

    def _client(self, processor_id: str, location: str = "eu") -> Any:
        c = SimpleNamespace()
        c.document_ai_processor_id = processor_id
        c.document_ai_location = location
        return c

    @patch("scans.document_ai.settings")
    def test_bare_id_builds_full_path(self, mock_settings: Any) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        client = self._client("abc123def456", "eu")
        name = DocumentAIService._get_processor_name(client)
        assert name == "projects/my-project/locations/eu/processors/abc123def456"

    def test_full_resource_name_returned_as_is(self) -> None:
        full = "projects/my-project/locations/eu/processors/abc123"
        client = self._client(full, "eu")
        assert DocumentAIService._get_processor_name(client) == full

    @patch("scans.document_ai.settings")
    def test_us_location(self, mock_settings: Any) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "proj"
        client = self._client("xyz789", "us")
        name = DocumentAIService._get_processor_name(client)
        assert "locations/us" in name


# ─────────────────────────────────────────────────────────────────────────────
# DocumentAIService — process_image_bytes  (mocked SDK)
# ─────────────────────────────────────────────────────────────────────────────


class TestDocumentAIServiceProcessImageBytes:
    """Tests for DocumentAIService.process_image_bytes with mocked Document AI.

    ``google.cloud.documentai`` is injected via sys.modules so the lazy
    ``from google.cloud import documentai`` inside process_image_bytes never
    attempts a real import (which fails under Python 3.14 + protobuf).
    """

    def _client(self, processor_id: str = "abc123", location: str = "eu") -> Any:
        c = SimpleNamespace()
        c.name = "Test Charity"
        c.document_ai_processor_id = processor_id
        c.document_ai_location = location
        return c

    def _mock_response(
        self,
        full_text: str = "Donation \u00a325.00",
        entity_type: str = "donation_amount",
        entity_text: str = "\u00a325.00",
    ) -> Any:
        """Build a minimal mock Document AI API response."""
        entity = MagicMock()
        entity.type_ = entity_type
        entity.mention_text = entity_text
        entity.confidence = 0.95
        entity.properties = []
        entity.normalised_value.money_value.units = 25
        entity.normalised_value.money_value.nanos = 0
        entity.normalised_value.date_value.year = 0
        entity.normalised_value.boolean_value = None
        entity.normalised_value.text = ""
        doc = MagicMock()
        doc.text = full_text
        doc.entities = [entity]
        doc.pages = [MagicMock()]
        response = MagicMock()
        response.document = doc
        return response

    @patch("scans.document_ai.settings")
    def test_raises_when_not_configured(self, mock_settings: Any) -> None:
        mock_settings.GOOGLE_CLOUD_PROJECT_ID = ""
        with pytest.raises(RuntimeError, match="not configured"):
            DocumentAIService.process_image_bytes(
                b"\xff\xd8\xff", self._client(processor_id="")
            )

    @patch("scans.document_ai.settings")
    def test_returns_document_ai_result(self, mock_settings: Any) -> None:
        import sys

        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        client = self._client("abc123")
        mock_doc_ai = MagicMock()
        mock_doc_ai.process_document.return_value = self._mock_response()

        mock_documentai = MagicMock()
        mock_documentai.RawDocument.return_value = MagicMock()
        mock_documentai.ProcessRequest.return_value = MagicMock()

        with (
            patch.dict(sys.modules, {"google.cloud.documentai": mock_documentai}),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=mock_doc_ai,
            ),
        ):
            result = DocumentAIService.process_image_bytes(b"\xff\xd8\xff", client)

        assert isinstance(result, DocumentAIResult)
        assert result.full_text == "Donation \u00a325.00"
        assert len(result.entities) == 1
        assert result.entities[0].type_ == "donation_amount"

    @patch("scans.document_ai.settings")
    def test_api_failure_raises_runtime_error(self, mock_settings: Any) -> None:
        import sys

        mock_settings.GOOGLE_CLOUD_PROJECT_ID = "my-project"
        client = self._client("abc123")
        mock_doc_ai = MagicMock()
        mock_doc_ai.process_document.side_effect = Exception("quota exceeded")

        mock_documentai = MagicMock()
        mock_documentai.RawDocument.return_value = MagicMock()
        mock_documentai.ProcessRequest.return_value = MagicMock()

        with (
            patch.dict(sys.modules, {"google.cloud.documentai": mock_documentai}),
            patch(
                "scans.document_ai.DocumentAIService._get_client",
                return_value=mock_doc_ai,
            ),
            pytest.raises(RuntimeError, match="quota exceeded"),
        ):
            DocumentAIService.process_image_bytes(b"\xff\xd8\xff", client)


# ─────────────────────────────────────────────────────────────────────────────
# OCRExtractor — _coerce_gift_aid
# ─────────────────────────────────────────────────────────────────────────────


class TestCoerceGiftAid:
    """Tests for OCRExtractor._coerce_gift_aid."""

    @pytest.mark.parametrize(
        "raw", ["yes", "true", "1", "tick", "\u2611", "\u2714", "\u2717", "\u2612"]
    )
    def test_positive_values(self, raw: str) -> None:
        assert OCRExtractor._coerce_gift_aid(raw) is True

    @pytest.mark.parametrize("raw", ["no", "false", "0"])
    def test_negative_values(self, raw: str) -> None:
        assert OCRExtractor._coerce_gift_aid(raw) is False

    @pytest.mark.parametrize("raw", ["maybe", "n/a", "", "unknown"])
    def test_indeterminate_returns_none(self, raw: str) -> None:
        assert OCRExtractor._coerce_gift_aid(raw) is None


# ─────────────────────────────────────────────────────────────────────────────
# OCRExtractor — _coerce_amount
# ─────────────────────────────────────────────────────────────────────────────


class TestCoerceAmount:
    """Tests for OCRExtractor._coerce_amount."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("25.00", "25.00"),
            ("\u00a325.00", "25.00"),
            ("1,250.00", "1250.00"),
            ("\u00a31,250.50", "1250.50"),
            ("15-00", "15.00"),
            ("250-00", "250.00"),
            ("100\n\u00a3", "100"),
            ("\u00a3100-", "100"),
            ("0.01", "0.01"),
            ("999999.99", "999999.99"),
        ],
    )
    def test_valid_amounts(self, raw: str, expected: str) -> None:
        assert OCRExtractor._coerce_amount(raw) == expected

    @pytest.mark.parametrize("raw", ["0.00", "0", "abc", "", "1000000.00"])
    def test_invalid_or_out_of_range_returns_none(self, raw: str) -> None:
        assert OCRExtractor._coerce_amount(raw) is None


# ─────────────────────────────────────────────────────────────────────────────
# OCRExtractor — _coerce_entity_value
# ─────────────────────────────────────────────────────────────────────────────


class TestCoerceEntityValue:
    """Tests for OCRExtractor._coerce_entity_value (dispatch routing)."""

    def test_gift_aid_routes_to_bool(self) -> None:
        assert OCRExtractor._coerce_entity_value("gift_aid", "yes") is True
        assert OCRExtractor._coerce_entity_value("gift_aid", "no") is False

    def test_amount_strips_currency_symbol(self) -> None:
        assert OCRExtractor._coerce_entity_value("amount", "\u00a350.00") == "50.00"

    def test_consent_field_returns_bool(self) -> None:
        assert OCRExtractor._coerce_entity_value("email_consent", "yes") is True
        assert OCRExtractor._coerce_entity_value("post_consent", "no") is False

    def test_string_field_returned_as_is(self) -> None:
        assert OCRExtractor._coerce_entity_value("cheque_number", "123456") == "123456"
        assert (
            OCRExtractor._coerce_entity_value("donor_name", "John Smith")
            == "John Smith"
        )

    def test_empty_string_returns_none(self) -> None:
        assert OCRExtractor._coerce_entity_value("donor_name", "   ") is None


# ─────────────────────────────────────────────────────────────────────────────
# OCRExtractor — _apply_entity_overrides
# ─────────────────────────────────────────────────────────────────────────────


class TestApplyEntityOverrides:
    """Tests for OCRExtractor._apply_entity_overrides."""

    def _base(self) -> dict[str, Any]:
        return {
            "amount": "",
            "amount_confidence": 0.0,
            "gift_aid": None,
            "gift_aid_confidence": 0.0,
            "payment_method": "",
            "payment_method_confidence": 0.0,
            "cheque_number": "",
            "cheque_number_confidence": 0.0,
            "donor_name": "",
            "donor_name_confidence": 0.0,
            "donor_first_name": "",
            "donor_first_name_confidence": 0.0,
            "donor_last_name": "",
            "donor_last_name_confidence": 0.0,
            "phone": "",
            "phone_confidence": 0.0,
            "email": "",
            "email_confidence": 0.0,
            "address_line1": "",
            "address_line1_confidence": 0.0,
            "address_line2": "",
            "address_line2_confidence": 0.0,
            "city": "",
            "city_confidence": 0.0,
            "postcode": "",
            "postcode_confidence": 0.0,
            "county": "",
            "county_confidence": 0.0,
        }

    def test_entity_overrides_empty_field(self) -> None:
        result = self._base()
        OCRExtractor._apply_entity_overrides(
            result, [_entity("donation_amount", "\u00a375.00", 0.95)]
        )
        assert result["amount"] == "75.00"
        assert result["amount_confidence"] == 0.95

    def test_higher_confidence_entity_wins(self) -> None:
        result = self._base()
        result["amount"] = "50.00"
        result["amount_confidence"] = 0.70
        OCRExtractor._apply_entity_overrides(
            result, [_entity("donation_amount", "\u00a375.00", 0.95)]
        )
        assert result["amount"] == "75.00"

    def test_lower_confidence_entity_does_not_override(self) -> None:
        result = self._base()
        result["amount"] = "50.00"
        result["amount_confidence"] = 0.95
        OCRExtractor._apply_entity_overrides(
            result, [_entity("donation_amount", "\u00a375.00", 0.60)]
        )
        assert result["amount"] == "50.00"

    def test_gift_aid_entity_sets_bool(self) -> None:
        result = self._base()
        OCRExtractor._apply_entity_overrides(result, [_entity("gift_aid", "yes", 0.92)])
        assert result["gift_aid"] is True

    def test_unknown_entity_type_ignored(self) -> None:
        result = self._base()
        original = dict(result)
        OCRExtractor._apply_entity_overrides(
            result, [_entity("unknown_field_xyz", "value", 0.99)]
        )
        assert result == original

    def test_normalised_value_preferred_over_mention_text(self) -> None:
        result = self._base()
        entities = [
            DocumentAIEntity(
                type_="donation_amount",
                mention_text="\u00a3 75.00",
                confidence=0.95,
                normalised_value="75.00",
            )
        ]
        OCRExtractor._apply_entity_overrides(result, entities)
        assert result["amount"] == "75.00"

    def test_form_field_amount_can_override_low_quality_regex_amount(self) -> None:
        result = self._base()
        result["amount"] = "1"
        result["amount_confidence"] = 0.90

        OCRExtractor._apply_entity_overrides(
            result,
            [
                _entity(
                    "I would like to donate",
                    "100\n\u00a3",
                    confidence=0.57,
                    source="form_field",
                )
            ],
        )
        assert result["amount"] == "100"

    def test_newline_label_phone_number_maps_correctly(self) -> None:
        result = self._base()

        OCRExtractor._apply_entity_overrides(
            result,
            [
                _entity(
                    "Phone\nNumber",
                    "07816 072718",
                    confidence=0.64,
                    source="form_field",
                )
            ],
        )
        assert result["phone"] == "07816 072718"

    def test_address_line_and_town_labels_map_correctly(self) -> None:
        result = self._base()

        OCRExtractor._apply_entity_overrides(
            result,
            [
                _entity(
                    "Address line 1", "12 Bridge Street", 0.94, source="form_field"
                ),
                _entity("Address line 2", "Flat 4", 0.93, source="form_field"),
                _entity("Town/City", "Bristol", 0.92, source="form_field"),
                _entity("Post code", "BS1 5AA", 0.91, source="form_field"),
            ],
        )

        assert result["address_line1"] == "12 Bridge Street"
        assert result["address_line2"] == "Flat 4"
        assert result["city"] == "Bristol"
        assert result["postcode"] == "BS1 5AA"

    def test_optional_and_your_label_wrappers_still_map(self) -> None:
        result = self._base()

        OCRExtractor._apply_entity_overrides(
            result,
            [
                _entity(
                    "Your Mobile Number (optional)",
                    "07816 072718",
                    0.94,
                    source="form_field",
                ),
                _entity(
                    "Your Email Address (optional)",
                    "jane@example.com",
                    0.93,
                    source="form_field",
                ),
            ],
        )

        assert result["phone"] == "07816 072718"
        assert result["email"] == "jane@example.com"


class TestFieldNormalizerUnmappedLabels:
    """Tests for reporting OCR form labels that still need onboarding."""

    def test_returns_only_unmapped_form_field_labels(self) -> None:
        client = SimpleNamespace(form_field_mapping={})
        entities = [
            _entity("Amount", "25.00", source="form_field"),
            _entity("Supporter Favourite Colour", "Blue", source="form_field"),
            _entity("Email Address", "jane@example.com", source="form_field"),
            _entity("Mystery Field", "Value", source="form_field"),
            _entity("person", "Jane Example", source="nlp"),
        ]

        from core.services.field_normalizer import FieldNormalizer

        unmapped_labels = FieldNormalizer.find_unmapped_labels(
            entities,
            client,
        )

        assert unmapped_labels == ["Mystery Field", "Supporter Favourite Colour"]


@pytest.mark.django_db
class TestFormParserEntityExtraction:
    """Tests that Form Parser key-value entities map correctly to result fields.

    Form Parser returns labelled entities directly — no regex fallback needed.
    Each test supplies explicit entities (as ``source="form_field"``) and
    verifies the extraction result.
    """

    def test_extracts_amount_contact_address_and_cheque_from_entities(self) -> None:
        """Form Parser entities for common donation fields populate the result."""
        campaign = CampaignFactory()
        entities = [
            _entity("donation_amount", "£100", confidence=0.99, source="form_field"),
            _entity("telephone", "07816 072718", confidence=0.95, source="form_field"),
            _entity(
                "email_address",
                "apple a@gmail.com",
                confidence=0.95,
                source="form_field",
            ),
            # Whole address as a single entity; parse_address_block fans it out.
            # City must appear on the same line as the postcode so it is extracted.
            _entity(
                "address",
                "7 Mount Close\nFarnham Common SL2 3QZ",
                confidence=0.92,
                source="form_field",
            ),
            _entity("cheque_number", "123456789", confidence=0.95, source="form_field"),
            _entity("cheque_date", "01/01/2026", confidence=0.95, source="form_field"),
        ]
        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(entities=entities), campaign
        )

        assert extracted["amount"] == "100"
        assert extracted["phone"] == "07816 072718"
        assert extracted["email"] == "a@gmail.com"
        assert extracted["address_line1"] == "7 Mount Close"
        assert extracted["city"] == "Farnham Common"
        assert extracted["postcode"] == "SL2 3QZ"
        assert extracted["cheque_number"] == "123456789"
        assert extracted["cheque_date"] == "01/01/2026"


# ─────────────────────────────────────────────────────────────────────────────
# OCRExtractor — extract_from_document_ai
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestExtractFromDocumentAI:
    """Tests for OCRExtractor.extract_from_document_ai."""

    def test_warm_record_qr_sets_identifiers(self) -> None:
        """QR data overrides appeal_code, package_code, urn at confidence 1.0."""
        campaign = CampaignFactory(appeal_code="APP001")
        qr_data = {"appeal_code": "APP001", "package_code": "PKG01", "urn": "URN12345"}
        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result("Gift Aid Yes Amount \u00a350.00"), campaign, qr_data=qr_data
        )
        assert extracted["appeal_code"] == "APP001"
        assert extracted["package_code"] == "PKG01"
        assert extracted["urn"] == "URN12345"
        assert extracted["urn_confidence"] == 1.0

    def test_cold_record_urn_confidence_below_one(self) -> None:
        """Without QR, urn_confidence must be < 1.0 (OCR regex, not guaranteed)."""
        campaign = CampaignFactory()
        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result("Name: John Smith  Amount: \u00a330.00"),
            campaign,
            qr_data=None,
        )
        assert extracted["urn_confidence"] < 1.0

    def test_entity_amount_overrides_regex(self) -> None:
        """High-confidence Document AI entity takes priority over regex baseline."""
        campaign = CampaignFactory()
        entities = [_entity("donation_amount", "\u00a399.99", confidence=0.99)]
        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result("Donation Amount \u00a399.99", entities),
            campaign,
            qr_data=None,
        )
        assert extracted["amount"] == "99.99"

    def test_gift_aid_entity_extracted(self) -> None:
        """Gift Aid entity from Document AI should appear in the result."""
        campaign = CampaignFactory()
        entities = [_entity("gift_aid_declaration", "Yes", confidence=0.95)]
        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result("Gift Aid Declaration Yes", entities), campaign, qr_data=None
        )
        assert extracted["gift_aid"] is True

    def test_warm_record_payment_still_extracted(self) -> None:
        """Even for warm records, payment fields must come from Document AI."""
        campaign = CampaignFactory(appeal_code="APP001")
        entities = [
            _entity("donation_amount", "\u00a325.00", confidence=0.97),
            _entity("gift_aid", "no", confidence=0.90),
            _entity("cheque_number", "654321", confidence=0.88),
        ]
        qr_data = {"appeal_code": "APP001", "package_code": "PKG01", "urn": "URN001"}
        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(
                "Cheque No 654321  Amount \u00a325.00  Gift Aid No", entities
            ),
            campaign,
            qr_data=qr_data,
        )
        assert extracted["amount"] == "25.00"
        assert extracted["gift_aid"] is False
        assert extracted["cheque_number"] == "654321"
        assert extracted["urn"] == "URN001"
        assert extracted["urn_confidence"] == 1.0

    def test_split_name_and_line_address_fields_are_supported(self) -> None:
        """Structured layouts with split name and address fields should extract cleanly."""
        campaign = CampaignFactory()
        entities = [
            _entity("Title", "Mrs", confidence=0.91, source="form_field"),
            _entity("First Name", "Jane", confidence=0.93, source="form_field"),
            _entity("Surname", "Example", confidence=0.94, source="form_field"),
            _entity(
                "Address Line 1",
                "12 Bridge Street",
                confidence=0.92,
                source="form_field",
            ),
            _entity("Address Line 2", "Flat 4", confidence=0.89, source="form_field"),
            _entity("Town/City", "Bristol", confidence=0.90, source="form_field"),
            _entity("Post Code", "BS1 5AA", confidence=0.95, source="form_field"),
        ]

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(entities=entities),
            campaign,
            qr_data=None,
        )

        assert extracted["title"] == "Mrs"
        assert extracted["donor_first_name"] == "Jane"
        assert extracted["donor_last_name"] == "Example"
        assert extracted["donor_name"] == "Jane Example"
        assert extracted["address_line1"] == "12 Bridge Street"
        assert extracted["address_line2"] == "Flat 4"
        assert extracted["city"] == "Bristol"
        assert extracted["postcode"] == "BS1 5AA"

    def test_common_label_wrappers_do_not_require_client_mapping(self) -> None:
        """Minor wording wrappers like 'your' and 'optional' should not break mapping."""
        campaign = CampaignFactory()
        entities = [
            _entity(
                "Your Email Address (optional)",
                "jane@example.com",
                confidence=0.94,
                source="form_field",
            ),
            _entity(
                "Your Mobile Number (optional)",
                "07816 072718",
                confidence=0.93,
                source="form_field",
            ),
        ]

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(entities=entities),
            campaign,
            qr_data=None,
        )

        assert extracted["email"] == "jane@example.com"
        assert extracted["phone"] == "07816 072718"

    def test_raw_text_fallback_extracts_donor_name_and_address_block(self) -> None:
        """Raw OCR text can recover donor identity fields when entities miss them."""
        campaign = CampaignFactory()
        raw_text = """Step 2: Payment
Mr A Appleton
1 Amazing Street
ATRINGTON
By completing this form I am confirming that I am aged 18 or over
AA11 A11
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["donor_name"] == "Mr A Appleton"
        assert extracted["address_line1"] == "1 Amazing Street"
        assert extracted["city"] == "Atrington"
        assert extracted["postcode"] == "AA11 A11"

    def test_raw_text_fallback_skips_appeal_code_before_real_address_block(
        self,
    ) -> None:
        """Appeal labels like ABC 2026 must not be mistaken for the postcode."""
        campaign = CampaignFactory()
        raw_text = """Step 2: Payment
Cash Appeal
I enclose a cheque/postal order payable to ABC Charity
ABC 2026
Mr A Appleton
1 Amazing Street
ATRINGTON
AA11 A11
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["donor_name"] == "Mr A Appleton"
        assert extracted["address_line1"] == "1 Amazing Street"
        assert extracted["city"] == "Atrington"
        assert extracted["postcode"] == "AA11 A11"
        assert extracted["address_line1"] != "Cash Appeal"

    def test_raw_text_fallback_extracts_payment_amount_and_date(self) -> None:
        """Cheque-style raw OCR text should recover payment amount and date."""
        campaign = CampaignFactory()
        raw_text = """THE BANK
Date 4-3-26
Pay ABC Charity
Three Pounds
3-00
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["donation_date"] == "4-3-26"
        assert extracted["amount"] == "3.00"

    def test_raw_text_fallback_extracts_currency_amount_without_pence_separator(
        self,
    ) -> None:
        """Cheque OCR with a pound sign and whole-number amount should still parse."""
        campaign = CampaignFactory()
        raw_text = """THE BANK
12-34-56
Date 2-2-26
€ 12
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["donation_date"] == "2-2-26"
        assert extracted["amount"] == "12"

    def test_raw_text_fallback_extracts_noisy_cheque_whole_number_amount(self) -> None:
        """Cheque OCR should recover a whole-number amount from noisy standalone lines."""
        campaign = CampaignFactory()
        raw_text = """THE BANK
12-34-56
Pay ABC
Date 2-2-26
Charity
Twelve
Pounds
t # 12
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["donation_date"] == "2-2-26"
        assert extracted["amount"] == "12"

    def test_raw_text_contact_fallback_ignores_charity_support_details(self) -> None:
        """Fallback should not treat footer/support details as donor contact data."""
        campaign = CampaignFactory()
        raw_text = """Step 4: Your contact details
Phone Number
Mobile Number
Email Address
Step 5: Keeping in touch
please contact Supporter Services by telephoning 0121 1234567 Monday to Friday
during office hours, emailing supporter.services@abc.org.uk
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["phone"] == ""
        assert extracted["email"] == ""

    def test_raw_text_contact_fallback_extracts_written_donor_phone(self) -> None:
        """Fallback should still recover a donor phone from the contact-details section."""
        campaign = CampaignFactory()
        raw_text = """Step 4: Your contact details
Phone Number
Mobile Number 07774 619191
Email Address
Step 5: Keeping in touch
"""

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text),
            campaign,
            qr_data=None,
        )

        assert extracted["phone"] == "07774 619191"

    def test_entity_email_is_ignored_when_contact_section_is_missing(self) -> None:
        """Entity-mapped support emails should be ignored without a Step 4 section."""
        campaign = CampaignFactory()
        raw_text = """Step 2: Payment
Please contact Supporter Services at supporter.services@abc.org.uk
"""
        entities = [
            _entity("email", "supporter.services@abc.org.uk", source="form_field")
        ]

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text, entities=entities),
            campaign,
            qr_data=None,
        )

        assert extracted["email"] == ""

    def test_entity_email_is_kept_when_present_in_contact_section(self) -> None:
        """Entity-mapped donor emails should still be kept from Step 4 contact details."""
        campaign = CampaignFactory()
        raw_text = """Step 4: Your contact details
Email Address jane.donor@example.com
Step 5: Keeping in touch
"""
        entities = [_entity("email", "jane.donor@example.com", source="form_field")]

        extracted = OCRExtractor.extract_from_document_ai(
            _doc_ai_result(raw_text, entities=entities),
            campaign,
            qr_data=None,
        )

        assert extracted["email"] == "jane.donor@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# ScanProcessingService — _run_document_ai
# ─────────────────────────────────────────────────────────────────────────────


class TestRunDocumentAI:
    """Tests for ScanProcessingService._run_document_ai (mocked API)."""

    def _placeholder(self) -> Any:
        ph = SimpleNamespace()
        ph.id = uuid.uuid4()
        ph.qr_decoded = True
        ph.ocr_data = {}
        ph.ocr_confidence = 0.0
        return ph

    def _client(self, processor_id: str = "abc123") -> Any:
        c = SimpleNamespace()
        c.name = "Test Charity"
        c.document_ai_processor_id = processor_id
        c.document_ai_location = "eu"
        return c

    def test_populates_ocr_data_on_placeholder(self) -> None:
        from scans.scan_processing import ScanProcessingService

        mock_result = _doc_ai_result(
            full_text="Donation \u00a325.00",
            entities=[_entity("donation_amount", "\u00a325.00")],
            confidence=0.93,
        )
        placeholder = self._placeholder()
        client = self._client()

        with patch(
            "scans.document_ai.DocumentAIService.process_image_bytes",
            return_value=mock_result,
        ) as mock_process:
            result = ScanProcessingService._run_document_ai(
                placeholder, b"\xff\xd8\xff", client
            )
            mock_process.assert_called_once_with(b"\xff\xd8\xff", client)

        assert result is mock_result
        assert placeholder.ocr_confidence == 0.93
        assert placeholder.ocr_data["confidence"] == 0.93
        assert placeholder.ocr_data["entity_count"] == 1
        assert placeholder.ocr_data["processor"] == "abc123"

    def test_qr_decoded_flag_stored_in_ocr_data(self) -> None:
        from scans.scan_processing import ScanProcessingService

        placeholder = self._placeholder()
        placeholder.qr_decoded = True

        with patch(
            "scans.document_ai.DocumentAIService.process_image_bytes",
            return_value=_doc_ai_result(),
        ):
            ScanProcessingService._run_document_ai(
                placeholder, b"\xff\xd8\xff", self._client()
            )

        assert placeholder.ocr_data["qr_decoded"] is True
