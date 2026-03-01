"""
ramp_engine.py - Dynamic ramp-up calculation based on configurable strategies.

Supported strategies:
    - constant_arrival: rampup = users / arrival_rate
    - fixed:            rampup = value
    - proportional:     rampup = base_ramp * (users / base_users)

Production safety:
    - Ramp-up is always >= 1
    - Ramp-up never exceeds 4x user count
    - Division by zero is guarded
    - Invalid configs fail fast with ValueError

Extensibility:
    Future strategies (adaptive, spike, soak, logarithmic) can be added
    by registering a new handler in _STRATEGY_HANDLERS.
"""
import logging
import math
from typing import Callable

logger = logging.getLogger(__name__)

# Safety constants
MIN_RAMPUP = 1
MAX_RAMPUP_MULTIPLIER = 4  # rampup <= users * 4


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------

def _constant_arrival(users: int, config: dict) -> int:
    """rampup = users / arrival_rate"""
    arrival_rate = config.get("arrival_rate")
    if arrival_rate is None:
        raise ValueError("constant_arrival strategy requires 'arrival_rate'")
    if not isinstance(arrival_rate, (int, float)) or arrival_rate <= 0:
        raise ValueError(f"arrival_rate must be > 0, got {arrival_rate}")

    return math.ceil(users / arrival_rate)


def _fixed(users: int, config: dict) -> int:
    """rampup = value (constant regardless of users)"""
    value = config.get("value")
    if value is None:
        raise ValueError("fixed strategy requires 'value'")
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"fixed value must be > 0, got {value}")

    return int(value)


def _proportional(users: int, config: dict) -> int:
    """rampup = base_ramp * (users / base_users)"""
    base_users = config.get("base_users")
    base_ramp = config.get("base_ramp")

    if base_users is None:
        raise ValueError("proportional strategy requires 'base_users'")
    if base_ramp is None:
        raise ValueError("proportional strategy requires 'base_ramp'")
    if not isinstance(base_users, (int, float)) or base_users <= 0:
        raise ValueError(f"base_users must be > 0, got {base_users}")
    if not isinstance(base_ramp, (int, float)) or base_ramp <= 0:
        raise ValueError(f"base_ramp must be > 0, got {base_ramp}")

    return math.ceil(base_ramp * (users / base_users))


# ---------------------------------------------------------------------------
# Strategy registry — add future strategies here
# ---------------------------------------------------------------------------

_STRATEGY_HANDLERS: dict[str, Callable[[int, dict], int]] = {
    "constant_arrival": _constant_arrival,
    "fixed": _fixed,
    "proportional": _proportional,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_default_ramp_strategy(first_load_step: int) -> dict:
    """Return the default ramp strategy for backward compatibility.

    Falls back to proportional with base_users=first_load_step, base_ramp=60.
    """
    return {
        "type": "proportional",
        "base_users": first_load_step,
        "base_ramp": 60,
    }


def calculate_rampup(users: int, ramp_config: dict) -> int:
    """Calculate dynamic ramp-up based on the configured strategy.

    Args:
        users:       Number of concurrent users for this load step.
        ramp_config: The ``ramp_strategy`` dict from scenarios.yaml.

    Returns:
        Ramp-up in seconds (always >= 1, capped at 4x users).

    Raises:
        ValueError: If the configuration is invalid or strategy unknown.
    """
    if users <= 0:
        raise ValueError(f"users must be > 0, got {users}")

    strategy_type = ramp_config.get("type")
    if not strategy_type:
        raise ValueError("ramp_strategy must include a 'type' field")

    handler = _STRATEGY_HANDLERS.get(strategy_type)
    if handler is None:
        supported = ", ".join(sorted(_STRATEGY_HANDLERS.keys()))
        raise ValueError(
            f"Unknown ramp strategy '{strategy_type}'. Supported: {supported}"
        )

    raw_rampup = handler(users, ramp_config)

    # Production safety: clamp to valid range
    max_allowed = users * MAX_RAMPUP_MULTIPLIER
    rampup = max(MIN_RAMPUP, min(raw_rampup, max_allowed))

    if rampup != raw_rampup:
        logger.warning(
            "Ramp-up clamped: raw=%d → clamped=%d (min=%d, max=%d)",
            raw_rampup, rampup, MIN_RAMPUP, max_allowed,
        )

    logger.info(
        "Strategy=%s | Users=%d | RampUp=%ds",
        strategy_type, users, rampup,
    )
    return rampup
