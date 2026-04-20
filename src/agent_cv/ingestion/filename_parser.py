from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dateutil import parser as date_parser


VENDOR_HINTS = {
    "dell": "Dell",
    "emc": "Dell EMC",
    "red hat": "Red Hat",
    "aws": "AWS",
    "cisco": "Cisco",
    "microsoft": "Microsoft",
    "vmware": "VMware",
    "terraform": "HashiCorp",
    "hashicorp": "HashiCorp",
}


@dataclass
class ParsedDocument:
    employee_name: str
    title: str
    vendor: str | None
    issue_date: date | None
    expiry_date: date | None
    is_cv: bool


def _detect_vendor(text: str) -> str | None:
    lower = text.lower()
    for hint, vendor in VENDOR_HINTS.items():
        if hint in lower:
            return vendor
    return None


def _extract_dates(text: str) -> tuple[date | None, date | None]:
    matches = re.findall(r"(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{4})", text)
    parsed: list[date] = []
    for value in matches:
        try:
            dt = date_parser.parse(value, dayfirst=True, default=datetime(2000, 1, 1))
            parsed.append(dt.date() if hasattr(dt, "date") and callable(dt.date) else dt)
        except (ValueError, OverflowError):
            continue
    if not parsed:
        return None, None
    parsed = sorted(parsed)
    issue = parsed[0]
    expiry = parsed[-1] if len(parsed) > 1 else None
    return issue, expiry


def parse_file_name(path: Path) -> ParsedDocument:
    stem = path.stem
    parts = [p.strip() for p in stem.split("-")]
    employee_name = parts[0] if parts else stem
    title = " - ".join(parts[1:]).strip() if len(parts) > 1 else stem
    is_cv = "cv" in stem.lower() or "curriculum" in stem.lower()
    vendor = _detect_vendor(stem)
    issue_date, expiry_date = _extract_dates(stem)
    return ParsedDocument(
        employee_name=employee_name,
        title=title or stem,
        vendor=vendor,
        issue_date=issue_date,
        expiry_date=expiry_date,
        is_cv=is_cv,
    )
