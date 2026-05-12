"""Unit tests for the scan folder watcher service.

Covers the non-PDF handling behaviour:
- Mixed folders expose PDF count only and carry a non_pdf_count field.
- All-non-PDF folders are excluded from the pending list.
- ingest_folder logs a warning and proceeds with PDFs only.

Additional coverage:
- _is_image_key extension checks
- _split_prefix_for_pdf_key / _folder_prefix_for_key / _folder_segments
- _group_keys_by_folder
- _new_keys_for_ingest early-return scenarios
- _create_scan_batch_from_file: validation error, exception, auto_process flag
- _resolve_campaign: DoesNotExist, MultipleObjectsReturned, happy-path (DB)
- ingest_folder: campaign not found, all-already-ingested, multiple PDFs
- discover_and_ingest_all: new folders, cached folders, invalid-campaign flag
- _folder_seen_cache_key determinism
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scans.scan_folder import (
    ScanFolderWatcherService,
    _build_pending_folder_entry,
    _create_scan_batch_from_file,
    _folder_prefix_for_key,
    _folder_segments,
    _group_keys_by_folder,
    _is_image_key,
    _new_keys_for_ingest,
    _split_prefix_for_pdf_key,
    _validate_physical_batch_files,
)


def _make_active_campaign_mock(**overrides: Any) -> MagicMock:
    """Build a Campaign mock that passes ``campaign_scan_block_reason``.

    Default ``MagicMock()`` instances fail the new active-campaign guard
    because attribute access returns more MagicMocks (so ``status !=
    "active"`` is always truthy). Use this helper to mint a mock that
    satisfies the guard while still letting individual tests override
    fields.
    """
    from campaigns.models import Campaign

    defaults: dict[str, Any] = {
        "status": Campaign.STATUS_ACTIVE,
        "client": MagicMock(is_active=True),
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


# ---------------------------------------------------------------------------
# _validate_physical_batch_files (simplified contract)
# ---------------------------------------------------------------------------


class TestValidatePhysicalBatchFiles:
    """Simplified validator only checks for empty list."""

    def test_empty_list_returns_error(self) -> None:
        """SF-VAL-001: Empty list is still rejected."""
        assert _validate_physical_batch_files([]) is not None

    def test_pdf_only_list_is_accepted(self) -> None:
        """SF-VAL-002: A list containing only PDFs passes validation."""
        assert (
            _validate_physical_batch_files(["ScanOutput/BRC/SP25/cheque/batch.pdf"])
            is None
        )

    def test_non_pdf_list_is_now_accepted(self) -> None:
        """SF-VAL-003: Non-PDFs are no longer rejected here (filtered upstream)."""
        assert (
            _validate_physical_batch_files(["ScanOutput/BRC/SP25/cheque/img.jpg"])
            is None
        )


# ---------------------------------------------------------------------------
# _build_pending_folder_entry — file_count and non_pdf_count
# ---------------------------------------------------------------------------


def _make_entry(keys: list[str]) -> dict[str, Any] | None:
    """Helper: build a pending-folder entry using a mock campaign resolver."""
    valid_payment_methods = {
        "card",
        "direct_debit",
        "cash",
        "caf",
        "cheque",
        "postal_order",
        "non_financial",
    }
    intake_prefix = "ScanOutput/"
    folder_prefix = "ScanOutput/BRC/SPRING25/cheque/"

    with (
        patch.object(ScanFolderWatcherService, "_resolve_campaign") as mock_campaign,
        patch("scans.scan_folder._scan_form_type_choices", return_value=[]),
    ):
        mock_campaign.return_value = _make_active_campaign_mock()
        return _build_pending_folder_entry(
            folder_prefix, keys, intake_prefix, valid_payment_methods
        )


class TestBuildPendingFolderEntry:
    """Tests for _build_pending_folder_entry."""

    def test_pdf_only_folder_has_correct_file_count(self) -> None:
        """SF-ENTRY-001: All-PDF folder: file_count == number of PDFs."""
        entry = _make_entry(
            [
                "ScanOutput/BRC/SPRING25/cheque/batch1.pdf",
                "ScanOutput/BRC/SPRING25/cheque/batch2.pdf",
            ]
        )
        assert entry is not None
        assert entry["file_count"] == 2
        assert entry["non_pdf_count"] == 0

    def test_mixed_folder_file_count_is_pdf_only(self) -> None:
        """SF-ENTRY-002: Mixed folder: file_count counts PDFs only."""
        entry = _make_entry(
            [
                "ScanOutput/BRC/SPRING25/cheque/batch.pdf",
                "ScanOutput/BRC/SPRING25/cheque/img.jpg",
                "ScanOutput/BRC/SPRING25/cheque/scan.tiff",
            ]
        )
        assert entry is not None
        assert entry["file_count"] == 1
        assert entry["non_pdf_count"] == 2

    def test_all_non_pdf_folder_has_zero_file_count(self) -> None:
        """SF-ENTRY-003: All-non-PDF folder: file_count == 0."""
        entry = _make_entry(
            [
                "ScanOutput/BRC/SPRING25/cheque/img.jpg",
                "ScanOutput/BRC/SPRING25/cheque/scan.tiff",
            ]
        )
        assert entry is not None
        assert entry["file_count"] == 0
        assert entry["non_pdf_count"] == 2

    def test_empty_folder_has_zero_counts(self) -> None:
        """SF-ENTRY-004: Empty image keys produce zero counts."""
        entry = _make_entry([])
        assert entry is not None
        assert entry["file_count"] == 0
        assert entry["non_pdf_count"] == 0


# ---------------------------------------------------------------------------
# list_pending_folders — zero-PDF folders excluded
# ---------------------------------------------------------------------------


class TestListPendingFolders:
    """list_pending_folders must exclude folders with no PDFs."""

    # Use a fixed intake prefix that matches our test folder keys.
    _INTAKE_PREFIX = "ScanOutput/"
    _PDF_KEY = "ScanOutput/BRC/SPRING25/cheque/batch.pdf"
    _JPG_KEY = "ScanOutput/BRC/SPRING25/cheque/img.jpg"
    _FOLDER = "ScanOutput/BRC/SPRING25/cheque/"

    def _patch_new_image_keys(self, keys: list[str]) -> Any:
        return patch(
            "scans.scan_folder._new_image_keys_under_prefix",
            return_value=keys,
        )

    def _patch_group_keys(self, folder_map: dict[str, list[str]]) -> Any:
        return patch(
            "scans.scan_folder._group_keys_by_folder",
            return_value=folder_map,
        )

    def _patch_intake_prefix(self) -> Any:
        return patch(
            "scans.scan_folder._intake_prefix",
            return_value=self._INTAKE_PREFIX,
        )

    @patch("scans.scan_folder._scan_form_type_choices", return_value=[])
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_all_non_pdf_folder_is_excluded(
        self, mock_campaign: Any, _mock_choices: Any
    ) -> None:
        """SF-LIST-001: Folder containing only non-PDFs is not surfaced in the UI."""
        mock_campaign.return_value = _make_active_campaign_mock()
        folder_map = {self._FOLDER: [self._JPG_KEY]}
        with (
            self._patch_intake_prefix(),
            self._patch_new_image_keys([self._JPG_KEY]),
            self._patch_group_keys(folder_map),
        ):
            result = ScanFolderWatcherService.list_pending_folders()

        assert result == []

    @patch("scans.scan_folder._scan_form_type_choices", return_value=[])
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_mixed_folder_is_included_with_pdf_count(
        self, mock_campaign: Any, _mock_choices: Any
    ) -> None:
        """SF-LIST-002: Mixed folder appears with file_count == PDF count."""
        mock_campaign.return_value = _make_active_campaign_mock()
        folder_map = {self._FOLDER: [self._PDF_KEY, self._JPG_KEY]}
        with (
            self._patch_intake_prefix(),
            self._patch_new_image_keys([self._PDF_KEY, self._JPG_KEY]),
            self._patch_group_keys(folder_map),
        ):
            result = ScanFolderWatcherService.list_pending_folders()

        assert len(result) == 1
        assert result[0]["file_count"] == 1
        assert result[0]["non_pdf_count"] == 1


# ---------------------------------------------------------------------------
# ingest_folder — warning logged when non-PDFs are skipped
# ---------------------------------------------------------------------------


class TestIngestFolderNonPdfWarning:
    """ingest_folder must log a warning when non-PDF files are present."""

    @patch("scans.scan_folder._create_scan_batch_from_file")
    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_warning_logged_for_non_pdf_keys(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
        mock_create_batch: Any,
    ) -> None:
        """SF-INGEST-001: Warning is logged when non-PDFs are present in the folder."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            [
                "ScanOutput/BRC/SPRING25/cheque/batch.pdf",
                "ScanOutput/BRC/SPRING25/cheque/img.jpg",
            ],
            None,
        )
        mock_create_batch.return_value = {
            "status": "ok",
            "batches": [{"scan_batch_id": "1", "batch_name": "B1", "file_count": 1}],
        }

        with patch("scans.scan_folder.logger") as mock_logger:
            ScanFolderWatcherService.ingest_folder(
                appeal_code="SPRING25",
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
                r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            )

        # Extract all warning calls and flatten into strings for assertion
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("non-PDF" in c or "non_pdf" in c.lower() for c in warning_calls), (
            "Expected a warning about non-PDF files, got: " + str(warning_calls)
        )

    @patch("scans.scan_folder._create_scan_batch_from_file")
    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_no_warning_when_only_pdfs(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
        mock_create_batch: Any,
    ) -> None:
        """SF-INGEST-002: No non-PDF warning emitted for a PDF-only folder."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            ["ScanOutput/BRC/SPRING25/cheque/batch.pdf"],
            None,
        )
        mock_create_batch.return_value = {
            "status": "ok",
            "batches": [{"scan_batch_id": "1", "batch_name": "B1", "file_count": 1}],
        }

        with patch("scans.scan_folder.logger") as mock_logger:
            ScanFolderWatcherService.ingest_folder(
                appeal_code="SPRING25",
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
                r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            )

        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        non_pdf_warnings = [
            c for c in warning_calls if "non-PDF" in c or "non_pdf" in c.lower()
        ]
        assert non_pdf_warnings == [], (
            f"Unexpected non-PDF warnings for PDF-only folder: {non_pdf_warnings}"
        )

    @patch("scans.scan_folder._create_scan_batch_from_file")
    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_ingestion_succeeds_with_mixed_folder(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
        mock_create_batch: Any,
    ) -> None:
        """SF-INGEST-003: Ingestion succeeds (status ok) for mixed folders."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            [
                "ScanOutput/BRC/SPRING25/cheque/batch.pdf",
                "ScanOutput/BRC/SPRING25/cheque/img.jpg",
                "ScanOutput/BRC/SPRING25/cheque/scan.tiff",
            ],
            None,
        )
        mock_create_batch.return_value = {
            "status": "ok",
            "batches": [{"scan_batch_id": "1", "batch_name": "B1", "file_count": 1}],
        }

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="SPRING25",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        )

        assert result["status"] == "ok"
        # Only the PDF should have been passed to _create_scan_batch_from_file
        mock_create_batch.assert_called_once()
        passed_keys: list[str] = mock_create_batch.call_args.kwargs["image_keys"]
        assert passed_keys == ["ScanOutput/BRC/SPRING25/cheque/batch.pdf"]

    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_all_non_pdf_folder_returns_error(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
    ) -> None:
        """SF-INGEST-004: All-non-PDF folder returns an error, no batch created."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            ["ScanOutput/BRC/SPRING25/cheque/img.jpg"],
            None,
        )

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="SPRING25",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        )

        assert result["status"] == "error"
        assert "No PDF files found" in result["error"]


# ---------------------------------------------------------------------------
# _is_image_key — file extension detection
# ---------------------------------------------------------------------------


class TestIsImageKey:
    """Tests for _is_image_key extension filter."""

    def test_pdf_is_accepted(self) -> None:
        """SF-IMG-001: .pdf extension returns True."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/batch.pdf") is True

    def test_jpg_is_accepted(self) -> None:
        """SF-IMG-002: .jpg extension returns True."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/scan.jpg") is True

    def test_tiff_is_accepted(self) -> None:
        """SF-IMG-003: .tiff extension returns True."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/scan.tiff") is True

    def test_png_is_accepted(self) -> None:
        """SF-IMG-004: .png extension returns True."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/scan.png") is True

    def test_uppercase_extension_is_accepted(self) -> None:
        """SF-IMG-005: Extension matching is case-insensitive."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/batch.PDF") is True

    def test_docx_is_rejected(self) -> None:
        """SF-IMG-006: Non-image extension returns False."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/notes.docx") is False

    def test_key_with_no_extension_is_rejected(self) -> None:
        """SF-IMG-007: Key without extension returns False."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/batch") is False

    def test_csv_is_rejected(self) -> None:
        """SF-IMG-008: .csv extension returns False."""
        assert _is_image_key("ScanOutput/BRC/SP25/cheque/data.csv") is False


