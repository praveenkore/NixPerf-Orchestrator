# NixPerf Orchestrator — Help & Configuration Guide

This document covers configuration, distributed setup, autonomous operation features, and troubleshooting.

---

## 1. Orchestrator Configuration (`config/scenarios.yaml`)

The `scenarios.yaml` file is the primary configuration point for the framework.

### 1.1 Core Fields

| Option | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `name` | string | ✅ | Unique scenario identifier. Used for file naming, logs, and reports. |
| `jmx_path` | string | ✅ | Relative path to the `.jmx` test plan (e.g., `scenarios/login.jmx`). |
| `load_steps` | list[int] | ✅ | User-count values to execute in order (e.g., `[500, 1000, 2000]`). |
| `sla.p95` | int (ms) | ✅ | P95 response-time hard limit. Execution stops if breached. |
| `sla.error_threshold` | float (%) | ✅ | Error-rate hard limit (0–100). Execution stops if breached. |
| `ramp_strategy` | dict | ✅ | How ramp-up duration is calculated per step (see §1.2). |
| `retry_count` | int | ✗ | JMeter retry attempts per step. Default: `1` (2 total attempts). |
| `timeout_seconds` | int | ✗ | Hard kill timeout per JMeter run. Default: `7200` (2 h). |
| `mode` | string | ✗ | Decision engine mode: `static` (default) or `adaptive` (see §1.3). |
| `jmeter_path` | string | ✗ | Path to the JMeter executable. Default: `jmeter`. |

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
| `adaptive` | Performs linear-regression trend analysis over the last 5 steps. Issues a pre-emptive `STOP` if the *predicted* next-step value exceeds 90 % of the SLA limit, before the breach actually occurs. |

### 1.4 Autonomous Operation Fields *(new)*

These optional fields enable reliable unattended execution:

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `warmup_users` | int | `10` | Users for the warm-up probe run before `load_steps[0]`. Results are discarded. Set to `0` to disable. |
| `cooldown_seconds` | int | `60` | Seconds to wait between consecutive load steps so TCP connections drain and backend thread pools recover. |
| `max_consecutive_failures` | int | `2` | Abort the scenario after this many back-to-back JMeter failures (no result file produced). Prevents hours of silent null-metric runs during infrastructure outages. |

### 1.5 Complete Example

```yaml
scenarios:
  - name: login_test
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
# slaves:
#   - 192.168.1.10
#   - 192.168.1.11

# Optional — Slack / webhook notification on test completion
# notification:
#   webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
```

---

## 2. Command-Line Interface

```
python -m orchestrator.main [OPTIONS]
```

| Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to `scenarios.yaml`. Default: `config/scenarios.yaml`. |
| `--skip-preflight` | Skip startup health checks (useful in CI where slaves are not used). |
| `--slaves IP1,IP2,...` | Comma-separated JMeter slave IPs. **Overrides** the `slaves` list in YAML. |
| `--webhook-url URL` | Slack/generic webhook URL for completion notifications. **Overrides** `notification.webhook_url` in YAML. |
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

### 3.2 Warmup Probe

Before the first load step, the orchestrator injects a low-traffic probe (default: `10 users`) to:

- Pre-fill JVM JIT caches and backend connection pools.
- Hydrate any application-layer caches.
- Ensure the first real measurement reflects steady-state, not cold-start behaviour.

The warmup run output is **never** included in reports or trend analysis.

**To disable warmup**, set `warmup_users: 0` in the scenario config.

### 3.3 Cooldown Between Steps

A configurable sleep (`cooldown_seconds`, default `60 s`) is inserted before each load step (except the first). This allows:

- Lingering TCP connections from the previous step to drain.
- Backend GC pauses and thread-pool resizing to complete.
- Metrics from the previous step to flush to the result file.

Without cooldown, step N+1 inherits residual load from step N, inflating latency measurements.

### 3.4 Decision States

The decision engine now has three states:

| State | Meaning | Action taken by orchestrator |
| :--- | :--- | :--- |
| `PROCEED` (🟢) | System healthy. | Escalate to the next load step. |
| `WARN` (🟠) | Approaching SLA limit (> 75 % of threshold by default). | Re-test the **same** load step once after a short settle. If still `WARN` or `STOP`, convert to `STOP`. |
| `STOP` (🔴) | SLA breached, or adaptive trend predicts imminent breach. | Record breakpoint, halt escalation, write reports. |

