# NixPerf Orchestrator — Help & Configuration Guide

This document covers configuration, distributed setup, autonomous operation features, and troubleshooting.

---

## 1. Orchestrator Configuration (`config/scenarios.yaml`)

The `scenarios.yaml` file is the primary configuration point for the framework.

### 1.1 Core Fields

| Option | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `name` | string | Yes | Unique scenario identifier. Must be 1–64 characters and contain only `[A-Za-z0-9_-]`. Used for checkpoint files, result CSV prefixes, logs, and reports. |
| `jmx_path` | string | Yes | Relative path to the `.jmx` test plan (e.g., `scenarios/login.jmx`). |
| `load_steps` | list[int] | Yes | User-count values to execute in order (e.g., `[500, 1000, 2000]`). |
| `sla.p95` | int (ms) | Yes | P95 response-time hard limit. Execution stops if breached. |
| `sla.error_threshold` | float (%) | Yes | Error-rate hard limit (0–100). Execution stops if breached. |
| `ramp_strategy` | dict | Yes | How ramp-up duration is calculated per step (see §1.2). |
| `retry_count` | int | No | JMeter retry attempts per step. Default: `1` (2 total attempts). |
| `timeout_seconds` | int | No | Hard kill timeout per JMeter run. Default: `7200` (2 h). |
| `mode` | string | No | Decision engine mode: `static` (default) or `adaptive` (see §1.3). |
| `jmeter_path` | string | No | Path to the JMeter executable. Default: `jmeter` (resolved via PATH). |

> **Scenario name constraints:** The `name` value is embedded in file system paths
> (`reports/.checkpoint_<name>.json`). The config validator rejects names that
> contain path separators, dots, spaces, or special characters. Stick to
> letters, digits, underscores, and hyphens.

### 1.2 Ramp Strategy

Three built-in strategies control how ramp-up duration scales with user count:

| Strategy type | Required params | Formula |
| :--- | :--- | :--- |
| `constant_arrival` | `arrival_rate` (users/s) | `rampup = users / arrival_rate` |
| `fixed` | `value` (seconds) | `rampup = value` (constant) |
| `proportional` | `base_users`, `base_ramp` (s) | `rampup = base_ramp × (users / base_users)` |

**Safety guards** always apply: ramp-up is clamped to `[1s, users × 4s]`.

```yaml
ramp_strategy:
  type: constant_arrival
  arrival_rate: 5        # 1 000 users → 200 s ramp-up
```

### 1.3 Decision Mode

| Mode | Behaviour |
| :--- | :--- |
| `static` | Evaluates each step independently against fixed thresholds. Issues `WARN` at 75 % of limit, `STOP` at 100 %. |
| `adaptive` | Performs linear-regression trend analysis over the last 5 steps. Issues a pre-emptive `STOP` if the *predicted* next-step value exceeds 90 % of the SLA limit, before the breach actually occurs. On checkpoint resume, previous metrics are restored so trend analysis is accurate from the first resumed step. |

### 1.4 Autonomous Operation Fields

These optional fields enable reliable unattended execution:

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `warmup_users` | int | `10` | Users for the warm-up probe run before `load_steps[0]`. Results are discarded and never affect trend analysis. Set to `0` to disable. |
| `cooldown_seconds` | int | `60` | Seconds to wait between consecutive load steps so TCP connections drain and backend thread pools recover. |
| `max_consecutive_failures` | int | `2` | Abort the scenario after this many back-to-back JMeter failures (no result file produced). Prevents hours of silent null-metric runs during infrastructure outages. |

### 1.5 Complete Example

