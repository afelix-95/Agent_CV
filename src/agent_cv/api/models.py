from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    max_files: int = Field(default=100, ge=1, le=5000)


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
