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
    conversation_id: str | None = None


class CertificationHit(BaseModel):
    employee_name: str
    certification_name: str
    vendor: str
    status: str
    issue_date: date | None = None
    expiry_date: date | None = None


class ExperienceHit(BaseModel):
    employee_name: str
    headline: str
    snippet: str
    source_document: str
    language: str


class QueryResponse(BaseModel):
    intent: Literal["certifications", "experience", "chat"]
    language: str
    answer: str
    summary: str
    total_results: int = 0
    shown_results: int = 0
    has_more: bool = False
    show_certification_details: bool = False
    certifications: list[CertificationHit] = Field(default_factory=list)
    experiences: list[ExperienceHit] = Field(default_factory=list)


class AuditLogEntry(BaseModel):
    aad_object_id: str | None = None
    chat_id: str | None = None
    query_text: str
    query_language: str | None = None
    response_language: str
    result_count: int
    latency_ms: int
    agent_tool_calls: list | None = None
    created_at: datetime


class AuditLogsResponse(BaseModel):
    total: int
    entries: list[AuditLogEntry]