# ---------------------------------------------------------------------------
# _split_prefix_for_pdf_key — split output prefix derivation
# ---------------------------------------------------------------------------


class TestSplitPrefixForPdfKey:
    """Tests for _split_prefix_for_pdf_key."""

    def test_standard_key_produces_correct_prefix(self) -> None:
        """SF-SPLIT-001: Standard key yields expected split prefix."""
        key = "ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf"
        result = _split_prefix_for_pdf_key(key)
        assert result == "ScanOutput/BRC/SPRING25/cheque/split/Batch-001_doc_"

    def test_different_filename_produces_correct_prefix(self) -> None:
        """SF-SPLIT-002: Different filename still yields correct prefix."""
        key = "ScanOutput/RNIB/WINTER25/card/MyBatch.pdf"
        result = _split_prefix_for_pdf_key(key)
        assert result == "ScanOutput/RNIB/WINTER25/card/split/MyBatch_doc_"


# ---------------------------------------------------------------------------
# _folder_prefix_for_key — folder prefix extraction
# ---------------------------------------------------------------------------


class TestFolderPrefixForKey:
    """Tests for _folder_prefix_for_key."""

    _INTAKE = "ScanOutput/"

    def test_valid_3_segment_key_returns_folder_prefix(self) -> None:
        """SF-PREFIX-001: Valid key returns its 3-segment folder prefix."""
        key = "ScanOutput/BRC/SPRING25/cheque/batch.pdf"
        result = _folder_prefix_for_key(key, self._INTAKE)
        assert result == "ScanOutput/BRC/SPRING25/cheque/"

    def test_key_at_intake_root_returns_none(self) -> None:
        """SF-PREFIX-002: Key directly under intake prefix returns None."""
        key = "ScanOutput/batch.pdf"
        result = _folder_prefix_for_key(key, self._INTAKE)
        assert result is None

    def test_key_with_two_segments_returns_none(self) -> None:
        """SF-PREFIX-003: Key with only 2 sub-segments returns None."""
        key = "ScanOutput/BRC/batch.pdf"
        result = _folder_prefix_for_key(key, self._INTAKE)
        assert result is None

    def test_key_in_sub_subdirectory_has_correct_folder(self) -> None:
        """SF-PREFIX-004: Key in sub-subdirectory still maps to 3-segment folder."""
        key = "ScanOutput/BRC/SPRING25/cheque/split/Batch-001_doc_001.pdf"
        result = _folder_prefix_for_key(key, self._INTAKE)
        assert result == "ScanOutput/BRC/SPRING25/cheque/"


