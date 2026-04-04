# Code Review Findings — NixPerf-Orchestrator

**Date:** 2026-04-04
**Reviewer:** Kilo (Automated Code Review)
**Scope:** Full codebase — `orchestrator/`, `config/`, `tests/`, `tmp/`

---

## Summary

| Category | Critical | High | Medium | Low | Total |
|----------|----------|------|--------|-----|-------|
| Security | 0 | 2 | 3 | 0 | 5 |
| Logic Bugs | 0 | 2 | 2 | 3 | 7 |
| Performance | 0 | 0 | 2 | 2 | 4 |
| **Total** | **0** | **4** | **7** | **5** | **16** |

---

## 1. SECURITY VULNERABILITIES (OWASP Top 10)

### S1 — Stored Cross-Site Scripting (XSS) in HTML Report Generation

- **OWASP:** A03:2021 — Injection
- **Severity:** `High`
- **Location:** `orchestrator/reporting.py:410-443`
- **Root Cause:** The `_render_scenario` and `_render_run_row` methods directly interpolate user-controlled data (scenario `name`, `abort_reason`, `reason`) into HTML output without any sanitization or escaping. An attacker who controls the YAML config can inject arbitrary JavaScript into the HTML report.
- **Affected Code:**
  ```python
  # reporting.py:426 — scenario name injected raw
  f"<h2>Scenario: {scenario['name']}</h>"
  # reporting.py:421 — abort_reason injected raw
  f'{scenario["abort_reason"]}</p>'
  # reporting.py:442 — reason injected raw
  f"<td>{run['reason']}</td>"
  ```
- **Recommended Fix:** Import `html` module and use `html.escape()` on all user-controlled values before interpolation in HTML templates.

---

### S2 — Server-Side Request Forgery (SSRF) via Webhook URL

- **OWASP:** A10:2021 — SSRF
- **Severity:** `High`
- **Location:** `orchestrator/reporting.py:160-167`
- **Root Cause:** The webhook URL passed via `--webhook-url` CLI flag bypasses the config validator's URL scheme check. An attacker can supply any URL including internal network addresses (`http://169.254.169.254/`, `http://localhost:6379/`), causing the orchestrator to make requests to internal services.
- **Affected Code:**
  ```python
  # reporting.py:160-167 — no URL validation at call site
  req = urllib.request.Request(
      webhook_url,  # unvalidated, can point to internal services
      data=payload,
      headers={"Content-Type": "application/json"},
      method="POST",
  )
  ```
- **Recommended Fix:** Add URL validation that enforces HTTPS-only and rejects private/loopback/link-local/reserved IP addresses. Apply this validation in `send_webhook_notification` before constructing the request.

---

### S3 — Plaintext SMTP Credentials in Configuration

- **OWASP:** A02:2021 — Cryptographic Failures
- **Severity:** `Medium`
- **Location:** `orchestrator/reporting.py:192-198`, `config/scenarios.yaml:57-63`
- **Root Cause:** SMTP credentials (user, password) are stored as plaintext in the YAML configuration file. The example config (`tmp/test_smtp_config.yaml:5`) even contains a literal `password: password`. There is no encryption at rest, and no mechanism to read credentials from environment variables or a secrets manager.
- **Affected Code:**
  ```yaml
  # scenarios.yaml:57-63 — plaintext credentials
  smtp:
    host: smtp.gmail.com
    user: your-email@gmail.com
    password: your-app-password
  ```
- **Recommended Fix:** Support environment variable references (e.g., `env:SMTP_PASSWORD`) in the SMTP config. Never commit config files containing real credentials.

---

### S4 — Path Traversal via `jmx_path` and `result_path`

- **OWASP:** A01:2021 — Broken Access Control
- **Severity:** `Medium`
- **Location:** `orchestrator/main.py:405`, `orchestrator/jmeter_runner.py:83`
- **Root Cause:** The `jmx_path` from config is used to construct file paths without sanitization. A path like `../../etc/crontab` or `....//....//etc/passwd` could read/write files outside the intended directories. The `result_path` in `_execute_step` is derived from `jmx_path.stem`, which could be empty or manipulated.
- **Affected Code:**
  ```python
  # main.py:404-405
  jmx_basename = Path(jmx_path).stem  # could be empty or ".."
  result_file  = f"results/{jmx_basename}_{users}.csv"
  ```
- **Recommended Fix:** Validate that `jmx_path` resolves to a file within the expected directory. Sanitize the basename used for result file naming by stripping non-alphanumeric characters.

---

### S5 — Command Injection Risk via `jmeter_path` CLI Argument

- **OWASP:** A03:2021 — Injection
- **Severity:** `Medium`
- **Location:** `orchestrator/main.py:542`, `orchestrator/jmeter_runner.py:189-198`
- **Root Cause:** The `--jmeter-path` CLI argument is passed directly to `subprocess.Popen` as the executable. While using a list (not `shell=True`) mitigates shell injection, a path like `/malicious/binary` could execute an arbitrary binary on the filesystem.
- **Affected Code:**
  ```python
  # main.py:542
  jmeter_path = args.jmeter_path or config.get("jmeter_path", "jmeter")
  # jmeter_runner.py:189
  command = [self.jmeter_path, ...]  # used as executable in Popen
  ```
