"""
jmeter_runner.py - Thin wrapper around the JMeter CLI with retry and timeout support.
"""
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 7200  # 2 hours
DEFAULT_RETRY_COUNT = 1


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
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        retry_count: int = DEFAULT_RETRY_COUNT,
    ) -> tuple[bool, str]:
        """Run a JMeter test with retry and timeout support.

        Args:
            jmx_path:    Path to the JMX test plan.
            result_path: Destination for the result CSV/JTL file.
            users:       Number of concurrent users to inject via ``-Jusers``.
            rampup:      Ramp-up period in seconds, injected via ``-Jrampup``.
            slaves:      Optional list of slave IPs for distributed mode.
            timeout:     Max seconds to wait before killing the process.
            retry_count: Number of retry attempts on failure.
        """
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        command = self._build_command(jmx_path, result_path, users, rampup, slaves)

        for attempt in range(1, retry_count + 2):  # +2 because range is exclusive & includes initial
            logger.info(
                "Executing (attempt %d/%d): %s",
                attempt, retry_count + 1, " ".join(command),
            )

            success, output = self._execute(command, timeout)

            if success:
                return True, output

            if attempt <= retry_count:
                logger.warning(
                    "JMeter failed on attempt %d — retrying in 5s...", attempt
                )
                time.sleep(5)
            else:
                logger.error(
                    "JMeter failed after %d attempt(s). Aborting.", retry_count + 1
                )

        return False, output

    # --- Private helpers ---

    def _execute(self, command: list[str], timeout: int) -> tuple[bool, str]:
        """Run the subprocess with timeout and capture output."""
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
            )
            logger.debug("JMeter stdout:\n%s", process.stdout[-500:] if process.stdout else "")
            return True, process.stdout
        except subprocess.TimeoutExpired:
            logger.error("JMeter timed out after %ds — killing process", timeout)
            return False, f"Timeout after {timeout}s"
        except subprocess.CalledProcessError as exc:
            logger.error(
                "JMeter failed (exit %d). stderr: %s",
                exc.returncode,
                exc.stderr[-500:] if exc.stderr else "",
            )
            return False, exc.stderr or f"Exit code {exc.returncode}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error running JMeter")
            return False, str(exc)

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
