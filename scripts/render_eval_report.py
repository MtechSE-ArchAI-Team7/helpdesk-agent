"""Render a saved DeepEval run as a standalone, local HTML report."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def output_text(value: Any) -> str:
    if isinstance(value, str):
        original = value
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return original
    return json.dumps(value, indent=2, ensure_ascii=True)


def render_report(data: dict[str, Any]) -> str:
    cases = data["testCases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("DeepEval report must contain a nonempty testCases list")
    passed = sum(case["success"] is True for case in cases)
    rows = []
    overview_rows = []
    category_counts: dict[str, list[int]] = {}
    for index, case in enumerate(cases, start=1):
        success = case["success"] is True
        metadata = case.get("metadata") or {}
        metrics = case.get("metricsData") or []
        status = "passed" if success else "failed"
        operation = escaped(metadata.get("operation", "unknown"))
        area = escaped(metadata.get("area", "unknown"))
        counts = category_counts.setdefault(str(metadata.get("area", "unknown")), [0, 0])
        counts[0] += 1
        counts[1] += int(success)
        duration = (
            f"{metadata['execution_duration_seconds']:.3f} s"
            if "execution_duration_seconds" in metadata
            else "Not recorded"
        )
        reasons = "<br>".join(escaped(metric.get("reason") or metric.get("error") or "") for metric in metrics)
        overview_rows.append(
            f'<tr data-status="{status}" data-operation="{operation}" data-area="{area}">'
            f'<td>{index}</td><td><a href="#case-{index}">{escaped(case["name"])}</a></td>'
            f'<td class="{status}">{"PASS" if success else "FAIL"}</td>'
            f"<td>{area}</td>"
            f"<td>{duration}</td><td>{reasons}</td></tr>"
        )
        metric_rows = "".join(
            f"<li><strong>{escaped(metric['name'])}</strong>: "
            f"score {escaped(metric.get('score'))} / threshold {escaped(metric.get('threshold'))}"
            f"<p>{escaped(metric.get('reason') or metric.get('error') or '')}</p></li>"
            for metric in metrics
        ) or "<li>Not available</li>"
        details = "".join(
            f"<h4>{label}</h4><pre>{escaped(output_text(case.get(field)))}</pre>"
            for label, field in (
                ("Input", "input"),
                ("Expected output", "expectedOutput"),
                ("Actual output", "actualOutput"),
            )
        )
        rows.append(
            f'<article id="case-{index}" data-status="{status}" data-operation="{operation}" data-area="{area}">'
            f'<h3><span class="{"passed" if success else "failed"}">'
            f"{'PASS' if success else 'FAIL'}</span> {escaped(case['name'])}</h3>"
            f"<p>Category: {area} · Operation: {operation} · "
            f"Model: {escaped(metadata.get('model', 'unknown'))} · "
            f"Duration: {duration}</p>"
            f"<ul>{metric_rows}</ul><details><summary>Input and output details</summary>"
            f"{details}</details></article>"
        )
    operations = sorted({str((case.get("metadata") or {}).get("operation", "unknown")) for case in cases})
    options = "".join(f'<option value="{escaped(op)}">{escaped(op)}</option>' for op in operations)
    areas = sorted({str((case.get("metadata") or {}).get("area", "unknown")) for case in cases})
    area_options = "".join(f'<option value="{escaped(area)}">{escaped(area)}</option>' for area in areas)
    category_rows = "".join(
        f"<tr><td>{escaped(area)}</td><td>{total}</td>"
        f'<td class="passed">{category_passed}</td>'
        f'<td class="failed">{total - category_passed}</td>'
        f"<td>{category_passed / total:.1%}</td></tr>"
        for area, (total, category_passed) in sorted(category_counts.items())
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepEval evaluation report</title>
<style>
body {{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;
padding:0 1rem;background:#f6f8fa;color:#182230}}
article {{background:white;border:1px solid #d0d7de;border-radius:8px;padding:1rem;margin:1rem 0}}
.table-container {{overflow-x:auto;margin:1rem 0}}
table {{width:100%;border-collapse:collapse;background:white;font-size:.9rem}}
th,td {{padding:.65rem;text-align:left;border:1px solid #d0d7de;vertical-align:top}}
th {{background:#eaeef2}} td {{overflow-wrap:anywhere}} a {{color:#0969da}}
.passed {{color:#176b35}} .failed {{color:#b42318}}
pre {{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:1rem;border-radius:6px}}
select {{padding:.4rem;margin-right:1rem}} summary {{cursor:pointer}} [hidden] {{display:none}}
</style></head><body>
<h1>DeepEval evaluation report</h1>
<p><strong>{passed} passed / {len(cases) - passed} failed / {len(cases)} total</strong>
 · Pass rate: {passed / len(cases):.1%} · Total run duration: {escaped(data.get("runDuration", "unknown"))} s</p>
<h2>Category summary</h2>
<div class="table-container"><table id="category-summary">
<thead><tr><th scope="col">Category</th><th scope="col">Number of tests</th>
<th scope="col">Pass</th><th scope="col">Fail</th><th scope="col">Pass rate</th></tr></thead>
<tbody>{category_rows}</tbody></table></div>
<label>Status <select id="status"><option value="">All</option><option value="failed">Failed</option>
<option value="passed">Passed</option></select></label>
<label>Operation <select id="operation"><option value="">All</option>{options}</select></label>
<label>Category <select id="area"><option value="">All</option>{area_options}</select></label>
<h2>Test overview</h2>
<div class="table-container"><table>
<thead><tr><th scope="col">#</th><th scope="col">Test</th><th scope="col">Result</th>
<th scope="col">Category</th><th scope="col">Duration</th>
<th scope="col">Reason</th></tr></thead>
<tbody>{"".join(overview_rows)}</tbody></table></div>
<h2>Test details</h2>
{"".join(rows)}
<script>
const statusFilter = document.getElementById('status');
const operationFilter = document.getElementById('operation');
const areaFilter = document.getElementById('area');
function filterCases() {{
  document.querySelectorAll('article, tbody tr[data-status]').forEach(row => {{
    row.hidden = (statusFilter.value !== '' && row.dataset.status !== statusFilter.value)
      || (operationFilter.value !== '' && row.dataset.operation !== operationFilter.value)
      || (areaFilter.value !== '' && row.dataset.area !== areaFilter.value);
  }});
}}
statusFilter.addEventListener('change', filterCases);
operationFilter.addEventListener('change', filterCases);
areaFilter.addEventListener('change', filterCases);
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="DeepEval test_run JSON")
    parser.add_argument("output", type=Path, help="Standalone HTML destination")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("DeepEval report must be a JSON object")
    args.output.write_text(render_report(data), encoding="utf-8")
    print(f"Saved HTML report: {args.output}")


if __name__ == "__main__":
    main()
