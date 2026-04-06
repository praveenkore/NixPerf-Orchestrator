"""
jmeter_runner.py - Thin wrapper around the JMeter CLI with retry, timeout,
and real-time output streaming.

Changes from original:
    - Replaced subprocess.run (buffered) with Popen + background reader thread
      so JMeter's console output is streamed live to the log.
    - JMeter summary lines ("summary =") are parsed and emitted at INFO level
      so operators can monitor throughput / error rate without waiting for the
      run to finish.
    - Retry delay is unchanged at 5 s; all other behaviour is backward-compatible.

Security / reliability fixes (vs previous revision):
    SEC-01  — Added a dedicated stderr drain thread.  Previously stderr was
              collected only after the process exited.  If JMeter wrote more
              than ~64 KB to stderr (crash stack trace, verbose GC log) the OS
              pipe buffer would fill and the process would block forever while
              the main thread waited in process.wait() — a classic deadlock.
    PERF-02 — stdout_lines and stderr_lines are now bounded deques
              (max _MAX_CAPTURED_LINES entries each).  Long 2-hour runs with
              verbose JMeter output no longer accumulate unbounded memory.
    PERF-05 — Removed the per-line threading.Lock() around stdout_lines.append().
              There is exactly one writer (_drain_stdout) and the main thread
              only reads stdout_lines after reader.join(), so no concurrent
              access is possible.  The lock was protecting against a race that
              could not occur.  stderr_lines follows the same pattern.
"""

import collections
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 7200  # 2 hours
DEFAULT_RETRY_COUNT = 1

# PERF-02: cap the number of lines retained in memory per run.
# Lines beyond this limit are still forwarded to the logger (debug level)
# but are not kept in the in-memory buffer.
_MAX_CAPTURED_LINES = 500

# Matches both JMeter "summary =" (cumulative) and "summary +" (interval) lines.
# Example:
#   summary =  12500 in 00:02:05 = 100.0/s Avg: 145 Min: 12 Max: 3201 Err: 23 (0.18%)
_SUMMARY_RE = re.compile(
    r"summary\s*[+=]\s*(\d+)\s+in\s+[\d:]+\s*=\s*([\d.]+)/s"
    r"\s+Avg:\s*(\d+).*?Err:\s*(\d+)\s*\(([\d.]+)%\)",
    re.IGNORECASE,
)


def _parse_summary_line(line: str) -> None:
    """Emit a structured INFO log from a JMeter console summary line."""
    match = _SUMMARY_RE.search(line)
    if match:
        samples, rate, avg_ms, errors, err_pct = match.groups()
        logger.info(
            "  ↳ Live progress — samples: %s | throughput: %s/s | "
            "avg: %sms | errors: %s (%.1f%%)",
            samples,
            rate,
            avg_ms,
            errors,
            float(err_pct),
        )


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
        """Run a JMeter test with retry, timeout, and live output streaming.

        Args:
            jmx_path:    Path to the JMX test plan.
            result_path: Destination for the result CSV/JTL file.
            users:       Number of concurrent users to inject via ``-Jusers``.
            rampup:      Ramp-up period in seconds, injected via ``-Jrampup``.
            slaves:      Optional list of slave IPs for distributed mode.
            timeout:     Max seconds to wait before killing the process.
            retry_count: Number of retry attempts on failure (total attempts = retry_count + 1).

        Returns:
            (success, output_text)
        """
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        command = self._build_command(jmx_path, result_path, users, rampup, slaves)

        output = ""
        for attempt in range(
            1, retry_count + 2
        ):  # +2: range exclusive + initial attempt
            logger.info(
                "Executing (attempt %d/%d): %s",
                attempt,
                retry_count + 1,
                " ".join(command),
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

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _execute(self, command: list[str], timeout: int) -> tuple[bool, str]:
        """Launch JMeter as a subprocess and stream its output in real time.

        Uses two background daemon threads to drain stdout and stderr
        concurrently so that:
          - The terminal / log file shows live progress during long runs.
          - JMeter summary lines trigger structured INFO log entries.
          - The main thread can enforce a wall-clock timeout independently.
          - SEC-01: stderr is continuously drained, preventing the OS pipe
            buffer from filling and deadlocking the process.

        Returns:
            (success, captured_stdout_text)
        """
        # PERF-02: bounded deques — memory stays constant regardless of run length.
        # PERF-05: no lock needed; each deque has exactly one writer thread and is
        # read only after the respective thread is joined (happens-before guarantee).
        stdout_lines: collections.deque = collections.deque(maxlen=_MAX_CAPTURED_LINES)
        stderr_lines: collections.deque = collections.deque(maxlen=_MAX_CAPTURED_LINES)

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def _drain_stdout() -> None:
                """Read stdout line-by-line and forward to logger."""
                try:
                    for line in process.stdout:  # type: ignore[union-attr]
                        stripped = line.rstrip()
                        if stripped:
                            stdout_lines.append(stripped)
                            logger.debug("[jmeter] %s", stripped)
                            if "summary" in stripped.lower():
                                _parse_summary_line(stripped)
                except ValueError:
                    pass

            def _drain_stderr() -> None:
                """SEC-01: drain stderr continuously to prevent pipe-buffer deadlock."""
                try:
                    for line in process.stderr:  # type: ignore[union-attr]
                        stripped = line.rstrip()
                        if stripped:
                            stderr_lines.append(stripped)
                            logger.debug("[jmeter-err] %s", stripped)
                except ValueError:
                    pass

            reader = threading.Thread(target=_drain_stdout, daemon=True)
            err_reader = threading.Thread(target=_drain_stderr, daemon=True)
            reader.start()
            err_reader.start()

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                reader.join(timeout=15)
                err_reader.join(timeout=15)
                captured = "\n".join(stdout_lines)
                logger.error("JMeter timed out after %ds — process killed", timeout)
                return False, captured or f"Timeout after {timeout}s"

            reader.join(timeout=30)
            err_reader.join(timeout=30)
            full_output = "\n".join(stdout_lines)

            if process.returncode != 0:
                stderr_text = "\n".join(stderr_lines)
                logger.error(
                    "JMeter failed (exit %d). stderr: %s",
                    process.returncode,
                    stderr_text[-500:] if stderr_text else "<empty>",
                )
                return False, stderr_text or f"Exit code {process.returncode}"

            logger.debug(
                "JMeter completed successfully (%d output lines captured)",
                len(stdout_lines),
            )
            return True, full_output

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
            "-t",
            jmx_path,
            "-l",
            result_path,
            f"-Jusers={users}",
            f"-Jrampup={rampup}",
        ]
        if slaves:
            command.extend(["-R", ",".join(slaves)])
        return command
