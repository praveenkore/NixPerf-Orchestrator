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
import re
from pathlib import Path

# SEC-03: scenario names are used in file paths — restrict to safe characters.
_SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """Raised when scenario configuration is invalid."""


def validate_config(config: dict) -> None:
    """Validate the entire config dict. Raises ConfigValidationError on issues."""
    # LOG-04: yaml.safe_load returns None for an empty file; guard before key lookup.
    if not isinstance(config, dict):
        raise ConfigValidationError(
            f"Config must be a YAML mapping, got: {type(config).__name__}"
        )
    if "scenarios" not in config or not isinstance(config["scenarios"], list):
        raise ConfigValidationError("Config must contain a 'scenarios' list")

    if len(config["scenarios"]) == 0:
        raise ConfigValidationError("No scenarios defined in config")

    for idx, scenario in enumerate(config["scenarios"]):
        _validate_scenario(scenario, idx)

    # ── Optional top-level keys ─────────────────────────────────────────────

    if "slaves" in config:
        slaves = config["slaves"]
        if not isinstance(slaves, list) or not all(isinstance(s, str) for s in slaves):
            raise ConfigValidationError(
                "Top-level 'slaves' must be a list of hostname/IP strings"
            )

    if "rmi_port" in config:
        port = config["rmi_port"]
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ConfigValidationError(
                f"Top-level 'rmi_port' must be an integer between 1 and 65535, got {port}"
            )

    if "notification" in config:
        notif = config["notification"]
        if not isinstance(notif, dict):
            raise ConfigValidationError("'notification' must be a dictionary")
        if "webhook_url" in notif:
            url = notif["webhook_url"]
            # SEC-05: only HTTPS is accepted at runtime (reporting._validate_webhook_url
            # rejects HTTP silently); fail fast here to surface misconfiguration early.
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ConfigValidationError(
                    f"notification.webhook_url must use HTTPS, got: '{url}'"
                )

    logger.info("Config validation passed ✓ (%d scenarios)", len(config["scenarios"]))


def _validate_scenario(scenario: dict, idx: int) -> None:
    prefix = f"scenarios[{idx}]"

    # Required top-level fields
    for field in ("name", "jmx_path", "load_steps", "sla"):
        if field not in scenario:
            raise ConfigValidationError(f"{prefix}: missing required field '{field}'")

    name = scenario["name"]
    prefix = f"scenario '{name}'"

    # SEC-03: scenario name is embedded in checkpoint and result file paths;
    # restrict to alphanumeric/underscore/hyphen to prevent path traversal.
    if not _SAFE_NAME_RE.match(name):
        raise ConfigValidationError(
            f"{prefix}: scenario 'name' must be 1–64 alphanumeric, underscore, "
            f"or hyphen characters (got: '{name}')"
        )

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

    # ramp_strategy validation
    if "ramp_strategy" in scenario:
        _validate_ramp_strategy(scenario["ramp_strategy"], prefix)
    elif "rampup" in scenario:
        # Legacy static rampup — still accepted for backward compat
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

    # ── Autonomous operation fields (all optional) ──────────────────────────

    if "cooldown_seconds" in scenario:
        cd = scenario["cooldown_seconds"]
        if not isinstance(cd, (int, float)) or cd < 0:
            raise ConfigValidationError(
                f"{prefix}: cooldown_seconds must be >= 0, got {cd}"
            )

    if "warmup_users" in scenario:
        wu = scenario["warmup_users"]
        if not isinstance(wu, int) or wu < 0:
            raise ConfigValidationError(
                f"{prefix}: warmup_users must be a non-negative integer, got {wu}"
            )

    if "max_consecutive_failures" in scenario:
        mcf = scenario["max_consecutive_failures"]
        if not isinstance(mcf, int) or mcf < 1:
            raise ConfigValidationError(
                f"{prefix}: max_consecutive_failures must be >= 1, got {mcf}"
            )

    if "mode" in scenario:
        valid_modes = ("static", "adaptive")
        if scenario["mode"] not in valid_modes:
            raise ConfigValidationError(
                f"{prefix}: mode must be one of {valid_modes}, got '{scenario['mode']}'"
            )


def _validate_ramp_strategy(ramp: dict, prefix: str) -> None:
    """Validate the ramp_strategy block."""
    if not isinstance(ramp, dict):
        raise ConfigValidationError(f"{prefix}: ramp_strategy must be a dictionary")

    strategy_type = ramp.get("type")
    if not strategy_type:
        raise ConfigValidationError(f"{prefix}: ramp_strategy.type is required")

    valid_types = ("constant_arrival", "fixed", "proportional")
    if strategy_type not in valid_types:
        raise ConfigValidationError(
            f"{prefix}: ramp_strategy.type must be one of {valid_types}, got '{strategy_type}'"
        )

    if strategy_type == "constant_arrival":
        _validate_positive(ramp, "arrival_rate", prefix)

    elif strategy_type == "fixed":
        _validate_positive(ramp, "value", prefix)

    elif strategy_type == "proportional":
        _validate_positive(ramp, "base_users", prefix)
        _validate_positive(ramp, "base_ramp", prefix)


def _validate_positive(config: dict, field: str, prefix: str) -> None:
    """Validate that a field exists and is a positive number."""
    val = config.get(field)
    if val is None:
        raise ConfigValidationError(f"{prefix}: ramp_strategy.{field} is required")
    if not isinstance(val, (int, float)) or val <= 0:
        raise ConfigValidationError(
            f"{prefix}: ramp_strategy.{field} must be > 0, got {val}"
        )
