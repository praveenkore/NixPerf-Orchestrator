"""
reporting.py - Generates JSON and HTML reports from scenario results.

New capabilities vs original:
    - WARN decision rendered in amber in HTML tables.
    - send_webhook_notification() — POST a completion summary to a Slack-compatible
      incoming-webhook URL (or any generic JSON endpoint).
    - save_baseline() / compare_to_baseline() — persist a golden-run baseline and
      flag P95 regressions on subsequent runs.
    - clean_old_results() — prune stale per-scenario CSV files from results/.
"""
import json
import logging
import smtplib
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Regression threshold: flag if current P95 is more than this % worse than baseline.
DEFAULT_REGRESSION_THRESHOLD = 0.20   # 20 %

# How many result CSV files to keep per scenario (older ones are deleted).
DEFAULT_KEEP_RESULTS = 5

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
    .warn    { color: #f57c00; font-weight: 700; }
    .proceed { color: #388e3c; font-weight: 700; }
    .breakpoint { background: #fff3e0; border-left: 4px solid #ff9800;
                  padding: 8px 14px; margin-top: 8px; border-radius: 4px; }
    .abort      { background: #fce4ec; border-left: 4px solid #d32f2f;
                  padding: 8px 14px; margin-top: 8px; border-radius: 4px; }
    .regression { background: #fff8e1; border-left: 4px solid #fbc02d;
                  padding: 8px 14px; margin-top: 8px; border-radius: 4px; font-size:0.85rem; }
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
    """Writes JSON and HTML summary reports to disk, sends webhook notifications,
    manages baselines, and prunes stale result files."""

    # ------------------------------------------------------------------
    # Core report generation (unchanged API)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Webhook notification
    # ------------------------------------------------------------------

    @staticmethod
    def send_webhook_notification(
        results: list[Any],
        webhook_url: str,
        extra_context: Optional[dict] = None,
    ) -> None:
        """POST a test-completion summary to a Slack-compatible webhook URL.

        The payload follows the Slack Incoming Webhooks format and is also
        accepted by most generic JSON webhook endpoints.

        Args:
            results:       Serialised scenario results (list of dicts).
            webhook_url:   Destination URL (Slack, Teams, PagerDuty, etc.).
            extra_context: Optional key/value pairs added as attachment fields
                           (e.g. {"regressions": 2, "environment": "staging"}).
        """
        summary_lines: list[str] = []
        for scenario in results:
            bp   = scenario.get("breakpoint")
            abrt = scenario.get("abort_reason")
            if abrt:
                line = f"• *{scenario['name']}*: ❌ aborted — {abrt}"
            elif bp:
                line = f"• *{scenario['name']}*: ⚠️ breakpoint at *{bp} users*"
            else:
                line = f"• *{scenario['name']}*: ✅ all load steps passed"
            summary_lines.append(line)

        attachments: list[dict] = [
            {
                "color": "#36a64f",
                "title": "NixPerf Load Test Complete",
                "text": "\n".join(summary_lines),
                "footer": (
                    f"NixPerf Orchestrator | "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                ),
            }
        ]

        if extra_context:
            attachments[0]["fields"] = [
                {"title": k, "value": str(v), "short": True}
                for k, v in extra_context.items()
            ]

        payload = json.dumps({"attachments": attachments}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(
                    "Webhook notification sent successfully (HTTP %d)", resp.status
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webhook notification failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # SMTP email notification
    # ------------------------------------------------------------------

    @staticmethod
    def send_email_notification(
        results: list[Any],
        smtp_config: dict,
        extra_context: Optional[dict] = None,
    ) -> None:
        """Send a test-completion summary via SMTP.

        Args:
            results:       Serialised scenario results (list of dicts).
            smtp_config:   Dict containing host, port, user, password,
                           sender, and recipient.
            extra_context: Optional key/value pairs (e.g. {"regressions": 2}).
        """
        host      = smtp_config.get("host")
        port      = smtp_config.get("port", 587)
        user      = smtp_config.get("user")
        password  = smtp_config.get("password")
        sender    = smtp_config.get("sender")
        recipient = smtp_config.get("recipient")
        use_tls   = smtp_config.get("use_tls", port == 587)

        if not all([host, sender, recipient]):
            logger.warning("SMTP configuration incomplete — skipping email notification")
            return

        # Prepare summary text
        summary_lines: list[str] = []
        for scenario in results:
            bp   = scenario.get("breakpoint")
            abrt = scenario.get("abort_reason")
            if abrt:
                line = f"• {scenario['name']}: ❌ aborted — {abrt}"
            elif bp:
                line = f"• {scenario['name']}: ⚠️ breakpoint at {bp} users"
            else:
                line = f"• {scenario['name']}: ✅ all load steps passed"
            summary_lines.append(line)

        subject = f"NixPerf Load Test Complete — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        body = (
            f"NixPerf Load Test Summary\n"
            f"=========================\n\n"
            + "\n".join(summary_lines)
            + "\n\n"
        )

        if extra_context:
            body += "Details:\n"
            for k, v in extra_context.items():
                body += f"• {k}: {v}\n"
            body += "\n"

        body += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"

        # Create email
        msg = MIMEMultipart()
        msg["From"]    = sender
        msg["To"]      = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(host, port, timeout=15) as server:
                if use_tls:
                    server.starttls()
                
                # Only attempt login if username is provided
                if user:
                    if "AUTH" in server.esmtp_features or "AUTH" in server.features:
                        server.login(user, password or "")
                    else:
                        logger.debug("SMTP server does not support AUTH — skipping login")
                
                server.send_message(msg)
            logger.info("Email notification sent successfully to %s", recipient)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Email notification failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Baseline management
    # ------------------------------------------------------------------

    @staticmethod
    def save_baseline(
        results: list[Any],
        baseline_path: str = "reports/baseline.json",
    ) -> None:
        """Persist the current results as the performance baseline.

        Subsequent runs will be compared against this file by
        ``compare_to_baseline()``.
        """
        path = Path(baseline_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(results, f, indent=4)
        logger.info("Baseline saved → %s", baseline_path)

    @staticmethod
    def compare_to_baseline(
        current: list[Any],
        baseline_path: str = "reports/baseline.json",
        regression_threshold: float = DEFAULT_REGRESSION_THRESHOLD,
    ) -> list[dict]:
        """Compare current results to the saved baseline.

        If no baseline exists, the current results are saved as the new
        baseline and an empty list is returned.

        Args:
            current:              Serialised scenario results for this run.
            baseline_path:        Path to the baseline JSON file.
            regression_threshold: Relative P95 increase that is flagged as a
                                  regression (default: 0.20 = 20 %).

        Returns:
            List of regression dicts (empty if none found).  Each dict contains:
                scenario, users, current_p95, baseline_p95, regression_pct.
        """
        path = Path(baseline_path)
        if not path.exists():
            logger.info(
                "No baseline found at '%s' — saving current run as baseline",
                baseline_path,
            )
            Reporter.save_baseline(current, baseline_path)
            return []

        try:
            with path.open("r", encoding="utf-8") as f:
                baseline = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read baseline file '%s' (%s) — skipping comparison",
                baseline_path, exc,
            )
            return []

        baseline_map: dict[str, dict] = {s["name"]: s for s in baseline}
        regressions: list[dict] = []

        for scenario in current:
            name = scenario["name"]
            if name not in baseline_map:
                logger.debug(
                    "Scenario '%s' not found in baseline — skipping comparison", name
                )
                continue

            base_runs: dict[int, dict] = {
                r["users"]: r for r in baseline_map[name].get("runs", [])
            }

            for run in scenario.get("runs", []):
                users     = run["users"]
                curr_p95  = run.get("metrics", {}).get("p95", 0)
                base_run  = base_runs.get(users)
                base_p95  = base_run.get("metrics", {}).get("p95", 0) if base_run else 0

                if base_p95 > 0 and curr_p95 > 0:
                    delta = (curr_p95 - base_p95) / base_p95
                    if delta > regression_threshold:
                        finding = {
                            "scenario":       name,
                            "users":          users,
                            "current_p95":    round(curr_p95, 1),
                            "baseline_p95":   round(base_p95, 1),
                            "regression_pct": round(delta * 100, 1),
                        }
                        regressions.append(finding)
                        logger.warning(
                            "⚠ REGRESSION — scenario '%s' at %d users: "
                            "P95 %.0f ms → %.0f ms (+%.0f%%)",
                            name, users, base_p95, curr_p95, delta * 100,
                        )

        if not regressions:
            logger.info("Baseline comparison: no regressions detected ✓")

        return regressions

    # ------------------------------------------------------------------
    # Result file retention
    # ------------------------------------------------------------------

    @staticmethod
    def clean_old_results(
        scenario_name: str,
        results_dir: str = "results",
        keep_last: int = DEFAULT_KEEP_RESULTS,
    ) -> None:
        """Delete old result CSV files, keeping only the most recent ``keep_last``.

        Files are sorted by modification time so the newest are always retained.

        Args:
            scenario_name: Scenario name used as the filename prefix.
            results_dir:   Directory containing result CSV files.
            keep_last:     Number of recent files to retain (default: 5).
        """
        dir_path = Path(results_dir)
        if not dir_path.exists():
            return

        pattern = f"{scenario_name}_*.csv"
        files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime)
        to_delete = files[:-keep_last] if len(files) > keep_last else []

        if not to_delete:
            return

        deleted = 0
        for old_file in to_delete:
            try:
                old_file.unlink()
                deleted += 1
                logger.debug("Cleaned up old result file: %s", old_file.name)
            except OSError as exc:
                logger.warning("Could not delete result file %s: %s", old_file.name, exc)

        logger.info(
            "Retention policy: removed %d old result file(s) for scenario '%s' "
            "(kept last %d)",
            deleted, scenario_name, keep_last,
        )

    # ------------------------------------------------------------------
    # Private HTML helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_scenario(scenario: dict) -> str:
        rows = "".join(Reporter._render_run_row(r) for r in scenario.get("runs", []))

        extras = ""
        if scenario.get("breakpoint"):
            extras += (
                f'<p class="breakpoint">⚠️ Breaking load point: '
                f'<strong>{scenario["breakpoint"]} users</strong></p>'
            )
        if scenario.get("abort_reason"):
            extras += (
                f'<p class="abort">❌ Scenario aborted: '
                f'{scenario["abort_reason"]}</p>'
            )

        return (
            f"<h2>Scenario: {scenario['name']}</h2>"
            f"{_TABLE_HEADER}{rows}</table>{extras}"
        )

    @staticmethod
    def _render_run_row(run: dict) -> str:
        decision  = run["decision"]
        css_class = {"STOP": "stop", "WARN": "warn"}.get(decision, "proceed")
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
