"""
preflight.py - Pre-flight health checks before running any test.

Validates:
    - Slave nodes reachable on RMI ports
    - JMX file exists
    - Results directory is writable
    - Sufficient disk space

Additional public helper (used per load step in main.py):
    check_slaves_alive() — lightweight per-step slave health re-check that
    returns the list of currently reachable slaves and raises PreflightError
    if too many have gone offline.
"""

import concurrent.futures
import logging
import shutil
import socket
from pathlib import Path
from typing import Optional
from orchestrator.jmeter_runner import DEFAULT_RMI_SERVER_PORT, DEFAULT_SERVER_RMI_LOCALPORT

logger = logging.getLogger(__name__)

DEFAULT_RMI_PORTS = [DEFAULT_RMI_SERVER_PORT, DEFAULT_SERVER_RMI_LOCALPORT]
MIN_DISK_SPACE_MB = 500

# Fraction of the original slave pool that must remain alive.
# If fewer than this fraction respond, the step is aborted.
DEFAULT_SLAVE_ALIVE_THRESHOLD = 0.5


class PreflightError(Exception):
    """Raised when a pre-flight check fails."""


def run_preflight_checks(
    scenarios: list[dict],
    slaves: Optional[list[str]] = None,
    jmeter_path: str = "jmeter",
) -> None:
    """Run all pre-flight checks. Raises PreflightError on first failure."""
    logger.info("Running pre-flight checks...")

    _check_jmeter_executable(jmeter_path)
    _check_result_dir()
    _check_disk_space()

    for scenario in scenarios:
        _check_jmx_exists(scenario["jmx_path"])

    if slaves:
        for slave in slaves:
            _check_slave_connectivity(slave, ports=DEFAULT_RMI_PORTS)

    logger.info("All pre-flight checks passed ✓")


def _check_jmeter_executable(jmeter_path: str) -> None:
    """Verify that the JMeter executable is available in PATH or at the given path."""
    if shutil.which(jmeter_path) is None:
        raise PreflightError(
            f"JMeter executable not found: '{jmeter_path}'. "
            f"Please ensure JMeter is installed and in your PATH, "
            f"or specify the path with --jmeter-path."
        )
    logger.debug("JMeter executable found: %s", jmeter_path)


def _check_jmx_exists(jmx_path: str) -> None:
    if not Path(jmx_path).exists():
        raise PreflightError(f"JMX file not found: {jmx_path}")
    logger.debug("JMX found: %s", jmx_path)


def _check_result_dir() -> None:
    result_dir = Path("results")
    result_dir.mkdir(parents=True, exist_ok=True)
    test_file = result_dir / ".write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        logger.debug("Results directory is writable")
    except OSError as exc:
        raise PreflightError(f"Results directory not writable: {exc}") from exc


def _check_disk_space() -> None:
    """Check that at least MIN_DISK_SPACE_MB is available.

    PERF-04: Uses shutil.disk_usage() which works on all platforms
    (Linux, macOS, Windows) instead of the Unix-only os.statvfs().
    """
    try:
        usage = shutil.disk_usage(".")
        free_mb = usage.free / (1024 * 1024)
        if free_mb < MIN_DISK_SPACE_MB:
            raise PreflightError(
                f"Low disk space: {free_mb:.0f} MB available, "
                f"need at least {MIN_DISK_SPACE_MB} MB"
            )
        logger.debug("Disk space OK: %.0f MB free", free_mb)
    except PreflightError:
        raise
    except Exception as exc:
        logger.debug("Disk space check skipped: %s", exc)


def check_slaves_alive(
    slaves: list[str],
    alive_threshold: float = DEFAULT_SLAVE_ALIVE_THRESHOLD,
    ports: list[int] = DEFAULT_RMI_PORTS,
) -> list[str]:
    """Per-step lightweight slave health check — called before every load step.

    Unlike the startup ``run_preflight_checks``, this function is tolerant of
    partial slave failure: it logs a warning for each unreachable slave and
    returns only the alive subset, *unless* the fraction of alive slaves drops
    below ``alive_threshold`` in which case it raises ``PreflightError`` so
    the caller can abort the scenario rather than run under-loaded.

    Args:
        slaves:          List of slave hostnames or IP addresses to probe.
        alive_threshold: Minimum fraction of slaves that must be reachable
                         (default: 0.5 — at least 50 % must be alive).
        ports:           RMI ports to probe (default: [1099, 50000]).

    Returns:
        Sorted list of slave addresses that are currently reachable.

    Raises:
        PreflightError: If fewer than ``alive_threshold`` fraction of slaves respond.
    """
    if not slaves:
        return []

    alive: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(slaves), 20)
    ) as executor:
        future_to_slave = {
            executor.submit(_check_slave_connectivity, slave, ports): slave
            for slave in slaves
        }
        for future in concurrent.futures.as_completed(future_to_slave):
            slave = future_to_slave[future]
            try:
                future.result()
                alive.append(slave)
            except PreflightError:
                logger.warning(
                    "Slave %s is unreachable — excluding from this load step", slave
                )

    alive_fraction = len(alive) / len(slaves)
    if alive_fraction < alive_threshold:
        raise PreflightError(
            f"Only {len(alive)}/{len(slaves)} slave(s) are alive "
            f"({alive_fraction * 100:.0f}% < required {alive_threshold * 100:.0f}%). "
            f"Aborting load step to avoid under-loaded results."
        )

    if len(alive) < len(slaves):
        logger.warning(
            "Reduced slave pool for this step: %d/%d alive — "
            "load will be distributed across fewer nodes",
            len(alive),
            len(slaves),
        )
    else:
        logger.debug("All %d slave(s) are alive ✓", len(slaves))

    return alive


def _check_slave_connectivity(slave: str, ports: list[int] = DEFAULT_RMI_PORTS) -> None:
    """Verify that the slave is reachable on all required RMI ports."""
    for port in ports:
        try:
            sock = socket.create_connection((slave, port), timeout=5)
            sock.close()
            logger.debug("Slave %s:%d reachable", slave, port)
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            raise PreflightError(
                f"Cannot reach slave {slave}:{port} — {exc}. "
                f"Ensure jmeter-server is running and port is open."
            ) from exc
