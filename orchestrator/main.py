import yaml
import os
import time
from orchestrator.jmeter_runner import JMeterRunner
from orchestrator.parser import ResultsParser
from orchestrator.decision_engine import DecisionEngine
from orchestrator.reporting import Reporter

def load_config(config_path):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def main():
    config_path = "config/scenarios.yaml"
    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}")
        return

    config = load_config(config_path)
    runner = JMeterRunner() # Assumes 'jmeter' is in PATH
    
    overall_results = []

    for scenario_cfg in config['scenarios']:
        name = scenario_cfg['name']
        jmx_path = scenario_cfg['jmx_path']
        load_steps = scenario_cfg['load_steps']
        sla_p95 = scenario_cfg['sla']['p95']
        error_threshold = scenario_cfg['sla']['error_threshold']

        print(f"\n>>> Starting Scenario: {name}")
        
        scenario_results = {
            "name": name,
            "runs": [],
            "breakpoint": None
        }

        engine = DecisionEngine(sla_p95, error_threshold)

        for users in load_steps:
            print(f"--- Running with {users} users ---")
            
            result_file = f"results/{name}_{users}_{int(time.time())}.csv"
            
            # Step 1: Run JMeter
            success, output = runner.run(jmx_path, result_file, users)
            
            # Step 2: Parse results
            metrics = None
            if os.path.exists(result_file):
                parser = ResultsParser(result_file)
                metrics = parser.parse()
            
            # Step 3: Evaluate
            decision, reason = engine.evaluate(metrics)
            
            run_info = {
                "users": users,
                "metrics": metrics if metrics else {"error_percent": 0, "p95": 0, "avg_response_time": 0},
                "decision": decision,
                "reason": reason
            }
            scenario_results["runs"].append(run_info)

            print(f"Result: {decision} - {reason}")
            if metrics:
                print(f"Metrics: Error={metrics['error_percent']:.2f}%, P95={metrics['p95']:.2f}ms")

            if decision == "STOP":
                scenario_results["breakpoint"] = users
                break

        overall_results.append(scenario_results)

    # Generate Reports
    timestamp = int(time.time())
    Reporter.generate_json_report(overall_results, f"reports/summary_{timestamp}.json")
    Reporter.generate_html_summary(overall_results, f"reports/summary_{timestamp}.html")

    print("\n>>> Performance testing completed.")

if __name__ == "__main__":
    main()
