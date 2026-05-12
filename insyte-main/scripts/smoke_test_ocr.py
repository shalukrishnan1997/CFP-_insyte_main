"""One-shot OCR smoke test.

Decodes the committed service-account credentials, boots Django, slices the first
page off ``tests/fixtures/000015.pdf``, and calls the live Google Document AI
``processDocument`` endpoint via :class:`scans.document_ai.DocumentAIService`.

Run::

    uv run python scripts/smoke_test_ocr.py

Optional override for the processor (used when no Client row has one configured)::

    OCR_TEST_PROCESSOR_ID=projects/.../processors/abc \\
    OCR_TEST_LOCATION=eu \\
    uv run python scripts/smoke_test_ocr.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDS_B64 = REPO_ROOT / "gcp-service-account.json.b64"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "000015.pdf"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _decode_credentials() -> tuple[str, str]:
    raw = CREDS_B64.read_bytes()
    decoded = base64.b64decode(raw)
    project_id = json.loads(decoded).get("project_id")
    if not project_id:
        sys.exit("ERROR: decoded service-account JSON has no 'project_id' field")
    fd, path = tempfile.mkstemp(prefix="gcp-creds-", suffix=".json")
    with os.fdopen(fd, "wb") as fh:
        fh.write(decoded)
    return path, project_id


def _resolve_client() -> Any:
    from django.db.utils import OperationalError

    from clients.models import Client

    try:
        real = (
            Client.objects.exclude(document_ai_processor_id="")
            .exclude(document_ai_processor_id__isnull=True)
            .first()
        )
    except OperationalError as exc:
        print(f"[client] DB lookup failed ({exc}); falling back to env vars")
        real = None

    if real is not None:
        print(
            f"[client] using DB row id={real.id} name={real.name!r} "
            f"processor={real.document_ai_processor_id!r} "
            f"location={real.document_ai_location!r}"
        )
        return real

    pid = os.environ.get("OCR_TEST_PROCESSOR_ID")
    loc = os.environ.get("OCR_TEST_LOCATION", "eu")
    if not pid:
        sys.exit(
            "ERROR: no Client row has document_ai_processor_id set, and "
            "OCR_TEST_PROCESSOR_ID env var is not provided. Set one or seed a Client."
        )
    print(f"[client] using stub: processor={pid!r} location={loc!r}")
    return SimpleNamespace(
        name="ocr-smoke-test-stub",
        document_ai_processor_id=pid,
        document_ai_location=loc,
    )


def _first_page_bytes() -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(FIXTURE))
    page_count = len(reader.pages)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    buf = io.BytesIO()
    writer.write(buf)
    out = buf.getvalue()
    print(
        f"[fixture] {FIXTURE.name}: {page_count} pages, sliced to 1 page ({len(out)} bytes)"
    )
    return out


def main() -> int:
    creds_path, project_id = _decode_credentials()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT_ID", project_id)
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "responsehandling.settings.development"
    )
    print(f"[creds] decoded to {creds_path}, project_id={project_id!r}")

    try:
        import django

        django.setup()

        from scans.document_ai import DocumentAIService

        client = _resolve_client()
        if not DocumentAIService.is_configured(client):
            sys.exit("ERROR: DocumentAIService.is_configured() returned False")

        page_bytes = _first_page_bytes()
        print("[ocr] calling DocumentAIService.process_image_bytes()...")
        result = DocumentAIService.process_image_bytes(page_bytes, client)

        text = result.full_text or ""
        print(f"\n[result] full_text length: {len(text)} chars")
        print(f"[result] first 300 chars:\n  {text[:300]!r}")
        print(f"\n[result] entity count: {len(result.entities)}")
        for entity in result.entities[:10]:
            print(
                f"  - type={entity.type_!r} "
                f"text={entity.mention_text[:60]!r} "
                f"conf={entity.confidence:.3f} "
                f"source={entity.source!r}"
            )

        ok_text = len(text) > 50
        ok_entities = len(result.entities) > 0
        print(
            f"\n[verdict] text_ok={ok_text} entities_ok={ok_entities} "
            f"-> {'PASS' if ok_text else 'FAIL'}"
        )
        return 0 if ok_text else 2
    finally:
        try:
            os.unlink(creds_path)
            print(f"[cleanup] removed {creds_path}")
        except OSError as exc:
            print(f"[cleanup] WARNING: could not remove {creds_path}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
