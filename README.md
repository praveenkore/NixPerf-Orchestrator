# Performance Automation Framework

This framework automates load escalation tests using JMeter. For detailed configuration and slave setup, see [HELP.md](file:///c:/Users/prave/Downloads/Code-base/performance-automation/HELP.md).

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install PyYAML
    ```
2.  **JMeter**: Ensure `jmeter` is in your system PATH.
3.  **Configuration**: Edit `config/scenarios.yaml` to define your test scenarios and load steps.
4.  **JMX Files**: Place your JMX files in the `scenarios/` directory. Ensure they use `${__P(users, 1)}` for thread counts and `${__P(rampup, 1)}` for ramp-up periods.
5.  **Results**: Results are saved as `.csv` files in the `results/` folder.

## Usage

Run the orchestrator:
```bash
python -m orchestrator.main
```
Then the orchestrator detects the break condition and stops that scenario.

## How it Works

1.  **Orchestrator** reads `config/scenarios.yaml`.
2.  For each scenario, starts at the first load step (e.g., 500 users).
3.  Executes JMeter in CLI mode, injecting `users` and `rampup` as properties.
4.  After each run, parses the CSV result file.
5.  The **Decision Engine** checks if:
    - Error % > threshold (default 50%)
    - P95 latency > SLA
6.  If healthy, moves to the next load step; if a threshold is breached, records the breakpoint.
7.  Generates JSON and HTML reports in `reports/`.
