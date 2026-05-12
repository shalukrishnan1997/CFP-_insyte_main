"""OCR extraction package for scanned donation forms.

Public API::

    from scans.ocr import OCRExtractor

    result = OCRExtractor.extract_from_document_ai(doc_ai_result, campaign)
    match = OCRExtractor.match_donor(result["urn"], campaign)

Package layout:

* ``entity_overrides.py`` — ``_ENTITY_FIELD_MAP`` + coercion helpers; maps
                            Document AI Form Parser entity types to internal
                            field names and coerces their raw string values.
* ``donor_matching.py``   — URN extraction from OCR text + Donor / DataFileDonor
                            DB lookup.
* ``extractor.py``        — ``OCRExtractor`` orchestrator class.
* ``field_extractors.py`` — address parsing utilities (``parse_address_block``,
                            ``address_looks_noisy``) + URN pattern helpers
                            used by ``donor_matching``.
* ``patterns.py``         — compiled regex constants used by field_extractors.
"""

from scans.ocr.extractor import OCRExtractor

__all__ = ["OCRExtractor"]