# ---------------------------------------------------------------------------
# _folder_segments — 3-segment path parsing
# ---------------------------------------------------------------------------


class TestFolderSegments:
    """Tests for _folder_segments."""

    _INTAKE = "ScanOutput/"

    def test_valid_folder_returns_three_segments(self) -> None:
        """SF-SEG-001: Valid 3-segment folder returns (client, appeal, payment)."""
        result = _folder_segments("ScanOutput/BRC/SPRING25/cheque/", self._INTAKE)
        assert result == ("BRC", "SPRING25", "cheque")

    def test_two_segment_folder_returns_none(self) -> None:
        """SF-SEG-002: Folder with fewer than 3 segments returns None."""
        result = _folder_segments("ScanOutput/BRC/SPRING25/", self._INTAKE)
        assert result is None

    def test_four_segment_folder_returns_none(self) -> None:
        """SF-SEG-003: Folder with more than 3 segments returns None."""
        result = _folder_segments("ScanOutput/BRC/SPRING25/cheque/extra/", self._INTAKE)
        assert result is None

    def test_segments_are_stripped_of_whitespace(self) -> None:
        """SF-SEG-004: Extra whitespace in segment values is stripped."""
        result = _folder_segments("ScanOutput/ BRC / SPRING25 / cheque/", self._INTAKE)
        assert result == ("BRC", "SPRING25", "cheque")


