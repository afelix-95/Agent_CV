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
from datetime import datetime, timezone
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

    # Photo placeholder
    pdf.set_fill_color(26, 77, 179)
    pdf.set_draw_color(*LIGHT_BLUE)
    pdf.rect(SB_PAD_L, 20, 36, 36, style="FD")
    pdf.set_font("DejaVu", "", 6.5)
    pdf.set_text_color(*LIGHT_BLUE)
    pdf.set_xy(SB_PAD_L, 35)
    pdf.cell(36, 4, "Photo", align="C")

    sb_y = 62.0

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
        pdf.cell(CONTENT_W, 6, f"  {title}", fill=True)
        cy = pdf.get_y() + 2

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
        cy = pdf.get_y() + 1

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
            cy = pdf.get_y() + 1

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
            _check_page_break(10)
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
            "from_date": "string – e.g. 09/2015",
            "to_date": "string – e.g. 06/2019 (empty if current)",
            "is_current": "boolean",
            "title": "string – degree or qualification name",
            "activities": "string – subjects, thesis, notable work (optional)",
            "org_name": "string – institution name",
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
    "communication_skills": "string – paragraph",
    "computer_skills": "string – paragraph",
    "other_skills": "string – paragraph (hobbies, volunteering, etc.)",
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
        cert_summary = "Professional certifications on record:\n" + "\n".join(lines)

    system_prompt = (
        f"You are an expert CV analyst and professional translator. "
        f"Your task is to extract structured information from a raw CV text "
        f"and translate ALL text fields into {lang_name} ({target_language}).\n\n"
        f"Return ONLY a valid JSON object matching the schema below. "
        f"Do not include any explanation or markdown fences. "
        f"Leave a field as an empty string or empty array if the information is not present in the CV.\n\n"
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
# 3. Build Europass XML v3.3.0
# ---------------------------------------------------------------------------

_NS = "http://europass.cedefop.europa.eu/Europass"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_SCHEMA_LOC = (
    "http://europass.cedefop.europa.eu/Europass "
    "http://europass.cedefop.europa.eu/xml/v3.3.0/EuropassSchema.xsd"
)


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text:
        el.text = text
    return el


def build_europass_xml(cv_data: dict, target_language: str) -> str:
    """Build a Europass XML v3.3.0 string from the structured cv_data dict."""
    ET.register_namespace("", _NS)
    ET.register_namespace("xsi", _XSI)

    root = ET.Element(
        f"{{{_NS}}}SkillsPassport",
        {
            f"{{{_XSI}}}schemaLocation": _SCHEMA_LOC,
            "locale": target_language,
        },
    )

    # DocumentInfo
    doc_info = _sub(root, f"{{{_NS}}}DocumentInfo")
    _sub(doc_info, f"{{{_NS}}}DocumentType", "ECV")
    _sub(doc_info, f"{{{_NS}}}CreationDate", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    _sub(doc_info, f"{{{_NS}}}LastUpdateDate", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    _sub(doc_info, f"{{{_NS}}}XSDVersion", "V3.3")
    _sub(doc_info, f"{{{_NS}}}Generator", "AgentCV")
    _sub(doc_info, f"{{{_NS}}}Comment", "Europass CV generated by Agent CV translation feature")

    # LearnerInfo
    learner = _sub(root, f"{{{_NS}}}LearnerInfo")

    # Identification
    ident = _sub(learner, f"{{{_NS}}}Identification")
    name_el = _sub(ident, f"{{{_NS}}}PersonName")
    _sub(name_el, f"{{{_NS}}}FirstName", cv_data.get("first_name") or "")
    _sub(name_el, f"{{{_NS}}}Surname", cv_data.get("surname") or "")

    contact = _sub(ident, f"{{{_NS}}}ContactInfo")
    if cv_data.get("address"):
        addr = _sub(contact, f"{{{_NS}}}Address")
        addr_contact = _sub(addr, f"{{{_NS}}}Contact")
        _sub(addr_contact, f"{{{_NS}}}AddressLine", cv_data["address"])
    if cv_data.get("email"):
        email_el = _sub(contact, f"{{{_NS}}}Email")
        _sub(email_el, f"{{{_NS}}}Contact", cv_data["email"])
    if cv_data.get("phone"):
        tel_list = _sub(contact, f"{{{_NS}}}TelephoneList")
        tel = _sub(tel_list, f"{{{_NS}}}Telephone")
        _sub(tel, f"{{{_NS}}}Contact", cv_data["phone"])

    if cv_data.get("birthdate"):
        demo = _sub(ident, f"{{{_NS}}}Demographics")
        _sub(demo, f"{{{_NS}}}Birthdate").set("value", cv_data["birthdate"])

    # Headline
    if cv_data.get("headline"):
        headline = _sub(learner, f"{{{_NS}}}Headline")
        htype = _sub(headline, f"{{{_NS}}}Type")
        _sub(htype, f"{{{_NS}}}Code", "job_applied_for")
        hdesc = _sub(headline, f"{{{_NS}}}Description")
        _sub(hdesc, f"{{{_NS}}}Label", cv_data["headline"])

    # WorkExperienceList
    work_list = cv_data.get("work_experience") or []
    if work_list:
        we_list = _sub(learner, f"{{{_NS}}}WorkExperienceList")
        for w in work_list:
            we = _sub(we_list, f"{{{_NS}}}WorkExperience")
            period = _sub(we, f"{{{_NS}}}Period")
            if w.get("from_date"):
                _sub(period, f"{{{_NS}}}From").set("value", w["from_date"])
            if w.get("is_current"):
                _sub(period, f"{{{_NS}}}Current", "true")
            elif w.get("to_date"):
                _sub(period, f"{{{_NS}}}To").set("value", w["to_date"])
            if w.get("position"):
                pos = _sub(we, f"{{{_NS}}}Position")
                _sub(pos, f"{{{_NS}}}Label", w["position"])
            if w.get("activities"):
                _sub(we, f"{{{_NS}}}Activities", w["activities"])
            if w.get("employer_name"):
                employer = _sub(we, f"{{{_NS}}}Employer")
                _sub(employer, f"{{{_NS}}}Name", w["employer_name"])
                if w.get("employer_city") or w.get("employer_country"):
                    emp_ci = _sub(employer, f"{{{_NS}}}ContactInfo")
                    emp_addr = _sub(emp_ci, f"{{{_NS}}}Address")
                    emp_contact = _sub(emp_addr, f"{{{_NS}}}Contact")
                    if w.get("employer_city"):
                        _sub(emp_contact, f"{{{_NS}}}Municipality", w["employer_city"])
                    if w.get("employer_country"):
                        country_el = _sub(emp_contact, f"{{{_NS}}}Country")
                        _sub(country_el, f"{{{_NS}}}Label", w["employer_country"])

    # EducationList
    edu_list = cv_data.get("education") or []
    if edu_list:
        ed_list = _sub(learner, f"{{{_NS}}}EducationList")
        for e in edu_list:
            ed = _sub(ed_list, f"{{{_NS}}}Education")
            period = _sub(ed, f"{{{_NS}}}Period")
            if e.get("from_date"):
                _sub(period, f"{{{_NS}}}From").set("value", e["from_date"])
            if e.get("is_current"):
                _sub(period, f"{{{_NS}}}Current", "true")
            elif e.get("to_date"):
                _sub(period, f"{{{_NS}}}To").set("value", e["to_date"])
            if e.get("title"):
                _sub(ed, f"{{{_NS}}}Title", e["title"])
            if e.get("activities"):
                _sub(ed, f"{{{_NS}}}Activities", e["activities"])
            if e.get("org_name"):
                org = _sub(ed, f"{{{_NS}}}Organisation")
                _sub(org, f"{{{_NS}}}Name", e["org_name"])
                if e.get("org_city") or e.get("org_country"):
                    org_ci = _sub(org, f"{{{_NS}}}ContactInfo")
                    org_addr = _sub(org_ci, f"{{{_NS}}}Address")
                    org_contact = _sub(org_addr, f"{{{_NS}}}Contact")
                    if e.get("org_city"):
                        _sub(org_contact, f"{{{_NS}}}Municipality", e["org_city"])
                    if e.get("org_country"):
                        country_el = _sub(org_contact, f"{{{_NS}}}Country")
                        _sub(country_el, f"{{{_NS}}}Label", e["org_country"])

    # Skills
    skills = _sub(learner, f"{{{_NS}}}Skills")
    linguistic = _sub(skills, f"{{{_NS}}}Linguistic")

    if cv_data.get("mother_tongue"):
        mt_list = _sub(linguistic, f"{{{_NS}}}MotherTongueList")
        mt = _sub(mt_list, f"{{{_NS}}}MotherTongue")
        mt_desc = _sub(mt, f"{{{_NS}}}Description")
        _sub(mt_desc, f"{{{_NS}}}Label", cv_data["mother_tongue"])

    foreign_langs = cv_data.get("foreign_languages") or []
    if foreign_langs:
        fl_list = _sub(linguistic, f"{{{_NS}}}ForeignLanguageList")
        for lang in foreign_langs:
            fl = _sub(fl_list, f"{{{_NS}}}ForeignLanguage")
            fl_desc = _sub(fl, f"{{{_NS}}}Description")
            if lang.get("code"):
                _sub(fl_desc, f"{{{_NS}}}Code", lang["code"])
            _sub(fl_desc, f"{{{_NS}}}Label", lang.get("label") or lang.get("code") or "")
            prof = _sub(fl, f"{{{_NS}}}ProficiencyLevel")
            _sub(prof, f"{{{_NS}}}Listening", lang.get("listening") or "")
            _sub(prof, f"{{{_NS}}}Reading", lang.get("reading") or "")
            _sub(prof, f"{{{_NS}}}SpokenInteraction", lang.get("spoken_interaction") or "")
            _sub(prof, f"{{{_NS}}}SpokenProduction", lang.get("spoken_production") or "")
            _sub(prof, f"{{{_NS}}}Writing", lang.get("writing") or "")

    if cv_data.get("communication_skills"):
        comm = _sub(skills, f"{{{_NS}}}Communication")
        _sub(comm, f"{{{_NS}}}Description", cv_data["communication_skills"])

    if cv_data.get("computer_skills"):
        comp = _sub(skills, f"{{{_NS}}}Computer")
        _sub(comp, f"{{{_NS}}}Description", cv_data["computer_skills"])

    if cv_data.get("other_skills"):
        other = _sub(skills, f"{{{_NS}}}Other")
        _sub(other, f"{{{_NS}}}Description", cv_data["other_skills"])

    return ET.tostring(root, encoding="unicode", xml_declaration=False)


# ---------------------------------------------------------------------------
# 4. Render PDF from XML (via fpdf2)
# ---------------------------------------------------------------------------

def _xml_to_template_context(xml_str: str, cv_data: dict, target_language: str) -> dict:
    """
    Parse the Europass XML and assemble the Jinja2 template context dict.
    Falls back to cv_data values when XML fields are empty.
    """
    ns = {"ep": _NS}
    root = ET.fromstring(
        f'<?xml version="1.0" encoding="UTF-8"?>{xml_str}'
        if not xml_str.startswith("<?xml") else xml_str
    )

    def _text(element: ET.Element | None) -> str:
        return (element.text or "").strip() if element is not None else ""

    def _find(path: str) -> ET.Element | None:
        return root.find(path, ns)

    learner = _find(".//ep:LearnerInfo")

    def _ltext(path: str) -> str:
        if learner is None:
            return ""
        el = learner.find(path, ns)
        return _text(el)

    ctx: dict = {
        "locale": target_language,
        "labels": _get_labels(target_language),
        "photo_data": None,
        "first_name": _ltext("ep:Identification/ep:PersonName/ep:FirstName") or cv_data.get("first_name", ""),
        "surname": _ltext("ep:Identification/ep:PersonName/ep:Surname") or cv_data.get("surname", ""),
        "headline": _ltext("ep:Headline/ep:Description/ep:Label") or cv_data.get("headline", ""),
        "address": _ltext("ep:Identification/ep:ContactInfo/ep:Address/ep:Contact/ep:AddressLine") or cv_data.get("address", ""),
        "email": _ltext("ep:Identification/ep:ContactInfo/ep:Email/ep:Contact") or cv_data.get("email", ""),
        "phone": _ltext("ep:Identification/ep:ContactInfo/ep:TelephoneList/ep:Telephone/ep:Contact") or cv_data.get("phone", ""),
        "website": cv_data.get("website", ""),
        "birthdate": cv_data.get("birthdate", ""),
        "nationality": cv_data.get("nationality", ""),
        "mother_tongue": _ltext("ep:Skills/ep:Linguistic/ep:MotherTongueList/ep:MotherTongue/ep:Description/ep:Label") or cv_data.get("mother_tongue", ""),
        "driving_licences": cv_data.get("driving_licences") or [],
        "work_experience": [],
        "education": [],
        "foreign_languages": [],
        "communication_skills": _ltext("ep:Skills/ep:Communication/ep:Description") or cv_data.get("communication_skills", ""),
        "computer_skills": _ltext("ep:Skills/ep:Computer/ep:Description") or cv_data.get("computer_skills", ""),
        "other_skills": _ltext("ep:Skills/ep:Other/ep:Description") or cv_data.get("other_skills", ""),
    }

    # Work experience
    if learner is not None:
        for we in learner.findall("ep:WorkExperienceList/ep:WorkExperience", ns):
            from_el = we.find("ep:Period/ep:From", ns)
            to_el = we.find("ep:Period/ep:To", ns)
            curr_el = we.find("ep:Period/ep:Current", ns)
            ctx["work_experience"].append({
                "from_date": (from_el.get("value") if from_el is not None else None) or _text(from_el),
                "to_date": (to_el.get("value") if to_el is not None else None) or _text(to_el),
                "is_current": _text(curr_el).lower() == "true",
                "position": _text(we.find("ep:Position/ep:Label", ns)),
                "activities": _text(we.find("ep:Activities", ns)),
                "employer_name": _text(we.find("ep:Employer/ep:Name", ns)),
                "employer_city": _text(we.find("ep:Employer/ep:ContactInfo/ep:Address/ep:Contact/ep:Municipality", ns)),
                "employer_country": _text(we.find("ep:Employer/ep:ContactInfo/ep:Address/ep:Contact/ep:Country/ep:Label", ns)),
            })

        # Education
        for ed in learner.findall("ep:EducationList/ep:Education", ns):
            from_el = ed.find("ep:Period/ep:From", ns)
            to_el = ed.find("ep:Period/ep:To", ns)
            curr_el = ed.find("ep:Period/ep:Current", ns)
            ctx["education"].append({
                "from_date": (from_el.get("value") if from_el is not None else None) or _text(from_el),
                "to_date": (to_el.get("value") if to_el is not None else None) or _text(to_el),
                "is_current": _text(curr_el).lower() == "true",
                "title": _text(ed.find("ep:Title", ns)),
                "activities": _text(ed.find("ep:Activities", ns)),
                "org_name": _text(ed.find("ep:Organisation/ep:Name", ns)),
                "org_city": _text(ed.find("ep:Organisation/ep:ContactInfo/ep:Address/ep:Contact/ep:Municipality", ns)),
                "org_country": _text(ed.find("ep:Organisation/ep:ContactInfo/ep:Address/ep:Contact/ep:Country/ep:Label", ns)),
            })

        # Foreign languages
        for fl in learner.findall("ep:Skills/ep:Linguistic/ep:ForeignLanguageList/ep:ForeignLanguage", ns):
            prof = fl.find("ep:ProficiencyLevel", ns)
            ctx["foreign_languages"].append({
                "code": _text(fl.find("ep:Description/ep:Code", ns)),
                "label": _text(fl.find("ep:Description/ep:Label", ns)),
                "listening": _text(prof.find("ep:Listening", ns)) if prof is not None else "",
                "reading": _text(prof.find("ep:Reading", ns)) if prof is not None else "",
                "spoken_interaction": _text(prof.find("ep:SpokenInteraction", ns)) if prof is not None else "",
                "spoken_production": _text(prof.find("ep:SpokenProduction", ns)) if prof is not None else "",
                "writing": _text(prof.find("ep:Writing", ns)) if prof is not None else "",
            })

    # Fall back to cv_data arrays if XML parsing yielded nothing
    if not ctx["work_experience"] and cv_data.get("work_experience"):
        ctx["work_experience"] = cv_data["work_experience"]
    if not ctx["education"] and cv_data.get("education"):
        ctx["education"] = cv_data["education"]
    if not ctx["foreign_languages"] and cv_data.get("foreign_languages"):
        ctx["foreign_languages"] = cv_data["foreign_languages"]

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

    # 5. Save to exports dir
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
