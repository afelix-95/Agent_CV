"""Vision-based certificate extraction using multimodal models."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from openai import AzureOpenAI
from dateutil import parser as date_parser
from pypdf import PdfReader

from agent_cv.config import settings


def _get_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


def pdf_to_images(pdf_path: str, dpi: int = 150) -> list[bytes]:
    """
    Convert PDF pages to PNG images.
    Handles both text-based and scanned (image-based) PDFs.
    """
    images: list[bytes] = []
    try:
        import fitz  # PyMuPDF for better image conversion
        doc = fitz.open(pdf_path)
        for page_num, page in enumerate(doc):
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
            img_bytes = pix.tobytes("png")
            images.append(img_bytes)
        doc.close()
    except ImportError:
        # Fallback: use pypdf + PIL
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                if "/XObject" in page["/Resources"]:
                    for obj in page["/Resources"]["/XObject"].get_object():
                        if reader.get_object(page["/Resources"]["/XObject"][obj])["/Subtype"] == "/Image":
                            data = reader.get_object(page["/Resources"]["/XObject"][obj]).get_data()
                            images.append(data)
        except Exception:
            pass
    return images


def extract_certificates_from_image(image_bytes: bytes, context: str = "") -> list[dict]:
    """
    Use multimodal model to detect and extract certificate information from an image.
    Returns a list of detected certificates with extracted fields.
    """
    client = _get_client()
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    prompt = f"""
    Analyze this image and extract all visible certificates or certifications.
    
    Context: {context}
    
    For each certificate found, extract:
    - Certificate name/title
    - Issuer/Organization
    - Issue date (if visible)
    - Expiry date (if visible)
    - Credential ID or code (if visible)
    - Any other relevant details
    
    Return ONLY valid JSON as an array where each object represents one certificate.
    Only include fields that are clearly visible in the image.
    If no certificates are found, return an empty array.
    
    Response format:
    [
      {{
        "name": "...",
        "issuer": "...",
        "issue_date": "YYYY-MM-DD or null",
        "expiry_date": "YYYY-MM-DD or null",
        "credential_id": "...",
        "details": "..."
      }}
    ]
    """

    try:
        response = client.chat.completions.create(
            model=settings.azure_openai_chat_deployment,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                        },
                    ],
                }
            ],
            max_completion_tokens=2000,
            temperature=0,
        )
        result_text = response.choices[0].message.content or ""
        parsed = _parse_certificates_payload(result_text)
        if parsed:
            return _normalize_issuer(parsed, context)

        # Fallback: ask for strict row-wise table OCR when model does not return usable JSON.
        row_prompt = """
        You are reading a transcript table image.
        Extract each certification row and return plain text lines only.
        Format each line exactly as: Type|Name|Active Since|Inactive
        Use ISO dates YYYY-MM-DD where available, otherwise N/A.
        Do not include header lines or explanations.
        """
        response_rows = client.chat.completions.create(
            model=settings.azure_openai_chat_deployment,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": row_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                        },
                    ],
                }
            ],
            max_completion_tokens=2000,
            temperature=0,
        )
        row_text = response_rows.choices[0].message.content or ""
        parsed_rows = _parse_table_rows_to_certificates(row_text, context)
        if parsed_rows:
            return parsed_rows
    except Exception:
        pass
    return []


def extract_all_certificates_from_pdf(pdf_path: str, is_transcript: bool = False) -> list[dict]:
    """
    Extract all certificates from a PDF, handling:
    - Multi-certificate PDFs (especially transcripts)
    - Image-based/scanned PDFs
    - Regular text-based PDFs
    
    Returns list of certificate dicts with extracted fields.
    """
    all_certificates: list[dict] = []
    filename = Path(pdf_path).name
    vendor_hint = _infer_vendor_hint(f"{filename} {pdf_path}")
    context = (
        "This is a transcript document containing multiple certificates."
        if is_transcript
        else ""
    )

    try:
        full_text = _extract_pdf_text(pdf_path)

        # For transcript PDFs with selectable text, deterministic text parsing is faster
        # and more reliable than vision — the table structure maps directly to regex patterns.
        if full_text.strip() and is_transcript:
            parsed_from_text = parse_transcript_text_generic(full_text, vendor_hint=vendor_hint)
            if parsed_from_text:
                return parsed_from_text

        # For single-certificate PDFs, use vision as the primary extraction method.
        # Vision handles both text-based and scanned (image-only) PDFs uniformly and
        # is more reliable for structured visual documents (logos, layout, signatures).
        # If the PDF has no selectable text (scanned), pdf_to_images still works via PyMuPDF.
        images = pdf_to_images(pdf_path)
        for page_num, img_bytes in enumerate(images):
            certificates = extract_certificates_from_image(
                img_bytes,
                context=f"{context} vendor_hint={vendor_hint} filename={filename} (Page {page_num + 1})",
            )
            all_certificates.extend(certificates)

        # If multimodal returned OCR-like rows but not normalized cert objects, try generic text parsing.
        if is_transcript and not all_certificates and images:
            ocr_text = _extract_table_text_from_images(images, vendor_hint=vendor_hint)
            if ocr_text:
                all_certificates = parse_transcript_text_generic(ocr_text, vendor_hint=vendor_hint)
    except Exception:
        pass

    filtered = _filter_non_certification_rows(all_certificates)
    return _normalize_issuer(_dedupe_certificates(filtered), context=f"{context} {vendor_hint}")


def parse_transcript_text_generic(full_text: str, vendor_hint: str = "") -> list[dict]:
    """
    Parse certificate rows from transcript text in multiple vendor formats.

    Supports common structures:
    - table rows: title + optional credential number + earned + expires
    - card rows: title with Date/Earned on + Current Until/Expires on fields
    - OCR table rows with Type|Name|...|Active Since|Inactive
    """
    text = full_text or ""
    certs: list[dict] = []

    section = _extract_active_certifications_section(text)
    normalized = " ".join(section.split()) if section else " ".join(text.split())

    # Pattern A: table rows with credential id and two dates.
    table_with_id = re.compile(
        r"(?P<title>.+?)\s*(?P<number>[A-Z0-9]{4,}-[A-Z0-9]{4,})\s*"
        r"(?P<earned>(?:[A-Za-z]{3}\s+\d{1,2},\s*\d{4})|(?:\d{4}-\d{2}-\d{2}))\s*"
        r"(?P<expires>(?:[A-Za-z]{3}\s+\d{1,2},\s*\d{4})|(?:\d{4}-\d{2}-\d{2})|N/A)",
    )
    for match in table_with_id.finditer(normalized):
        title = _clean_title(match.group("title"))
        if not _is_plausible_certificate_title(title):
            continue
        expires_raw = match.group("expires").strip()
        certs.append(
            {
                "name": title,
                "issuer": vendor_hint,
                "issue_date": _to_iso_date(match.group("earned")),
                "expiry_date": None if expires_raw.upper() == "N/A" else _to_iso_date(expires_raw),
                "credential_id": match.group("number").strip(),
                "details": "Extracted from transcript text table",
            }
        )

    # Pattern B: vendor cards with Date/Earned and Current Until/Expires fields.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        title = _clean_title(line)
        if not _is_plausible_certificate_title(title):
            continue

        window = " ".join(lines[idx : idx + 9])
        issue_match = re.search(
            r"(?:Date|Earned\s*on|Active\s*Since)\s*:?\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4}|\d{4}-\d{2}-\d{2})",
            window,
            flags=re.IGNORECASE,
        )
        expiry_match = re.search(
            r"(?:Current\s*Until|Expires\s*on|Inactive)\s*:?\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4}|\d{4}-\d{2}-\d{2}|N/A)",
            window,
            flags=re.IGNORECASE,
        )
        if issue_match or expiry_match:
            exp_raw = expiry_match.group(1).strip() if expiry_match else ""
            certs.append(
                {
                    "name": title,
                    "issuer": vendor_hint,
                    "issue_date": _to_iso_date(issue_match.group(1)) if issue_match else None,
                    "expiry_date": None if exp_raw.upper() == "N/A" else _to_iso_date(exp_raw),
                    "credential_id": "",
                    "details": "Extracted from transcript text block",
                }
            )

    return _normalize_issuer(_dedupe_certificates(certs), context=vendor_hint)


def parse_microsoft_transcript_text(full_text: str) -> list[dict]:
    # Backward compatible alias used by older call sites.
    return parse_transcript_text_generic(full_text, vendor_hint="Microsoft")


def _parse_active_cert_line(line: str) -> dict | None:
    # Core date pattern seen in transcripts.
    date_pat = r"[A-Za-z]{3}\s+\d{1,2},\s+\d{4}"

    # Match rows with earned and expiry dates.
    both_dates = re.search(
        rf"^(?P<title>.+?)\s+(?P<number>[A-Z0-9-]{{6,}})\s+(?P<earned>{date_pat})\s+(?P<expires>{date_pat}|N/A)$",
        line,
    )
    if both_dates:
        title = both_dates.group("title").strip()
        cert_number = both_dates.group("number").strip()
        earned = _to_iso_date(both_dates.group("earned"))
        expires_raw = both_dates.group("expires").strip()
        expires = None if expires_raw.upper() == "N/A" else _to_iso_date(expires_raw)
        return {
            "name": title,
            "issuer": "Microsoft",
            "issue_date": earned,
            "expiry_date": expires,
            "credential_id": cert_number,
            "details": "Extracted from transcript text",
        }

    return None


def _clean_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "")).strip(" -:|\t")
    return cleaned


def _is_plausible_certificate_title(title: str) -> bool:
    t = (title or "").lower()
    if len(t) < 8:
        return False
    exclude_tokens = [
        "active certifications",
        "certification title",
        "certification number",
        "passed exams",
        "historical certifications",
        "exam transcript",
        "current credentials",
    ]
    if any(tok in t for tok in exclude_tokens):
        return False
    include_tokens = [
        "certified",
        "credential",
        "associate",
        "administrator",
        "engineer",
        "specialist",
        "professional",
        "trainer",
        "fundamentals",
        "security",
        "network",
        "ccna",
        "ccnp",
        "rhce",
        "rhcsa",
    ]
    return any(tok in t for tok in include_tokens)


def _extract_active_certifications_section(full_text: str) -> str:
    text = full_text or ""
    lowered = text.lower()

    # Prefer the table header region when present, because transcripts usually contain
    # an earlier summary block (e.g., "Active certifications 3") that has no cert rows.
    table_header = "certification title certification"
    table_idx = lowered.find(table_header)

    start = table_idx if table_idx != -1 else lowered.find("active certifications")
    if start == -1:
        return ""

    end_candidates = [
        lowered.find("passed exams", start + 1),
        lowered.find("exam transcript", start + 1),
        lowered.find("historical certifications", start + 1),
        lowered.find("microsoft certified trainer history", start + 1),
    ]
    valid_ends = [idx for idx in end_candidates if idx != -1]
    end = min(valid_ends) if valid_ends else len(text)

    return text[start:end]


def _to_iso_date(date_text: str) -> str | None:
    try:
        text = str(date_text).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        return date_parser.parse(text, dayfirst=False).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _extract_pdf_text(pdf_path: str) -> str:
    try:
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _dedupe_certificates(certs: list[dict]) -> list[dict]:
    seen: set[tuple[str, str | None, str | None]] = set()
    result: list[dict] = []
    for cert in certs:
        key = (
            (cert.get("name") or "").strip().lower(),
            cert.get("issue_date"),
            cert.get("expiry_date"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(cert)
    return result


def _parse_certificates_payload(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if not text:
        return []

    # Try direct JSON first.
    try:
        obj = json.loads(text)
        return _coerce_cert_list(obj)
    except json.JSONDecodeError:
        pass

    # Try code-fenced JSON blocks.
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        try:
            obj = json.loads(candidate)
            return _coerce_cert_list(obj)
        except json.JSONDecodeError:
            pass

    # Try extracting first JSON array.
    arr_match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if arr_match:
        try:
            obj = json.loads(arr_match.group(0))
            return _coerce_cert_list(obj)
        except json.JSONDecodeError:
            pass

    # Try extracting first JSON object that wraps a list.
    obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if obj_match:
        try:
            obj = json.loads(obj_match.group(0))
            return _coerce_cert_list(obj)
        except json.JSONDecodeError:
            pass

    return []


def _coerce_cert_list(obj) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ["certificates", "items", "results", "data"]:
            value = obj.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _normalize_issuer(certs: list[dict], context: str) -> list[dict]:
    normalized: list[dict] = []
    ctx = (context or "").lower()
    for cert in certs:
        item = dict(cert)
        name_lower = str(item.get("name") or "").lower()
        issuer = str(item.get("issuer") or "").strip()

        # Derive issuer from strong, unambiguous signals in the cert name and context.
        # These override whatever the model returned to correct misidentifications.
        derived: str | None = None
        if (
            "red hat" in name_lower or "rhcsa" in name_lower or "rhce" in name_lower
            or "red hat" in ctx or "redhat" in ctx or "rhcsa" in ctx or "rhce" in ctx
        ):
            derived = "Red Hat"
        elif "microsoft" in name_lower or "azure" in name_lower or "microsoft" in ctx:
            derived = "Microsoft"
        elif "cisco" in name_lower or "cisco" in ctx:
            derived = "Cisco"

        if derived:
            item["issuer"] = derived
        elif issuer:
            item["issuer"] = issuer
        normalized.append(item)
    return normalized


def _parse_table_rows_to_certificates(row_text: str, context: str) -> list[dict]:
    certs: list[dict] = []
    for raw_line in (row_text or "").splitlines():
        line = raw_line.strip().strip("`")
        if not line or "|" not in line:
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue

        row_type = parts[0].lower()
        if row_type not in {"credential", "certificate", "certification"}:
            continue

        name = parts[1]
        if len(parts) >= 5:
            active_col = parts[-2]
            inactive_col = parts[-1]
        else:
            active_col = parts[2]
            inactive_col = parts[3]

        active_since = _to_iso_date(active_col) if active_col.upper() != "N/A" else None
        inactive = _to_iso_date(inactive_col) if inactive_col.upper() != "N/A" else None

        issuer = "Cisco" if "cisco" in (context or "").lower() or "cisco" in name.lower() else ""
        certs.append(
            {
                "name": name,
                "issuer": issuer,
                "issue_date": active_since,
                "expiry_date": inactive,
                "credential_id": "",
                "details": f"Extracted from {parts[0]} row",
            }
        )

    return _dedupe_certificates(certs)


def _extract_table_text_from_images(images: list[bytes], vendor_hint: str = "") -> str:
    client = _get_client()
    output_lines: list[str] = []

    for img in images:
        b64_image = base64.b64encode(img).decode("utf-8")
        prompt = (
            "Extract transcript rows from the image. "
            "Return only plain text rows, one per line, in this format: "
            "Type|Name|Group|Active Since|Inactive. "
            "Do not include headers or explanations. "
            "If no rows are visible return an empty response. "
            f"Vendor hint: {vendor_hint}"
        )
        try:
            resp = client.chat.completions.create(
                model=settings.azure_openai_chat_deployment,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                            },
                        ],
                    }
                ],
                max_completion_tokens=2000,
                temperature=0,
            )
            text = resp.choices[0].message.content or ""
            if text.strip():
                output_lines.append(text)
        except Exception:
            continue

    return "\n".join(output_lines)


def _infer_vendor_hint(text: str) -> str:
    # Normalize separators so "Red_Hat" and "Red-Hat" match the same as "Red Hat".
    t = re.sub(r"[_\-]", " ", (text or "").lower())
    if "microsoft" in t or "learn" in t:
        return "Microsoft"
    if "cisco" in t:
        return "Cisco"
    if "red hat" in t or "redhat" in t or "rhce" in t or "rhcsa" in t:
        return "Red Hat"
    return ""


def _filter_non_certification_rows(certs: list[dict]) -> list[dict]:
    result: list[dict] = []
    for cert in certs:
        name = str(cert.get("name") or "").lower()
        details = str(cert.get("details") or "").lower()
        if "type shown as exam" in details:
            continue
        if name.startswith("[") and "exam" in details:
            continue
        # Filter out Red Hat exam entries (EX200, EX300, etc.)
        if re.match(r"^ex\d{3,4}\b", name.strip()):
            continue
        result.append(cert)
    return result


def detect_is_transcript(filename: str) -> bool:
    """Heuristic: detect if filename suggests a transcript with multiple certs."""
    lower = filename.lower()
    return any(
        marker in lower
        for marker in ["transcript", "transkript", "historico", "record", "history"]
    )
