"""
parser.py - Parses JMeter result files (CSV or JTL format) into Metrics.
"""
import csv
import logging
import statistics
from pathlib import Path
from typing import Optional

from orchestrator.models import Metrics

logger = logging.getLogger(__name__)

_MIN_SAMPLES_FOR_PERCENTILES = 2


class ResultsParser:
    """Reads a JMeter result file and computes performance metrics.

    JMeter's default CSV/JTL columns expected:
        timeStamp, elapsed, label, responseCode, responseMessage,
        threadName, dataType, success, failureMessage, bytes, ...
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)

    def parse(self) -> Optional[Metrics]:
        """Parse the result file and return a Metrics object, or None if empty."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Result file not found: {self.file_path}")

        elapsed_times, error_count, total_count = self._read_rows()

        if total_count == 0:
            logger.warning("No valid rows found in %s", self.file_path)
            return None

        return Metrics(
            total_requests=total_count,
            error_count=error_count,
            error_percent=(error_count / total_count) * 100,
            avg_response_time=sum(elapsed_times) / total_count,
            min_response_time=min(elapsed_times),
            max_response_time=max(elapsed_times),
            p95=self._percentile(elapsed_times, 95),
            p99=self._percentile(elapsed_times, 99),
        )

    # --- Private helpers ---

    def _read_rows(self) -> tuple[list[int], int, int]:
        """Stream rows from the CSV file and accumulate raw counters."""
        elapsed_times: list[int] = []
        error_count = 0
        total_count = 0

        with self.file_path.open(mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    elapsed = int(row["elapsed"])
                    is_success = row["success"].strip().lower() == "true"
                except (ValueError, KeyError):
                    logger.debug("Skipping malformed row: %s", row)
                    continue

                elapsed_times.append(elapsed)
                if not is_success:
                    error_count += 1
                total_count += 1

        return elapsed_times, error_count, total_count

    @staticmethod
    def _percentile(data: list[int], pct: int) -> float:
        """Return the Nth percentile from a sorted data list."""
        if len(data) < _MIN_SAMPLES_FOR_PERCENTILES:
            return 0.0
        # quantiles(data, n=100) gives 99 cut points; index pct-1 gives the pct-th percentile
        return statistics.quantiles(data, n=100)[pct - 1]
