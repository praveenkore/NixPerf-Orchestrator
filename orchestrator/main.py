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

from orchestrator.config_validator import ConfigValidationError, validate_config
from orchestrator.decision_engine import Decision, DecisionEngine
from orchestrator.jmeter_runner import JMeterRunner
from orchestrator.models import Metrics, RunResult, ScenarioResult
from orchestrator.parser import ResultsParser
from orchestrator.preflight import PreflightError, run_preflight_checks
from orchestrator.ramp_engine import calculate_rampup, get_default_ramp_strategy
from orchestrator.reporting import Reporter

# ---------------------------------------------------------------------------
# Logging setup  (Item 8: structured logging with timestamps and levels)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
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
    sla_p95: float = scenario_cfg["sla"]["p95"]
    error_threshold: float = scenario_cfg["sla"]["error_threshold"]
    retry_count: int = scenario_cfg.get("retry_count", 1)
    timeout: int = scenario_cfg.get("timeout_seconds", 7200)
    mode: str = scenario_cfg.get("mode", "static")

    # Resolve ramp strategy (backward compatible)
    ramp_config: dict = scenario_cfg.get(
        "ramp_strategy",
        get_default_ramp_strategy(load_steps[0]) if load_steps else {"type": "fixed", "value": 60},
    )

    engine = DecisionEngine(
        sla_p95=sla_p95,
        error_threshold_percent=error_threshold,
        mode=mode,
    )
    result = ScenarioResult(name=name)

    logger.info("=" * 60)
    logger.info(
        "SCENARIO START: %s (mode=%s, ramp=%s, retry=%d, timeout=%ds)",
        name, mode, ramp_config.get("type"), retry_count, timeout,
    )

    for users in load_steps:
        # Dynamic ramp-up calculation
        rampup = calculate_rampup(users, ramp_config)
        logger.info(
            "Executing scenario %s | Users=%d | RampUp=%ds",
            name, users, rampup,
        )

        run = _execute_step(name, jmx_path, users, rampup, runner, engine, retry_count, timeout)
        result.runs.append(run)

        if run.metrics:
            logger.info(
                "Decision: %s | Error=%.2f%% | P95=%.0fms | Reason: %s",
                run.decision, run.metrics.error_percent, run.metrics.p95, run.reason,
            )
        else:
            logger.warning("Decision: %s | Reason: %s", run.decision, run.reason)

        if run.decision == Decision.STOP:
            result.breakpoint_users = users
            logger.warning("⚠ BREAKPOINT: Scenario '%s' stopped at %d users.", name, users)
            break

    logger.info("SCENARIO END: %s — breakpoint=%s", name, result.breakpoint_users or "none")
    return result


def _execute_step(
    name: str,
    jmx_path: str,
    users: int,
    rampup: int,
    runner: JMeterRunner,
    engine: DecisionEngine,
    retry_count: int,
    timeout: int,
) -> RunResult:
    """Run one load step: execute JMeter, parse results, evaluate."""
    timestamp = int(time.time())
    result_file = f"results/{name}_{users}_{timestamp}.csv"

    success, output = runner.run(
        jmx_path, result_file, users,
        rampup=rampup,
        timeout=timeout,
        retry_count=retry_count,
    )

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
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight connectivity checks",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Step 1: Load and validate config
    config = load_config(args.config)
    try:
        validate_config(config)
    except ConfigValidationError as exc:
        logger.error("Config validation failed: %s", exc)
        sys.exit(1)

    # Step 2: Pre-flight checks
    if not args.skip_preflight:
        try:
            run_preflight_checks(config["scenarios"])
        except PreflightError as exc:
            logger.error("Pre-flight check failed: %s", exc)
            sys.exit(1)
    else:
        logger.info("Pre-flight checks skipped (--skip-preflight)")

    # Step 3: Run all scenarios
    runner = JMeterRunner()
    scenario_results: list[ScenarioResult] = [
        run_scenario(cfg, runner) for cfg in config["scenarios"]
    ]

    # Step 4: Generate reports
    _write_reports(scenario_results)
    logger.info("Performance testing complete. Reports saved to reports/")


if __name__ == "__main__":
    main()
