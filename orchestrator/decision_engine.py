"""
decision_engine.py - Evaluates run metrics and decides whether to escalate or stop.

Supports two modes:
    - 'static': Standard threshold-based evaluation (default).
    - 'adaptive': Placeholder for future intelligent escalation (Phase 2).
"""
import logging
from enum import Enum
from typing import Optional

from orchestrator.models import Metrics

logger = logging.getLogger(__name__)


class Decision(str, Enum):
    PROCEED = "PROCEED"
    STOP = "STOP"


class EscalationMode(str, Enum):
    STATIC = "static"
    ADAPTIVE = "adaptive"


class DecisionEngine:
    """Compares run metrics against configured SLA and error thresholds.

    Args:
        sla_p95:               Maximum allowed P95 response time (ms).
        error_threshold_percent: Maximum allowed error rate (%).
        mode:                  Escalation mode ('static' or 'adaptive').
    """

    def __init__(
        self,
        sla_p95: float,
        error_threshold_percent: float,
        mode: str = EscalationMode.STATIC,
    ) -> None:
        self.sla_p95 = sla_p95
        self.error_threshold_percent = error_threshold_percent
        self.mode = EscalationMode(mode)

        if self.mode == EscalationMode.ADAPTIVE:
            logger.info("Adaptive escalation mode enabled (Phase 2 placeholder)")

    def evaluate(self, metrics: Optional[Metrics]) -> tuple[Decision, str]:
        """Return a Decision and a human-readable reason string."""
        if metrics is None:
            return Decision.STOP, "No metrics collected — JMeter may have failed"

        if self.mode == EscalationMode.ADAPTIVE:
            return self._evaluate_adaptive(metrics)

        return self._evaluate_static(metrics)

    def _evaluate_static(self, metrics: Metrics) -> tuple[Decision, str]:
        """Standard threshold-based evaluation."""
        if metrics.error_percent > self.error_threshold_percent:
            return (
                Decision.STOP,
                f"Error rate {metrics.error_percent:.2f}% exceeds threshold "
                f"{self.error_threshold_percent}%",
            )

        if metrics.p95 > self.sla_p95:
            return (
                Decision.STOP,
                f"P95 latency {metrics.p95:.0f}ms exceeds SLA of {self.sla_p95}ms",
            )

        return Decision.PROCEED, "System healthy — escalating load"

    def _evaluate_adaptive(self, metrics: Metrics) -> tuple[Decision, str]:
        """Adaptive escalation — Phase 2 placeholder.

        Future implementation will:
            - Detect degradation patterns across runs
            - Predict breaking point using regression
            - Estimate safe capacity
            - Auto-classify bottleneck type
        """
        # For now, fall back to static evaluation with a log message
        logger.debug(
            "Adaptive mode: falling back to static evaluation (Phase 2 not yet implemented)"
        )
        return self._evaluate_static(metrics)
