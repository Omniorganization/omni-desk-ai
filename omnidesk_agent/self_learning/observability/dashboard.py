from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Optional, Union

from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.observability.report import LearningReportBuilder


class LearningDashboard:
    def __init__(self, audit_log: LearningAuditLog, report_builder: Optional[LearningReportBuilder] = None):
        self.audit_log = audit_log
        self.report_builder = report_builder or LearningReportBuilder()

    @classmethod
    def from_audit_path(cls, path: Union[str, Path]) -> "LearningDashboard":
        return cls(LearningAuditLog(path))

    def summary(self, *, days: int = 7) -> dict[str, Any]:
        events = self.audit_log.read_days(days)
        report = self.report_builder.build(events, period=f"last_{days}_days")
        report["event_count"] = len(events)
        return report

    def render_html(self, *, days: int = 7) -> str:
        summary = self.summary(days=days)
        metrics = summary.get("metrics", {})
        violations = summary.get("slo", {}).get("violations", [])
        rows = "\n".join(
            f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
            for k, v in sorted(metrics.items())
        )
        violation_items = "\n".join(
            f"<li><b>{html.escape(v.get('severity', ''))}</b> {html.escape(v.get('metric', ''))}: "
            f"{html.escape(str(v.get('value')))} {html.escape(v.get('operator', ''))} {html.escape(str(v.get('threshold')))}</li>"
            for v in violations
        ) or "<li>No SLO violations.</li>"
        recommendations = "\n".join(f"<li>{html.escape(r)}</li>" for r in summary.get("recommendations", []))
        recent_events = self.audit_log.read_days(days)[-10:]
        recent_event_items = "\n".join(
            "<li><pre>" + html.escape(json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True, default=str)) + "</pre></li>"
            for e in recent_events
        ) or "<li>No recent learning events.</li>"
        payload = html.escape(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>OmniDesk Learning Observability</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 32px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    td, th {{ border: 1px solid #ddd; padding: 8px; }}
    code, pre {{ background: #f6f8fa; padding: 8px; display: block; overflow: auto; }}
  </style>
</head>
<body>
  <h1>OmniDesk Learning Observability</h1>
  <p><b>Status:</b> {html.escape(str(summary.get("ok")))}</p>
  <p><b>Industrial readiness:</b> {html.escape(str(metrics.get("industrial_readiness_score")))}</p>
  <h2>Metrics</h2>
  <table><tbody>{rows}</tbody></table>
  <h2>SLO Violations</h2>
  <ul>{violation_items}</ul>
  <h2>Recommendations</h2>
  <ul>{recommendations}</ul>
  <h2>Recent Learning Events</h2>
  <ul>{recent_event_items}</ul>
  <h2>Raw Report</h2>
  <pre>{payload}</pre>
</body>
</html>"""
