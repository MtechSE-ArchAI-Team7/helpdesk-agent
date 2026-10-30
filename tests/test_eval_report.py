import pytest

from scripts.render_eval_report import render_report

pytestmark = pytest.mark.unit


def test_overview_links_results_and_escapes_content():
    report = render_report(
        {
            "testCases": [
                {
                    "name": "<unsafe>",
                    "success": True,
                    "metadata": {
                        "operation": "triage",
                        "area": "classification",
                        "execution_duration_seconds": 2.5,
                    },
                    "runDuration": 0.003,
                    "metricsData": [
                        {"name": "Behavior", "score": 1, "threshold": 1, "reason": "Matched"},
                    ],
                },
                {
                    "name": "failed-case",
                    "success": False,
                    "metadata": {"operation": "browser_evidence"},
                    "metricsData": [
                        {"name": "Behavior", "score": 0, "threshold": 1, "error": "<error>"},
                    ],
                },
            ],
        }
    )
    assert "1 passed / 1 failed / 2 total" in report
    assert "<title>DeepEval evaluation report</title>" in report
    assert "<h1>DeepEval evaluation report</h1>" in report
    assert "Case durations include" not in report
    assert "Suite:" not in report
    assert "Local report; no external resources or uploads." not in report
    assert report.count("<tr data-status=") == 2
    assert '<a href="#case-1">&lt;unsafe&gt;</a>' in report
    assert '<article id="case-1"' in report
    assert '<a href="#case-2">failed-case</a>' in report
    assert '<article id="case-2"' in report
    assert 'class="passed">PASS</td>' in report
    assert 'class="failed">FAIL</td>' in report
    overview = report.split("<h2>Test overview</h2>", 1)[1].split("</table>", 1)[0]
    assert overview.count('<th scope="col">') == 6
    assert '<th scope="col">Operation</th>' not in overview
    assert "Score / threshold" not in overview
    assert "<td>triage</td>" not in overview
    assert "score 1 / threshold 1" in report
    assert "2.500 s" in report
    assert "0.003 s" not in report
    assert "&lt;error&gt;" in report
    assert "<unsafe>" not in report
    assert "article, tbody tr[data-status]" in report
    assert 'data-area="classification"' in report
    assert "<td>classification</td>" in report
    assert "Category: classification" in report
    assert '<option value="classification">classification</option>' in report
    assert "row.dataset.area !== areaFilter.value" in report


def test_overview_handles_missing_metrics():
    report = render_report({"testCases": [{"name": "case", "success": False}]})
    assert "Not available" in report
    assert "<td>unknown</td>" in report
    assert "<td>Not recorded</td>" in report


def test_category_summary_counts_rates_and_placement():
    report = render_report(
        {
            "testCases": [
                {"name": "pass", "success": True, "metadata": {"area": "security"}},
                {"name": "fail", "success": False, "metadata": {"area": "security"}},
                {"name": "other", "success": True, "metadata": {"area": "<classification>"}},
                {"name": "unknown", "success": False},
            ]
        }
    )
    summary = report.split('<table id="category-summary">', 1)[1].split("</table>", 1)[0]
    assert summary.count('<th scope="col">') == 5
    assert ('<td>security</td><td>2</td><td class="passed">1</td><td class="failed">1</td><td>50.0%</td>') in summary
    assert (
        '<td>&lt;classification&gt;</td><td>1</td><td class="passed">1</td><td class="failed">0</td><td>100.0%</td>'
    ) in summary
    assert ('<td>unknown</td><td>1</td><td class="passed">0</td><td class="failed">1</td><td>0.0%</td>') in summary
    assert "data-status" not in summary
    assert report.index('id="category-summary"') < report.index("<label>Status")
