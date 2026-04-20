from __future__ import annotations

import hashlib
from pathlib import Path

from pypdf import PdfReader

from agent_cv.config import settings
from agent_cv.db.connection import get_connection
from agent_cv.ingestion.embedding_service import chunk_text, embed_texts
from agent_cv.ingestion.filename_parser import parse_file_name


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


def ingest_documents(max_files: int = 100) -> dict[str, int]:
    root = Path(settings.pdf_root)
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED]
    files = files[:max_files]

    inserted = 0
    skipped = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for file_path in files:
                digest = _hash_file(file_path)
                cur.execute("select document_id from source_documents where sha256_hash = %s", (digest,))
                if cur.fetchone():
                    skipped += 1
                    continue

                parsed = parse_file_name(file_path)
                text = _extract_text(file_path)

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
                        mime_type, sha256_hash, detected_language, ingest_status
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    returning document_id
                    """,
                    (
                        employee_id,
                        "local",
                        str(file_path),
                        file_path.name,
                        "application/pdf" if file_path.suffix.lower() == ".pdf" else "text/plain",
                        digest,
                        "pt",
                        "ingested",
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
                    (document_id, str(file_path), text[:50000], 0.60),
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
                        (employee_id, document_version_id, "summary", text[:10000], "pt"),
                    )
                    _store_cv_chunks(cur, employee_id, document_version_id, text)
                else:
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
                            parsed.issue_date,
                            parsed.expiry_date,
                            _compute_status(parsed.expiry_date),
                            "pt",
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
                        _store_cert_chunks(cur, certification_id, document_version_id, text)

                inserted += 1
        conn.commit()

    return {"inserted": inserted, "skipped": skipped, "scanned": len(files)}


def _store_cv_chunks(cur, employee_id: str, document_version_id: str, text: str) -> None:
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
                "pt",
            ),
        )


def _store_cert_chunks(cur, certification_id: str, document_version_id: str, text: str) -> None:
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
                "pt",
            ),
        )


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
