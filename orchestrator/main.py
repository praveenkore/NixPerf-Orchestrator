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

Security / reliability fixes (vs previous revision):
    SEC-04  — Slave addresses supplied via --slaves or config are now validated:
              loopback, link-local, and multicast IPs are rejected; hostnames must
              match RFC-1123 syntax.  Prevents internal-network probing (SSRF).
    SEC-08  — load_config() restricts --config to .yaml/.yml extensions and guards
              against yaml.safe_load() returning None for an empty file.
    LOG-01  — clean_old_results() is now called with the JMX-derived safe name
              (matching the actual result CSV filename prefix) instead of the
              scenario name.  Previously no files were ever deleted.
    LOG-02  — On checkpoint resume, Metrics objects are reconstructed and pushed
              into DecisionEngine._history so adaptive mode has full trend context.
    LOG-03  — The WARN run's history entry is popped before the re-test executes,
              preventing the same load level from appearing twice in the slope window.
    LOG-05  — Result CSV filenames now include both the scenario name and the JMX
              basename, preventing two scenarios that share a JMX file from
              overwriting each other's results at the same user count.
    LOG-06  — Replaced the fragile first_pending_idx calculation with a simple
              first_real_step_done flag; eliminates a spurious cooldown before
              the first actually-executed step in a resumed run.
    LOG-07  — The original WARN run is appended to result.runs before the re-test
              so the full audit trail (WARN → re-test outcome) is preserved.

Usage:
    python -m orchestrator.main
    python -m orchestrator.main --config path/to/scenarios.yaml
    python -m orchestrator.main --webhook-url https://hooks.slack.com/...
    python -m orchestrator.main --slaves 10.0.0.1,10.0.0.2 --no-resume
