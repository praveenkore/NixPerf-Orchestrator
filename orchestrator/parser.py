"""
parser.py - Parses JMeter result files (CSV or JTL) in batches for memory efficiency.

For very large result files (millions of rows), the parser reads data in configurable
batch sizes and uses reservoir sampling to estimate percentiles without holding all
elapsed times in memory.

Metrics computed:
    - total_requests, error_count, error_percent
    - avg_response_time, min_response_time, max_response_time
    - p95, p99  (via reservoir sampling — accurate approximation for large files)
"""
import csv
import logging
import random
from pathlib import Path
from typing import Optional

from orchestrator.models import Metrics

logger = logging.getLogger(__name__)

# Default batch size when streaming the CSV
DEFAULT_BATCH_SIZE: int = 10_000

# Maximum samples kept in the reservoir for percentile estimation.
# At 100k entries of int (8 bytes each) ≈ 800 KB — negligible.
DEFAULT_RESERVOIR_SIZE: int = 100_000


class ResultsParser:
    """Reads a JMeter result file and computes performance metrics.

    Processes the file in fixed-size batches to keep memory usage constant
    regardless of file size.  Percentiles are estimated via reservoir sampling.

    JMeter's default CSV/JTL columns expected:
        timeStamp, elapsed, label, responseCode, responseMessage,
        threadName, dataType, success, failureMessage, bytes, ...

    Args:
        file_path:      Path to the JMeter result CSV/JTL file.
        batch_size:     Number of rows to process per iteration (default: 10 000).
        reservoir_size: Max samples retained for percentile estimation (default: 100 000).
    """

    def __init__(
        self,
        file_path: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        reservoir_size: int = DEFAULT_RESERVOIR_SIZE,
    ) -> None:
        self.file_path = Path(file_path)
        self.batch_size = batch_size
        self.reservoir_size = reservoir_size

    def parse(self) -> Optional[Metrics]:
        """Stream-parse the result file and return computed Metrics, or None if empty."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Result file not found: {self.file_path}")

        aggregator = _RunningAggregator(self.reservoir_size)

        with self.file_path.open(mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            batch: list[tuple[int, bool]] = []

            for row in reader:
                parsed = self._parse_row(row)
                if parsed is None:
                    continue

                batch.append(parsed)

                if len(batch) >= self.batch_size:
                    aggregator.consume(batch)
                    batch.clear()
                    logger.debug(
                        "Processed batch — total so far: %d rows", aggregator.total_count
                    )

            # Flush any remaining rows
            if batch:
                aggregator.consume(batch)

        if aggregator.total_count == 0:
            logger.warning("No valid rows found in %s", self.file_path)
            return None

        logger.info(
            "Parsed %d rows from %s (reservoir: %d samples)",
            aggregator.total_count,
            self.file_path.name,
            len(aggregator.reservoir),
        )
        return aggregator.to_metrics()

    # --- Private helpers ---

    @staticmethod
    def _parse_row(row: dict) -> Optional[tuple[int, bool]]:
        """Extract (elapsed_ms, is_success) from a raw CSV row, or None if malformed."""
        try:
            elapsed = int(row["elapsed"])
            is_success = row["success"].strip().lower() == "true"
            return elapsed, is_success
        except (ValueError, KeyError):
            logger.debug("Skipping malformed row: %s", row)
            return None


class _RunningAggregator:
    """Maintains running statistics across batches for a streaming parse.

    Uses Vitter's reservoir sampling algorithm (Algorithm R) to maintain a
    fixed-size random sample of elapsed times for percentile estimation.
    """

    def __init__(self, reservoir_size: int) -> None:
        self.reservoir_size = reservoir_size
        self.total_count: int = 0
        self.error_count: int = 0
        self._sum: float = 0.0
        self._min: int = 2**62
        self._max: int = 0
        self.reservoir: list[int] = []

    def consume(self, batch: list[tuple[int, bool]]) -> None:
        """Process one batch of (elapsed, is_success) tuples."""
        for elapsed, is_success in batch:
            self.total_count += 1
            self._sum += elapsed
            if elapsed < self._min:
                self._min = elapsed
            if elapsed > self._max:
                self._max = elapsed
            if not is_success:
                self.error_count += 1

            # Reservoir sampling — Algorithm R
            if len(self.reservoir) < self.reservoir_size:
                self.reservoir.append(elapsed)
            else:
                replace_idx = random.randint(0, self.total_count - 1)
                if replace_idx < self.reservoir_size:
                    self.reservoir[replace_idx] = elapsed

    def to_metrics(self) -> Metrics:
        """Build the final Metrics dataclass from accumulated statistics."""
        return Metrics(
            total_requests=self.total_count,
            error_count=self.error_count,
            error_percent=(self.error_count / self.total_count) * 100,
            avg_response_time=self._sum / self.total_count,
            min_response_time=float(self._min),
            max_response_time=float(self._max),
            p95=self._percentile(95),
            p99=self._percentile(99),
        )

    def _percentile(self, pct: int) -> float:
        """Estimate the Nth percentile from the reservoir sample."""
        if not self.reservoir:
            return 0.0
        sorted_reservoir = sorted(self.reservoir)
        idx = max(0, int(len(sorted_reservoir) * pct / 100) - 1)
        return float(sorted_reservoir[idx])
