"""Request and response schemas.

Timestamps are accepted as ISO-8601 with an offset (``Z`` is accepted); a
naive timestamp is interpreted as UTC.  Stored and returned timestamps are
canonical UTC strings with microsecond precision, so string comparison in
SQLite is chronological.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IntervalOut(BaseModel):
    start_mm: int
    end_mm: int


class NdtOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=64, examples=["NDT-2026-1001"])
    weld_no: str = Field(min_length=1, max_length=64, examples=["W-100"])
    completed_at: datetime = Field(examples=["2026-01-02T08:00:00Z"])
    start_mm: int = Field(examples=[0])
    end_mm: int = Field(examples=[800])

    @field_validator("completed_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_mm <= self.start_mm:
            raise ValueError("end_mm must be greater than start_mm (half-open interval)")
        return self


class NdtOrderOut(BaseModel):
    order_no: str
    weld_no: str
    completed_at: str
    interval: IntervalOut


class CertificateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cert_no: str = Field(min_length=1, max_length=64, examples=["CERT-2026-1001"])
    weld_no: str = Field(min_length=1, max_length=64, examples=["W-100"])
    issued_at: datetime = Field(examples=["2026-01-10T09:00:00Z"])
    start_mm: int = Field(examples=[0])
    end_mm: int = Field(examples=[800])
    ndt_order_nos: list[str] = Field(
        default_factory=list,
        max_length=200,
        examples=[["NDT-2026-1001"]],
        description="Cited NDT orders. Only cited, same-weld, pre-issuance ones count as evidence.",
    )

    @field_validator("issued_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @field_validator("ndt_order_nos")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in v:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_mm <= self.start_mm:
            raise ValueError("end_mm must be greater than start_mm (half-open interval)")
        return self


class CertificateOut(BaseModel):
    cert_no: str
    weld_no: str
    issued_at: str
    approved: IntervalOut
    ndt_order_nos: list[str]


class RepairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=64, examples=["REP-2026-2001"])
    weld_no: str = Field(min_length=1, max_length=64, examples=["W-100"])
    occurred_at: datetime = Field(examples=["2026-01-05T10:00:00Z"])
    start_mm: int = Field(examples=[300])
    end_mm: int = Field(examples=[400])

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_mm <= self.start_mm:
            raise ValueError("end_mm must be greater than start_mm (half-open interval)")
        return self


class RepairUpdateIn(BaseModel):
    """Same-number correction; the repair number comes from the path."""

    model_config = ConfigDict(extra="forbid")

    weld_no: str = Field(min_length=1, max_length=64, examples=["W-100"])
    occurred_at: datetime = Field(examples=["2026-01-05T10:00:00Z"])
    start_mm: int = Field(examples=[250])
    end_mm: int = Field(examples=[450])

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_mm <= self.start_mm:
            raise ValueError("end_mm must be greater than start_mm (half-open interval)")
        return self


class RepairOut(BaseModel):
    order_no: str
    weld_no: str
    occurred_at: str
    interval: IntervalOut


class RepairRef(BaseModel):
    repair_no: str
    occurred_at: str
    interval: IntervalOut
    overlap: IntervalOut


class EvidenceOut(BaseModel):
    ndt_no: str
    weld_no: str
    completed_at: str | None
    interval: IntervalOut
    trusted: bool
    reason: Literal[
        "ok",
        "weld_mismatch",
        "not_completed_before_issuance",
        "outside_approved_interval",
        "missing",
    ]
    effective_coverage: list[IntervalOut]
    invalidating_repairs: list[RepairRef]


class GapReport(BaseModel):
    cert_no: str
    weld_no: str
    issued_at: str
    approved: IntervalOut
    gaps: list[IntervalOut]
    gap_total_mm: int
    evidence: list[EvidenceOut]


class ChangedCertificate(BaseModel):
    cert_no: str
    weld_no: str
    gaps_before: list[IntervalOut]
    gaps_after: list[IntervalOut]
    gap_total_before_mm: int
    gap_total_after_mm: int


class RepairIngestResult(BaseModel):
    repair: RepairOut
    action: Literal["created", "corrected", "unchanged"]
    candidates_checked: int = Field(
        description="Certificates the old or new repair could truncate; only these were recomputed."
    )
    changed_count: int
    changed_certificates: list[ChangedCertificate]
    audit_id: int


class HealthOut(BaseModel):
    status: Literal["ok"]
    db_path: str


# --------------------------------------------------------------------------
# Reverse repair-time lookup
# --------------------------------------------------------------------------


class GapIntervalIn(BaseModel):
    """One gap interval supplied by the archivist (complete list, no gaps = [])."""

    model_config = ConfigDict(extra="forbid")

    start_mm: int = Field(examples=[300])
    end_mm: int = Field(examples=[400])

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_mm <= self.start_mm:
            raise ValueError("end_mm must be greater than start_mm (half-open interval)")
        return self


class CertificateGapsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cert_no: str = Field(min_length=1, max_length=64, examples=["CERT-2026-1001"])
    gaps: list[GapIntervalIn] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "The complete gap list the certificate should have at the looked-up instant. "
            "An empty list means 'no gaps'. Overlapping/adjacent items are merged before "
            "comparison."
        ),
    )


class RepairLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: datetime = Field(
        examples=["2026-01-01T00:00:00Z"],
        description="Start of the UTC search window; included.",
    )
    window_end: datetime = Field(
        examples=["2026-01-10T00:00:00Z"],
        description="End of the UTC search window; included.",
    )
    certificates: list[CertificateGapsIn] = Field(
        min_length=1,
        max_length=200,
        description="Certificates on the weld and their complete expected gap lists.",
    )

    @field_validator("window_start", "window_end")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _window_and_unique_certs(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not be earlier than window_start")
        seen: set[str] = set()
        for item in self.certificates:
            if item.cert_no in seen:
                raise ValueError(f"duplicate certificate {item.cert_no!r}")
            seen.add(item.cert_no)
        return self


class TimeIntervalOut(BaseModel):
    start: str
    end: str
    start_inclusive: bool
    end_inclusive: bool


class TimeSolutionsOut(BaseModel):
    """A union of time spans.  Isolated instants are listed separately."""

    intervals: list[TimeIntervalOut]
    instants: list[str]


class RepairLookupEchoOut(BaseModel):
    repair_no: str
    weld_no: str
    recorded_occurred_at: str
    interval: IntervalOut


class CertificateLookupOut(BaseModel):
    cert_no: str
    weld_no: str
    expected_gaps: list[IntervalOut]
    has_solution: bool = Field(
        description="True when at least one instant in the search window matches this cert."
    )
    solutions: TimeSolutionsOut


class RepairLookupOut(BaseModel):
    repair: RepairLookupEchoOut
    query: TimeIntervalOut
    status: Literal["found", "no_common_time", "unsatisfiable"] = Field(
        description=(
            "`found`: at least one instant satisfies every certificate. "
            "`no_common_time`: every certificate has a solution on its own, but the "
            "solutions do not overlap. "
            "`unsatisfiable`: at least one certificate has no matching instant at all."
        )
    )
    common: TimeSolutionsOut
    certificates: list[CertificateLookupOut]
