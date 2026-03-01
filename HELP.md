# Performance Automation Framework - Help & Configuration Guide

This document provides detailed instructions on how to configure the framework, set up JMeter slaves, and customize test parameters.

## 1. Orchestrator Configuration (`config/scenarios.yaml`)

The `scenarios.yaml` file is the primary configuration point for the framework.

| Option | Description |
| :--- | :--- |
| `name` | Unique name for the test scenario. Used for file naming and reports. |
| `jmx_path` | Relative path to the `.jmx` file (e.g., `scenarios/login.jmx`). |
| `load_steps` | A list of user counts to execute sequentially (e.g., `[500, 1000, 2000]`). |
| `rampup` | Ramp-up time in **seconds** before all threads are active. Injected as `-Jrampup`. Default: `60`. |
| `sla.p95` | P95 response time threshold (ms). Tests stop if breached. |
| `sla.error_threshold` | Error rate (%) threshold for stopping (e.g., `50`). |
| `retry_count` | Number of retry attempts on JMeter failure. Default: `1`. |
| `timeout_seconds` | Max seconds to wait for a JMeter run. Default: `7200`. |
| `mode` | Escalation mode: `static` (default) or `adaptive` (Phase 2). |

---

## 2. Batch Parsing Configuration (Large Result Files)

For high-load tests generating millions of rows, the result parser reads the file in chunks instead of loading everything into memory.

| Parameter | Description | Default |
| :--- | :--- | :--- |
| `batch_size` | Rows processed per iteration. Lower = less memory per cycle. | `10 000` |
| `reservoir_size` | Max samples kept for P95/P99 estimation via reservoir sampling. | `100 000` |

Configure via code in `main.py` or per-scenario in YAML (future enhancement):
```python
parser = ResultsParser(result_file, batch_size=20_000, reservoir_size=200_000)
```

> **Note on percentile accuracy**: Reservoir sampling (Vitter's Algorithm R) provides
> statistically accurate P95/P99 within ~1% of exact values for files with 1M+ rows.

---

## 3. Distributed Load Testing (Slaves Setup)

To run tests across multiple machines, you must configure JMeter slaves.

### Step A: Slave Machine Configuration (Linux Recommended)

Run these commands on **each slave machine** to optimize for high load.

#### 1. Persistent File Limits

```bash
echo "* soft nofile 200000" | sudo tee -a /etc/security/limits.conf
echo "* hard nofile 200000" | sudo tee -a /etc/security/limits.conf
```

Verify after reboot with `ulimit -n`.

#### 2. Persistent TCP Tuning

```bash
echo "net.ipv4.ip_local_port_range=1024 65000" | sudo tee -a /etc/sysctl.conf
echo "net.ipv4.tcp_tw_reuse=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

These settings now survive reboots.

#### 3. Fix RMI Ports (Mandatory for Stability)

Edit `<JMETER_HOME>/bin/jmeter.properties` on **all** nodes:
```properties
server.rmi.localport=50000
client.rmi.localport=50001
server_port=1099
```

Then open these ports on **all** slaves:
- `1099` — JMeter server port
- `50000` — Server RMI local port
- `50001` — Client RMI local port

> **Why?** Without fixed RMI ports, JMeter uses random ephemeral ports that get blocked
> by firewalls, causing intermittent `java.rmi.ConnectException` failures under load.

#### 4. Start JMeter Server

```bash
./jmeter-server -Djava.rmi.server.hostname=<SLAVE_IP_ADDRESS>
```

### Step B: Orchestrator Configuration for Slaves

The `run` method in `jmeter_runner.py` accepts a `slaves` argument:
```python
runner.run(jmx_path, result_path, users, slaves=["192.168.1.10", "192.168.1.11"])
```

---

## 4. Time Synchronization (Mandatory)

Ensure **all nodes** (master + slaves) run `chrony` or `ntpd` for time consistency.

```bash
sudo apt install chrony
sudo systemctl enable chrony
sudo systemctl start chrony
```

Without time sync:
- Percentile timestamp calculations can **skew**
- Distributed logs **misalign**
- SLA breach detection may **drift**

---

## 5. JMeter Script (.jmx) Requirements

For the automated load escalation to work, your JMX files **MUST** use properties for dynamic injection:

1. Open your JMX in JMeter GUI.
2. Set **Number of Threads (users)** to `${__P(users, 1)}`.
3. Set **Ramp-Up (seconds)** to `${__P(rampup, 60)}`.

No manual JMX editing needed between runs — the orchestrator injects values automatically.

---

## 6. JVM Heap Tuning

If you encounter `OutOfMemoryError`, increase the heap size:

- **Linux**: `export HEAP="-Xms4g -Xmx4g"`
- **Windows**: `set HEAP=-Xms4g -Xmx4g`

---

## 7. Troubleshooting

| Problem | Solution |
| :--- | :--- |
| No results CSV | Check if `jmeter` is in PATH |
| `Connection Refused` | Verify RMI ports (1099, 50000, 50001) are open |
| Random distributed failures | Fix RMI ports in `jmeter.properties` (see Section 3) |
| SLA breached too early | Verify `sla.p95` values in `scenarios.yaml` |
| Pre-flight check fails | Ensure slaves are running `jmeter-server` and ports are reachable |
| Timestamp skew in reports | Enable time sync (chrony) across all nodes |
