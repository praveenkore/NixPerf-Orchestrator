"""
main.py - Entry point for the NixPerf Orchestrator.

Autonomous operation improvements vs original:
    1.  Crash recovery     — checkpoint saved after every load step; resume on restart.
    2.  Warmup probe       — low-traffic probe before escalation; results discarded.
    3.  Cooldown period    — configurable sleep between load steps for system recovery.
    4.  Infra-failure abort — scenario aborted after N consecutive JMeter failures.
    5.  Per-step slave check — slave health re-validated before each load step.
    6.  WARN re-test        — WARN decision triggers a same-step re-test before escalating.
    7.  Notifications       — webhook POST on completion (Slack / generic JSON endpoint).
    8.  Baseline comparison — P95 regressions flagged against a previous golden run.
    9.  Result retention    — old CSV files pruned automatically after each step.

Usage:
    python -m orchestrator.main
    python -m orchestrator.main --config path/to/scenarios.yaml
    python -m orchestrator.main --webhook-url https://hooks.slack.com/...
    python -m orchestrator.main --slaves 10.0.0.1,10.0.0.2 --no-resume
"""

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from orchestrator.config_validator import ConfigValidationError, validate_config
from orchestrator.decision_engine import Decision, DecisionEngine
from orchestrator.jmeter_runner import JMeterRunner
from orchestrator.models import Metrics, RunResult, ScenarioResult
from orchestrator.parser import ResultsParser
from orchestrator.preflight import (
    PreflightError,
    check_slaves_alive,
    run_preflight_checks,
)
from orchestrator.ramp_engine import calculate_rampup, get_default_ramp_strategy
from orchestrator.reporting import Reporter

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = "config/scenarios.yaml"
CHECKPOINT_DIR = Path("reports")

WARMUP_USERS_DEFAULT = 10  # users for the warmup probe
WARMUP_RAMPUP_DEFAULT = 30  # ramp-up seconds for the warmup
WARMUP_SETTLE_SECONDS = 30  # post-warmup settle time before step 1

COOLDOWN_DEFAULT_SECONDS = 60  # between load steps
MAX_CONSECUTIVE_FAILURES_DEFAULT = 2  # infra-failure abort threshold


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
# Checkpoint helpers (Gap #5 — state persistence)
# ---------------------------------------------------------------------------


def _checkpoint_path(scenario_name: str) -> Path:
    return CHECKPOINT_DIR / f".checkpoint_{scenario_name}.json"


