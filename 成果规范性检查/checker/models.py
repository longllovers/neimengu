from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    name: str
    code: str
    kind: str
    length: int
    decimals: int | None = None
    required: bool = False


@dataclass
class Issue:
    severity: str
    code: str
    location: str
    message: str
    expected: str | None = None
    actual: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    root: str
    province_code: str
    year: str
    gdb_schema: str
    zpj_schema: str
    issues: list[Issue] = field(default_factory=list)
    checked_vectors: int = 0
    checked_records: int = 0
    county_count: int = 0

    @property
    def passed(self) -> bool:
        return not any(item.severity in {"ERROR", "WARNING"} for item in self.issues)

    def add(
        self,
        severity: str,
        code: str,
        location: str | Path,
        message: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.issues.append(
            Issue(
                severity=severity,
                code=code,
                location=str(location),
                message=message,
                expected=expected,
                actual=actual,
                details=details or {},
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "root": self.root,
            "province_code": self.province_code,
            "year": self.year,
            "gdb_schema": self.gdb_schema,
            "zpj_schema": self.zpj_schema,
            "summary": {
                "errors": sum(i.severity == "ERROR" for i in self.issues),
                "warnings": sum(i.severity == "WARNING" for i in self.issues),
                "info": sum(i.severity == "INFO" for i in self.issues),
                "checked_vectors": self.checked_vectors,
                "checked_records": self.checked_records,
                "county_codes_loaded": self.county_count,
            },
            "issues": [item.to_dict() for item in self.issues],
        }
