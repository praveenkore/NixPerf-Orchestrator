# Performance Automation Framework - Help & Configuration Guide

This document provides detailed instructions on how to configure the framework, set up JMeter slaves, and customize test parameters.

## 1. Orchestrator Configuration (`config/scenarios.yaml`)

The `scenarios.yaml` file is the primary configuration point for the framework.

| Option | Description |
| :--- | :--- |
| `name` | Unique name for the test scenario. Used for file naming and reports. |
| `jmx_path` | Relative path to the `.jmx` file (e.g., `scenarios/login.jmx`). |
| `load_steps` | A list of user counts to execute sequentially (e.g., `[500, 1000, 2000]`). |
| `sla.p95` | The 95th percentile response time threshold (in milliseconds). Tests stop if breached. |
| `sla.error_threshold` | The percentage of failed requests allowed before stopping (e.g., `50`). |

---

## 2. Distributed Load Testing (Slaves Setup)

To run tests across multiple machines, you must configure JMeter slaves.

### Step A: Slave Machine Configuration (Linux Recommended)
Run these commands on each slave machine to optimize for high load:

1.  **Increase File Limits**:
    ```bash
    ulimit -n 200000
    ```
2.  **Tune TCP Stack**:
    ```bash
    sudo sysctl -w net.ipv4.ip_local_port_range="1024 65000"
    sudo sysctl -w net.ipv4.tcp_tw_reuse=1
    ```
3.  **Start JMeter Server**:
    Navigate to your JMeter `bin` directory and run:
    ```bash
    ./jmeter-server -Djava.rmi.server.hostname=<SLAVE_IP_ADDRESS>
    ```

### Step B: Orchestrator Configuration for Slaves
To enable the master to use these slaves, update the `JMeterRunner` call in `orchestrator/main.py` or modify the `jmeter_runner.py` to accept a list of IPs.

**Currently supported in `jmeter_runner.py`**:
The `run` method accepts a `slaves` argument:
```python
runner.run(jmx_path, result_path, users, slaves=["192.168.1.10", "192.168.1.11"])
```

---

## 3. JMeter Script (.jmx) Requirements

For the automated load escalation to work, your JMX files MUST use a property for the thread count:

1.  Open your JMX in JMeter GUI.
2.  Locate your **Thread Group**.
3.  Set **Number of Threads (users)** to `${__P(users, 1)}`.
4.  This allows the orchestrator to inject the load dynamically via the `-Jusers` flag.

---

## 4. JVM Heap Tuning

If you encounter `OutOfMemoryError`, increase the heap size in the `jmeter` startup script or environment variables:

- **Linux**: `export HEAP="-Xms4g -Xmx4g"`
- **Windows**: `set HEAP=-Xms4g -Xmx4g`

---

## 5. Troubleshooting

- **No Results CSV**: Check if `jmeter` is in your system PATH.
- **Connection Refused**: Ensure the master can reach the slave IPs on port 1099 and the dynamic RMI ports (or disable firewalls).
- **SLA Breached Early**: Verify your `sla.p95` values in `scenarios.yaml` are realistic for your environment.
