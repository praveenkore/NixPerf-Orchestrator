class DecisionEngine:
    def __init__(self, sla_p95, error_threshold_percent):
        self.sla_p95 = sla_p95
        self.error_threshold_percent = error_threshold_percent

    def evaluate(self, metrics):
        """
        Evaluates the current run metrics against SLA and error thresholds.
        Returns: ("PROCEED", "REASON") or ("STOP", "REASON")
        """
        if metrics is None:
            return "STOP", "No metrics collected"

        error_percent = metrics.get("error_percent", 0)
        p95 = metrics.get("p95", 0)

        if error_percent > self.error_threshold_percent:
            return "STOP", f"Error threshold exceeded: {error_percent:.2f}% > {self.error_threshold_percent}%"

        if p95 > self.sla_p95:
            # We might want to "PROCEED" but flag it, or "STOP" if it's too high.
            # Based on requirements: "Stop escalation if break condition is met"
            # Break condition includes SLA p95 threshold.
            return "STOP", f"SLA p95 breached: {p95}ms > {self.sla_p95}ms"

        return "PROCEED", "System healthy"
