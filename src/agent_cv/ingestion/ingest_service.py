from __future__ import annotations

import hashlib
from pathlib import Path

from pypdf import PdfReader

from agent_cv.config import settings
from agent_cv.db.connection import get_connection
from agent_cv.ingestion.embedding_service import chunk_text, embed_texts
from agent_cv.ingestion.filename_parser import parse_file_name
from agent_cv.ingestion.language_detection_service import detect_language
from agent_cv.ingestion.vision_extraction_service import (
    detect_is_transcript,
    extract_all_certificates_from_pdf,
)


SUPPORTED = {".pdf", ".txt", ".docx"}


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_text(path: Path) -> str:
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".pdf":
        try:
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    return ""


def ingest_documents(
    max_files: int = 100,
    force_reingest: bool = False,
    filename_contains: str | None = None,
) -> dict[str, int]:
    root = Path(settings.pdf_root)
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED]
    if filename_contains:
        needle = filename_contains.casefold()
        files = [p for p in files if needle in p.name.casefold()]
    files = files[:max_files]

    inserted = 0
    reingested = 0
    skipped = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for file_path in files:
                if force_reingest and _delete_existing_document(cur, file_path):
                    reingested += 1

                if _ingest_one_file(
                    cur,
                    actual_path=file_path,
                    logical_path=file_path,
                    source_system="local",
                    source_path=str(file_path),
                ):
                    inserted += 1
                else:
                    skipped += 1
        conn.commit()

    return {
        "inserted": inserted,
        "skipped": skipped,
        "scanned": len(files),
        "reingested": reingested,
    }