# ---------------------------------------------------------------------------
# _group_keys_by_folder — key grouping
# ---------------------------------------------------------------------------


class TestGroupKeysByFolder:
    """Tests for _group_keys_by_folder."""

    _INTAKE = "ScanOutput/"

    def test_single_folder_groups_all_keys_together(self) -> None:
        """SF-GROUP-001: All keys in the same folder end up in one group."""
        keys = [
            "ScanOutput/BRC/SPRING25/cheque/batch1.pdf",
            "ScanOutput/BRC/SPRING25/cheque/batch2.pdf",
        ]
        result = _group_keys_by_folder(keys, self._INTAKE)
        assert len(result) == 1
        assert set(result["ScanOutput/BRC/SPRING25/cheque/"]) == set(keys)

    def test_keys_across_two_folders_produce_two_groups(self) -> None:
        """SF-GROUP-002: Keys across different folders map to separate groups."""
        keys = [
            "ScanOutput/BRC/SPRING25/cheque/batch.pdf",
            "ScanOutput/BRC/SPRING25/card/batch.pdf",
        ]
        result = _group_keys_by_folder(keys, self._INTAKE)
        assert len(result) == 2
        assert "ScanOutput/BRC/SPRING25/cheque/" in result
        assert "ScanOutput/BRC/SPRING25/card/" in result

    def test_invalid_key_is_omitted(self) -> None:
        """SF-GROUP-003: Key not matching 3-segment path is excluded."""
        keys = [
            "ScanOutput/batch.pdf",  # Only 1 segment — invalid
            "ScanOutput/BRC/SPRING25/cheque/valid.pdf",
        ]
        result = _group_keys_by_folder(keys, self._INTAKE)
        assert "ScanOutput/BRC/SPRING25/cheque/" in result
        # Root-level key should not produce a group entry
        assert "ScanOutput/" not in result


