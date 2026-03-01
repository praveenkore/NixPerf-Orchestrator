"""
jmeter_runner.py - Thin wrapper around the JMeter CLI.
"""
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class JMeterRunner:
    """Executes JMeter in non-GUI distributed mode.

    Args:
        jmeter_path: Path (or executable name) for JMeter. Defaults to ``jmeter``
                     which assumes it is on the system PATH.
    """

    def __init__(self, jmeter_path: str = "jmeter") -> None:
        self.jmeter_path = jmeter_path

    def run(
        self,
        jmx_path: str,
        result_path: str,
        users: int,
        rampup: int = 60,
        slaves: Optional[list[str]] = None,
    ) -> tuple[bool, str]:
        """Run a JMeter test and return (success, output/error_message).

        Args:
            jmx_path:    Path to the JMX test plan.
            result_path: Destination for the result CSV/JTL file.
            users:       Number of concurrent users to inject via ``-Jusers``.
            rampup:      Ramp-up period in seconds, injected via ``-Jrampup``.
            slaves:      Optional list of slave IPs for distributed mode.
        """
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)

        command = self._build_command(jmx_path, result_path, users, rampup, slaves)
        logger.info("Executing: %s", " ".join(command))

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug("JMeter stdout: %s", process.stdout)
            return True, process.stdout
        except subprocess.CalledProcessError as exc:
            logger.error("JMeter failed (exit %d): %s", exc.returncode, exc.stderr)
            return False, exc.stderr
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error running JMeter")
            return False, str(exc)

    # --- Private helpers ---

    def _build_command(
        self,
        jmx_path: str,
        result_path: str,
        users: int,
        rampup: int,
        slaves: Optional[list[str]],
    ) -> list[str]:
        command = [
            self.jmeter_path,
            "-n",
            "-t", jmx_path,
            "-l", result_path,
            f"-Jusers={users}",
            f"-Jrampup={rampup}",
        ]
        if slaves:
            command.extend(["-R", ",".join(slaves)])
        return command
