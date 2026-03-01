"""
main.py - Entry point for the NixPerf Orchestrator.

Usage:
    python -m orchestrator.main
    python -m orchestrator.main --config path/to/scenarios.yaml
"""
import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from orchestrator.decision_engine import Decision, DecisionEngine
from orchestrator.jmeter_runner import JMeterRunner
from orchestrator.models import Metrics, RunResult, ScenarioResult
from orchestrator.parser import ResultsParser
from orchestrator.reporting import Reporter

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "config/scenarios.yaml"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def run_scenario(scenario_cfg: dict, runner: JMeterRunner) -> ScenarioResult:
    """Execute all load steps for a single scenario and return its result."""
    name = scenario_cfg["name"]
    jmx_path = scenario_cfg["jmx_path"]
    load_steps: list[int] = scenario_cfg["load_steps"]
    rampup: int = scenario_cfg.get("rampup", 60)   # seconds; default 60
    sla_p95: float = scenario_cfg["sla"]["p95"]
    error_threshold: float = scenario_cfg["sla"]["error_threshold"]

    engine = DecisionEngine(sla_p95=sla_p95, error_threshold_percent=error_threshold)
    result = ScenarioResult(name=name)

    logger.info("=" * 60)
    logger.info("Starting scenario: %s", name)

    for users in load_steps:
        logger.info("--- Load step: %d users ---", users)

        run = _execute_step(name, jmx_path, users, rampup, runner, engine)
        result.runs.append(run)

        logger.info(
            "Decision: %s — %s | Error=%.2f%% P95=%.0fms",
            run.decision, run.reason,
            run.metrics.error_percent if run.metrics else 0,
            run.metrics.p95 if run.metrics else 0,
        )

        if run.decision == Decision.STOP:
            result.breakpoint_users = users
            logger.warning("Stopping scenario '%s' at %d users.", name, users)
            break

    return result


def _execute_step(
    name: str,
    jmx_path: str,
    users: int,
    rampup: int,
    runner: JMeterRunner,
    engine: DecisionEngine,
) -> RunResult:
    """Run one load step: execute JMeter, parse results, evaluate."""
    timestamp = int(time.time())
    result_file = f"results/{name}_{users}_{timestamp}.csv"

    runner.run(jmx_path, result_file, users, rampup=rampup)

    metrics: Metrics | None = None
    if Path(result_file).exists():
        metrics = ResultsParser(result_file).parse()
    else:
        logger.warning("Result file not found after run: %s", result_file)

    decision, reason = engine.evaluate(metrics)
    return RunResult(users=users, metrics=metrics, decision=decision, reason=reason)


def _write_reports(results: list[ScenarioResult]) -> None:
    timestamp = int(time.time())
    raw = [r.to_dict() for r in results]
    Reporter.generate_json_report(raw, f"reports/summary_{timestamp}.json")
    Reporter.generate_html_summary(raw, f"reports/summary_{timestamp}.html")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NixPerf Orchestrator")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to scenarios YAML (default: {DEFAULT_CONFIG})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    runner = JMeterRunner()

    scenario_results: list[ScenarioResult] = [
        run_scenario(cfg, runner) for cfg in config["scenarios"]
    ]

    _write_reports(scenario_results)
    logger.info("Performance testing complete. Reports saved to reports/")


if __name__ == "__main__":
    main()
