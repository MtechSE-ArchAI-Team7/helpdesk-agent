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
                    "metadata": {"operation": "triage"},
                    "runDuration": 2.5,
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
    assert report.count("<tr data-status=") == 2
    assert '<a href="#case-1">&lt;unsafe&gt;</a>' in report
    assert '<article id="case-1"' in report
    assert '<a href="#case-2">failed-case</a>' in report
    assert '<article id="case-2"' in report
    assert 'class="passed">PASS</td>' in report
    assert 'class="failed">FAIL</td>' in report
    assert "Behavior: 1 / 1" in report
    assert "2.5 s" in report
    assert "&lt;error&gt;" in report
    assert "<unsafe>" not in report
    assert "article, tbody tr" in report


def test_overview_handles_missing_metrics():
    report = render_report({"testCases": [{"name": "case", "success": False}]})
    assert "Not available" in report
    assert "<td>unknown</td>" in report