### 3.5 Per-Step Slave Health Checks

At startup, all slave connectivity is validated once. In addition, **before every load step**, the orchestrator re-probes each slave on RMI ports `1099` and `50000`.

- Unreachable slaves are **excluded** from that step (reduced pool, logged as WARNING).
- If fewer than **50 %** of the original slave pool is alive, the step is **aborted** and the scenario records an `abort_reason`.
- This prevents under-loaded runs being silently accepted as valid results.

### 3.6 Consecutive Failure Abort

If JMeter fails to produce any result file on `max_consecutive_failures` (default: `2`) back-to-back steps, the scenario is aborted with a clear `abort_reason` logged and stored in the report. This catches:

- Infrastructure outages (slaves all down).
- Invalid JMX files that consistently crash JMeter.
- Disk-full conditions on the master node.

### 3.7 Webhook Notifications

On test completion, the orchestrator POSTs a structured JSON payload to the configured webhook URL. The payload is compatible with **Slack Incoming Webhooks** and most generic JSON endpoints (e.g., Microsoft Teams, PagerDuty, custom HTTP listeners).

The notification includes:
- Per-scenario summary (breakpoint users, abort reason if any).
- Number of P95 regressions detected vs baseline (if any).
- Timestamp.

```yaml
# In scenarios.yaml
notification:
  webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Or pass at runtime:

```bash
python -m orchestrator.main --webhook-url https://hooks.slack.com/...
```

> **Note:** Webhook failures are non-fatal — the test reports are always written to disk regardless.

### 3.8 Baseline Comparison & Regression Detection

After generating reports, the orchestrator compares current P95 values against a saved baseline:

- **First run**: current results are automatically saved as `reports/baseline.json`.
- **Subsequent runs**: P95 values at each `(scenario, users)` pair are compared.
  A **regression is flagged** when `(current_p95 - baseline_p95) / baseline_p95 > 20 %`.
- Regressions are logged as `WARNING`, saved to `reports/regressions_<timestamp>.json`, and included in the webhook notification payload.

To **reset the baseline**, delete `reports/baseline.json`. The next run will create a fresh one.

### 3.9 Result File Retention

After each load step, the orchestrator deletes old result CSV files for that scenario, retaining only the **5 most recent** files (by modification time). This prevents unbounded disk growth on long-running CI pipelines.

The retention count can be adjusted in `reporting.py`:

```python
Reporter.clean_old_results(scenario_name, keep_last=10)
```

### 3.10 Real-Time JMeter Progress

JMeter's console output is now **streamed live** to the log rather than buffered until completion. JMeter summary lines are parsed and emitted at `INFO` level so you can monitor progress during a 2-hour run without waiting for it to finish:

```
2026-03-30 14:22:01 | INFO     | orchestrator.jmeter_runner — ↳ Live progress —
    samples: 12500 | throughput: 100.0/s | avg: 145ms | errors: 23 (0.18%)
```

---

## 4. Batch Parsing Configuration (Large Result Files)

For high-load tests generating millions of rows, the result parser reads the file in chunks.

| Parameter | Description | Default |
| :--- | :--- | :--- |
| `batch_size` | Rows processed per iteration. Lower = less memory per cycle. | `10 000` |
| `reservoir_size` | Max samples kept for P95/P99 estimation via reservoir sampling. | `100 000` |

```python
# Custom parser settings (advanced — set in main.py if needed)
parser = ResultsParser(result_file, batch_size=20_000, reservoir_size=200_000)
```

> **Percentile accuracy**: Reservoir sampling (Vitter's Algorithm R) provides P95/P99 estimates within ~1 % of exact values for files with 1 M+ rows.

**File integrity check**: Before parsing, the orchestrator verifies that the result file has at least 10 data rows and that the last row is not truncated. A `WARNING` is logged for suspicious files, but parsing continues — incomplete results are preferable to no results.

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

Edit `<JMETER_HOME>/bin/jmeter.properties` on **all** nodes:

```properties
server.rmi.localport=50000
client.rmi.localport=50001
server_port=1099
```

Open these ports on **all slave firewalls**:

| Port | Purpose |
| :--- | :--- |
| `1099` | JMeter server port |
| `50000` | Server RMI local port |
| `50001` | Client RMI local port |

> **Why?** Without fixed RMI ports, JMeter uses random ephemeral ports that get blocked by firewalls, causing `java.rmi.ConnectException` failures under load.

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
# Linux
export HEAP="-Xms4g -Xmx4g"

# Windows
set HEAP=-Xms4g -Xmx4g
```

