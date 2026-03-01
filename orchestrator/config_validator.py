"""
config_validator.py - Validates scenarios.yaml before execution.

Checks:
    - Required fields present
    - jmx_path points to an existing file
    - load_steps is a sorted list of positive integers
    - SLA values are numeric and positive
    - error_threshold is between 0 and 100
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """Raised when scenario configuration is invalid."""


def validate_config(config: dict) -> None:
    """Validate the entire config dict. Raises ConfigValidationError on issues."""
    if "scenarios" not in config or not isinstance(config["scenarios"], list):
        raise ConfigValidationError("Config must contain a 'scenarios' list")

    if len(config["scenarios"]) == 0:
        raise ConfigValidationError("No scenarios defined in config")

    for idx, scenario in enumerate(config["scenarios"]):
        _validate_scenario(scenario, idx)

    logger.info("Config validation passed ✓ (%d scenarios)", len(config["scenarios"]))


def _validate_scenario(scenario: dict, idx: int) -> None:
    prefix = f"scenarios[{idx}]"

    # Required top-level fields
    for field in ("name", "jmx_path", "load_steps", "sla"):
        if field not in scenario:
            raise ConfigValidationError(f"{prefix}: missing required field '{field}'")

    name = scenario["name"]
    prefix = f"scenario '{name}'"

    # jmx_path exists
    jmx_path = Path(scenario["jmx_path"])
    if not jmx_path.exists():
        raise ConfigValidationError(f"{prefix}: JMX file not found: {jmx_path}")

    # load_steps validation
    steps = scenario["load_steps"]
    if not isinstance(steps, list) or len(steps) == 0:
        raise ConfigValidationError(f"{prefix}: load_steps must be a non-empty list")

    for i, step in enumerate(steps):
        if not isinstance(step, (int, float)) or step <= 0:
            raise ConfigValidationError(
                f"{prefix}: load_steps[{i}] must be a positive number, got {step}"
            )

    if steps != sorted(steps):
        logger.warning(
            "%s: load_steps are not sorted — will execute in given order: %s",
            prefix, steps,
        )

    # SLA validation
    sla = scenario["sla"]
    if not isinstance(sla, dict):
        raise ConfigValidationError(f"{prefix}: 'sla' must be a dictionary")

    if "p95" not in sla:
        raise ConfigValidationError(f"{prefix}: sla.p95 is required")
    if not isinstance(sla["p95"], (int, float)) or sla["p95"] <= 0:
        raise ConfigValidationError(f"{prefix}: sla.p95 must be a positive number")

    if "error_threshold" not in sla:
        raise ConfigValidationError(f"{prefix}: sla.error_threshold is required")
    threshold = sla["error_threshold"]
    if not isinstance(threshold, (int, float)) or not (0 <= threshold <= 100):
        raise ConfigValidationError(
            f"{prefix}: sla.error_threshold must be between 0 and 100, got {threshold}"
        )

    # Optional fields validation
    if "rampup" in scenario:
        rampup = scenario["rampup"]
        if not isinstance(rampup, (int, float)) or rampup <= 0:
            raise ConfigValidationError(
                f"{prefix}: rampup must be a positive number, got {rampup}"
            )

    if "retry_count" in scenario:
        rc = scenario["retry_count"]
        if not isinstance(rc, int) or rc < 0:
            raise ConfigValidationError(
                f"{prefix}: retry_count must be a non-negative integer, got {rc}"
            )

    if "timeout_seconds" in scenario:
        ts = scenario["timeout_seconds"]
        if not isinstance(ts, (int, float)) or ts <= 0:
            raise ConfigValidationError(
                f"{prefix}: timeout_seconds must be positive, got {ts}"
            )
