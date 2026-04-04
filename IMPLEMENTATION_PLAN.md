# Implementation Plan — Code Review Remediation

**Project:** NixPerf-Orchestrator  
**Date:** 2026-04-04  
**Total Findings:** 16 (4 High, 7 Medium, 5 Low)  
**Estimated Effort:** 3 phases across 2 sessions  

---

## Phase 1 — Critical Security & Data Integrity Fixes

**Goal:** Address all High-severity findings that are exploitable or cause silent data loss.  
**Files Modified:** `reporting.py`, `jmeter_runner.py`, `parser.py`

---

### Task 1.1 — Fix Stored XSS in HTML Report Generation (S1)

- **File:** `orchestrator/reporting.py`
- **Finding:** S1 — Stored XSS via unescaped HTML interpolation
- **Changes:**
  1. Add `import html` at the top of the file.
  2. In `_render_scenario()` (line ~410): wrap `scenario['name']`, `scenario["breakpoint"]`, and `scenario["abort_reason"]` with `html.escape(str(...))`.
  3. In `_render_run_row()` (line ~431): wrap `decision` and `run['reason']` with `html.escape(str(...))`.
- **Testing:** Create a scenario with `<script>alert(1)</script>` in the name field and verify the HTML report contains `&lt;script&gt;` instead.
- **Dependencies:** None

---

### Task 1.2 — Fix SSRF via Webhook URL (S2)

- **File:** `orchestrator/reporting.py`
- **Finding:** S2 — SSRF via unvalidated webhook URL from CLI
- **Changes:**
  1. Add `import ipaddress` and `from urllib.parse import urlparse` at the top of the file.
  2. Create a new private method `_validate_webhook_url(url: str) -> None` that:
     - Parses the URL and rejects any scheme other than `https`.
     - Resolves the hostname; if it's an IP, rejects private/loopback/link-local/reserved ranges.
  3. Call `_validate_webhook_url(webhook_url)` at the start of `send_webhook_notification()` before constructing the request.
- **Testing:** Attempt to send a webhook to `http://127.0.0.1/` and verify it raises `ValueError`.
- **Dependencies:** None

---

### Task 1.3 — Fix Race Condition on Shared `stdout_lines` (L1)

- **File:** `orchestrator/jmeter_runner.py`
- **Finding:** L1 — Race condition between reader thread and main thread
- **Changes:**
  1. Create a `threading.Lock` instance (`stdout_lock`) in `_execute()`.
  2. In `_drain_stdout()`: wrap `stdout_lines.append(stripped)` with `with stdout_lock:`.
  3. Wrap the `ValueError` catch in `_drain_stdout()` to handle the case where stdout is closed after kill.
  4. After `process.kill()`: add `process.wait(timeout=5)` to ensure the process is fully terminated before joining the reader.
  5. Increase `reader.join(timeout=5)` to `reader.join(timeout=15)` after kill.
  6. Wrap `full_output = "\n".join(stdout_lines)` with `with stdout_lock:`.
- **Testing:** Run a JMeter test that produces large output; verify no truncated or garbled output in logs.
- **Dependencies:** None

---

### Task 1.4 — Fix Memory Issue in `_check_file_integrity` (L2)

- **File:** `orchestrator/parser.py`
- **Finding:** L2 — Entire file loaded into memory during integrity check
- **Changes:**
  1. Rewrite `_check_file_integrity()` to stream the file line-by-line:
     - Read and discard the header line.
     - Iterate over remaining lines, incrementing a counter and retaining only the last line.
     - After iteration, perform the row count and column count checks against the counter and last line.
  2. Remove the `all_lines` and `data_lines` variables entirely.
- **Testing:** Create a large CSV file (1M+ rows) and verify memory usage stays constant during parsing.
- **Dependencies:** None

---

## Phase 2 — Reliability & Medium-Severity Fixes

**Goal:** Address Medium-severity findings that affect production reliability and security posture.  
**Files Modified:** `reporting.py`, `main.py`, `decision_engine.py`, `parser.py`, `preflight.py`

---

### Task 2.1 — Fix Plaintext SMTP Credentials (S3)

- **File:** `orchestrator/reporting.py`
- **Finding:** S3 — Plaintext credentials in YAML config
- **Changes:**
  1. In `send_email_notification()`: after reading `password` from config, check if it starts with `env:`.
  2. If it does, resolve it via `os.environ.get(password[4:], "")`.
  3. If the env var is not set, log a warning and return early (skip email).
  4. Add `import os` if not already present.
  5. Update the example config comment in `config/scenarios.yaml` to show the `env:` syntax.
- **Testing:** Set `password: env:TEST_SMTP_PASSWORD` in config, export the env var, and verify email sends successfully.
- **Dependencies:** None

---

### Task 2.2 — Fix Path Traversal via `jmx_path` (S4)

