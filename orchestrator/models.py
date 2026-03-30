"""
models.py - Shared dataclasses for type-safe data exchange between modules.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Metrics:
    """Computed performance metrics from a single JMeter run."""
    total_requests: int
    error_count: int
    error_percent: float
    avg_response_time: float
    min_response_time: float
    max_response_time: float
    p95: float
    p99: float

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class RunResult:
    """Result of a single load step execution."""
    users: int
    metrics: Optional[Metrics]
    decision: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "users": self.users,
            "metrics": self.metrics.to_dict() if self.metrics else {},
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass
class ScenarioResult:
    """Aggregated result for a full scenario across all load steps."""
    name: str
    runs: list = field(default_factory=list)
    breakpoint_users: Optional[int] = None
    abort_reason: Optional[str] = None  # set when scenario is aborted due to infra failure

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "runs": [r.to_dict() for r in self.runs],
            "breakpoint": self.breakpoint_users,
            "abort_reason": self.abort_reason,
        }
