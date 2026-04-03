import logging
import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())

from orchestrator.main import _write_reports
from orchestrator.models import ScenarioResult, RunResult, Metrics
from orchestrator.decision_engine import Decision

logging.basicConfig(level=logging.INFO)

# Mock results
res = ScenarioResult(name="smtp_test")
res.runs.append(RunResult(
    users=10,
    metrics=Metrics(
        total_requests=100,
        error_count=0,
        error_percent=0.0,
        avg_response_time=150.0,
        min_response_time=100.0,
        max_response_time=300.0,
        p95=200.0,
        p99=300.0
    ),
    decision=Decision.PROCEED,
    reason="Healthy"
))

smtp_config = {
    "host": "127.0.0.1",
    "port": 1025,
    "user": None,
    "password": None,
    "sender": "NixPerf <test@nixperf.local>",
    "recipient": "recipient@nixperf.local",
    "use_tls": False
}

print("Triggering _write_reports...")
_write_reports([res], webhook_url=None, smtp_config=smtp_config)
print("Finished.")