# ---------------------------------------------------------------------------
# _new_keys_for_ingest — early-return scenarios
# ---------------------------------------------------------------------------


class TestNewKeysForIngest:
    """Tests for _new_keys_for_ingest early-exit paths."""

    def test_no_image_files_returns_error_response(self) -> None:
        """SF-NKI-001: No image files in R2 prefix returns error dict."""
        with patch(
            "scans.scan_folder._list_all_keys_under_prefix",
            return_value=[],
        ):
            keys, response = _new_keys_for_ingest("ScanOutput/BRC/SPRING25/cheque/")

        assert keys == []
        assert response is not None
        assert response["status"] == "error"
        assert "No image files found" in response["error"]

    def test_all_already_ingested_returns_ok_with_zero_count(self) -> None:
        """SF-NKI-002: All keys already in DB returns ok with file_count=0."""
        pdf_key = "ScanOutput/BRC/SPRING25/cheque/batch.pdf"
        with (
            patch(
                "scans.scan_folder._list_all_keys_under_prefix",
                return_value=[pdf_key],
            ),
            patch(
                "scans.scan_folder._already_ingested_keys",
                return_value={pdf_key},
            ),
        ):
            keys, response = _new_keys_for_ingest("ScanOutput/BRC/SPRING25/cheque/")

        assert keys == []
        assert response is not None
        assert response["status"] == "ok"
        assert response["file_count"] == 0
        assert response["processing_triggered"] is False

    def test_new_keys_are_returned_sorted(self) -> None:
        """SF-NKI-003: New keys are returned in sorted order, no early response."""
        keys_on_r2 = [
            "ScanOutput/BRC/SPRING25/cheque/b.pdf",
            "ScanOutput/BRC/SPRING25/cheque/a.pdf",
        ]
        with (
            patch(
                "scans.scan_folder._list_all_keys_under_prefix",
                return_value=keys_on_r2,
            ),
            patch(
                "scans.scan_folder._already_ingested_keys",
                return_value=set(),
            ),
        ):
            keys, response = _new_keys_for_ingest("ScanOutput/BRC/SPRING25/cheque/")

        assert response is None
        assert keys == sorted(keys_on_r2)

    def test_non_image_keys_are_excluded_from_new_keys(self) -> None:
        """SF-NKI-004: Non-image keys (e.g. .csv) are excluded before DB check."""
        with (
            patch(
                "scans.scan_folder._list_all_keys_under_prefix",
                return_value=[
                    "ScanOutput/BRC/SPRING25/cheque/batch.pdf",
                    "ScanOutput/BRC/SPRING25/cheque/manifest.csv",
                ],
            ),
            patch(
                "scans.scan_folder._already_ingested_keys",
                return_value=set(),
            ),
        ):
            keys, response = _new_keys_for_ingest("ScanOutput/BRC/SPRING25/cheque/")

        assert response is None
        assert keys == ["ScanOutput/BRC/SPRING25/cheque/batch.pdf"]


# ---------------------------------------------------------------------------
# _create_scan_batch_from_file — sub-function level
# ---------------------------------------------------------------------------


class TestCreateScanBatchFromFile:
    """Tests for _create_scan_batch_from_file."""

    _CAMPAIGN = MagicMock(id="camp-1", name="SPRING25")

    def _call(
        self,
        image_keys: list[str],
        auto_process: bool = False,
    ) -> dict[str, Any]:
        return _create_scan_batch_from_file(
            campaign=self._CAMPAIGN,
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            user=None,
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            auto_process=auto_process,
            image_keys=image_keys,
        )

    def test_empty_keys_returns_error(self) -> None:
        """SF-CBFF-001: Empty image_keys returns an error response."""
        result = self._call([])
        assert result["status"] == "error"
        assert "No files" in result["error"]

    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_value_error_from_service_returns_error(self, mock_create: Any) -> None:
        """SF-CBFF-002: ValueError from the processing service returns an error."""
        mock_create.side_effect = ValueError("Duplicate batch name")

        result = self._call(["ScanOutput/BRC/SPRING25/cheque/batch.pdf"])

        assert result["status"] == "error"
        assert "Duplicate batch name" in result["error"]

    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_unexpected_exception_returns_generic_error(self, mock_create: Any) -> None:
        """SF-CBFF-003: Unexpected exception returns a generic server error."""
        mock_create.side_effect = RuntimeError("Database gone away")

        result = self._call(["ScanOutput/BRC/SPRING25/cheque/batch.pdf"])

        assert result["status"] == "error"
        assert "Failed to create batch" in result["error"]

    @patch("scans.tasks.process_scan_batch_task")
    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_auto_process_true_triggers_celery_task(
        self, mock_create: Any, mock_task: Any
    ) -> None:
        """SF-CBFF-004: auto_process=True triggers the OCR Celery task."""
        fake_batch = MagicMock()
        fake_batch.id = "batch-uuid-1"
        fake_batch.batch_name = "B1"
        fake_batch.total_scans = 3
        mock_create.return_value = fake_batch

        result = self._call(
            ["ScanOutput/BRC/SPRING25/cheque/batch.pdf"], auto_process=True
        )

        mock_task.delay.assert_called_once_with("batch-uuid-1")
        assert result["status"] == "ok"
        assert result["processing_triggered"] is True

    @patch("scans.tasks.process_scan_batch_task")
    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_auto_process_false_does_not_trigger_task(
        self, mock_create: Any, mock_task: Any
    ) -> None:
        """SF-CBFF-005: auto_process=False does not trigger the Celery task."""
        fake_batch = MagicMock()
        fake_batch.id = "batch-uuid-2"
        fake_batch.batch_name = "B2"
        fake_batch.total_scans = 2
        mock_create.return_value = fake_batch

        result = self._call(
            ["ScanOutput/BRC/SPRING25/cheque/batch.pdf"], auto_process=False
        )

        mock_task.delay.assert_not_called()
        assert result["status"] == "ok"
        assert result["processing_triggered"] is False