```yaml
scenarios:
  - name: login_test              # letters, digits, _ and - only; max 64 chars
    jmx_path: scenarios/login.jmx
    load_steps: [500, 1000, 2000, 5000, 10000, 15000]

    ramp_strategy:
      type: constant_arrival
      arrival_rate: 5

    sla:
      p95: 2000             # ms
      error_threshold: 50   # %

    mode: static            # or adaptive

    # Autonomous operation
    warmup_users: 10
    cooldown_seconds: 60
    max_consecutive_failures: 2

    retry_count: 1
    timeout_seconds: 7200

# Optional — distributed slaves (can also be passed via --slaves CLI flag)
# Addresses must be valid hostnames or non-loopback/non-link-local IPs.
# slaves:
#   - 192.168.1.10
#   - 192.168.1.11

# Optional — Slack / webhook notification on test completion
# Must use HTTPS. DNS-resolved IPs are checked against private/reserved ranges.
# notification:
#   webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ

# Optional — SMTP email notification
# smtp:
#   host: smtp.gmail.com
#   port: 587
#   user: your-email@gmail.com
#   password: env:SMTP_PASSWORD   # reads from environment variable at runtime
#   sender: NixPerf <noreply@domain.com>
#   recipient: team@domain.com
```

---

## 2. Command-Line Interface

```
python -m orchestrator.main [OPTIONS]
```

| Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to a `.yaml` or `.yml` scenarios file. Default: `config/scenarios.yaml`. |
| `--skip-preflight` | Skip startup health checks (useful in CI where slaves are not used). |
| `--slaves IP1,IP2,...` | Comma-separated JMeter slave addresses. **Overrides** the `slaves` list in YAML. Each address is validated before connecting (see §5.1). |
| `--webhook-url URL` | Slack/generic webhook URL for completion notifications. Must use `https://`. **Overrides** `notification.webhook_url` in YAML. |
| `--no-resume` | Ignore saved checkpoints and always start from the first load step. |
| `--jmeter-path PATH` | Path to the JMeter executable. **Overrides** `jmeter_path` in YAML. |

### Examples

```bash
# Basic run
python -m orchestrator.main

# Custom config + skip health checks (CI mode)
python -m orchestrator.main --config config/nightly.yaml --skip-preflight

# Distributed run with Slack notification
python -m orchestrator.main \
  --slaves 10.0.0.1,10.0.0.2 \
  --webhook-url https://hooks.slack.com/services/XXX/YYY/ZZZ

# Force full restart (ignore previous checkpoint)
python -m orchestrator.main --no-resume
```

---

## 3. Autonomous Operation Features

### 3.1 Crash Recovery & Checkpointing

After every completed load step, the orchestrator writes a checkpoint file to:

```
reports/.checkpoint_<scenario_name>.json
```

If the process is interrupted (OS kill, network drop, power loss), the next invocation **automatically detects the checkpoint**, skips already-completed steps, and resumes from where it left off.

- Use `--no-resume` to force a clean restart.
- Checkpoints are deleted automatically on clean scenario completion.
- Checkpoint files are plain JSON and can be inspected for debugging.
- On resume, previously collected `Metrics` objects are reconstructed and pushed into the `DecisionEngine` history so that **adaptive trend analysis is accurate from the very first resumed step**.

### 3.2 Warmup Probe

Before the first load step, the orchestrator injects a low-traffic probe (default: `10 users`) to:

- Pre-fill JVM JIT caches and backend connection pools.
- Hydrate any application-layer caches.
- Ensure the first real measurement reflects steady-state, not cold-start behaviour.

The warmup run output is **never** included in reports or trend analysis.

To disable warmup, set `warmup_users: 0` in the scenario config.

### 3.3 Cooldown Between Steps

A configurable sleep (`cooldown_seconds`, default `60 s`) is inserted before each load step (except the first step of each session). This allows:

- Lingering TCP connections from the previous step to drain.
- Backend GC pauses and thread-pool resizing to complete.
- Metrics from the previous step to flush to the result file.

Without cooldown, step N+1 inherits residual load from step N, inflating latency measurements.

### 3.4 Decision States

The decision engine has three states:

