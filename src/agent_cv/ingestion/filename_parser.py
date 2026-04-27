from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


VENDOR_HINTS = {
    "dell": "Dell",
    "emc": "Dell EMC",
    "red hat": "Red Hat",
    "redhat": "Red Hat",
    "rhcsa": "Red Hat",
    "rhce": "Red Hat",
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
    is_cv: bool


def _detect_vendor(text: str) -> str | None:
    # Normalize separators so "Red_Hat" / "Red-Hat" match the same as "Red Hat".
    lower = re.sub(r"[_\-]", " ", (text or "").lower())
    for hint, vendor in VENDOR_HINTS.items():
        if hint in lower:
            return vendor
    return None


def parse_file_name(path: Path) -> ParsedDocument:
    stem = path.stem
    parts = [p.strip() for p in stem.split("-")]
    employee_name = parts[0] if parts else stem
    title = " - ".join(parts[1:]).strip() if len(parts) > 1 else stem
    is_cv = "cv" in stem.lower() or "curriculum" in stem.lower()
    vendor = _detect_vendor(stem)
    return ParsedDocument(
        employee_name=employee_name,
        title=title or stem,
        vendor=vendor,
        is_cv=is_cv,
    )