# ---------------------------------------------------------------------------
# ScanFolderWatcherService._resolve_campaign — DB-backed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolveCampaign:
    """Integration tests for _resolve_campaign using real DB records."""

    def test_found_by_appeal_code_returns_campaign(self) -> None:
        """SF-RCAMP-001: Matching appeal code returns the campaign instance."""
        from tests.factories import CampaignFactory

        campaign = CampaignFactory(appeal_code="SPRING25")

        result = ScanFolderWatcherService._resolve_campaign(  # pyright: ignore[reportPrivateUsage]
            appeal_code="SPRING25"
        )

        assert result is not None
        assert str(result.id) == str(campaign.id)

    def test_lookup_is_case_insensitive(self) -> None:
        """SF-RCAMP-002: Appeal code lookup is case-insensitive."""
        from tests.factories import CampaignFactory

        campaign = CampaignFactory(appeal_code="spring25")

        result = ScanFolderWatcherService._resolve_campaign(  # pyright: ignore[reportPrivateUsage]
            appeal_code="SPRING25"
        )

        assert result is not None
        assert str(result.id) == str(campaign.id)

    def test_not_found_returns_none(self) -> None:
        """SF-RCAMP-003: No matching campaign returns None."""
        result = ScanFolderWatcherService._resolve_campaign(  # pyright: ignore[reportPrivateUsage]
            appeal_code="NONEXISTENT99"
        )
        assert result is None

    def test_multiple_matches_returns_none(self) -> None:
        """SF-RCAMP-004: Ambiguous appeal code (multiple campaigns) returns None."""
        from tests.factories import CampaignFactory

        CampaignFactory(appeal_code="DUPCODE")
        CampaignFactory(appeal_code="DUPCODE")

        result = ScanFolderWatcherService._resolve_campaign(  # pyright: ignore[reportPrivateUsage]
            appeal_code="DUPCODE"
        )
        assert result is None

    def test_client_code_narrows_ambiguous_match(self) -> None:
        """SF-RCAMP-005: client_code disambiguates when multiple campaigns share appeal code."""
        from tests.factories import CampaignFactory, ClientFactory

        client_a = ClientFactory(client_code="BRCA")
        client_b = ClientFactory(client_code="BRCB")
        campaign_a = CampaignFactory(appeal_code="SHARED25", client=client_a)
        CampaignFactory(appeal_code="SHARED25", client=client_b)

        result = ScanFolderWatcherService._resolve_campaign(  # pyright: ignore[reportPrivateUsage]
            appeal_code="SHARED25", client_code="BRCA"
        )

        assert result is not None
        assert str(result.id) == str(campaign_a.id)


# ---------------------------------------------------------------------------
# ingest_folder — campaign not found
# ---------------------------------------------------------------------------


class TestIngestFolderCampaignNotFound:
    """ingest_folder returns error when the campaign cannot be resolved."""

    @patch.object(ScanFolderWatcherService, "_resolve_campaign", return_value=None)
    def test_returns_error_when_campaign_not_found(self, _mock: Any) -> None:
        """SF-INGEST-005: Missing campaign returns status=error."""
        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="UNKNOWN",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/UNKNOWN/cheque/",
        )

        assert result["status"] == "error"
        assert "UNKNOWN" in result["error"]


# ---------------------------------------------------------------------------
# ingest_folder — all already ingested (idempotency)
# ---------------------------------------------------------------------------