- **Recommended Fix:** Resolve the path using `shutil.which()` and verify it points to a known JMeter installation. Reject paths outside standard install locations.

---

## 2. LOGIC BUGS AND FUNCTIONAL FLAWS

### L1 — Race Condition on Shared `stdout_lines` List

- **Severity:** `High`
- **Location:** `orchestrator/jmeter_runner.py:125-160`
- **Root Cause:** The `stdout_lines` list is appended to by the background `_drain_stdout` thread and read by the main thread after `process.wait()`. The `reader.join(timeout=10)` can return before the thread finishes, causing `"\n".join(stdout_lines)` to read a partially-populated list. After `process.kill()`, the reader thread may still be blocked on `process.stdout` iteration with only a 5-second join timeout.
- **Affected Code:**
  ```python
  # jmeter_runner.py:125-160
  stdout_lines: list[str] = []  # shared between threads
  def _drain_stdout() -> None:
      for line in process.stdout:  # may block after kill
          stdout_lines.append(stripped)  # no synchronization
  reader = threading.Thread(target=_drain_stdout, daemon=True)
  reader.start()
  process.wait(timeout=timeout)
  reader.join(timeout=10)  # may return before thread finishes
  full_output = "\n".join(stdout_lines)  # race condition
  ```
- **Recommended Fix:** Add a `threading.Lock` to protect `stdout_lines`. After `process.kill()`, call `process.wait(timeout=5)` to ensure the process is fully terminated before joining the reader thread. Increase join timeout after kill.

---

### L2 — `_check_file_integrity` Reads Entire File Into Memory

- **Severity:** `High`
- **Location:** `orchestrator/parser.py:122-124`
- **Root Cause:** The integrity check uses `f.readlines()` which loads the entire JTL file into memory. This defeats the purpose of the streaming batch parser (designed for files with millions of rows) and can cause OOM on large result files.
- **Affected Code:**
  ```python
  # parser.py:122-124
  with self.file_path.open("r", encoding="utf-8", errors="replace") as f:
      all_lines = f.readlines()  # loads entire file into memory!
  data_lines = [ln for ln in all_lines[1:] if ln.strip()]  # second full copy
  ```
- **Recommended Fix:** Rewrite to stream the file line-by-line, counting rows and retaining only the last line. This keeps memory usage O(1) regardless of file size.

---

### L3 — WARN Re-test Failure Not Counted Toward `consecutive_failures`

- **Severity:** `Medium`
- **Location:** `orchestrator/main.py:311-340`
- **Root Cause:** When a WARN triggers a re-test via `_execute_step`, if the re-test itself fails (returns `metrics=None`), the failure is not counted toward `consecutive_failures`. This means a scenario could have repeated WARN-triggered re-test failures without triggering the infra-failure abort, potentially masking infrastructure issues.
- **Affected Code:**
  ```python
  # main.py:318-323 — re-test failure is invisible to consecutive_failures
  retest = _execute_step(
      name, jmx_path, users, rampup,
      runner, engine,
      retry_count, timeout,
      slaves=active_slaves,
  )
  ```
- **Recommended Fix:** Check `retest.metrics is None` after the re-test call and increment `consecutive_failures` accordingly. Break out of the loop if the threshold is exceeded.

---

### L4 — Unbounded `_history` Growth in `DecisionEngine`

- **Severity:** `Medium`
- **Location:** `orchestrator/decision_engine.py:72, 94`
- **Root Cause:** Every non-None metrics object is appended to `_history` and never pruned. For scenarios with many load steps, this list grows without bound. The adaptive evaluation only uses the last `ADAPTIVE_TREND_WINDOW` (5) entries, making the rest wasteful.
- **Affected Code:**
  ```python
  # decision_engine.py:94
  self._history.append(metrics)  # never trimmed
  ```
- **Recommended Fix:** Add a `MAX_HISTORY_SIZE` constant (e.g., 100) and trim the list when it exceeds this size, keeping only the most recent entries.

---

### L5 — Percentile Calculation Edge Case for Small Reservoirs

- **Severity:** `Low`
- **Location:** `orchestrator/parser.py:213-218`
- **Root Cause:** The percentile index calculation `int(len * pct / 100) - 1` can produce misleading results for very small reservoirs. For example, with 10 samples at p95, it returns the 9th element (the max), which inflates the reported p95.
- **Affected Code:**
  ```python
  # parser.py:218
  idx = max(0, int(len(sorted_reservoir) * pct / 100) - 1)
  ```
- **Recommended Fix:** Use linear interpolation between adjacent ranks for more accurate percentile estimation, especially at smaller sample sizes.

---

### L6 — Glob Metacharacter Injection in `clean_old_results`