def ingest_sharepoint_file(
    filename: str,
    content: bytes,
    sharepoint_item_id: str,
    sharepoint_web_url: str,
    sharepoint_modified_at: str | None = None,
) -> dict[str, int]:
    """Ingest a single file downloaded from SharePoint.

    Writes the content to a temporary file, runs it through the normal ingestion
    pipeline, then removes the temp file.  The ``sharepoint_item_id`` and
    ``sharepoint_web_url`` are persisted on the ``source_documents`` row so the
    agent can later share the link in Teams.
    """
    import tempfile

    logical_path = Path(filename)
    suffix = logical_path.suffix.lower()
    if suffix not in SUPPORTED:
        return {"inserted": 0, "skipped": 1, "scanned": 1, "reingested": 0}

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                inserted = _ingest_one_file(
                    cur,
                    actual_path=tmp_path,
                    logical_path=logical_path,
                    source_system="sharepoint",
                    source_path=sharepoint_item_id,
                    sharepoint_item_id=sharepoint_item_id,
                    sharepoint_web_url=sharepoint_web_url,
                    sharepoint_modified_at=sharepoint_modified_at,
                )
            conn.commit()

        return {
            "inserted": 1 if inserted else 0,
            "skipped": 0 if inserted else 1,
            "scanned": 1,
            "reingested": 0,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


def _ingest_one_file(
    cur,
    actual_path: Path,
    logical_path: Path,
    source_system: str,
    source_path: str,
    sharepoint_item_id: str | None = None,
    sharepoint_web_url: str | None = None,
    sharepoint_modified_at: str | None = None,
) -> bool:
    """Insert a single file into the database.

    ``actual_path`` is the real path on disk used for text/vision extraction.
    ``logical_path`` carries the original filename for metadata parsing.

    Returns True if the document was inserted, False if it already existed.
    """
    digest = _hash_file(actual_path)

    cur.execute("select document_id from source_documents where sha256_hash = %s", (digest,))
    existing = cur.fetchone()
    if existing:
        # Document content already ingested — but if this call comes from SharePoint,
        # backfill the SharePoint fields so get_employee_cv_link can return the URL.
        if sharepoint_item_id or sharepoint_web_url:
            cur.execute(
                """
                update source_documents
                   set sharepoint_item_id   = coalesce(sharepoint_item_id, %s),
                       sharepoint_web_url   = coalesce(sharepoint_web_url, %s),
                       sharepoint_modified_at = coalesce(%s, sharepoint_modified_at)
                 where document_id = %s
                   and (sharepoint_item_id is null
                        or sharepoint_web_url is null
                        or sharepoint_modified_at is null)
                """,
                (sharepoint_item_id, sharepoint_web_url, sharepoint_modified_at, existing["document_id"]),
            )
        return False

    parsed = parse_file_name(logical_path)
    text = _extract_text(actual_path)
    detected_language = detect_language(text)

    cur.execute(
        """
        insert into employees (full_name)
        values (%s)
        on conflict (full_name) do update set updated_at = now()
        returning employee_id
        """,
        (parsed.employee_name,),
    )
    employee = cur.fetchone()
    employee_id = employee["employee_id"]

    cur.execute(
        """
        insert into source_documents (
            employee_id, source_system, source_path, original_filename,
            mime_type, sha256_hash, detected_language, ingest_status,
            sharepoint_item_id, sharepoint_web_url, sharepoint_modified_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning document_id
        """,
        (
            employee_id,
            source_system,
            source_path,
            logical_path.name,
            "application/pdf" if logical_path.suffix.lower() == ".pdf" else "text/plain",
            digest,
            detected_language,
            "ingested",
            sharepoint_item_id,
            sharepoint_web_url,
            sharepoint_modified_at,
        ),
    )
    doc = cur.fetchone()
    document_id = doc["document_id"]

    cur.execute(
        """
        insert into document_versions (
            document_id, version_number, blob_uri, text_snapshot,
            extraction_confidence, is_current
        ) values (%s, 1, %s, %s, %s, true)
        returning document_version_id
        """,
        (document_id, source_path, text[:50000], 0.60),
    )
    version = cur.fetchone()
    document_version_id = version["document_version_id"]

    if parsed.is_cv:
        cur.execute(
            """
            insert into cv_sections (
                employee_id, document_version_id, section_type, section_text, language_code
            ) values (%s, %s, %s, %s, %s)
            """,
            (employee_id, document_version_id, "summary", text[:10000], detected_language),
        )
        _store_cv_chunks(cur, employee_id, document_version_id, text, detected_language)
    else:
        is_transcript = detect_is_transcript(logical_path.name)
        vision_certs = []

        # Always attempt vision extraction for certificate documents — relying only
        # on filename parsing misses dates and can mis-identify the vendor when the
        # PDF contains selectable text but no structured fields.
        try:
            vision_certs = extract_all_certificates_from_pdf(
                str(actual_path), is_transcript=is_transcript
            )
        except Exception:
            vision_certs = []

        if vision_certs:
            _store_extracted_certificates(
                cur, employee_id, document_version_id, vision_certs, text, detected_language
            )
        elif not is_transcript:
            _store_single_certification(
                cur, employee_id, document_version_id, parsed, text, detected_language
            )

    return True


def _delete_existing_document(cur, file_path: Path) -> bool:
    digest = _hash_file(file_path)
    cur.execute(
        "select document_id from source_documents where source_path = %s or original_filename = %s or sha256_hash = %s",
        (str(file_path), file_path.name, digest),
    )
    rows = cur.fetchall()
    if not rows:
        return False

    for row in rows:
        cur.execute("delete from source_documents where document_id = %s", (row["document_id"],))

    return True


def _store_cv_chunks(cur, employee_id: str, document_version_id: str, text: str, language_code: str = "en") -> None:
    chunks = chunk_text(text)
    if not chunks:
        return
    embeddings = embed_texts(chunks)
    for order, (chunk, vector) in enumerate(zip(chunks, embeddings)):
        cur.execute(
            """
            insert into cv_chunks (
                employee_id, document_version_id, chunk_text, chunk_order,
                token_count, embedding, language_code
            ) values (%s, %s, %s, %s, %s, %s::vector, %s)
            """,
            (
                employee_id,
                document_version_id,
                chunk,
                order,
                len(chunk) // 4,
                str(vector),
                language_code,
            ),
        )


def _store_cert_chunks(cur, certification_id: str, document_version_id: str, text: str, language_code: str = "en") -> None:
    chunks = chunk_text(text)
    if not chunks:
        return
    embeddings = embed_texts(chunks)
    for chunk, vector in zip(chunks, embeddings):
        cur.execute(
            """
            insert into certification_chunks (
                certification_id, document_version_id, chunk_text,
                token_count, embedding, language_code
            ) values (%s, %s, %s, %s, %s::vector, %s)
            """,
            (
                certification_id,
                document_version_id,
                chunk,
                len(chunk) // 4,
                str(vector),
                language_code,
            ),
        )


def _store_single_certification(
    cur, employee_id: str, document_version_id: str, parsed, text: str, language_code: str = "en"
) -> None:
    """Store a single certification using only filename-derived metadata (employee name, title, vendor).
    Dates are never inferred from the filename — they will remain NULL unless vision extraction
    was able to read them from the document content."""
    cur.execute(
        """
        insert into certifications (
            employee_id, document_version_id, cert_name, issue_date,
            expiry_date, status, extracted_language, confidence_score
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            employee_id,
            document_version_id,
            parsed.title,
            None,
            None,
            "unknown",
            language_code,
            0.60,
        ),
    )

    cur.execute(
        "select certification_id from certifications where document_version_id = %s",
        (document_version_id,),
    )
    cert_row = cur.fetchone()
    certification_id = cert_row["certification_id"] if cert_row else None

    if parsed.vendor:
        cur.execute(
            "insert into vendors (vendor_name) values (%s) on conflict (vendor_name) do nothing",
            (parsed.vendor,),
        )
        cur.execute(
            """
            update certifications c
            set vendor_id = v.vendor_id
            from vendors v
            where c.document_version_id = %s and v.vendor_name = %s
            """,
            (document_version_id, parsed.vendor),
        )

    if certification_id and text:
        _store_cert_chunks(cur, certification_id, document_version_id, text, language_code)


def _store_extracted_certificates(
    cur, employee_id: str, document_version_id: str, vision_certs: list[dict], text: str, language_code: str = "en"
) -> None:
    """Store multiple certificates extracted from vision API."""
    from datetime import datetime as dt

    for cert_data in vision_certs:
        cert_name = cert_data.get("name") or cert_data.get("issuer") or "Unknown Certificate"
        issue_date_str = cert_data.get("issue_date")
        expiry_date_str = cert_data.get("expiry_date")
        vendor = cert_data.get("issuer", "")

        try:
            issue_date = dt.strptime(issue_date_str, "%Y-%m-%d").date() if issue_date_str else None
        except (ValueError, TypeError):
            issue_date = None

        try:
            expiry_date = dt.strptime(expiry_date_str, "%Y-%m-%d").date() if expiry_date_str else None
        except (ValueError, TypeError):
            expiry_date = None

        cur.execute(
            """
            insert into certifications (
                employee_id, document_version_id, cert_name, issue_date,
                expiry_date, status, extracted_language, confidence_score
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning certification_id
            """,
            (
                employee_id,
                document_version_id,
                cert_name,
                issue_date,
                expiry_date,
                _compute_status(expiry_date),
                language_code,
                0.75,
            ),
        )
        cert_row = cur.fetchone()
        certification_id = cert_row["certification_id"] if cert_row else None

        if vendor and certification_id:
            cur.execute(
                "insert into vendors (vendor_name) values (%s) on conflict (vendor_name) do nothing",
                (vendor,),
            )
            cur.execute(
                """
                update certifications c
                set vendor_id = v.vendor_id
                from vendors v
                where c.certification_id = %s and v.vendor_name = %s
                """,
                (certification_id, vendor),
            )

        if certification_id and text:
            _store_cert_chunks(cur, certification_id, document_version_id, text[:2000], language_code)


def _compute_status(expiry_date):
    if expiry_date is None:
        return "unknown"
    from datetime import date, timedelta

    today = date.today()
    if expiry_date < today:
        return "expired"
    if expiry_date <= today + timedelta(days=90):
        return "expiring_90d"
    return "active"