- **File:** `orchestrator/main.py`
- **Finding:** S4 — Path traversal in result file naming
- **Changes:**
  1. In `_execute_step()`: sanitize `jmx_basename` by stripping any path separators and non-filename-safe characters.
  2. Add validation that `jmx_basename` is non-empty after sanitization.
  3. Use the sanitized name in the `result_file` path.
- **Testing:** Pass `jmx_path: "../../etc/passwd"` and verify the result file is created under `results/` with a safe name.
- **Dependencies:** None

---

### Task 2.3 — Fix Command Injection Risk via `jmeter_path` (S5)

- **File:** `orchestrator/main.py`
- **Finding:** S5 — Arbitrary binary execution via `--jmeter-path`
- **Changes:**
  1. In `main()`: after resolving `jmeter_path`, use `shutil.which()` to resolve it.
  2. If `shutil.which()` returns `None`, check if the raw path points to an existing file.
  3. If neither, raise an error and exit.
  4. Use the resolved absolute path for the `JMeterRunner` instance.
- **Testing:** Pass `--jmeter-path /nonexistent/binary` and verify the tool exits with an error.
- **Dependencies:** None

---

### Task 2.4 — Fix WARN Re-test Failure Counting (L3)

- **File:** `orchestrator/main.py`
- **Finding:** L3 — Re-test failures not counted toward `consecutive_failures`
- **Changes:**
  1. After the re-test `_execute_step()` call (line ~318): check if `retest.metrics is None`.
  2. If `None`: increment `consecutive_failures`, check against `max_failures`, and break if exceeded (saving checkpoint).
  3. If not `None`: reset `consecutive_failures` to 0.
  4. Move the `consecutive_failures` reset (currently at line 309) to also cover the re-test path.
- **Testing:** Simulate a WARN re-test that fails (returns no metrics) and verify the scenario aborts after `max_consecutive_failures`.
- **Dependencies:** None

---

### Task 2.5 — Fix Unbounded `_history` Growth (L4)

- **File:** `orchestrator/decision_engine.py`
- **Finding:** L4 — Unbounded history list in adaptive mode
- **Changes:**
  1. Add a `MAX_HISTORY_SIZE = 100` constant at the module level.
  2. In `evaluate()`: after `self._history.append(metrics)`, trim the list if it exceeds `MAX_HISTORY_SIZE` by keeping only the last N entries.
- **Testing:** Run a scenario with 150+ load steps and verify `_history` never exceeds 100 entries.
- **Dependencies:** None

---

### Task 2.6 — Fix Reservoir Double-Sort (P1)

- **File:** `orchestrator/parser.py`
- **Finding:** P1 — Reservoir sorted twice for p95 and p99
- **Changes:**
  1. In `to_metrics()`: sort the reservoir once and store the sorted list.
  2. Create a new static method `_percentile_from_sorted(sorted_data, pct)` that takes a pre-sorted list.
  3. Replace the two `_percentile()` calls with `_percentile_from_sorted(sorted_reservoir, 95)` and `_percentile_from_sorted(sorted_reservoir, 99)`.
  4. Update `_percentile_from_sorted` to use linear interpolation between adjacent ranks for accuracy.
- **Testing:** Parse a result file and verify p95/p99 values match the previous implementation within acceptable tolerance.
- **Dependencies:** Task 1.4 (parser.py changes)

---

### Task 2.7 — Fix Sequential Slave Checks (P2)

- **File:** `orchestrator/preflight.py`
- **Finding:** P2 — Sequential slave connectivity checks block for up to 100s
- **Changes:**
  1. Add `import concurrent.futures` at the top of the file.
  2. In `check_slaves_alive()`: replace the sequential `for` loop with a `ThreadPoolExecutor`.
  3. Submit all slave checks as futures and collect results via `as_completed()`.
  4. Keep the existing threshold logic unchanged.
  5. Also parallelize the startup checks in `run_preflight_checks()` using the same pattern.
- **Testing:** Configure 5 unreachable slaves and verify the check completes in ~5s instead of ~25s.
- **Dependencies:** None

---

## Phase 3 — Low-Severity & Hardening Fixes

**Goal:** Address all Low-severity findings and add defensive hardening.  
**Files Modified:** `reporting.py`, `parser.py`, `main.py`

---

### Task 3.1 — Fix Percentile Edge Case (L5)

- **File:** `orchestrator/parser.py`
- **Finding:** L5 — Percentile calculation inaccurate for small reservoirs
- **Changes:**
  1. Update `_percentile_from_sorted()` (created in Task 2.6) to use linear interpolation: `value = data[f] + d * (data[c] - data[f])` where `f = floor(k)` and `d = k - f`.
  2. This is already included in Task 2.6 if the interpolation approach is used.
- **Testing:** Create a reservoir of 10 known values and verify p95 is interpolated correctly (not just the max value).
- **Dependencies:** Task 2.6