- **Severity:** `Low`
- **Location:** `orchestrator/reporting.py:383-384`
- **Root Cause:** The `scenario_name` is used directly in a glob pattern. If a scenario name contains glob metacharacters (`*`, `?`, `[`, `]`), it could match and delete unintended files in the `results/` directory.
- **Affected Code:**
  ```python
  # reporting.py:383-384
  pattern = f"{scenario_name}_*.csv"
  files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime)
  ```
- **Recommended Fix:** Sanitize the scenario name by stripping glob metacharacters before constructing the pattern, or use `str.startswith()` filtering instead of glob.

---

### L7 — Missing SSL Context for STARTTLS

- **Severity:** `Low`
- **Location:** `orchestrator/reporting.py:242-243`
- **Root Cause:** `server.starttls()` is called without specifying a custom SSL context. While the default context verifies certificates against the system CA bundle, it does not enforce minimum TLS versions or certificate pinning, and may fail in environments with private CAs.
- **Affected Code:**
  ```python
  # reporting.py:242-243
  if use_tls:
      server.starttls()  # no SSL context = default CA bundle
  ```
- **Recommended Fix:** Create an explicit SSL context with `ssl.create_default_context()` and pass it to `starttls(context=...)`.

---

## 3. PERFORMANCE ISSUES

### P1 — Reservoir Sorted Twice for P95 and P99

- **Severity:** `Medium`
- **Location:** `orchestrator/parser.py:200-218`
- **Root Cause:** `to_metrics()` calls `_percentile(95)` then `_percentile(99)`, each of which sorts the entire reservoir independently. With `DEFAULT_RESERVOIR_SIZE = 100,000`, this means two O(n log n) sorts of 100K integers per parse call.
- **Affected Code:**
  ```python
  # parser.py:209-210
  p95=self._percentile(95),
  p99=self._percentile(99),
  ```
- **Recommended Fix:** Sort the reservoir once in `to_metrics()` and pass the sorted list to a shared percentile helper, reducing from 2 sorts to 1.

---

### P2 — Sequential Slave Connectivity Checks

- **Severity:** `Medium`
- **Location:** `orchestrator/preflight.py:51-53`, `orchestrator/preflight.py:137-139`
- **Root Cause:** Slave connectivity checks are performed sequentially with a 5-second timeout per port. With 10 slaves and 2 ports each, worst case is 10 x 2 x 5 = 100 seconds of blocking. This runs both at startup and before every load step.
- **Affected Code:**
  ```python
  # preflight.py:51-53
  for slave in slaves:
      _check_slave_connectivity(slave)  # blocks up to 10s per slave
  ```
- **Recommended Fix:** Use `concurrent.futures.ThreadPoolExecutor` to check all slaves in parallel. This reduces worst-case time from O(slaves x ports x timeout) to O(ports x timeout).

---

### P3 — Redundant O(n^2) `is_first_real_step` Check

- **Severity:** `Low`
- **Location:** `orchestrator/main.py:247-249`
- **Root Cause:** For each load step `i`, the code checks `all(u in completed_users for u in load_steps[:i])`. This is O(n x m) per step and O(n^2 x m) total. While unlikely to be a bottleneck in practice (load_steps are typically < 20), it is unnecessarily expensive.
- **Affected Code:**
  ```python
  # main.py:247-249
  is_first_real_step = (i == 0 and not completed_users) or (
      i > 0 and all(u in completed_users for u in load_steps[:i])
  )
  ```
- **Recommended Fix:** Compute the first pending step index once before the loop using a single pass, then compare against it in O(1) per iteration.

---

### P4 — Missing Division-by-Zero Guard in `to_metrics()`

- **Severity:** `Low`
- **Location:** `orchestrator/parser.py:200-211`
- **Root Cause:** While `parse()` checks `total_count == 0` before calling `to_metrics()`, the method itself has no guard. If `to_metrics()` is ever called directly or the check is bypassed in a future refactor, it will raise `ZeroDivisionError`.
- **Affected Code:**
  ```python
  # parser.py:205-206
  error_percent=(self.error_count / self.total_count) * 100,
  avg_response_time=self._sum / self.total_count,
  ```
- **Recommended Fix:** Add a defensive check at the top of `to_metrics()` that returns a zeroed Metrics object when `total_count == 0`.

---

## Priority Remediation Order

1. **S1 + S2** (High Security) — XSS and SSRF should be addressed first as they are exploitable by anyone who can influence the config or CLI args.
2. **L1** (High Logic) — Race condition can cause silent data loss in JMeter output.
3. **L2** (High Logic) — Memory issue defeats the streaming design and can crash the process on large files.
4. **L3 + L4** (Medium Logic) — Failure tracking and memory growth issues that affect reliability in production.
5. **S3 + S4 + S5** (Medium Security) — Credential handling and path validation improvements.
6. **P1 + P2** (Medium Performance) — Sorting and parallelism optimizations.
7. **L5 + L6 + L7 + P3 + P4** (Low) — Edge cases and minor improvements.

---

*Generated by Kilo Code Review — 2026-04-04*