def _save_checkpoint(result: ScenarioResult) -> None:
    """Persist scenario progress to disk after every load step."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(result.name)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2)
        logger.debug("Checkpoint saved: %s", path.name)
    except OSError as exc:
        logger.warning("Could not save checkpoint for '%s': %s", result.name, exc)


def _load_checkpoint(scenario_name: str) -> Optional[dict]:
    """Return a previously saved checkpoint dict, or None if none exists."""
    path = _checkpoint_path(scenario_name)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        completed = [r["users"] for r in data.get("runs", [])]
        logger.info(
            "Checkpoint found for '%s' — previously completed steps: %s",
            scenario_name,
            completed,
        )
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read checkpoint for '%s' (%s) — starting fresh",
            scenario_name,
            exc,
        )
        return None


def _clear_checkpoint(scenario_name: str) -> None:
    """Remove the checkpoint file after a clean scenario completion."""
    path = _checkpoint_path(scenario_name)
    try:
        if path.exists():
            path.unlink()
            logger.debug("Checkpoint cleared for '%s'", scenario_name)
    except OSError:
        pass  # non-fatal


# ---------------------------------------------------------------------------
# Core scenario logic
# ---------------------------------------------------------------------------


def run_scenario(
    scenario_cfg: dict,
    runner: JMeterRunner,
    slaves: Optional[list[str]] = None,
    resume: bool = True,
) -> ScenarioResult:
    """Execute all load steps for a single scenario and return its result.

    Autonomous operation features added here:
        - Resume from checkpoint (Gap #5)
        - Warmup probe          (Gap #3)
        - Cooldown between steps(Gap #2)
        - Slave re-check / step (Gap #4)
        - Consecutive-failure abort (Gap #1 / #6)
        - WARN re-test          (Gap #8 — decision back-pressure)
        - Checkpoint save / step (Gap #5)
        - Result file retention  (Gap #9)
    """
    name = scenario_cfg["name"]
    jmx_path = scenario_cfg["jmx_path"]
    load_steps = scenario_cfg["load_steps"]
    sla_p95 = scenario_cfg["sla"]["p95"]
    error_thresh = scenario_cfg["sla"]["error_threshold"]
    retry_count = scenario_cfg.get("retry_count", 1)
    timeout = scenario_cfg.get("timeout_seconds", 7200)
    mode = scenario_cfg.get("mode", "static")
    cooldown = scenario_cfg.get("cooldown_seconds", COOLDOWN_DEFAULT_SECONDS)
    warmup_users = scenario_cfg.get("warmup_users", WARMUP_USERS_DEFAULT)
    max_failures = scenario_cfg.get(
        "max_consecutive_failures", MAX_CONSECUTIVE_FAILURES_DEFAULT
    )

    ramp_config: dict = scenario_cfg.get(
        "ramp_strategy",
        get_default_ramp_strategy(load_steps[0])
        if load_steps
        else {"type": "fixed", "value": 60},
    )

    engine = DecisionEngine(
        sla_p95=sla_p95,
        error_threshold_percent=error_thresh,
        mode=mode,
    )
    result = ScenarioResult(name=name)

    # ── Resume from checkpoint ──────────────────────────────────────────────
    completed_users: set[int] = set()
    if resume:
        checkpoint = _load_checkpoint(name)
        if checkpoint:
            for run_data in checkpoint.get("runs", []):
                result.runs.append(
                    RunResult(
                        users=run_data["users"],
                        metrics=None,  # lightweight — don't reconstruct Metrics
                        decision=run_data["decision"],
                        reason=run_data["reason"],
                    )
                )
                completed_users.add(run_data["users"])
            result.breakpoint_users = checkpoint.get("breakpoint")

            if result.breakpoint_users:
                logger.info(
                    "Checkpoint: scenario '%s' already reached breakpoint at %d users — "
                    "skipping re-run",
                    name,
                    result.breakpoint_users,
                )
                return result

    logger.info("=" * 60)
    logger.info(
        "SCENARIO START: %s | mode=%s | ramp=%s | retry=%d | "
        "timeout=%ds | cooldown=%ds | warmup=%d users",
        name,
        mode,
        ramp_config.get("type"),
        retry_count,
        timeout,
        cooldown,
        warmup_users,
    )

    # ── Warmup probe (Gap #3) ───────────────────────────────────────────────
    if warmup_users > 0 and not completed_users:
        logger.info(
            "Warmup probe: %d users for up to %ds (results discarded)...",
            warmup_users,
            min(timeout, 300),
        )
        warmup_rampup = min(WARMUP_RAMPUP_DEFAULT, warmup_users * 3)
        _execute_step(
            name,
            jmx_path,
            warmup_users,
            warmup_rampup,
            runner,
            engine,
            retry_count=1,
            timeout=min(timeout, 300),
            slaves=slaves,
            discard=True,
        )
        logger.info(
            "Warmup complete — settling for %ds before escalation...",
            WARMUP_SETTLE_SECONDS,
        )
        time.sleep(WARMUP_SETTLE_SECONDS)

    # ── Main load escalation loop ───────────────────────────────────────────
    consecutive_failures = 0

    first_pending_idx = 0
    for idx, step in enumerate(load_steps):
        if step not in completed_users:
            first_pending_idx = idx
            break

    for i, users in enumerate(load_steps):
        # Skip steps already completed in a prior interrupted run.
        if users in completed_users:
            logger.info("Skipping %d-user step (completed in previous run)", users)
            continue

        # ── Cooldown between steps (Gap #2) ────────────────────────────────
        # Apply cooldown before every step except the very first one.
        is_first_real_step = i == first_pending_idx
        if not is_first_real_step:
            logger.info(
                "Cooldown: waiting %ds for system recovery before next step...",
                cooldown,
            )
            time.sleep(cooldown)

        # ── Per-step slave health check (Gap #4) ───────────────────────────
        active_slaves = slaves
        if slaves:
            try:
                active_slaves = check_slaves_alive(slaves)
                logger.info(
                    "Slave health check: %d/%d alive ✓",
                    len(active_slaves),
                    len(slaves),
                )
            except PreflightError as exc:
                logger.error(
                    "Slave check failed before %d-user step: %s — aborting scenario '%s'",
                    users,
                    exc,
                    name,
                )
                result.abort_reason = f"Slave failure before {users}-user step: {exc}"
                _save_checkpoint(result)
                break

        # ── Execute the load step ───────────────────────────────────────────
        rampup = calculate_rampup(users, ramp_config)
        logger.info(
            "Load step: scenario=%s | users=%d | rampup=%ds", name, users, rampup
        )

        run = _execute_step(
            name,
            jmx_path,
            users,
            rampup,
            runner,
            engine,
            retry_count,
            timeout,
            slaves=active_slaves,
        )

        # ── Consecutive infra-failure tracking (Gap #1) ────────────────────
        if run.metrics is None:
            consecutive_failures += 1
            logger.warning(
                "Step produced no metrics — consecutive failures: %d / %d",
                consecutive_failures,
                max_failures,
            )
            if consecutive_failures >= max_failures:
                logger.error(
                    "Aborting scenario '%s' after %d consecutive JMeter failures "
                    "(infrastructure issue?)",
                    name,
                    consecutive_failures,
                )
                result.abort_reason = (
                    f"Aborted after {consecutive_failures} consecutive "
                    "JMeter failures — possible infrastructure outage"
                )
                result.runs.append(run)
                _save_checkpoint(result)
                break
        else:
            consecutive_failures = 0  # reset on any successful metric collection

        # ── WARN back-pressure: re-test same step before escalating (Gap #8) ─
        if run.decision == Decision.WARN:
            logger.warning(
                "WARN at %d users (%s) — re-testing same load before escalating...",
                users,
                run.reason,
            )
            time.sleep(max(30, cooldown // 2))  # short settle before re-test
            retest = _execute_step(
                name,
                jmx_path,
                users,
                rampup,
                runner,
                engine,
                retry_count,
                timeout,
                slaves=active_slaves,
            )
            if retest.metrics is None:
                consecutive_failures += 1
                logger.warning(
                    "WARN re-test produced no metrics — consecutive failures: %d / %d",
                    consecutive_failures,
                    max_failures,
                )
                if consecutive_failures >= max_failures:
                    logger.error(
                        "Aborting scenario '%s' after %d consecutive failures "
                        "(including WARN re-test failure)",
                        name,
                        consecutive_failures,
                    )
                    result.abort_reason = (
                        f"Aborted after {consecutive_failures} consecutive "
                        "JMeter failures — including WARN re-test failure"
                    )
                    result.runs.append(retest)
                    _save_checkpoint(result)
                    break
            else:
                consecutive_failures = 0

            if retest.decision in (Decision.WARN, Decision.STOP):
                # Still degraded — treat as a hard stop.
                logger.warning(
                    "System still degraded on re-test (%s) — converting to STOP",
                    retest.reason,
                )
                run = RunResult(
                    users=retest.users,
                    metrics=retest.metrics,
                    decision=Decision.STOP,
                    reason=f"Degraded on WARN re-test: {retest.reason}",
                )
            else:
                logger.info(
                    "System recovered on re-test — proceeding to next load step"
                )
                run = retest

        result.runs.append(run)

        if run.metrics:
            logger.info(
                "Decision: %s | Error=%.2f%% | P95=%.0fms | Reason: %s",
                run.decision,
                run.metrics.error_percent,
                run.metrics.p95,
                run.reason,
            )
        else:
            logger.warning("Decision: %s | Reason: %s", run.decision, run.reason)

        # ── Checkpoint after every step (Gap #5) ───────────────────────────
        _save_checkpoint(result)

        # ── Prune old result files (Gap #9) ────────────────────────────────
        Reporter.clean_old_results(name)

        # ── Breakpoint reached — stop escalation ────────────────────────────
        if run.decision == Decision.STOP:
            result.breakpoint_users = users
            logger.warning(
                "⚠ BREAKPOINT: scenario '%s' stopped at %d users.", name, users
            )
            _save_checkpoint(result)
            break

    logger.info(
        "SCENARIO END: %s — breakpoint=%s | abort=%s",
        name,
        result.breakpoint_users or "none",
        result.abort_reason or "none",
    )

    # Clear checkpoint on clean completion (no abort, no crash).
    if not result.abort_reason:
        _clear_checkpoint(name)

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
    slaves: Optional[list[str]] = None,
    discard: bool = False,
) -> RunResult:
    """Run one load step: execute JMeter, optionally parse results, evaluate.

    Args:
        discard: When True (warmup mode), skip parsing and evaluation and
                 delete the result file immediately.  The engine's history
                 is NOT updated so warmup traffic does not skew trend analysis.
    """
    # Use JMX basename + users for CSV naming (Gap #10)
    jmx_basename = Path(jmx_path).stem
    safe_name = "".join(c for c in jmx_basename if c.isalnum() or c in ("_", "-"))
    if not safe_name:
        safe_name = "unnamed"
    result_file = f"results/{safe_name}_{users}.csv"

    runner.run(
        jmx_path,
        result_file,
        users,
        rampup=rampup,
        slaves=slaves,
        timeout=timeout,
        retry_count=retry_count,
    )

    # Warmup path — discard result and return a synthetic PROCEED.
    if discard:
        try:
            Path(result_file).unlink(missing_ok=True)
        except OSError:
            pass
        return RunResult(
            users=users,
            metrics=None,
            decision=Decision.PROCEED,
            reason="Warmup probe — results discarded",
        )

    # Normal path — parse and evaluate.
    metrics: Optional[Metrics] = None
    if Path(result_file).exists():
        metrics = ResultsParser(result_file).parse()
    else:
        logger.warning("Result file not found after run: %s", result_file)

    decision, reason = engine.evaluate(metrics)
    return RunResult(users=users, metrics=metrics, decision=decision, reason=reason)


# ---------------------------------------------------------------------------
# Report generation + post-run actions
# ---------------------------------------------------------------------------


def _write_reports(
    results: list[ScenarioResult],
    webhook_url: Optional[str] = None,
    smtp_config: Optional[dict] = None,
) -> None:
    """Generate JSON + HTML reports, run baseline comparison, send notifications."""
    timestamp = int(time.time())
    raw = [r.to_dict() for r in results]

    Reporter.generate_json_report(raw, f"reports/summary_{timestamp}.json")
    Reporter.generate_html_summary(raw, f"reports/summary_{timestamp}.html")

    # Baseline regression check (Gap #7 / #12).
    regressions = Reporter.compare_to_baseline(raw)
    if regressions:
        reg_path = f"reports/regressions_{timestamp}.json"
        Reporter.generate_json_report(regressions, reg_path)
        logger.warning(
            "⚠ %d regression(s) detected vs baseline — details: %s",
            len(regressions),
            reg_path,
        )

    # Webhook notification (Gap #6).
    if webhook_url:
        extra: dict = {}
        if regressions:
            extra["regressions"] = len(regressions)
        Reporter.send_webhook_notification(
            raw, webhook_url, extra_context=extra or None
        )

    # Email notification (Gap #11).
    if smtp_config:
        extra = {}
        if regressions:
            extra["regressions"] = len(regressions)
        Reporter.send_email_notification(raw, smtp_config, extra_context=extra or None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NixPerf Orchestrator — autonomous load-test runner"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to scenarios YAML (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight connectivity checks (useful in CI)",
    )
    parser.add_argument(
        "--webhook-url",
        default=None,
        metavar="URL",
        help="Slack / generic webhook URL for completion notifications",
    )
    parser.add_argument(
        "--slaves",
        default=None,
        metavar="IP1,IP2,...",
        help="Comma-separated JMeter slave IPs (overrides config-level slaves list)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore checkpoints and always start from scratch",
    )
    parser.add_argument(
        "--jmeter-path",
        default=None,
        help="Path to the JMeter executable (overrides config-level jmeter_path)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Step 1: Load and validate config.
    config = load_config(args.config)
    try:
        validate_config(config)
    except ConfigValidationError as exc:
        logger.error("Config validation failed: %s", exc)
        sys.exit(1)

    # Step 2: Resolve slave list (CLI flag overrides config key).
    slaves: Optional[list[str]] = None
    if args.slaves:
        slaves = [s.strip() for s in args.slaves.split(",") if s.strip()]
    elif config.get("slaves"):
        slaves = list(config["slaves"])

    # Step 3: Resolve JMeter path.
    jmeter_path = args.jmeter_path or config.get("jmeter_path", "jmeter")
    resolved = shutil.which(jmeter_path)
    if resolved:
        jmeter_path = resolved
    elif not Path(jmeter_path).is_file():
        logger.error(
            "JMeter executable not found: '%s'. "
            "Ensure JMeter is installed and in your PATH.",
            jmeter_path,
        )
        sys.exit(1)

    # Step 4: Pre-flight checks.
    if not args.skip_preflight:
        try:
            run_preflight_checks(
                config["scenarios"], slaves=slaves, jmeter_path=jmeter_path
            )
        except PreflightError as exc:
            logger.error("Pre-flight check failed: %s", exc)
            sys.exit(1)
    else:
        logger.info("Pre-flight checks skipped (--skip-preflight)")

    # Step 5: Run all scenarios.
    runner = JMeterRunner(jmeter_path=jmeter_path)
    resume = not args.no_resume
    webhook = args.webhook_url or (config.get("notification", {}) or {}).get(
        "webhook_url"
    )

    scenario_results: list[ScenarioResult] = [
        run_scenario(cfg, runner, slaves=slaves, resume=resume)
        for cfg in config["scenarios"]
    ]

    # Step 5: Generate reports, compare baseline, notify.
    smtp_config = config.get("smtp")
    _write_reports(scenario_results, webhook_url=webhook, smtp_config=smtp_config)
    logger.info("Performance testing complete. Reports saved to reports/")


if __name__ == "__main__":
    main()