| State | Meaning | Action taken by orchestrator |
| :--- | :--- | :--- |
| `PROCEED` | System healthy. | Escalate to the next load step. |
| `WARN` | Approaching SLA limit (> 75 % of threshold by default). | Re-test the **same** load step after a short settle. The original WARN result is preserved in the audit trail. The engine history is adjusted to avoid double-counting the same load level in slope calculations. If the re-test is still `WARN` or `STOP`, the step converts to `STOP`. |
| `STOP` | SLA breached, or adaptive trend predicts imminent breach. | Record breakpoint, halt escalation, write reports. |

### 3.5 Per-Step Slave Health Checks

At startup, all slave connectivity is validated once (parallel checks for speed). In addition, **before every load step**, the orchestrator re-probes each slave on RMI ports `1099` and `50000`.

- Unreachable slaves are **excluded** from that step (reduced pool, logged as WARNING).
- If fewer than **50 %** of the original slave pool is alive, the step is **aborted** and the scenario records an `abort_reason`.
- This prevents under-loaded runs being silently accepted as valid results.

**Address validation:** before any connectivity attempt, every slave address is checked:
- Loopback addresses (`127.x.x.x`, `::1`) are rejected.
- Link-local addresses (`169.254.x.x`, `fe80::/10`) are rejected.
- Multicast addresses are rejected.
- Hostnames must match the RFC-1123 pattern.

This prevents the orchestrator from probing internal network infrastructure via crafted slave addresses.

### 3.6 Consecutive Failure Abort

If JMeter fails to produce any result file on `max_consecutive_failures` (default: `2`) back-to-back steps, the scenario is aborted with a clear `abort_reason` logged and stored in the report. WARN re-test failures also count toward this limit. This catches:

- Infrastructure outages (slaves all down).
- Invalid JMX files that consistently crash JMeter.
- Disk-full conditions on the master node.

### 3.7 Webhook Notifications

On test completion, the orchestrator POSTs a structured JSON payload to the configured webhook URL. The payload is compatible with **Slack Incoming Webhooks** and most generic JSON endpoints (e.g., Microsoft Teams, PagerDuty, custom HTTP listeners).

The notification includes:
- Per-scenario summary (breakpoint users, abort reason if any).
- Number of P95 regressions detected vs baseline (if any).
- Timestamp.

**Security:** Webhook URLs are validated to enforce HTTPS-only. The hostname is DNS-resolved and **every returned IP address** is checked against private, loopback, link-local, reserved, and multicast ranges. This prevents Server-Side Request Forgery (SSRF) via DNS rebinding (where a public hostname resolves to an internal IP). Non-HTTPS URLs or addresses that resolve to internal network ranges are rejected with a warning log; the test reports are always written to disk regardless.

```yaml
# In scenarios.yaml
notification:
  webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Or pass at runtime:

```bash
python -m orchestrator.main --webhook-url https://hooks.slack.com/...
```

### 3.8 Baseline Comparison & Regression Detection

After generating reports, the orchestrator compares current P95 values against a saved baseline:

- **First run**: current results are automatically saved as `reports/baseline.json`.
- **Subsequent runs**: P95 values at each `(scenario, users)` pair are compared.
  A **regression is flagged** when `(current_p95 - baseline_p95) / baseline_p95 > 20 %`.
- Regressions are logged as `WARNING`, saved to `reports/regressions_<timestamp>.json`, and included in the webhook notification payload.

To **reset the baseline**, delete `reports/baseline.json`. The next run will create a fresh one.

### 3.9 SMTP Email Notifications

The orchestrator can send a summary of the test results via email using an SMTP relay.

**Configuration (`scenarios.yaml`):**

```yaml
smtp:
  host: smtp.gmail.com        # SMTP server address
  port: 587                   # Port (587 for STARTTLS)
  user: your-email@gmail.com  # Optional: SMTP username
  password: env:SMTP_PASSWORD # env: prefix reads from the named environment variable
  sender: NixPerf <nixperf@domain.com>
  recipient: team@domain.com
