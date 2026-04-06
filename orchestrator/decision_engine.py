"""
decision_engine.py - Evaluates run metrics and decides whether to escalate or stop.

Supports two modes:
    - 'static':   Standard threshold-based evaluation (default).
    - 'adaptive': Trend-aware evaluation using linear regression over recent steps.

Decision states:
    - PROCEED: System healthy, escalate to next load step.
    - WARN:    Approaching SLA limits — re-test same step before escalating.
    - STOP:    SLA breached or trend predicts imminent breach — halt escalation.
"""

import logging
from collections import deque
from enum import Enum
from typing import Optional

from orchestrator.models import Metrics

logger = logging.getLogger(__name__)

# Fraction of the hard limit at which a WARN is raised (e.g. 0.75 = warn at 75% of limit).
DEFAULT_WARN_FACTOR = 0.75

# Minimum history depth before adaptive trend analysis is used.
ADAPTIVE_MIN_HISTORY = 3

# How many recent steps to include in the trend regression window.
ADAPTIVE_TREND_WINDOW = 5

# Adaptive stop: if the predicted next-step value exceeds this fraction of the SLA limit.
ADAPTIVE_STOP_PREDICTION_FACTOR = 0.90

# Adaptive warn: if P95 is rising faster than this fraction of the SLA limit per step.
ADAPTIVE_RAPID_RISE_FACTOR = 0.10

# Maximum number of Metrics entries retained in history for adaptive mode.
MAX_HISTORY_SIZE = 100


class Decision(str, Enum):
    PROCEED = "PROCEED"
    WARN = "WARN"  # approaching threshold — re-test before escalating
    STOP = "STOP"


class EscalationMode(str, Enum):
    STATIC = "static"
    ADAPTIVE = "adaptive"