class TestIngestFolderIdempotency:
    """ingest_folder is idempotent: re-ingesting a folder returns ok + file_count=0."""

    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    @patch(
        "scans.scan_folder._new_keys_for_ingest",
        return_value=(
            [],
            {
                "status": "ok",
                "file_count": 0,
                "total_batches": 0,
                "batches": [],
                "processing_triggered": False,
                "message": "All files in this folder have already been ingested.",
            },
        ),
    )
    def test_second_call_returns_ok_with_no_batches(
        self, _mock_nki: Any, mock_campaign: Any
    ) -> None:
        """SF-INGEST-006: Second call returns ok, file_count=0, no batches."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="SPRING25",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        )

        assert result["status"] == "ok"
        assert result["file_count"] == 0
        assert result["batches"] == []
        assert result["processing_triggered"] is False


# ---------------------------------------------------------------------------
# ingest_folder — multiple PDFs produce multiple batches
# ---------------------------------------------------------------------------


class TestIngestFolderMultiplePDFs:
    """Multiple PDFs in one folder each produce their own ScanBatch."""

    @patch("scans.scan_folder._create_scan_batch_from_file")
    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_two_pdfs_produce_two_batches(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
        mock_create_batch: Any,
    ) -> None:
        """SF-INGEST-007: Two PDFs result in two separate scan batches."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            [
                "ScanOutput/BRC/SPRING25/cheque/batch_a.pdf",
                "ScanOutput/BRC/SPRING25/cheque/batch_b.pdf",
            ],
            None,
        )
        mock_create_batch.side_effect = [
            {
                "status": "ok",
                "batches": [
                    {"scan_batch_id": "1", "batch_name": "B1", "file_count": 40}
                ],
            },
            {
                "status": "ok",
                "batches": [
                    {"scan_batch_id": "2", "batch_name": "B2", "file_count": 35}
                ],
            },
        ]

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="SPRING25",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        )

        assert result["status"] == "ok"
        assert result["total_batches"] == 2
        assert result["file_count"] == 75
        assert len(result["batches"]) == 2

    @patch("scans.scan_folder._create_scan_batch_from_file")
    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_partial_failure_returns_ok_with_error_message(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
        mock_create_batch: Any,
    ) -> None:
        """SF-INGEST-008: One batch fails but another succeeds — status remains ok."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            [
                "ScanOutput/BRC/SPRING25/cheque/good.pdf",
                "ScanOutput/BRC/SPRING25/cheque/bad.pdf",
            ],
            None,
        )
        mock_create_batch.side_effect = [
            {
                "status": "ok",
                "batches": [
                    {"scan_batch_id": "1", "batch_name": "B1", "file_count": 10}
                ],
            },
            {"status": "error", "error": "Duplicate batch name"},
        ]

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="SPRING25",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        )

        assert result["status"] == "ok"
        assert result["total_batches"] == 1
        assert "Errors" in result["message"]

    @patch("scans.scan_folder._create_scan_batch_from_file")
    @patch("scans.scan_folder._new_keys_for_ingest")
    @patch.object(ScanFolderWatcherService, "_resolve_campaign")
    def test_all_batches_fail_returns_error(
        self,
        mock_campaign: Any,
        mock_new_keys: Any,
        mock_create_batch: Any,
    ) -> None:
        """SF-INGEST-009: All batches fail — status=error."""
        mock_campaign.return_value = _make_active_campaign_mock(
            id="abc", name="SPRING25"
        )
        mock_new_keys.return_value = (
            ["ScanOutput/BRC/SPRING25/cheque/bad.pdf"],
            None,
        )
        mock_create_batch.return_value = {
            "status": "error",
            "error": "Processing failed",
        }

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code="SPRING25",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        )

        assert result["status"] == "error"
        assert "Processing failed" in result["error"]


# ---------------------------------------------------------------------------
# discover_and_ingest_all — folder discovery and cache-based deduplication
# ---------------------------------------------------------------------------


class TestDiscoverAndIngestAll:
    """Tests for ScanFolderWatcherService.discover_and_ingest_all."""

    _VALID_FOLDER: dict[str, Any] = {
        "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
        "file_count": 2,
        "is_valid": True,
        "appeal_code": "SPRING25",
        "payment_method": "cheque",
    }
    _INVALID_FOLDER: dict[str, Any] = {
        "r2_prefix": "ScanOutput/BRC/UNKNOWN/cheque/",
        "file_count": 1,
        "is_valid": False,
        "appeal_code": "UNKNOWN",
        "payment_method": "cheque",
    }

    @patch.object(ScanFolderWatcherService, "list_pending_folders")
    @patch("django.core.cache.cache")
    def test_new_folder_included_in_new_folders(
        self, mock_cache: Any, mock_list: Any
    ) -> None:
        """SF-DISC-001: Unseen folder appears in new_folders list."""
        mock_list.return_value = [self._VALID_FOLDER]
        mock_cache.get.return_value = None  # Not seen yet

        result = ScanFolderWatcherService.discover_and_ingest_all()

        assert len(result["new_folders"]) == 1
        assert result["skipped"] == 0
        assert result["ingested"] == 0
        assert result["new_folders"][0]["notification_type"] == "info"

    @patch.object(ScanFolderWatcherService, "list_pending_folders")
    @patch("django.core.cache.cache")
    def test_seen_folder_is_skipped(self, mock_cache: Any, mock_list: Any) -> None:
        """SF-DISC-002: Previously-seen folder is counted as skipped."""
        mock_list.return_value = [self._VALID_FOLDER]
        mock_cache.get.return_value = True  # Already cached

        result = ScanFolderWatcherService.discover_and_ingest_all()

        assert result["new_folders"] == []
        assert result["skipped"] == 1

    @patch.object(ScanFolderWatcherService, "list_pending_folders")
    @patch("django.core.cache.cache")
    def test_invalid_campaign_folder_gets_warning_notification_type(
        self, mock_cache: Any, mock_list: Any
    ) -> None:
        """SF-DISC-003: Folder with unresolved campaign gets notification_type='warning'."""
        mock_list.return_value = [self._INVALID_FOLDER]
        mock_cache.get.return_value = None

        result = ScanFolderWatcherService.discover_and_ingest_all()

        assert len(result["new_folders"]) == 1
        assert result["new_folders"][0]["notification_type"] == "warning"

    @patch.object(ScanFolderWatcherService, "list_pending_folders")
    @patch("django.core.cache.cache")
    def test_ingested_is_always_zero(self, mock_cache: Any, mock_list: Any) -> None:
        """SF-DISC-004: discover_and_ingest_all never ingests — ingested is always 0."""
        mock_list.return_value = [self._VALID_FOLDER, self._INVALID_FOLDER]
        mock_cache.get.return_value = None

        result = ScanFolderWatcherService.discover_and_ingest_all()

        assert result["ingested"] == 0

    @patch.object(ScanFolderWatcherService, "list_pending_folders")
    @patch("django.core.cache.cache")
    def test_new_folder_is_marked_in_cache(
        self, mock_cache: Any, mock_list: Any
    ) -> None:
        """SF-DISC-005: New folder is stored in cache so it is not re-notified."""
        mock_list.return_value = [self._VALID_FOLDER]
        mock_cache.get.return_value = None

        ScanFolderWatcherService.discover_and_ingest_all()

        mock_cache.set.assert_called_once()
        # Verify the cache set value is True and TTL = 86400
        _key, value, *_ = mock_cache.set.call_args.args
        assert value is True
        assert mock_cache.set.call_args.kwargs.get("timeout") == 86400

    @patch.object(ScanFolderWatcherService, "list_pending_folders")
    @patch("django.core.cache.cache")
    def test_empty_pending_list_returns_empty_results(
        self, mock_cache: Any, mock_list: Any
    ) -> None:
        """SF-DISC-006: No pending folders produces empty new_folders and results."""
        mock_list.return_value = []

        result = ScanFolderWatcherService.discover_and_ingest_all()

        assert result["new_folders"] == []
        assert result["skipped"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# _folder_seen_cache_key — determinism
# ---------------------------------------------------------------------------


class TestFolderSeenCacheKey:
    """Tests for ScanFolderWatcherService._folder_seen_cache_key."""

    def test_same_prefix_always_produces_same_key(self) -> None:
        """SF-CACHE-001: Same prefix produces identical cache key on repeated calls."""
        prefix = "ScanOutput/BRC/SPRING25/cheque/"
        key1 = ScanFolderWatcherService._folder_seen_cache_key(prefix)  # pyright: ignore[reportPrivateUsage]
        key2 = ScanFolderWatcherService._folder_seen_cache_key(prefix)  # pyright: ignore[reportPrivateUsage]
        assert key1 == key2

    def test_different_prefixes_produce_different_keys(self) -> None:
        """SF-CACHE-002: Different prefixes produce different cache keys."""
        key_a = ScanFolderWatcherService._folder_seen_cache_key(  # pyright: ignore[reportPrivateUsage]
            "ScanOutput/BRC/SPRING25/cheque/"
        )
        key_b = ScanFolderWatcherService._folder_seen_cache_key(  # pyright: ignore[reportPrivateUsage]
            "ScanOutput/BRC/SPRING25/card/"
        )
        assert key_a != key_b

    def test_key_has_expected_prefix_format(self) -> None:
        """SF-CACHE-003: Cache key starts with the expected namespace prefix."""
        key = ScanFolderWatcherService._folder_seen_cache_key(  # pyright: ignore[reportPrivateUsage]
            "ScanOutput/BRC/SPRING25/cheque/"
        )
        assert key.startswith("scan_r2_folder_seen:")