```

Set the environment variable before running:

```bash
export SMTP_PASSWORD="your-app-password"
python -m orchestrator.main
```

> **Security:** Always use the `env:VARIABLE_NAME` syntax for the `password` field.
> Never store plaintext credentials in `scenarios.yaml` or commit them to version control.
> If the named environment variable is not set, the orchestrator logs a warning that
> names the missing variable and skips the email notification (non-fatal).

**Troubleshooting:**
- **STARTTLS**: The orchestrator uses STARTTLS with a verified SSL context. Ensure your relay supports it on port 587.
- **Gmail App Passwords**: Use an App Password (not your primary Google password) with Gmail SMTP.
- **Multiple Recipients**: Specify a distribution-list address; multiple individual recipients are not yet supported.

### 3.10 Result File Retention

After each load step, the orchestrator deletes old result CSV files for that scenario, retaining only the **5 most recent** files (by modification time). This prevents unbounded disk growth on long-running CI pipelines.

The retention count can be adjusted by passing `keep_last` to `clean_old_results()`:

```python
Reporter.clean_old_results(safe_jmx_name, keep_last=10)
```

### 3.11 Real-Time JMeter Progress

JMeter's stdout is streamed live to the log via a background drain thread. JMeter summary lines are parsed and emitted at `INFO` level so you can monitor progress during a 2-hour run without waiting for it to finish. A dedicated stderr drain thread runs concurrently to prevent pipe-buffer deadlocks.

```
2026-04-04 14:22:01 | INFO     | orchestrator.jmeter_runner —
    ↳ Live progress — samples: 12500 | throughput: 100.0/s | avg: 145ms | errors: 23 (0.18%)
```

---

## 4. Batch Parsing Configuration (Large Result Files)

For high-load tests generating millions of rows, the result parser reads the file in chunks and estimates percentiles via reservoir sampling.

| Parameter | Description | Default |
| :--- | :--- | :--- |
| `batch_size` | Rows processed per iteration. Lower = less memory per cycle. | `10 000` |
| `reservoir_size` | Max samples kept for P95/P99 estimation via reservoir sampling. | `100 000` |

```python
# Custom parser settings (advanced — set in main.py if needed)
parser = ResultsParser(result_file, batch_size=20_000, reservoir_size=200_000)
```

> **Percentile accuracy**: Reservoir sampling (Vitter's Algorithm R) with linear interpolation provides accurate P95/P99 estimates even for files with millions of rows. The reservoir is sorted once and reused for both percentiles.

**File integrity check**: Before parsing, the orchestrator performs an O(1) check using `stat()` and a 512-byte tail seek. It verifies that the file is large enough to contain the minimum expected rows, and that the last row is not truncated. A `WARNING` is logged for suspicious files but parsing continues.

---

## 5. Distributed Load Testing (Slave Setup)

### 5.1 Configuring Slaves

Slaves can be specified in two ways (CLI flag takes precedence):

**Option A — `scenarios.yaml`:**
```yaml
slaves:
  - 192.168.1.10
  - 192.168.1.11
```

**Option B — CLI flag:**
```bash
python -m orchestrator.main --slaves 192.168.1.10,192.168.1.11
```

**Address validation rules (applied to both sources):**

| Address type | Example | Accepted? |
| :--- | :--- | :--- |
| Regular private IP | `10.0.0.5`, `192.168.1.10` | Yes |
| Hostname | `slave01.internal`, `jmeter-node-2` | Yes (RFC-1123) |
| Loopback | `127.0.0.1`, `::1` | No — rejected at startup |
| Link-local | `169.254.0.1`, `fe80::1` | No — rejected at startup |
| Multicast | `224.0.0.1`, `ff02::1` | No — rejected at startup |
| Invalid format | `not a host!` | No — rejected at startup |

### 5.2 Slave Machine Setup (Linux Recommended)

Run these commands **on each slave machine** before starting JMeter server.

#### Persistent File Limits

```bash
echo "* soft nofile 200000" | sudo tee -a /etc/security/limits.conf
echo "* hard nofile 200000" | sudo tee -a /etc/security/limits.conf
```

Verify after reboot with `ulimit -n`.

#### Persistent TCP Tuning

```bash
echo "net.ipv4.ip_local_port_range=1024 65000" | sudo tee -a /etc/sysctl.conf
echo "net.ipv4.tcp_tw_reuse=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

