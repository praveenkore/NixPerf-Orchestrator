"""
reporting.py - Generates JSON and HTML reports from scenario results.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HTML_STYLE = """
    body  { font-family: Inter, sans-serif; margin: 24px; background: #f4f4f9; color: #333; }
    h1    { color: #1a1a2e; }
    h2    { color: #16213e; border-bottom: 2px solid #007bff; padding-bottom: 4px; }
    p     { font-size: 0.9rem; }
    table { border-collapse: collapse; width: 100%; margin-top: 12px;
            background: #fff; border-radius: 8px; overflow: hidden;
            box-shadow: 0 2px 6px rgba(0,0,0,.08); }
    th, td { border: 1px solid #e0e0e0; padding: 10px 14px; text-align: left; }
    th    { background: #007bff; color: #fff; }
    tr:nth-child(even) { background: #f8f9ff; }
    .stop    { color: #d32f2f; font-weight: 700; }
    .proceed { color: #388e3c; font-weight: 700; }
    .breakpoint { background: #fff3e0; border-left: 4px solid #ff9800;
                  padding: 8px 14px; margin-top: 8px; border-radius: 4px; }
"""

_TABLE_HEADER = """
    <table>
      <tr>
        <th>Load (Users)</th>
        <th>Status</th>
        <th>Error %</th>
        <th>P95 (ms)</th>
        <th>Avg (ms)</th>
        <th>Reason</th>
      </tr>
"""


class Reporter:
    """Writes JSON and HTML summary reports to disk."""

    @staticmethod
    def generate_json_report(results: list[Any], output_path: str) -> None:
        """Serialize results to a pretty-printed JSON file."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(results, f, indent=4)
        logger.info("JSON report → %s", output_path)

    @staticmethod
    def generate_html_summary(results: list[Any], output_path: str) -> None:
        """Render an HTML summary report from scenario results."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body_rows = "".join(Reporter._render_scenario(s) for s in results)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>NixPerf - Test Summary</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <h1>NixPerf - Performance Test Summary</h1>
  <p>Generated: {timestamp}</p>
  {body_rows}
</body>
</html>"""

        path.write_text(html, encoding="utf-8")
        logger.info("HTML report → %s", output_path)

    # --- Private helpers ---

    @staticmethod
    def _render_scenario(scenario: dict) -> str:
        rows = "".join(Reporter._render_run_row(r) for r in scenario.get("runs", []))
        breakpoint_html = ""
        if scenario.get("breakpoint"):
            breakpoint_html = (
                f'<p class="breakpoint">⚠️ Breaking load point: '
                f'<strong>{scenario["breakpoint"]} users</strong></p>'
            )
        return f"<h2>Scenario: {scenario['name']}</h2>{_TABLE_HEADER}{rows}</table>{breakpoint_html}"

    @staticmethod
    def _render_run_row(run: dict) -> str:
        decision = run["decision"]
        css_class = "stop" if decision == "STOP" else "proceed"
        m = run.get("metrics", {})
        return (
            f"<tr>"
            f"<td>{run['users']}</td>"
            f"<td class='{css_class}'>{decision}</td>"
            f"<td>{m.get('error_percent', 0):.2f}%</td>"
            f"<td>{m.get('p95', 0):.0f}</td>"
            f"<td>{m.get('avg_response_time', 0):.0f}</td>"
            f"<td>{run['reason']}</td>"
            f"</tr>"
        )