"""

import argparse
import ipaddress
import json
import logging
import re
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
    DEFAULT_RMI_PORTS,
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

# SEC-04: RFC-1123 hostname pattern used to validate slave addresses.
_SLAVE_HOSTNAME_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$"
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    # SEC-08: restrict to YAML files; prevents --config /etc/passwd style reads.
    path = Path(config_path).resolve()
    if path.suffix not in (".yaml", ".yml"):
        logger.error(
            "Config must be a .yaml or .yml file: %s", config_path
        )
        sys.exit(1)
    if not path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # SEC-08 / LOG-04: yaml.safe_load returns None for an empty file.
    if data is None:
        logger.error(
            "Config file is empty or contains no valid YAML: %s", config_path
        )
        sys.exit(1)
    return data


# ---------------------------------------------------------------------------
# SEC-04: Slave address validation
# ---------------------------------------------------------------------------


def _validate_slave_address(addr: str) -> None:
    """Reject loopback / link-local / multicast addresses and malformed inputs.

    Args:
        addr: A hostname or IP address string from --slaves or config.

    Raises:
        ValueError: If the address fails validation.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # Not a bare IP — validate as a hostname.
        if not _SLAVE_HOSTNAME_RE.match(addr):
            raise ValueError(
                f"Slave address is not a valid hostname or IP: '{addr}'"
            )
        return  # valid hostname — accepted
    # Reject dangerous IP ranges.
    if ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise ValueError(
            f"Slave address cannot be loopback / link-local / multicast: '{addr}'"
        )


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
    result_fields: Optional[dict[str, bool]] = None,
) -> ScenarioResult:
    """Execute all load steps for a single scenario and return its result.

    Autonomous operation features:
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
    steady_state_seconds = scenario_cfg.get("steady_state_seconds", 120)
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

    # LOG-01 / LOG-05: derive file-system safe names once, reuse throughout.
    # Result files are named  results/<safe_scenario>_<safe_jmx>_<users>.csv
    # and clean_old_results() uses safe_jmx_name as the glob prefix.
    jmx_basename = Path(jmx_path).stem
    safe_jmx_name = (
        "".join(c for c in jmx_basename if c.isalnum() or c in ("_", "-")) or "unnamed"
    )
    safe_scenario_name = (
        "".join(c for c in name if c.isalnum() or c in ("_", "-")) or "unnamed"
    )

    # ── Resume from checkpoint ──────────────────────────────────────────────
    completed_users: set[int] = set()
    if resume:
        checkpoint = _load_checkpoint(name)
        if checkpoint:
            for run_data in checkpoint.get("runs", []):
                # LOG-02: reconstruct Metrics and warm up engine._history so
                # adaptive mode has full trend context after a restart.
                m_data = run_data.get("metrics") or {}
                metrics_obj: Optional[Metrics] = None
                if m_data and m_data.get("total_requests", 0) > 0:
                    try:
                        metrics_obj = Metrics(**m_data)
                        engine._history.append(metrics_obj)
                    except (TypeError, KeyError):
                        pass
                result.runs.append(
                    RunResult(
                        users=run_data["users"],
                        metrics=metrics_obj,
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
        warmup_duration = warmup_rampup + 60  # short steady-state for warmup
        _execute_step(
            name,
            jmx_path,
            warmup_users,
            warmup_rampup,
            runner,
            engine,
            retry_count=1,
            timeout=min(timeout, 300),
            duration=warmup_duration,
            slaves=slaves,
            result_fields=result_fields,
            discard=True,
            safe_jmx_name=safe_jmx_name,
            safe_scenario_name=safe_scenario_name,
        )
        logger.info(
            "Warmup complete — settling for %ds before escalation...",
            WARMUP_SETTLE_SECONDS,
        )
        time.sleep(WARMUP_SETTLE_SECONDS)

    # ── Main load escalation loop ───────────────────────────────────────────
    consecutive_failures = 0

    # LOG-06: replaced the fragile first_pending_idx scan with a simple flag.
    # Cooldown is applied only after the first step that actually executes in
    # this session, regardless of which steps were skipped from a checkpoint.
    first_real_step_done = False

    for i, users in enumerate(load_steps):
        # Skip steps already completed in a prior interrupted run.
        if users in completed_users:
            logger.info("Skipping %d-user step (completed in previous run)", users)
            continue

        # ── Cooldown between steps (Gap #2) ────────────────────────────────
        if first_real_step_done:
            logger.info(
                "Cooldown: waiting %ds for system recovery before next step...",
                cooldown,
            )
            time.sleep(cooldown)
        first_real_step_done = True

        # ── Per-step slave health check (Gap #4) ───────────────────────────
        active_slaves = slaves
        if slaves:
            try:
                # Use default RMI ports for slave health checks.
                active_slaves = check_slaves_alive(
                    slaves, ports=DEFAULT_RMI_PORTS
                )
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
        duration = rampup + steady_state_seconds
        logger.info(
            "Load step: scenario=%s | users=%d | rampup=%ds | duration=%ds",
            name, users, rampup, duration,
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
            duration=duration,
            slaves=active_slaves,
            result_fields=result_fields,
            safe_jmx_name=safe_jmx_name,
            safe_scenario_name=safe_scenario_name,
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
            # LOG-07: record the WARN entry in the audit trail BEFORE the re-test
            # so that both the warning signal and the re-test outcome are visible.
            result.runs.append(run)
            _save_checkpoint(result)
            # LOG-01: pass full prefix so glob matches the actual CSV filenames.
            Reporter.clean_old_results(f"{safe_scenario_name}_{safe_jmx_name}")

            time.sleep(max(30, cooldown // 2))  # short settle before re-test

            # LOG-03: remove the WARN step's history entry from the engine before
            # the re-test.  Without this, the same load level appears twice in the
            # slope window and artificially flattens the adaptive trend.
            if engine._history:
                engine._history.pop()

            # BUG-03: reset consecutive_failures before the re-test — the original
            # WARN run DID collect metrics, so it should not count toward the abort
            # threshold.  Without this reset, a single no-metric re-test could
            # incorrectly bring the counter to the abort limit.
            consecutive_failures = 0

            retest = _execute_step(
                name,
                jmx_path,
                users,
                rampup,
                runner,
                engine,
                retry_count,
                timeout,
                duration=duration,
                slaves=active_slaves,
                result_fields=result_fields,
                safe_jmx_name=safe_jmx_name,
                safe_scenario_name=safe_scenario_name,
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

        # ── Append this step's final result ────────────────────────────────
        # In the WARN path, the original WARN run was already appended above;
        # here we append the re-test outcome (STOP or PROCEED).
        # In all other paths (PROCEED / STOP / no-metrics) we append once here.
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
        # LOG-01: use full prefix (scenario + jmx name) to match actual CSV filenames.
        Reporter.clean_old_results(f"{safe_scenario_name}_{safe_jmx_name}")

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
    duration: Optional[int] = None,
    slaves: Optional[list[str]] = None,
    result_fields: Optional[dict[str, bool]] = None,
    discard: bool = False,
    safe_jmx_name: str = "",
    safe_scenario_name: str = "",
) -> RunResult:
    """Run one load step: execute JMeter, optionally parse results, evaluate.

    Args:
        discard:           When True (warmup mode), skip parsing and evaluation
                           and delete the result file immediately.  The engine's
                           history is NOT updated so warmup traffic does not skew
                           trend analysis.
        safe_jmx_name:     Pre-computed FS-safe JMX basename (derived by caller).
        safe_scenario_name: Pre-computed FS-safe scenario name (derived by caller).

    LOG-05: result_file includes both scenario name and JMX basename to prevent
    two scenarios that share a JMX file from overwriting each other's results.
    """
    # Derive names as fallback only — callers should always supply them.
    if not safe_jmx_name:
        jmx_stem = Path(jmx_path).stem
        safe_jmx_name = (
            "".join(c for c in jmx_stem if c.isalnum() or c in ("_", "-")) or "unnamed"
        )
    if not safe_scenario_name:
        safe_scenario_name = (
            "".join(c for c in name if c.isalnum() or c in ("_", "-")) or "unnamed"
        )

    # LOG-05: scenario name prefix prevents cross-scenario result file collisions.
    result_file = f"results/{safe_scenario_name}_{safe_jmx_name}_{users}.csv"

    # BUG-01: delete any pre-existing result file before launching JMeter.
    # JMeter appends results to an existing -l file rather than overwriting it.
    # Without this, a WARN re-test would merge its output with the prior run's data,
    # doubling the sample count and producing incorrect metrics and error rates.
    try:
        Path(result_file).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not delete stale result file %s: %s", result_file, exc)

    success, runner_output = runner.run(
        jmx_path,
        result_file,
        users,
        rampup=rampup,
        duration=duration,
        slaves=slaves,
        result_fields=result_fields,
        timeout=timeout,
        retry_count=retry_count,
    )

    # Warmup path — discard result and return PROCEED if successful.
    if discard:
        try:
            Path(result_file).unlink(missing_ok=True)
        except OSError:
            pass
        if not success:
            return RunResult(
                users=users,
                metrics=None,
                decision=Decision.STOP,
                reason=f"Warmup probe failed: {runner_output[:200]}",
            )
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
    elif not success:
        # runner.run() already logged the failure, but we need to return
        # a Decision.STOP so the orchestrator halts escalation.
        return RunResult(
            users=users,
            metrics=None,
            decision=Decision.STOP,
            reason=f"JMeter failed to start or crash: {runner_output[:200]}",
        )
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
        raw_slaves = [s.strip() for s in args.slaves.split(",") if s.strip()]
        # SEC-04: validate every slave address before attempting connectivity.
        for addr in raw_slaves:
            try:
                _validate_slave_address(addr)
            except ValueError as exc:
                logger.error("Invalid slave address from --slaves: %s", exc)
                sys.exit(1)
        slaves = raw_slaves
    elif config.get("slaves"):
        raw_slaves = list(config["slaves"])
        # SEC-04: also validate addresses sourced from the config file.
        for addr in raw_slaves:
            try:
                _validate_slave_address(addr)
            except ValueError as exc:
                logger.error("Invalid slave address in config: %s", exc)
                sys.exit(1)
        slaves = raw_slaves

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

    # Step 4: Resolve result fields.
    result_fields = config.get("result_fields")

    # Step 5: Pre-flight checks.
    if not args.skip_preflight:
        try:
            run_preflight_checks(
                config["scenarios"],
                slaves=slaves,
                jmeter_path=jmeter_path,
            )
        except PreflightError as exc:
            logger.error("Pre-flight check failed: %s", exc)
            sys.exit(1)
    else:
        logger.info("Pre-flight checks skipped (--skip-preflight)")

    # Step 6: Run all scenarios.
    runner = JMeterRunner(jmeter_path=jmeter_path)
    resume = not args.no_resume
    webhook = args.webhook_url or (config.get("notification", {}) or {}).get(
        "webhook_url"
    )

    scenario_results: list[ScenarioResult] = [
        run_scenario(
            cfg,
            runner,
            slaves=slaves,
            resume=resume,
            result_fields=result_fields,
        )
        for cfg in config["scenarios"]
    ]

    # Step 6: Generate reports, compare baseline, notify.
    smtp_config = config.get("smtp")
    _write_reports(scenario_results, webhook_url=webhook, smtp_config=smtp_config)
    logger.info("Performance testing complete. Reports saved to reports/")


if __name__ == "__main__":
    main()