#### Fix RMI Ports (Mandatory for Stability)

Starting from v1.1.1, the **NixPerf Orchestrator automatically enforces** these ports by injecting them into the JMeter command line. While manual property edits are still recommended for completeness, they are no longer strictly required if using the orchestrator.

Edit `<JMETER_HOME>/bin/jmeter.properties` on **all** nodes (Optional but recommended):

```properties
server.rmi.localport=50000
client.rmi.localport=50001
server_port=1099
```

Open these ports on **all slave firewalls**:

| Port | Purpose | Enforced by Orchestrator? |
| :--- | :--- | :--- |
| `1099` | JMeter server port | Yes (`server_port`) |
| `50000` | Server RMI local port | Yes (`server.rmi.localport`) |
| `50001` | Client RMI local port | Yes (`client.rmi.localport`) |

> **Why?** Without fixed RMI ports, JMeter uses random ephemeral ports that get blocked by firewalls, causing `java.rmi.ConnectException` failures under load. By enforcing these ports automatically, the orchestrator ensures stability without requiring manual configuration on every node.

#### Start JMeter Server

```bash
./jmeter-server -Djava.rmi.server.hostname=<SLAVE_IP_ADDRESS>
```

---

## 6. Time Synchronization (Mandatory for Distributed Runs)

Ensure **all nodes** (master + slaves) run `chrony` or `ntpd`:

```bash
sudo apt install chrony
sudo systemctl enable --now chrony
```

Without time sync:
- Percentile calculations can **skew** across nodes.
- Distributed logs **misalign**, making debugging difficult.
- Checkpoint timestamps may **conflict** on resume.

---

## 7. JMeter Script (.jmx) Requirements

For automatic load injection to work, your JMX files **must** use JMeter properties for thread count and ramp-up:

1. Open your JMX in JMeter GUI.
2. Set **Number of Threads (users)** → `${__P(users, 1)}`
3. Set **Ramp-Up (seconds)** → `${__P(rampup, 60)}`

No manual editing is needed between runs. The orchestrator injects the correct values for each load step via `-Jusers=<N> -Jrampup=<S>`.

---

## 8. JVM Heap Tuning

If JMeter throws `OutOfMemoryError` at high user counts:

```bash
# Linux / macOS
export HEAP="-Xms4g -Xmx4g"

# Windows
set HEAP=-Xms4g -Xmx4g
```

Set these on **both master and slave nodes** before starting JMeter.

---

## 9. Output Files Reference

| Path | Description |
| :--- | :--- |
| `results/<scenario>_<jmx_basename>_<users>.csv` | Raw JMeter JTL result file per step. Naming includes both the scenario name and the JMX basename to prevent cross-scenario collisions. The 5 most recent files per scenario are retained; older files are pruned automatically. |
| `reports/summary_<ts>.json` | Full structured results for all scenarios. |
| `reports/summary_<ts>.html` | Human-readable HTML report with colour-coded decisions. |
| `reports/baseline.json` | Saved baseline for regression comparison. Delete to reset. |
| `reports/regressions_<ts>.json` | P95 regressions vs baseline (only created when regressions exist). |
| `reports/.checkpoint_<name>.json` | Per-scenario crash-recovery checkpoint. Auto-deleted on clean completion. |