class DecisionEngine:
    """Compares run metrics against configured SLA and error thresholds.

    Args:
        sla_p95:                 Maximum allowed P95 response time (ms).
        error_threshold_percent: Maximum allowed error rate (%).
        mode:                    Escalation mode ('static' or 'adaptive').
        warn_factor:             Fraction of hard limit that triggers a WARN
                                 (default: 0.75, i.e. warn at 75% of limit).
    """

    def __init__(
        self,
        sla_p95: float,
        error_threshold_percent: float,
        mode: str = EscalationMode.STATIC,
        warn_factor: float = DEFAULT_WARN_FACTOR,
    ) -> None:
        self.sla_p95 = sla_p95
        self.error_threshold_percent = error_threshold_percent
        self.mode = EscalationMode(mode)
        self.warn_factor = warn_factor

        # History of evaluated Metrics — used by adaptive mode.
        # PERF-03: deque(maxlen) enforces the cap in O(1) without list re-allocation.
        self._history: deque[Metrics] = deque(maxlen=MAX_HISTORY_SIZE)

        if self.mode == EscalationMode.ADAPTIVE:
            logger.info(
                "Adaptive escalation mode enabled (warn_factor=%.2f, "
                "trend window=%d steps)",
                warn_factor,
                ADAPTIVE_TREND_WINDOW,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, metrics: Optional[Metrics]) -> tuple[Decision, str]:
        """Return a Decision and a human-readable reason string.

        Always appends non-None metrics to internal history before evaluation
        so that adaptive mode has access to the full trend.
        """
        if metrics is None:
            return Decision.STOP, "No metrics collected — JMeter may have failed"

        # deque(maxlen=MAX_HISTORY_SIZE) evicts the oldest entry automatically.
        self._history.append(metrics)

        if self.mode == EscalationMode.ADAPTIVE:
            return self._evaluate_adaptive(metrics)

        return self._evaluate_static(metrics)

    # ------------------------------------------------------------------
    # Static evaluation
    # ------------------------------------------------------------------

    def _evaluate_static(self, metrics: Metrics) -> tuple[Decision, str]:
        """Threshold-based evaluation with an intermediate WARN band.

        Evaluation order:
            1. Hard STOP  — error rate or P95 exceeds the configured limit.
            2. WARN band  — either metric is within warn_factor of the limit.
            3. PROCEED    — all metrics healthy.
        """
        # ── Hard-stop thresholds ────────────────────────────────────────────
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

        # ── WARN band (approaching limit) ───────────────────────────────────
        warn_error_limit = self.error_threshold_percent * self.warn_factor
        warn_p95_limit = self.sla_p95 * self.warn_factor

        if metrics.error_percent > warn_error_limit:
            return (
                Decision.WARN,
                f"Error rate {metrics.error_percent:.2f}% approaching threshold "
                f"{self.error_threshold_percent}% — system unstable",
            )

        if metrics.p95 > warn_p95_limit:
            return (
                Decision.WARN,
                f"P95 {metrics.p95:.0f}ms approaching SLA limit "
                f"{self.sla_p95:.0f}ms — system under stress",
            )

        return Decision.PROCEED, "System healthy — escalating load"

    # ------------------------------------------------------------------
    # Adaptive evaluation
    # ------------------------------------------------------------------

    def _evaluate_adaptive(self, metrics: Metrics) -> tuple[Decision, str]:
        """Trend-aware evaluation using linear regression over recent load steps.

        Falls back to static evaluation when history is insufficient.
        Hard-stop thresholds from static mode are always enforced first.

        Algorithm:
            1. Static hard-stop check (always applied).
            2. Linear slope of P95 and error-rate over the last N steps.
            3. Project one step ahead: if the prediction breaches 90% of the
               SLA limit, issue a pre-emptive STOP.
            4. If the per-step rise in P95 exceeds 10% of the SLA, issue WARN.
            5. Otherwise delegate to static evaluation for the WARN/PROCEED decision.
        """
        # ── Static hard-stop always takes precedence ─────────────────────────
        static_decision, static_reason = self._evaluate_static(metrics)
        if static_decision == Decision.STOP:
            return static_decision, static_reason

        # ── Need at least ADAPTIVE_MIN_HISTORY data points for trends ────────
        if len(self._history) < ADAPTIVE_MIN_HISTORY:
            logger.debug(
                "Adaptive: only %d history point(s) — falling back to static evaluation "
                "(need %d)",
                len(self._history),
                ADAPTIVE_MIN_HISTORY,
            )
            return static_decision, static_reason

        # ── Compute slopes over the recent trend window ───────────────────────
        # deque does not support slice notation; convert to list first.
        window = list(self._history)[-ADAPTIVE_TREND_WINDOW:]
        p95_values = [m.p95 for m in window]
        error_values = [m.error_percent for m in window]

        p95_slope = self._linear_slope(p95_values)
        error_slope = self._linear_slope(error_values)

        predicted_p95 = metrics.p95 + p95_slope
        predicted_error = metrics.error_percent + error_slope

        logger.debug(
            "Adaptive trend: P95 slope=+%.0fms/step (predicted next=%.0fms) | "
            "Error slope=+%.2f%%/step (predicted next=%.2f%%)",
            p95_slope,
            predicted_p95,
            error_slope,
            predicted_error,
        )

        # ── Pre-emptive STOP if next step is predicted to breach the SLA ─────
        stop_p95_threshold = self.sla_p95 * ADAPTIVE_STOP_PREDICTION_FACTOR
        stop_error_threshold = (
            self.error_threshold_percent * ADAPTIVE_STOP_PREDICTION_FACTOR
        )

        if predicted_p95 > stop_p95_threshold:
            return (
                Decision.STOP,
                f"Adaptive: P95 trend (+{p95_slope:.0f}ms/step) predicts SLA breach — "
                f"next step estimated {predicted_p95:.0f}ms vs limit {self.sla_p95:.0f}ms",
            )

        if predicted_error > stop_error_threshold:
            return (
                Decision.STOP,
                f"Adaptive: error-rate trend (+{error_slope:.2f}%/step) predicts threshold "
                f"breach — next step estimated {predicted_error:.2f}% vs limit "
                f"{self.error_threshold_percent}%",
            )

        # ── WARN if P95 is rising rapidly even if not yet near the limit ──────
        rapid_rise_threshold = self.sla_p95 * ADAPTIVE_RAPID_RISE_FACTOR
        if p95_slope > rapid_rise_threshold:
            return (
                Decision.WARN,
                f"Adaptive: rapid P95 increase detected (+{p95_slope:.0f}ms/step) — "
                f"monitoring trend before escalating",
            )

        # ── Default: delegate to static (covers WARN band and PROCEED) ────────
        return static_decision, static_reason

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _linear_slope(values: list[float]) -> float:
        """Return the average first-difference (mean rate of change) of a series.

        This is equivalent to (last - first) / (n - 1) for equally spaced
        observations, but more robust to noise in the interior of the window.

        Returns 0.0 for a series with fewer than 2 values.
        """
        if len(values) < 2:
            return 0.0
        diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
        return sum(diffs) / len(diffs)