---

### Task 3.2 — Fix Glob Injection in `clean_old_results` (L6)

- **File:** `orchestrator/reporting.py`
- **Finding:** L6 — Glob metacharacters in scenario name
- **Changes:**
  1. In `clean_old_results()`: replace `dir_path.glob(pattern)` with a list comprehension that filters `dir_path.glob("*.csv")` by `p.name.startswith(f"{safe_prefix}_")`.
  2. Sanitize `scenario_name` by stripping `*`, `?`, `[`, `]` characters before constructing the prefix.
- **Testing:** Create a scenario named `test*` with result files and verify only `test_*.csv` files are matched, not all CSVs.
- **Dependencies:** None

---

### Task 3.3 — Fix Missing SSL Context for STARTTLS (L7)

- **File:** `orchestrator/reporting.py`
- **Finding:** L7 — No explicit SSL context for SMTP STARTTLS
- **Changes:**
  1. Add `import ssl` at the top of the file.
  2. In `send_email_notification()`: create `ssl_context = ssl.create_default_context()` before the SMTP connection.
  3. Pass `context=ssl_context` to `server.starttls()`.
- **Testing:** Send an email via TLS and verify the connection uses the default CA bundle.
- **Dependencies:** None

---

### Task 3.4 — Fix Redundant `is_first_real_step` Check (P3)

- **File:** `orchestrator/main.py`
- **Finding:** P3 — O(n^2) check per load step
- **Changes:**
  1. Before the main loop: compute `first_pending_idx` by iterating `load_steps` once to find the first step not in `completed_users`.
  2. Inside the loop: replace the `is_first_real_step` calculation with `i == first_pending_idx`.
- **Testing:** Resume a scenario from checkpoint with 10 completed steps and verify cooldown is correctly skipped/applied.
- **Dependencies:** None

---

### Task 3.5 — Add Division-by-Zero Guard in `to_metrics()` (P4)

- **File:** `orchestrator/parser.py`
- **Finding:** P4 — Missing defensive guard in `to_metrics()`
- **Changes:**
  1. At the top of `to_metrics()`: add `if self.total_count == 0: return Metrics(total_requests=0, error_count=0, error_percent=0.0, avg_response_time=0.0, min_response_time=0.0, max_response_time=0.0, p95=0.0, p99=0.0)`.
- **Testing:** Call `to_metrics()` on an empty aggregator and verify it returns a zeroed Metrics object without raising.
- **Dependencies:** Task 2.6

---

## Execution Checklist

```
Phase 1 (High Priority)
  [ ] Task 1.1 — S1: XSS fix in reporting.py
  [ ] Task 1.2 — S2: SSRF fix in reporting.py
  [ ] Task 1.3 — L1: Race condition fix in jmeter_runner.py
  [ ] Task 1.4 — L2: Memory fix in parser.py

Phase 2 (Medium Priority)
  [ ] Task 2.1 — S3: SMTP credentials in reporting.py
  [ ] Task 2.2 — S4: Path traversal fix in main.py
  [ ] Task 2.3 — S5: jmeter_path validation in main.py
  [ ] Task 2.4 — L3: WARN re-test failure counting in main.py
  [ ] Task 2.5 — L4: History trimming in decision_engine.py
  [ ] Task 2.6 — P1: Double-sort fix in parser.py
  [ ] Task 2.7 — P2: Parallel slave checks in preflight.py

Phase 3 (Low Priority)
  [ ] Task 3.1 — L5: Percentile interpolation in parser.py
  [ ] Task 3.2 — L6: Glob injection fix in reporting.py
  [ ] Task 3.3 — L7: SSL context in reporting.py
  [ ] Task 3.4 — P3: is_first_real_step optimization in main.py
  [ ] Task 3.5 — P4: Division-by-zero guard in parser.py
```

---

## File Change Summary

| File | Tasks | Nature of Changes |
|------|-------|-------------------|
| `orchestrator/reporting.py` | 1.1, 1.2, 2.1, 3.2, 3.3 | HTML escaping, URL validation, env var credentials, glob fix, SSL context |
| `orchestrator/jmeter_runner.py` | 1.3 | Thread lock, process cleanup, timeout handling |
| `orchestrator/parser.py` | 1.4, 2.6, 3.1, 3.5 | Streaming integrity check, single-sort percentile, interpolation, zero guard |
| `orchestrator/main.py` | 2.2, 2.3, 2.4, 3.4 | Path sanitization, jmeter_path resolution, failure counting, loop optimization |
| `orchestrator/decision_engine.py` | 2.5 | History list trimming |
| `orchestrator/preflight.py` | 2.7 | Parallel slave connectivity checks |
| `config/scenarios.yaml` | 2.1 | Updated SMTP example with `env:` syntax |

---

*Generated by Kilo — 2026-04-04*
