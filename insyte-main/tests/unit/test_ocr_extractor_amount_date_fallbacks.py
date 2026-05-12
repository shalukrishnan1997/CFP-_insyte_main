"""Regression tests for raw-text OCR fallbacks around amounts and stamped dates."""

from scans.ocr.extractor import OCRExtractor


def test_extract_payment_date_skips_processed_context() -> None:
    snippet = (
        "Office use — processed date 03/06/2026 (bank receipt)\n"
        "Gift Aid declaration Date: 07/06/2026 supporter signature …"
    )
    extracted = OCRExtractor._extract_payment_date_from_raw_text(snippet)
    assert extracted == "07/06/2026"


def test_extract_amount_near_donate_phrase_without_leading_currency() -> None:
    raw_text = """Step 5: Amount
        I would like to donate £
        36
        Signed ........ """
    recovered = OCRExtractor._extract_amount_near_donation_phrases(raw_text)
    assert recovered == "36"


def test_coerce_payment_method_visa_checkbox_yes_returns_card() -> None:
    value = OCRExtractor._coerce_entity_value(
        "payment_method",
        "yes",
        entity_label="Please debit my Visa/Mastercard",
    )
    assert value == "card"


def test_coerce_payment_method_bare_yes_ambiguous_returns_none() -> None:
    value = OCRExtractor._coerce_entity_value("payment_method", "yes")
    assert value is None
