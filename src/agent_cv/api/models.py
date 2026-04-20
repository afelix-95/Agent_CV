from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    max_files: int = Field(default=100, ge=1, le=5000)
    force_reingest: bool = False
    filename_contains: str | None = Field(default=None, min_length=1)


class QueryRequest(BaseModel):
    query: str = Field(min_length=2)
    language: Literal["pt", "en"] | None = None


class CertificationHit(BaseModel):
    employee_name: str
    certification_name: str
    vendor: str
    status: str
    issue_date: date | None = None
    expiry_date: date | None = None


class QueryResponse(BaseModel):
    language: str
    summary: str
    certifications: list[CertificationHit]


class AuditLogEntry(BaseModel):
    query_text: str
    query_language: str | None = None
    response_language: str
    result_count: int
    latency_ms: int
    normalized_intent_json: dict | None = None
    created_at: datetime


class AuditLogsResponse(BaseModel):
    total: int
    entries: list[AuditLogEntry]
