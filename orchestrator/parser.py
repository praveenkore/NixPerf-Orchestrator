"""
parser.py - Parses JMeter result files (CSV or JTL) in batches for memory efficiency.

For very large result files (millions of rows), the parser reads data in configurable
batch sizes and uses reservoir sampling to estimate percentiles without holding all
elapsed times in memory.

Metrics computed:
    - total_requests, error_count, error_percent
    - avg_response_time, min_response_time, max_response_time
    - p95, p99  (via reservoir sampling — accurate approximation for large files)

Error counting (PARSE-01):
    JMeter has two independent notions of "failure":
      1. HTTP-level errors  — response code is not in 2xx range (e.g. 4xx, 5xx).
         This is what JMeter's console 'Err:' counter measures.
      2. Assertion failures — a JMeter assertion (response-time, body-content, etc.)
         failed, even if the HTTP code was 200.  These set success=false in the CSV
         but are NOT counted in the console 'Err:' total.

    The original code used `success` from the CSV, which includes assertion failures
    and caused a large discrepancy between the orchestrator's reported error rate and
    the JMeter console.  The fix counts HTTP-level errors (responseCode not in 1xx-3xx)
    to match the JMeter console.  Both counts are logged at INFO level for visibility.
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

# Minimum number of data rows required before results are considered trustworthy.
# Files with fewer rows are flagged as possibly truncated/incomplete.
MIN_VALID_ROWS: int = 10

# Minimal CSV format saves 5 columns (timeStamp, elapsed, label,
# responseCode, success) = 4 commas per row.  Fewer than this on the
# last row is a reliable indicator of truncation.
MIN_EXPECTED_COLUMNS: int = 4


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

        self._check_file_integrity()  # warn-only; never blocks parsing

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
                        "Processed batch — total so far: %d rows",
                        aggregator.total_count,
                    )

            # Flush any remaining rows
            if batch:
                aggregator.consume(batch)

        if aggregator.total_count == 0:
            # DIAG-02: logic to surface why parsing failed (missing headers, etc.)
            try:
                with self.file_path.open(mode="r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    logger.warning(
                        "No valid rows found in %s. First line of file: '%s'. "
                        "Expected header with 'elapsed' and 'success' columns.",
                        self.file_path,
                        first_line,
                    )
            except Exception: # noqa: BLE001
                logger.warning("No valid rows found in %s", self.file_path)
            return None

        # PARSE-01: log both HTTP errors and assertion failures so mismatches are visible.
        assertion_failures = aggregator.assertion_failure_count
        http_errors = aggregator.error_count
        if assertion_failures != http_errors:
            logger.info(
                "Error breakdown for %s — HTTP errors: %d (%.1f%%) | "
                "Assertion failures (success=false): %d (%.1f%%) | "
                "Using HTTP errors for SLA evaluation (matches JMeter console)",
                self.file_path.name,
                http_errors,
                (http_errors / aggregator.total_count) * 100,
                assertion_failures,
                (assertion_failures / aggregator.total_count) * 100,
            )

        logger.info(
            "Parsed %d rows from %s (reservoir: %d samples)",
            aggregator.total_count,
            self.file_path.name,
            len(aggregator.reservoir),
        )
        return aggregator.to_metrics()

    # --- Private helpers ---

    def _check_file_integrity(self) -> bool:
        """Warn (but never raise) if the JTL file looks truncated or suspiciously small.

        PERF-01: Previously this method did an O(n) full scan (open + read every
        line) and then parse() immediately did a second full scan.  The new
        implementation is O(1):
            1. os.stat() for a quick size sanity check (replaces row-count scan).
            2. Seek to the last 512 bytes to inspect only the final line for
               truncation (replaces reading the entire file to reach the last row).

        Returns:
            True  — file looks complete.
            False — file looks incomplete; a WARNING has been logged.
        """
        try:
            file_size = self.file_path.stat().st_size
            # A valid JTL needs at least: header row + MIN_VALID_ROWS data rows.
            # A minimal row is ~30 bytes; add ~80 bytes for the header.
            min_expected_bytes = 80 + MIN_VALID_ROWS * 30
            if file_size < min_expected_bytes:
                logger.warning(
                    "JTL file is only %d bytes — possibly incomplete or truncated "
                    "(expected >= %d bytes for %d rows): %s",
                    file_size,
                    min_expected_bytes,
                    MIN_VALID_ROWS,
                    self.file_path.name,
                )
                return False

            # Read the last 512 bytes in binary mode to check the final row.
            with self.file_path.open("rb") as fbin:
                fbin.seek(max(0, file_size - 512))
                tail = fbin.read().decode("utf-8", errors="replace")

            last_line = tail.rstrip().splitlines()[-1] if tail.strip() else ""
            if last_line.count(",") < MIN_EXPECTED_COLUMNS:
                logger.warning(
                    "JTL last row appears truncated (%d comma(s), expected >= %d): %s",
                    last_line.count(","),
                    MIN_EXPECTED_COLUMNS,
                    self.file_path.name,
                )
                return False

            return True

        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "File integrity check skipped (%s): %s", exc, self.file_path.name
            )
            return True

    @staticmethod
    def _parse_row(row: dict) -> Optional[tuple[int, bool, bool]]:
        """Extract (elapsed_ms, is_http_error, is_assertion_failure) from a raw CSV row.

        PARSE-01: Returns two independent error flags:
          - is_http_error:        True when responseCode is 4xx/5xx (matches JMeter console).
          - is_assertion_failure: True when success=false in the CSV (includes assertion fails).

        Handles common JMeter header casing variations (e.g. 'elapsed' vs 'Elapsed').
        """
        try:
            # Case-insensitive lookup for specific keys.
            row_keys = {k.strip().lower(): k for k in row.keys() if k is not None}

            elapsed_key = row_keys.get("elapsed")
            success_key = row_keys.get("success")
            rc_key = row_keys.get("responsecode")

            if elapsed_key is None or success_key is None:
                return None

            elapsed_val = row.get(elapsed_key)
            success_val = row.get(success_key)

            if elapsed_val is None or success_val is None:
                return None

            elapsed = int(elapsed_val)

            # is_assertion_failure: reflects success column (includes JMeter assertions)
            is_assertion_failure = str(success_val).strip().lower() != "true"

            # is_http_error: true only for explicit HTTP error codes (4xx, 5xx, etc.)
            # This matches what JMeter's console 'Err:' counter shows.
            is_http_error = False
            if rc_key is not None:
                rc_val = row.get(rc_key, "")
                rc_str = str(rc_val).strip()
                try:
                    rc_int = int(rc_str)
                    # Treat anything outside 1xx–3xx range as an error.
                    # JMeter also uses non-numeric codes (e.g. "Non HTTP response code")
                    # which are also treated as errors.
                    is_http_error = rc_int >= 400
                except ValueError:
                    # Non-numeric response code (e.g. "Non HTTP response code") = error
                    is_http_error = bool(rc_str) and not rc_str.isspace()
            else:
                # No responseCode column — fall back to success column
                is_http_error = is_assertion_failure

            return elapsed, is_http_error, is_assertion_failure
        except (ValueError, KeyError, AttributeError, TypeError):
            logger.debug("Skipping malformed row: %s", row)
            return None


class _RunningAggregator:
    """Maintains running statistics across batches for a streaming parse.

    Uses Vitter's reservoir sampling algorithm (Algorithm R) to maintain a
    fixed-size random sample of elapsed times for percentile estimation.

    PARSE-01: Tracks HTTP errors (responseCode >= 400) separately from
    assertion failures (success=false) to match JMeter console 'Err:' reporting.
    """

    def __init__(self, reservoir_size: int) -> None:
        self.reservoir_size = reservoir_size
        self.total_count: int = 0
        self.error_count: int = 0            # HTTP-level errors (4xx/5xx)
        self.assertion_failure_count: int = 0  # success=false in CSV (incl. assertions)
        self._sum: float = 0.0
        self._min: int = 2**62
        self._max: int = 0
        self.reservoir: list[int] = []

    def consume(self, batch: list[tuple[int, bool, bool]]) -> None:
        """Process one batch of (elapsed, is_http_error, is_assertion_failure) tuples."""
        for elapsed, is_http_error, is_assertion_failure in batch:
            self.total_count += 1
            self._sum += elapsed
            if elapsed < self._min:
                self._min = elapsed
            if elapsed > self._max:
                self._max = elapsed
            if is_http_error:
                self.error_count += 1
            if is_assertion_failure:
                self.assertion_failure_count += 1

            # Reservoir sampling — Algorithm R
            if len(self.reservoir) < self.reservoir_size:
                self.reservoir.append(elapsed)
            else:
                replace_idx = random.randint(0, self.total_count - 1)
                if replace_idx < self.reservoir_size:
                    self.reservoir[replace_idx] = elapsed

    def to_metrics(self) -> Metrics:
        """Build the final Metrics dataclass from accumulated statistics."""
        if self.total_count == 0:
            return Metrics(
                total_requests=0,
                error_count=0,
                error_percent=0.0,
                avg_response_time=0.0,
                min_response_time=0.0,
                max_response_time=0.0,
                p95=0.0,
                p99=0.0,
            )
        sorted_reservoir = sorted(self.reservoir)
        return Metrics(
            total_requests=self.total_count,
            error_count=self.error_count,
            error_percent=(self.error_count / self.total_count) * 100,
            avg_response_time=self._sum / self.total_count,
            min_response_time=float(self._min),
            max_response_time=float(self._max),
            p95=self._percentile_from_sorted(sorted_reservoir, 95),
            p99=self._percentile_from_sorted(sorted_reservoir, 99),
        )

    @staticmethod
    def _percentile_from_sorted(sorted_data: list[int], pct: int) -> float:
        """Estimate the Nth percentile from a pre-sorted list using linear interpolation."""
        if not sorted_data:
            return 0.0
        n = len(sorted_data)
        k = (pct / 100) * (n - 1)
        f = int(k)
        c = f + 1 if f + 1 < n else f
        d = k - f
        return float(sorted_data[f] + d * (sorted_data[c] - sorted_data[f]))