Set these on **both master and slave nodes** before starting JMeter.

---

## 9. Output Files Reference

| Path | Description |
| :--- | :--- |
| `results/<jmx_basename>_<users>.csv` | Raw JMeter JTL result file per step. Overwrites on each run. |
| `reports/summary_<ts>.json` | Full structured results for all scenarios. |
| `reports/summary_<ts>.html` | Human-readable HTML report with colour-coded decisions. |
| `reports/baseline.json` | Saved baseline for regression comparison. Delete to reset. |
| `reports/regressions_<ts>.json` | P95 regressions vs baseline (only created when regressions exist). |
| `reports/.checkpoint_<name>.json` | Per-scenario crash-recovery checkpoint. Auto-deleted on clean completion. |

---

## 10. Troubleshooting

| Symptom | Likely cause | Resolution |
| :--- | :--- | :--- |
| No result CSV produced | `jmeter` not found on PATH | Add JMeter `bin/` to `PATH`, set `jmeter_path` in YAML, or use `--jmeter-path` CLI flag |
| `Connection Refused` on slave | RMI ports not open | Open ports 1099, 50000, 50001 on slave firewalls |
| Random distributed failures | Ephemeral RMI ports | Fix `server.rmi.localport` in `jmeter.properties` (see §5.2) |
| Scenario aborted after 2 steps | `max_consecutive_failures` hit | Check slave connectivity; inspect JMeter stderr in logs |
| SLA stops too early | Thresholds too tight | Increase `sla.p95` or `sla.error_threshold` in `scenarios.yaml` |
| WARN triggers on every step | `warn_factor` too low (75 %) | Raise `warn_factor` in `DecisionEngine` constructor or widen SLA limits |
| Warmup inflating step-1 results | Warmup disabled | Set `warmup_users: 10` (or higher) in scenario config |
| Pre-flight check fails | Slaves not running `jmeter-server` | Start `jmeter-server` on each slave node before invoking orchestrator |
| Webhook not received | Wrong URL or network block | Check URL format (`https://`); test with `curl -X POST <url>` manually |
| Baseline regression false-positive | Baseline from a bad run | Delete `reports/baseline.json`; re-run on a healthy system |
| Checkpoint resume skips wrong steps | Clock skew or manual edit | Run with `--no-resume` to force a clean start |
| Timestamp skew in reports | No time sync | Install and enable `chrony` on all nodes (see §6) |
| `OutOfMemoryError` in JMeter | Default JVM heap too small | Set `HEAP=-Xms4g -Xmx4g` before starting JMeter (see §8) |
| Old CSVs filling disk | Default retention (5 files) too high | Call `Reporter.clean_old_results(name, keep_last=2)` or reduce test frequency |

---

## 11. Python Environment Setup

The NixPerf Orchestrator is a Python-based framework and works best in an isolated virtual environment (`venv`).

### 11.1 Creating a Virtual Environment

Running within a virtual environment ensures that the orchestrator's dependencies (like `PyYAML`) do not conflict with other system-level Python packages.

**Linux / macOS:**
```bash
# Create the environment
python -m venv myenv

# Activate it
source myenv/bin/activate
```

**Windows (PowerShell):**
```powershell
# Create the environment
python -m venv myenv

# Activate it
.\myenv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
# Create the environment
python -m venv myenv

# Activate it
myenv\Scripts\activate.bat
```

### 11.2 Installing Dependencies

Once the environment is activated, install the required packages using `pip`:

```bash
pip install PyYAML
```

### 11.3 Verifying the Setup

You can verify that the environment is correctly configured by checking the installed version of PyYAML and ensuring that the orchestrator can be invoked:

```bash
# Check packages
pip list

# Test invocation
python -m orchestrator.main --help
```

> [!TIP]
> Always remember to **activate** your environment (`source myenv/bin/activate` or equivalent) before running any tests. You will typically see `(myenv)` prepended to your shell prompt when it is active.
