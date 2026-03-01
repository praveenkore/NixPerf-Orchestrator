import json
import os

class Reporter:
    @staticmethod
    def generate_json_report(results, output_path):
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=4)
        print(f"JSON report generated: {output_path}")

    @staticmethod
    def generate_html_summary(results, output_path):
        """
        Generates a simple HTML summary of the performance test results.
        """
        html_content = f"""
        <html>
        <head>
            <title>Performance Test Summary</title>
            <style>
                body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f9; }}
                h1 {{ color: #333; }}
                table {{ border-collapse: collapse; width: 100%; margin-top: 20px; background: white; }}
                th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                th {{ background-color: #007bff; color: white; }}
                tr:nth-child(even) {{ background-color: #f2f2f2; }}
                .status-stop {{ color: red; font-weight: bold; }}
                .status-proceed {{ color: green; font-weight: bold; }}
            </style>
        </head>
        <body>
            <h1>Performance Test Summary</h1>
            <p>Generated at: {os.path.basename(output_path)}</p>
        """

        for scenario in results:
            html_content += f"<h2>Scenario: {scenario['name']}</h2>"
            html_content += """
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
            for run in scenario['runs']:
                status_class = "status-stop" if run['decision'] == "STOP" else "status-proceed"
                html_content += f"""
                <tr>
                    <td>{run['users']}</td>
                    <td class="{status_class}">{run['decision']}</td>
                    <td>{run['metrics']['error_percent']:.2f}%</td>
                    <td>{run['metrics']['p95']:.2f}</td>
                    <td>{run['metrics']['avg_response_time']:.2f}</td>
                    <td>{run['reason']}</td>
                </tr>
                """
            html_content += "</table>"
            
            if scenario.get('breakpoint'):
                html_content += f"<p><strong>Breaking Load Point: {scenario['breakpoint']} users</strong></p>"

        html_content += "</body></html>"

        with open(output_path, 'w') as f:
            f.write(html_content)
        print(f"HTML report generated: {output_path}")
