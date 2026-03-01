"""
preflight.py - Pre-flight health checks before running any test.

Validates:
    - Slave nodes reachable on RMI ports
    - JMX file exists
    - Results directory is writable
    - Sufficient disk space
"""
import logging
import os
import socket
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RMI_PORTS = [1099, 50000]
MIN_DISK_SPACE_MB = 500


class PreflightError(Exception):
    """Raised when a pre-flight check fails."""


def run_preflight_checks(
    scenarios: list[dict],
    slaves: Optional[list[str]] = None,
) -> None:
    """Run all pre-flight checks. Raises PreflightError on first failure."""
    logger.info("Running pre-flight checks...")

    _check_result_dir()
    _check_disk_space()

    for scenario in scenarios:
        _check_jmx_exists(scenario["jmx_path"])

    if slaves:
        for slave in slaves:
            _check_slave_connectivity(slave)

    logger.info("All pre-flight checks passed ✓")


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
    """Check that at least MIN_DISK_SPACE_MB is available."""
    try:
        stat = os.statvfs(".") if hasattr(os, "statvfs") else None
        if stat:
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_mb < MIN_DISK_SPACE_MB:
                raise PreflightError(
                    f"Low disk space: {free_mb:.0f} MB available, "
                    f"need at least {MIN_DISK_SPACE_MB} MB"
                )
            logger.debug("Disk space OK: %.0f MB free", free_mb)
        else:
            # Windows fallback — skip detailed check
            logger.debug("Disk space check skipped (Windows)")
    except PreflightError:
        raise
    except Exception:
        logger.debug("Disk space check skipped (not supported)")


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
