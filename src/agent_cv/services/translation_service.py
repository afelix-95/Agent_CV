"""
translation_service.py
----------------------
Translates an employee's CV into a target language and renders it as a
Europass-format PDF.

Flow:
  1. Fetch raw CV text + employee metadata from the DB
  2. Call the LLM once to extract structured fields AND translate them
  3. Build a Europass XML v3.3.0 string from the structured data
  4. Parse the XML into a structured context dict
  5. Render the context dict to PDF bytes using fpdf2 (pure Python, no system deps)
  6. Save the PDF to /tmp/agent_cv_exports/{uuid}.pdf and return the export_id
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy imports – imported lazily so unit tests don't require them
# ---------------------------------------------------------------------------

def _find_dejavu_font(name: str) -> Path:
    """Locate a DejaVu TTF — bundled package fonts take priority over system paths."""
    candidates = [
        Path(__file__).parent.parent / "fonts" / name,       # bundled in package
        Path("/usr/share/fonts/truetype/dejavu") / name,     # Debian/Ubuntu
        Path("/usr/share/fonts/dejavu") / name,              # Fedora/RHEL
        Path("/usr/share/fonts/TTF") / name,                 # Arch
        Path("C:/Windows/Fonts") / name,                     # Windows
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"DejaVu font '{name}' not found. "
        "Place it in src/agent_cv/fonts/ or install 'fonts-dejavu-core'."
    )


def _fpdf2_context_to_pdf(ctx: dict) -> bytes:
    """Render the Europass CV context dict to PDF bytes using fpdf2."""
    from fpdf import FPDF

    # ---- Layout constants (all in mm) -----------------------------------
    SIDEBAR_W = 62
    SB_PAD_L = 8
    SB_PAD_R = 6
    SB_TEXT_W = SIDEBAR_W - SB_PAD_L - SB_PAD_R
    CONTENT_X = SIDEBAR_W + 8
    CONTENT_W = 210 - CONTENT_X - 8
    PAGE_BREAK_Y = 272

    # ---- Colours --------------------------------------------------------
    BLUE = (0, 51, 153)
    LIGHT_BLUE = (170, 196, 255)
    WHITE = (255, 255, 255)
    DARK = (51, 51, 51)
    GREY = (100, 100, 100)

    labels = ctx.get("labels") or {}

    # ---- FPDF subclass: draw blue sidebar background on every page ------
    class _CV(FPDF):
        def header(self):
            self.set_fill_color(*BLUE)
            self.rect(0, 0, SIDEBAR_W, 297, style="F")

    pdf = _CV(format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_font("DejaVu",  "",  str(_find_dejavu_font("DejaVuSans.ttf")))
    pdf.add_font("DejaVu",  "B", str(_find_dejavu_font("DejaVuSans-Bold.ttf")))
    pdf.add_font("DejaVu",  "I", str(_find_dejavu_font("DejaVuSans-Oblique.ttf")))
    pdf.add_page()

    # ================================================================
    # SIDEBAR
    # ================================================================
    # EU logo
    pdf.set_xy(SB_PAD_L, 10)
    pdf.set_font("DejaVu", "", 7)
    pdf.set_text_color(*LIGHT_BLUE)
    pdf.cell(SB_TEXT_W, 4, "europass")
    pdf.set_xy(SB_PAD_L, 14)
    pdf.set_font("DejaVu", "B", 8)
    pdf.set_text_color(*WHITE)
    pdf.cell(SB_TEXT_W, 4, "Curriculum Vitae")

    sb_y = 22.0

    # Name
    full_name = f"{ctx.get('first_name', '')} {ctx.get('surname', '')}".strip()
    if full_name:
        pdf.set_xy(SB_PAD_L, sb_y)
        pdf.set_font("DejaVu", "B", 10)
        pdf.set_text_color(*WHITE)
        pdf.multi_cell(SB_TEXT_W, 5, full_name)
        sb_y = pdf.get_y() + 2

    # Headline
    if ctx.get("headline"):
        pdf.set_xy(SB_PAD_L, sb_y)
        pdf.set_font("DejaVu", "I", 8)
        pdf.set_text_color(*LIGHT_BLUE)
        pdf.multi_cell(SB_TEXT_W, 4, ctx["headline"])
        sb_y = pdf.get_y() + 3

    def _sb_section(title: str) -> float:
        nonlocal sb_y
        pdf.set_xy(SB_PAD_L, sb_y)
        pdf.set_font("DejaVu", "B", 7)
        pdf.set_text_color(*LIGHT_BLUE)
        pdf.cell(SB_TEXT_W, 4, title.upper())
        line_y = sb_y + 4.5
        pdf.set_draw_color(*LIGHT_BLUE)
        pdf.line(SB_PAD_L, line_y, SIDEBAR_W - SB_PAD_R, line_y)
        return line_y + 1.5

    def _sb_item(text: str, y: float) -> float:
        pdf.set_xy(SB_PAD_L, y)
        pdf.set_font("DejaVu", "", 7.5)
        pdf.set_text_color(*WHITE)
        pdf.multi_cell(SB_TEXT_W, 4, str(text))
        return pdf.get_y() + 0.5

    # Contact section
    sb_y = _sb_section(labels.get("contact", "Contact"))
    for val in [ctx.get("address"), ctx.get("phone"), ctx.get("email"), ctx.get("website")]:
        if val and sb_y < 250:
            sb_y = _sb_item(val, sb_y)

    # Personal info section
    if sb_y < 240:
        sb_y += 2
        sb_y = _sb_section(labels.get("personal", "Personal info"))
        for lbl_key, val in [
            ("dob", ctx.get("birthdate")),
            ("nationality", ctx.get("nationality")),
            ("mother_tongue", ctx.get("mother_tongue")),
        ]:
            if val and sb_y < 255:
                sb_y = _sb_item(f"{labels.get(lbl_key, lbl_key)}: {val}", sb_y)
        if ctx.get("driving_licences") and sb_y < 258:
            sb_y = _sb_item(
                f"{labels.get('driving', 'Driving')}: {', '.join(ctx['driving_licences'])}",
                sb_y,
            )

    # Footer
    pdf.set_xy(SB_PAD_L, 284)
    pdf.set_font("DejaVu", "", 6)
    pdf.set_text_color(*LIGHT_BLUE)
    pdf.cell(SB_TEXT_W, 4, labels.get("generated_with", "Agent CV"))

    # ================================================================
    # MAIN CONTENT
    # ================================================================
    cy = 12.0  # current Y position in main content

    def _check_page_break(needed: float = 20) -> None:
        nonlocal cy
        if cy + needed > PAGE_BREAK_Y:
            pdf.add_page()
            cy = 12.0

    def _section_header(title: str) -> None:
        nonlocal cy
        _check_page_break(10)
        pdf.set_xy(CONTENT_X, cy)
        pdf.set_fill_color(*BLUE)
        pdf.set_text_color(*WHITE)
        pdf.set_font("DejaVu", "B", 8.5)
        pdf.cell(CONTENT_W, 6, f"  {title}", fill=True, ln=True)
        cy += 8

    def _timeline_entry(entry: dict, date_w: float = 26) -> None:
        nonlocal cy
        _check_page_break(15)
        start_y = cy
        detail_x = CONTENT_X + date_w + 2
        detail_w = CONTENT_W - date_w - 2

        # Date column (left)
        from_d = entry.get("from_date") or ""
        is_cur = entry.get("is_current", False)
        to_d = labels.get("present", "Present") if is_cur else (entry.get("to_date") or "")
        date_str = f"{from_d}\n{to_d}" if (from_d and to_d and from_d != to_d) else (from_d or to_d)
        pdf.set_xy(CONTENT_X, start_y)
        pdf.set_font("DejaVu", "", 7.5)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(date_w, 4, date_str)
        date_end_y = pdf.get_y()

        # Detail column (right)
        det_y = start_y
        position = entry.get("position") or entry.get("title") or ""
        if position:
            pdf.set_xy(detail_x, det_y)
            pdf.set_font("DejaVu", "B", 8.5)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(detail_w, 4.5, position)
            det_y = pdf.get_y()

        employer = entry.get("employer_name") or entry.get("org_name") or ""
        if employer:
            city = entry.get("employer_city") or entry.get("org_city") or ""
            country = entry.get("employer_country") or entry.get("org_country") or ""
            loc = ", ".join(p for p in [city, country] if p)
            org_line = f"{employer}{', ' + loc if loc else ''}"
            pdf.set_xy(detail_x, det_y)
            pdf.set_font("DejaVu", "I", 8)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(detail_w, 4, org_line)
            det_y = pdf.get_y()

        activities = entry.get("activities") or ""
        if activities:
            pdf.set_xy(detail_x, det_y)
            pdf.set_font("DejaVu", "", 8)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(detail_w, 4, activities)
            det_y = pdf.get_y()

        cy = max(det_y, date_end_y) + 3

    # Work Experience
    if ctx.get("work_experience"):
        _section_header(labels.get("work_experience", "Work Experience"))
        for entry in ctx["work_experience"]:
            _timeline_entry(entry)

    # Education
    if ctx.get("education"):
        cy += 2
        _section_header(labels.get("education", "Education and Training"))
        for entry in ctx["education"]:
            _timeline_entry(entry)

    # Language Skills
    if ctx.get("foreign_languages"):
        cy += 2
        _section_header(labels.get("language_skills", "Language Skills"))
        col_w = CONTENT_W / 6
        col_labels = [
            labels.get("language", "Language"),
            labels.get("listening", "Listening"),
            labels.get("reading", "Reading"),
            labels.get("spoken_interaction", "Spoken int."),
            labels.get("spoken_production", "Spoken prod."),
            labels.get("writing", "Writing"),
        ]
        _check_page_break(8)
        pdf.set_xy(CONTENT_X, cy)
        pdf.set_fill_color(230, 235, 255)
        pdf.set_text_color(*DARK)
        pdf.set_font("DejaVu", "B", 7)
        for lbl in col_labels:
            pdf.cell(col_w, 5, lbl, border="B", fill=True)
        cy += 6

        for lang in ctx["foreign_languages"]:
            _check_page_break(6)
            pdf.set_xy(CONTENT_X, cy)
            pdf.set_font("DejaVu", "", 7.5)
            pdf.set_text_color(*DARK)
            row_vals = [
                lang.get("label") or lang.get("code") or "",
                lang.get("listening") or "–",
                lang.get("reading") or "–",
                lang.get("spoken_interaction") or "–",
                lang.get("spoken_production") or "–",
                lang.get("writing") or "–",
            ]
            for val in row_vals:
                pdf.cell(col_w, 5, val)
            cy += 6

        if labels.get("cef_note"):
            _check_page_break(8)
            pdf.set_xy(CONTENT_X, cy)
            pdf.set_font("DejaVu", "I", 6.5)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(CONTENT_W, 3.5, labels["cef_note"])
            cy = pdf.get_y() + 1

    # Communication / Digital / Other Skills
    for skill_key, label_key in [
        ("communication_skills", "communication_skills"),
        ("computer_skills", "computer_skills"),
        ("other_skills", "other_skills"),
    ]:
        text = ctx.get(skill_key) or ""
        if text:
            cy += 2
            _section_header(labels.get(label_key, label_key))
            pdf.set_xy(CONTENT_X, cy)
            pdf.set_font("DejaVu", "", 8)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(CONTENT_W, 4, text)
            cy = pdf.get_y() + 2

    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# UI label dictionaries (keyed by ISO 639-1 code, fallback to English)
# ---------------------------------------------------------------------------

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "contact": "Contact",
        "personal": "Personal info",
        "dob": "Date of birth",
        "nationality": "Nationality",
        "mother_tongue": "Mother tongue",
        "driving": "Driving licence",
        "work_experience": "Work Experience",
        "education": "Education and Training",
        "language_skills": "Language Skills",
        "language": "Language",
        "listening": "Listening",
        "reading": "Reading",
        "spoken_interaction": "Spoken interaction",
        "spoken_production": "Spoken production",
        "writing": "Writing",
        "cef_note": "Levels: A1/A2: Basic – B1/B2: Independent – C1/C2: Proficient (Common European Framework of Reference for Languages)",
        "communication_skills": "Communication Skills",
        "computer_skills": "Digital Skills",
        "other_skills": "Other Skills",
        "present": "Present",
        "generated_with": "Generated with Agent CV",
    },
    "pt": {
        "contact": "Contacto",
        "personal": "Dados pessoais",
        "dob": "Data de nascimento",
        "nationality": "Nacionalidade",
        "mother_tongue": "Língua materna",
        "driving": "Carta de condução",
        "work_experience": "Experiência Profissional",
        "education": "Educação e Formação",
        "language_skills": "Competências Linguísticas",
        "language": "Idioma",
        "listening": "Compreensão oral",
        "reading": "Leitura",
        "spoken_interaction": "Interação oral",
        "spoken_production": "Produção oral",
        "writing": "Escrita",
        "cef_note": "Níveis: A1/A2: Utilizador elementar – B1/B2: Utilizador independente – C1/C2: Utilizador proficiente (Quadro Europeu Comum de Referência para as Línguas)",
        "communication_skills": "Competências de Comunicação",
        "computer_skills": "Competências Digitais",
        "other_skills": "Outras Competências",
        "present": "Atual",
        "generated_with": "Gerado com Agent CV",
    },
    "es": {
        "contact": "Contacto",
        "personal": "Información personal",
        "dob": "Fecha de nacimiento",
        "nationality": "Nacionalidad",
        "mother_tongue": "Lengua materna",
        "driving": "Permiso de conducir",
        "work_experience": "Experiencia Laboral",
        "education": "Educación y Formación",
        "language_skills": "Competencias Lingüísticas",
        "language": "Idioma",
        "listening": "Comprensión oral",
        "reading": "Lectura",
        "spoken_interaction": "Interacción oral",
        "spoken_production": "Producción oral",
        "writing": "Escritura",
        "cef_note": "Niveles: A1/A2: Básico – B1/B2: Independiente – C1/C2: Competente (Marco Común Europeo de Referencia para las Lenguas)",
        "communication_skills": "Competencias de Comunicación",
        "computer_skills": "Competencias Digitales",
        "other_skills": "Otras Competencias",
        "present": "Actualidad",
        "generated_with": "Generado con Agent CV",
    },
    "fr": {
        "contact": "Contact",
        "personal": "Informations personnelles",
        "dob": "Date de naissance",
        "nationality": "Nationalité",
        "mother_tongue": "Langue maternelle",
        "driving": "Permis de conduire",
        "work_experience": "Expérience Professionnelle",
        "education": "Éducation et Formation",
        "language_skills": "Compétences Linguistiques",
        "language": "Langue",
        "listening": "Compréhension de l'oral",
        "reading": "Lecture",
        "spoken_interaction": "Interaction orale",
        "spoken_production": "Production orale",
        "writing": "Écriture",
        "cef_note": "Niveaux : A1/A2 : Débutant – B1/B2 : Indépendant – C1/C2 : Expérimenté (Cadre européen commun de référence pour les langues)",
        "communication_skills": "Compétences en Communication",
        "computer_skills": "Compétences Numériques",
        "other_skills": "Autres Compétences",
        "present": "Présent",
        "generated_with": "Généré avec Agent CV",
    },
    "de": {
        "contact": "Kontakt",
        "personal": "Persönliche Daten",
        "dob": "Geburtsdatum",
        "nationality": "Nationalität",
        "mother_tongue": "Muttersprache",
        "driving": "Führerschein",
        "work_experience": "Berufserfahrung",
        "education": "Bildung und Ausbildung",
        "language_skills": "Sprachkenntnisse",
        "language": "Sprache",
        "listening": "Hörverstehen",
        "reading": "Lesen",
        "spoken_interaction": "Mündliche Interaktion",
        "spoken_production": "Mündliche Produktion",
        "writing": "Schreiben",
        "cef_note": "Niveaus: A1/A2: Grundlagen – B1/B2: Selbstständig – C1/C2: Kompetent (Gemeinsamer Europäischer Referenzrahmen für Sprachen)",
        "communication_skills": "Kommunikationskompetenzen",
        "computer_skills": "Digitale Kompetenzen",
        "other_skills": "Sonstige Kompetenzen",
        "present": "Heute",
        "generated_with": "Erstellt mit Agent CV",
    },
}

_LANG_NAMES: dict[str, str] = {
    "en": "English",
    "pt": "Portuguese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "cs": "Czech",
    "ro": "Romanian",
    "hu": "Hungarian",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "el": "Greek",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
}


def _get_labels(lang: str) -> dict[str, str]:
    return _LABELS.get(lang, _LABELS["en"])


# ---------------------------------------------------------------------------
# 1. Fetch CV data from DB
# ---------------------------------------------------------------------------

def _fetch_cv_data(employee_name: str) -> dict:
    """
    Returns a dict with keys:
      employee_id, full_name, department, role, country, primary_language,
      cv_text (concatenated section text),
      certifications (list of cert dicts)
    """
    from agent_cv.db.connection import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Fuzzy-match the employee name
            cur.execute(
                """
                SELECT employee_id, full_name, department, role, country, primary_language
                FROM employees
                WHERE active_flag = true
                  AND full_name ILIKE %s
                ORDER BY full_name
                LIMIT 1
                """,
                (f"%{employee_name.strip()}%",),
            )
            emp = cur.fetchone()
            if not emp:
                return {}

            # Fetch the most recent CV section text
            cur.execute(
                """
                SELECT cs.section_text
                FROM cv_sections cs
                JOIN document_versions dv ON dv.document_version_id = cs.document_version_id
                WHERE cs.employee_id = %s
                  AND dv.is_current = true
                ORDER BY dv.extracted_at DESC NULLS LAST
                LIMIT 5
                """,
                (emp["employee_id"],),
            )
            sections = cur.fetchall()
            cv_text = "\n\n".join(r["section_text"] for r in sections if r["section_text"])

            # Fetch certifications
            cur.execute(
                """
                SELECT c.cert_name, c.cert_code, c.issue_date, c.expiry_date, c.status,
                       v.vendor_name
                FROM certifications c
                LEFT JOIN vendors v ON v.vendor_id = c.vendor_id
                WHERE c.employee_id = %s
                ORDER BY c.issue_date DESC NULLS LAST
                """,
                (emp["employee_id"],),
            )
            certs = [dict(r) for r in cur.fetchall()]

    return {
        "employee_id": str(emp["employee_id"]),
        "full_name": emp["full_name"],
        "department": emp["department"],
        "role": emp["role"],
        "country": emp["country"],
        "primary_language": emp["primary_language"] or "en",
        "cv_text": cv_text,
        "certifications": certs,
    }


# ---------------------------------------------------------------------------
# 2. LLM: extract structure + translate
# ---------------------------------------------------------------------------

_EXTRACTION_SCHEMA = {
    "first_name": "string",
    "surname": "string",
    "headline": "string – current role or job title",
    "address": "string – street, city, postal code, country (single line)",
    "phone": "string",
    "email": "string",
    "website": "string – optional",
    "birthdate": "string – dd/mm/yyyy or empty",
    "nationality": "string",
    "mother_tongue": "string – full language name",
    "driving_licences": "array of strings, e.g. ['A', 'B']",
    "work_experience": [
        {
            "from_date": "string – e.g. 01/2020",
            "to_date": "string – e.g. 03/2023 (empty if current)",
            "is_current": "boolean",
            "position": "string – job title",
            "activities": "string – description of tasks and achievements",
            "employer_name": "string",
            "employer_city": "string",
            "employer_country": "string",
        }
    ],
    "education": [
        {
            "from_date": "string – e.g. 09/2015 (use issue date for certifications; empty if unknown)",
            "to_date": "string – e.g. 06/2019 (empty for point-in-time certifications or if current)",
            "is_current": "boolean",
            "title": "string – degree, qualification name, OR certification/license name",
            "activities": "string – subjects, thesis, notable work, or certification description (optional)",
            "org_name": "string – institution name OR certifying body / vendor name",
            "org_city": "string",
            "org_country": "string",
        }
    ],
    "foreign_languages": [
        {
            "code": "string – ISO 639-1 e.g. 'fr'",
            "label": "string – language name in target language",
            "listening": "string – CEFR level e.g. B2 (empty if unknown)",
            "reading": "string – CEFR level",
            "spoken_interaction": "string – CEFR level",
            "spoken_production": "string – CEFR level",
            "writing": "string – CEFR level",
        }
    ],
    "communication_skills": "string – interpersonal or soft-skills paragraph (do NOT include certifications or technical tools here)",
    "computer_skills": "string – brief paragraph on technical environment, tools, and platforms the person works with (do NOT list certification names here; those belong in 'education')",
    "other_skills": "string – paragraph for hobbies, volunteering, driving licence details, etc. (do NOT include certifications here)",
}


def _extract_and_translate(
    cv_text: str,
    employee_info: dict,
    target_language: str,
) -> dict:
    """
    Single LLM call that:
      - Extracts structured CV fields from raw text
      - Translates all text fields into *target_language*
    Returns a dict matching _EXTRACTION_SCHEMA.
    """
    from openai import AzureOpenAI
    from agent_cv.config import settings

    lang_name = _LANG_NAMES.get(target_language, target_language.upper())

    cert_summary = ""
    if employee_info.get("certifications"):
        lines = [
            f"  - {c['cert_name']}"
            + (f" ({c['vendor_name']})" if c.get("vendor_name") else "")
            + (f", issued {c['issue_date']}" if c.get("issue_date") else "")
            for c in employee_info["certifications"][:20]
        ]
        cert_summary = (
            "Professional certifications on record (each MUST appear as a separate entry "
            "in the 'education' array with org_name = certifying body):\n"
            + "\n".join(lines)
        )

    system_prompt = (
        f"You are an expert CV analyst and professional translator. "
        f"Your task is to extract structured information from a raw CV text "
        f"and translate ALL text fields into {lang_name} ({target_language}).\n\n"
        f"Return ONLY a valid JSON object matching the schema below. "
        f"Do not include any explanation or markdown fences. "
        f"Leave a field as an empty string or empty array if the information is not present in the CV.\n\n"
        f"IMPORTANT RULES:\n"
        f"- ALL professional certifications, licenses, and vendor qualifications (e.g. CCNP, CCNA, "
        f"Fortinet NSE, AZ-900, ITIL, ISO certifications, Red Hat, AWS, etc.) MUST be placed as "
        f"individual entries in the 'education' array, NOT in computer_skills or other_skills.\n"
        f"- Use org_name for the certifying body (e.g. 'Cisco', 'Microsoft', 'Fortinet').\n"
        f"- Use from_date for the issue/completion date when available.\n"
        f"- computer_skills should only describe the technical environment and tools, not list cert names.\n"
        f"- other_skills should only contain soft skills, hobbies, or volunteering info.\n\n"
        f"JSON schema:\n{json.dumps(_EXTRACTION_SCHEMA, indent=2, ensure_ascii=False)}"
    )

    user_prompt = (
        f"Employee: {employee_info.get('full_name', '')}\n"
        f"Department: {employee_info.get('department') or ''}\n"
        f"Role/Title: {employee_info.get('role') or ''}\n"
        f"Country: {employee_info.get('country') or ''}\n\n"
        f"Raw CV text:\n{cv_text[:12000]}\n\n"
        f"{cert_summary}\n\n"
        f"Extract and translate all fields to {lang_name}."
    )

    client = AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )

    response = client.chat.completions.create(
        model=settings.azure_openai_chat_deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    # Strip markdown fences if the model ignores the instruction
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 3. Build Europass XML v4.0
# ---------------------------------------------------------------------------

_EP  = "http://www.europass.eu/1.0"
_HR  = "http://www.hr-xml.org/3"
_OA  = "http://www.openapplications.org/oagis/9"
_EU  = "http://www.europass_eures.eu/1.0"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"

# ISO 639-1 → ISO 639-2/T (used by Europass v4 PrimaryLanguageCode)
_ISO2_TO_ISO3: dict[str, str] = {
    "pt": "por", "en": "eng", "fr": "fra", "de": "deu", "es": "spa",
    "it": "ita", "nl": "nld", "pl": "pol", "cs": "ces", "ro": "ron",
    "hu": "hun", "sv": "swe", "da": "dan", "fi": "fin", "el": "ell",
    "bg": "bul", "hr": "hrv", "sk": "slk", "sl": "slv", "lt": "lit",
    "lv": "lav", "et": "est",
}


def _fmt_date(raw: str | None) -> str:
    """Convert 'MM/YYYY' or 'YYYY' to 'YYYY-MM' or 'YYYY' for Europass v4."""
    if not raw:
        return ""
    raw = raw.strip()
    # Already in YYYY-MM or YYYY
    if re.match(r"^\d{4}(-\d{2})?$", raw):
        return raw
    # MM/YYYY
    m = re.match(r"^(\d{1,2})/(\d{4})$", raw)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    # YYYY/MM
    m = re.match(r"^(\d{4})/(\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return raw


def _ep(tag: str) -> str:
    return f"{{{_EP}}}{tag}"


def _hr(tag: str) -> str:
    return f"{{{_HR}}}{tag}"


def _oa(tag: str) -> str:
    return f"{{{_OA}}}{tag}"


def _eu(tag: str) -> str:
    return f"{{{_EU}}}{tag}"


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text:
        el.text = text
    return el


def build_europass_xml(cv_data: dict, target_language: str) -> str:
    """Build a Europass XML v4.0 string from the structured cv_data dict."""
    ET.register_namespace("",    _EP)
    ET.register_namespace("hr",   _HR)
    ET.register_namespace("oa",   _OA)
    ET.register_namespace("eures", _EU)
    ET.register_namespace("xsi",  _XSI)

    root = ET.Element(
        _ep("Candidate"),
        {
            f"{{{_XSI}}}schemaLocation": f"{_EP} Candidate.xsd",
        },
    )

    # DocumentID
    doc_id = _sub(root, _hr("DocumentID"))
    doc_id.set("schemeID", "Test-0001")
    doc_id.set("schemeName", "DocumentIdentifier")
    doc_id.set("schemeAgencyName", "EUROPASS")
    doc_id.set("schemeVersionID", "4.0")

    # CandidateSupplier
    supplier = _sub(root, _ep("CandidateSupplier"))
    party_id = _sub(supplier, _hr("PartyID"))
    party_id.set("schemeID", "Test-0001")
    party_id.set("schemeName", "PartyID")
    party_id.set("schemeAgencyName", "EUROPASS")
    party_id.set("schemeVersionID", "1.0")
    _sub(supplier, _hr("PartyName"), "Owner")
    sup_contact = _sub(supplier, _ep("PersonContact"))
    sup_name = _sub(sup_contact, _ep("PersonName"))
    _sub(sup_name, _oa("GivenName"), cv_data.get("first_name") or "")
    _sub(sup_name, _hr("FamilyName"), cv_data.get("surname") or "")
    _sub(supplier, _hr("PrecedenceCode"), "1")

    # CandidatePerson
    person = _sub(root, _ep("CandidatePerson"))
    person_name = _sub(person, _ep("PersonName"))
    _sub(person_name, _oa("GivenName"), cv_data.get("first_name") or "")
    _sub(person_name, _hr("FamilyName"), cv_data.get("surname") or "")
    birth_el = _sub(person, _hr("BirthDate"))
    if cv_data.get("birthdate"):
        birth_el.text = cv_data["birthdate"]
    # PrimaryLanguageCode: use ISO 639-2/T of the *source* language
    src_lang = cv_data.get("_source_language") or "en"
    iso3 = _ISO2_TO_ISO3.get(src_lang, src_lang)
    lang_code_el = _sub(person, _ep("PrimaryLanguageCode"), iso3)
    lang_code_el.set("name", "NORMAL")

    # CandidateProfile
    profile = _sub(root, _ep("CandidateProfile"))
    profile.set("languageCode", target_language)
    profile_id = _sub(profile, _hr("ID"))
    profile_id.set("schemeID", "Test-0001")
    profile_id.set("schemeName", "CandidateProfileID")
    profile_id.set("schemeAgencyName", "EUROPASS")
    profile_id.set("schemeVersionID", "1.0")

    # EmploymentHistory
    work_list = cv_data.get("work_experience") or []
    if work_list:
        emp_hist = _sub(profile, _ep("EmploymentHistory"))
        for w in work_list:
            emp_entry = _sub(emp_hist, _ep("EmployerHistory"))
            _sub(emp_entry, _hr("OrganizationName"), w.get("employer_name") or "")
            if w.get("employer_city") or w.get("employer_country"):
                org_contact = _sub(emp_entry, _ep("OrganizationContact"))
                comm = _sub(org_contact, _ep("Communication"))
                addr = _sub(comm, _ep("Address"))
                if w.get("employer_city"):
                    _sub(addr, _oa("CityName"), w["employer_city"])
                if w.get("employer_country"):
                    _sub(addr, _ep("CountryCode"), w["employer_country"])
            pos_hist = _sub(emp_entry, _ep("PositionHistory"))
            pos_title = _sub(pos_hist, _ep("PositionTitle"), w.get("position") or "")
            pos_title.set("typeCode", "FREETEXT")
            period = _sub(pos_hist, _eu("EmploymentPeriod"))
            start_d = _fmt_date(w.get("from_date"))
            if start_d:
                start_el = _sub(period, _eu("StartDate"))
                _sub(start_el, _hr("FormattedDateTime"), start_d)
            if w.get("is_current"):
                _sub(period, _hr("CurrentIndicator"), "true")
            else:
                end_d = _fmt_date(w.get("to_date"))
                if end_d:
                    end_el = _sub(period, _eu("EndDate"))
                    _sub(end_el, _hr("FormattedDateTime"), end_d)
                _sub(period, _hr("CurrentIndicator"), "false")
            if w.get("activities"):
                _sub(pos_hist, _oa("Description"), w["activities"])
            if w.get("employer_city"):
                _sub(pos_hist, _ep("City"), w["employer_city"])
            if w.get("employer_country"):
                _sub(pos_hist, _ep("Country"), w["employer_country"])

    # EducationHistory
    edu_list = cv_data.get("education") or []
    if edu_list:
        edu_hist = _sub(profile, _ep("EducationHistory"))
        for e in edu_list:
            att = _sub(edu_hist, _ep("EducationOrganizationAttendance"))
            _sub(att, _hr("OrganizationName"), e.get("org_name") or "")
            if e.get("org_city") or e.get("org_country"):
                org_contact = _sub(att, _ep("OrganizationContact"))
                comm = _sub(org_contact, _ep("Communication"))
                addr = _sub(comm, _ep("Address"))
                if e.get("org_city"):
                    _sub(addr, _oa("CityName"), e["org_city"])
                if e.get("org_country"):
                    _sub(addr, _ep("CountryCode"), e["org_country"])
            att_period = _sub(att, _ep("AttendancePeriod"))
            start_d = _fmt_date(e.get("from_date"))
            if start_d:
                start_el = _sub(att_period, _ep("StartDate"))
                _sub(start_el, _hr("FormattedDateTime"), start_d)
            if e.get("is_current"):
                _sub(att_period, _ep("Ongoing"), "true")
            else:
                end_d = _fmt_date(e.get("to_date"))
                if end_d:
                    end_el = _sub(att_period, _ep("EndDate"))
                    _sub(end_el, _hr("FormattedDateTime"), end_d)
                _sub(att_period, _ep("Ongoing"), "false")
            degree = _sub(att, _ep("EducationDegree"))
            _sub(degree, _hr("DegreeName"), e.get("title") or "")

    # PersonQualifications: foreign languages
    foreign_langs = cv_data.get("foreign_languages") or []
    if foreign_langs:
        _sub(profile, _ep("eures:Licenses"))  # required placeholder
        _sub(profile, _ep("Certifications"))  # required placeholder
        _sub(profile, _ep("PublicationHistory"))  # required placeholder
        quals = _sub(profile, _ep("PersonQualifications"))
        _CEF_DIMS = [
            ("listening",          "CEF-Understanding-Listening"),
            ("reading",            "CEF-Understanding-Reading"),
            ("spoken_interaction", "CEF-Speaking-Interaction"),
            ("spoken_production",  "CEF-Speaking-Production"),
            ("writing",            "CEF-Writing-Production"),
        ]
        for lang in foreign_langs:
            comp = _sub(quals, _ep("PersonCompetency"))
            comp_id = _sub(comp, _ep("CompetencyID"), lang.get("label") or lang.get("code") or "")
            comp_id.set("schemeName", "FREE_TEXT")
            _sub(comp, _hr("TaxonomyID"), "language")
            for field_key, dim_code in _CEF_DIMS:
                level = lang.get(field_key) or ""
                if level:
                    dim = _sub(comp, _eu("CompetencyDimension"))
                    _sub(dim, _hr("CompetencyDimensionTypeCode"), dim_code)
                    score = _sub(dim, _eu("Score"))
                    _sub(score, _hr("ScoreText"), level)

    # RenderingInformation
    rendering = _sub(root, _ep("RenderingInformation"))
    design = _sub(rendering, _ep("Design"))
    _sub(design, _ep("Template"), "Template1")
    _sub(design, _ep("Color"), "Default")
    _sub(design, _ep("FontSize"), "Medium")
    _sub(design, _ep("Logo"), "None")
    _sub(design, _ep("PageNumbers"), "false")
    sections_order = _sub(design, _ep("SectionsOrder"))
    for sec_name in ("work-experience", "education-training", "language"):
        sec = _sub(sections_order, _ep("Section"))
        _sub(sec, _ep("Title"), sec_name)

    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
        root, encoding="unicode", xml_declaration=False
    )


# ---------------------------------------------------------------------------
# 4. Render PDF from XML (via fpdf2)
# ---------------------------------------------------------------------------

def _xml_to_template_context(xml_str: str, cv_data: dict, target_language: str) -> dict:
    """
    Parse the Europass v4.0 XML and assemble the template context dict.
    Falls back to cv_data values when XML fields are empty.
    """
    ns = {"ep": _EP, "hr": _HR, "oa": _OA, "eures": _EU}
    root = ET.fromstring(xml_str if xml_str.startswith("<?xml") else f'<?xml version="1.0" encoding="UTF-8"?>{xml_str}')

    def _text(element: ET.Element | None) -> str:
        return (element.text or "").strip() if element is not None else ""

    profile = root.find(".//ep:CandidateProfile", ns)

    def _ptext(path: str) -> str:
        if profile is None:
            return ""
        el = profile.find(path, ns)
        return _text(el)

    ctx: dict = {
        "locale": target_language,
        "labels": _get_labels(target_language),
        "photo_data": None,
        "first_name": _text(root.find(".//ep:CandidatePerson/ep:PersonName/oa:GivenName", ns)) or cv_data.get("first_name", ""),
        "surname": _text(root.find(".//ep:CandidatePerson/ep:PersonName/hr:FamilyName", ns)) or cv_data.get("surname", ""),
        "headline": cv_data.get("headline", ""),
        "address": cv_data.get("address", ""),
        "email": cv_data.get("email", ""),
        "phone": cv_data.get("phone", ""),
        "website": cv_data.get("website", ""),
        "birthdate": _text(root.find(".//ep:CandidatePerson/hr:BirthDate", ns)) or cv_data.get("birthdate", ""),
        "nationality": cv_data.get("nationality", ""),
        "mother_tongue": cv_data.get("mother_tongue", ""),
        "driving_licences": cv_data.get("driving_licences") or [],
        "work_experience": [],
        "education": [],
        "foreign_languages": [],
        "communication_skills": cv_data.get("communication_skills", ""),
        "computer_skills": cv_data.get("computer_skills", ""),
        "other_skills": cv_data.get("other_skills", ""),
    }

    # Work experience (v4.0: EmploymentHistory/EmployerHistory/PositionHistory)
    if profile is not None:
        for eh in profile.findall("ep:EmploymentHistory/ep:EmployerHistory", ns):
            ph = eh.find("ep:PositionHistory", ns)
            if ph is None:
                continue
            period = ph.find("eures:EmploymentPeriod", ns)
            from_dt = _text(period.find("eures:StartDate/hr:FormattedDateTime", ns)) if period is not None else ""
            to_dt   = _text(period.find("eures:EndDate/hr:FormattedDateTime", ns)) if period is not None else ""
            curr    = _text(period.find("hr:CurrentIndicator", ns)).lower() == "true" if period is not None else False
            ctx["work_experience"].append({
                "from_date": from_dt,
                "to_date": to_dt,
                "is_current": curr,
                "position": _text(ph.find("ep:PositionTitle", ns)),
                "activities": _text(ph.find("oa:Description", ns)),
                "employer_name": _text(eh.find("hr:OrganizationName", ns)),
                "employer_city": _text(eh.find("ep:OrganizationContact/ep:Communication/ep:Address/oa:CityName", ns)),
                "employer_country": _text(eh.find("ep:OrganizationContact/ep:Communication/ep:Address/ep:CountryCode", ns)),
            })

        # Education (v4.0: EducationHistory/EducationOrganizationAttendance)
        for att in profile.findall("ep:EducationHistory/ep:EducationOrganizationAttendance", ns):
            ap = att.find("ep:AttendancePeriod", ns)
            from_dt = _text(ap.find("ep:StartDate/hr:FormattedDateTime", ns)) if ap is not None else ""
            to_dt   = _text(ap.find("ep:EndDate/hr:FormattedDateTime", ns)) if ap is not None else ""
            ongoing = _text(ap.find("ep:Ongoing", ns)).lower() == "true" if ap is not None else False
            ctx["education"].append({
                "from_date": from_dt,
                "to_date": to_dt,
                "is_current": ongoing,
                "title": _text(att.find("ep:EducationDegree/hr:DegreeName", ns)),
                "activities": "",
                "org_name": _text(att.find("hr:OrganizationName", ns)),
                "org_city": _text(att.find("ep:OrganizationContact/ep:Communication/ep:Address/oa:CityName", ns)),
                "org_country": _text(att.find("ep:OrganizationContact/ep:Communication/ep:Address/ep:CountryCode", ns)),
            })

        # Foreign languages (v4.0: PersonQualifications/PersonCompetency)
        _DIM_MAP = {
            "CEF-Understanding-Listening": "listening",
            "CEF-Understanding-Reading":   "reading",
            "CEF-Speaking-Interaction":    "spoken_interaction",
            "CEF-Speaking-Production":     "spoken_production",
            "CEF-Writing-Production":      "writing",
        }
        for comp in profile.findall("ep:PersonQualifications/ep:PersonCompetency", ns):
            if _text(comp.find("hr:TaxonomyID", ns)) != "language":
                continue
            lang_entry: dict = {
                "code": "",
                "label": _text(comp.find("ep:CompetencyID", ns)),
                "listening": "", "reading": "",
                "spoken_interaction": "", "spoken_production": "", "writing": "",
            }
            for dim in comp.findall("eures:CompetencyDimension", ns):
                dim_type = _text(dim.find("hr:CompetencyDimensionTypeCode", ns))
                score    = _text(dim.find("eures:Score/hr:ScoreText", ns))
                field    = _DIM_MAP.get(dim_type)
                if field:
                    lang_entry[field] = score
            ctx["foreign_languages"].append(lang_entry)

    # Fallback: if XML parsing produced no work_experience, use cv_data directly
    if not ctx["work_experience"]:
        ctx["work_experience"] = cv_data.get("work_experience") or []
    if not ctx["education"]:
        ctx["education"] = cv_data.get("education") or []
    if not ctx["foreign_languages"]:
        ctx["foreign_languages"] = cv_data.get("foreign_languages") or []

    return ctx


def render_pdf_from_xml(xml_str: str, cv_data: dict, target_language: str) -> bytes:
    """Parse Europass XML and render to PDF bytes using fpdf2."""
    ctx = _xml_to_template_context(xml_str, cv_data, target_language)
    return _fpdf2_context_to_pdf(ctx)


# ---------------------------------------------------------------------------
# 5. Orchestrator
# ---------------------------------------------------------------------------

def translate_and_export_cv(
    employee_name: str,
    target_language: str,
) -> dict:
    """
    Full pipeline: fetch → LLM extraction+translation → XML → PDF.

    Returns:
      {
        "export_id": str,         # UUID, use with /exports/{export_id}
        "employee_name": str,     # matched full name
        "target_language": str,   # ISO 639-1 code
        "error": str | None,      # present only on failure
      }
    """
    target_language = (target_language or "en").lower().strip()[:5]

    # 1. Fetch DB data
    db_data = _fetch_cv_data(employee_name)
    if not db_data:
        return {"error": f"No employee found matching '{employee_name}'"}

    cv_text = db_data.get("cv_text") or ""
    if not cv_text:
        return {"error": f"No CV text found for '{db_data['full_name']}'"}

    # 2. Extract + translate via LLM
    try:
        cv_data = _extract_and_translate(cv_text, db_data, target_language)
        # Carry source language so build_europass_xml can set PrimaryLanguageCode
        cv_data["_source_language"] = db_data.get("primary_language") or "pt"
    except Exception as exc:
        logger.exception("translate_and_export_cv: LLM extraction failed")
        return {"error": f"Translation failed: {exc}"}

    # 3. Build Europass XML
    try:
        xml_str = build_europass_xml(cv_data, target_language)
    except Exception as exc:
        logger.exception("translate_and_export_cv: XML build failed")
        return {"error": f"XML generation failed: {exc}"}

    # 4. Render PDF
    try:
        pdf_bytes = render_pdf_from_xml(xml_str, cv_data, target_language)
    except Exception as exc:
        logger.exception("translate_and_export_cv: PDF render failed")
        return {"error": f"PDF rendering failed: {exc}"}

    # 5. Embed Europass XML as attachment.xml inside the PDF
    try:
        import io
        from pypdf import PdfReader, PdfWriter
        xml_bytes = xml_str.encode("utf-8")
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        writer.append(reader)
        writer.add_attachment("attachment.xml", xml_bytes)
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()
    except Exception:
        logger.exception("translate_and_export_cv: failed to embed XML attachment — saving PDF without it")

    # 6. Save to exports dir
    export_id = str(uuid.uuid4())
    exports_dir = "/tmp/agent_cv_exports"
    os.makedirs(exports_dir, exist_ok=True)
    pdf_path = os.path.join(exports_dir, f"{export_id}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    logger.info(
        "translate_and_export_cv: PDF saved — employee=%s lang=%s export_id=%s size=%d bytes",
        db_data["full_name"],
        target_language,
        export_id,
        len(pdf_bytes),
    )

    return {
        "export_id": export_id,
        "employee_name": db_data["full_name"],
        "target_language": target_language,
    }