---

## 10. Troubleshooting

| Symptom | Likely cause | Resolution |
| :--- | :--- | :--- |
| Config validation fails with "name must be 1–64 alphanumeric…" | Scenario `name` contains spaces, dots, slashes, or other special characters | Rename the scenario to use only `[A-Za-z0-9_-]` characters |
| Config validation fails with "must use HTTPS" | Webhook URL uses `http://` | Change the URL to start with `https://` |
| Startup error "Slave address is not a valid hostname or IP" | `--slaves` contains an invalid or loopback/link-local address | Check each slave address; loopback (`127.x`, `::1`) and link-local (`169.254.x`, `fe80::`) are rejected |
| No result CSV produced | `jmeter` not found on PATH | Add JMeter `bin/` to `PATH`, set `jmeter_path` in YAML, or use `--jmeter-path` |
| `Connection Refused` on slave | RMI ports not open | Open ports 1099, 50000, 50001 on slave firewalls |
| Random distributed failures | Ephemeral RMI ports | Fix `server.rmi.localport` in `jmeter.properties` (see §5.2) |
| Scenario aborted after 2 steps | `max_consecutive_failures` hit | Check slave connectivity; inspect JMeter stderr in logs |
| SLA stops too early | Thresholds too tight | Increase `sla.p95` or `sla.error_threshold` in `scenarios.yaml` |
| WARN triggers on every step | `warn_factor` too low (75 %) | Raise `warn_factor` in `DecisionEngine` constructor or widen SLA limits |
| Warmup inflating step-1 results | Warmup disabled | Set `warmup_users: 10` (or higher) in scenario config |
| Pre-flight check fails | Slaves not running `jmeter-server` | Start `jmeter-server` on each slave node before invoking orchestrator |
| Webhook not received | Wrong URL, network block, or URL resolves to private IP | Confirm URL starts with `https://`; verify DNS resolution does not point to internal infrastructure |
| Email: "environment variable '' not set" (blank name) | Old code bug — upgrade to v1.1.0 | This is fixed; upgrade and set `password: env:YOUR_VAR_NAME` |
| Baseline regression false-positive | Baseline from a bad run | Delete `reports/baseline.json`; re-run on a healthy system |
| Old CSVs filling disk | Result files not being pruned | Confirm the JMX basename matches the `clean_old_results` prefix; this is handled automatically in v1.1.0 |
| Checkpoint resume skips wrong steps | Clock skew or manual edit | Run with `--no-resume` to force a clean start |
| Timestamp skew in reports | No time sync | Install and enable `chrony` on all nodes (see §6) |
| `OutOfMemoryError` in JMeter | Default JVM heap too small | Set `HEAP=-Xms4g -Xmx4g` before starting JMeter (see §8) |
| Process hangs after JMeter crash | Stderr pipe buffer deadlock (pre-v1.1.0) | Upgrade to v1.1.0; stderr is now continuously drained on a dedicated thread |

---

## 11. Python Environment Setup

The NixPerf Orchestrator is a Python-based framework and works best in an isolated virtual environment (`venv`).

### 11.1 Creating a Virtual Environment

Running within a virtual environment ensures that the orchestrator's dependencies (like `PyYAML`) do not conflict with other system-level Python packages.

**Linux / macOS:**
```bash
python -m venv myenv
source myenv/bin/activate
```

**Windows (PowerShell):**
```powershell
python -m venv myenv
.\myenv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
python -m venv myenv
myenv\Scripts\activate.bat
```

### 11.2 Installing Dependencies

```bash
pip install PyYAML
```

### 11.3 Verifying the Setup

```bash
# Check installed packages
pip list

# Confirm the orchestrator is runnable
python -m orchestrator.main --help
```

> **Tip:** Always activate your environment (`source myenv/bin/activate` or equivalent)
> before running tests. You will see `(myenv)` prepended to your shell prompt when active.
