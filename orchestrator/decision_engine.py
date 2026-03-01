"""
decision_engine.py - Evaluates run metrics and decides whether to escalate or stop.
"""
import logging
from enum import Enum
from typing import Optional

from orchestrator.models import Metrics

logger = logging.getLogger(__name__)


class Decision(str, Enum):
    PROCEED = "PROCEED"
    STOP = "STOP"


class DecisionEngine:
    """Compares run metrics against configured SLA and error thresholds.

    Args:
        sla_p95:               Maximum allowed P95 response time (ms).
        error_threshold_percent: Maximum allowed error rate (%).
    """

    def __init__(self, sla_p95: float, error_threshold_percent: float) -> None:
        self.sla_p95 = sla_p95
        self.error_threshold_percent = error_threshold_percent

    def evaluate(self, metrics: Optional[Metrics]) -> tuple[Decision, str]:
        """Return a Decision and a human-readable reason string."""
        if metrics is None:
            return Decision.STOP, "No metrics collected — JMeter may have failed"

        if metrics.error_percent > self.error_threshold_percent:
            return (
                Decision.STOP,
                f"Error rate {metrics.error_percent:.2f}% exceeds threshold {self.error_threshold_percent}%",
            )

        if metrics.p95 > self.sla_p95:
            return (
                Decision.STOP,
                f"P95 latency {metrics.p95:.0f}ms exceeds SLA of {self.sla_p95}ms",
            )

        return Decision.PROCEED, "System healthy — escalating load"
