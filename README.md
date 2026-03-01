# Performance Automation Framework

This framework automates load escalation tests using JMeter. For detailed configuration and slave setup, see [HELP.md](./HELP.md).

## Environment Requirements

- **Python** 3.10+
- **Apache JMeter** 5.6+
- **OS**: Linux recommended for master and slave nodes
- **RAM**: Minimum 8 GB per slave node
- **Ports**: 1099, 50000, 50001 open on all nodes
- **Time Sync**: NTP/Chrony enabled across all nodes
- **PyYAML**: `pip install PyYAML`

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install PyYAML
    ```
2.  **JMeter**: Ensure `jmeter` is in your system PATH.
3.  **Configuration**: Edit `config/scenarios.yaml` to define your test scenarios and load steps.
4.  **JMX Files**: Place your JMX files in the `scenarios/` directory. Use `${__P(users,1)}` for thread counts and `${__P(rampup,60)}` for ramp-up.
5.  **Results**: Results are saved as `.csv` files in the `results/` folder.

## Usage

```bash
python -m orchestrator.main
python -m orchestrator.main --config path/to/scenarios.yaml
```

## How it Works

1.  **Pre-flight checks** validate slave connectivity and config integrity.
2.  **Orchestrator** reads `config/scenarios.yaml`.
3.  For each scenario, starts at the first load step (e.g., 500 users).
4.  Executes JMeter in CLI mode, injecting `users` and `rampup` as properties.
5.  After each run, parses the CSV result file in batches (memory-safe for large files).
6.  The **Decision Engine** checks if:
    - Error % > threshold (default 50%)
    - P95 latency > SLA
7.  If healthy, moves to the next load step; if a threshold is breached, records the breakpoint.
8.  Retries failed JMeter executions (configurable retry count and timeout).
9.  Generates JSON and HTML reports in `reports/`.
