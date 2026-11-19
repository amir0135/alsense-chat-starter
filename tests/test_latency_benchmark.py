"""The offline harness must exercise the real endpoints and count calls, not invent timings or Azure results."""

import json
import subprocess
import sys
from pathlib import Path
from statistics import median


def test_offline_benchmark_exercises_each_scenario_and_counts_real_boundary_calls(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "latency.json"
    subprocess.run(
        [sys.executable, "scripts/benchmark_latency.py", "--output", str(output)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output.read_text())
    assert "NOT Azure" in result["provider"] and result["runs_per_scenario"] == 5
    expected = {
        "simple_question": (2, 1, 1),
        "site_resolution": (2, 1, 2),
        "dashboard_navigation_unsupported": (1, 0, 0),
        "report_preparation": (3, 3, 3),
        "report_lifecycle": (3, 5, 7),
    }
    for name, counts in expected.items():
        samples = result["results"][name]["samples"]
        assert len(samples) == 5
        assert {(row["model_calls"], row["tool_calls"], row["adapter_calls"]) for row in samples} == {counts}
        assert all(row["elapsed_ms"] > 0 and row["correlation_ids"] for row in samples)
        assert result["results"][name]["median_ms"] == median(row["elapsed_ms"] for row in samples)
    assert "Authorization" not in output.read_text() and "Bearer" not in output.read_text()


def test_dashboard_suite_replays_only_real_paths_and_records_rounds_and_rejected_claims(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "dashboard.json"
    subprocess.run(
        [sys.executable, "scripts/benchmark_latency.py", "--suite", "dashboard", "--output", str(output)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output.read_text())
    assert result["scenario"] == "Open the alarms page and show only critical and high priority alarms."
    expected = {
        "dashboard_scripted": (0, 1, 1),
        "dashboard_direct": (1, 0, 0),
        "dashboard_sites": (2, 1, 2),
        "dashboard_docs": (2, 1, 1),
        "dashboard_repair_direct": (2, 0, 0),
        "dashboard_repair_after_tool": (3, 1, 1),
    }
    for name, counts in expected.items():
        samples = result["results"][name]["samples"]
        assert len(samples) == 5
        assert {(row["model_calls"], row["tool_calls"], row["adapter_calls"]) for row in samples} == {counts}
        for sample in samples:
            assert sample["ui_command_count"] == 0
            assert sample["domains"] == ["sites", "troubleshooting"]
            assert sample["sse_frames"] == (["step", "step", "reply"] if counts[1] else ["reply"])
            assert set(sample["tool_names"]) <= {"get_permitted_sites", "search_docs"}
            assert len(sample["model_request_metrics"]) == counts[0]
            assert len([span for span in sample["spans"] if span["stage"] == "model"]) == counts[0]
            assert len([span for span in sample["spans"] if span["stage"] == "api"]) == counts[2]
            guards = [span for span in sample["spans"] if span["stage"] == "guard"]
            assert [span["status"] for span in guards] == (["ERROR", "UNSET"] if "repair" in name else ["UNSET"])
            for metrics in sample["model_request_metrics"]:
                assert set(metrics["tool_names"]) == {"get_permitted_sites", "search_docs", "prepare_support_ticket"}
    assert "Bearer" not in output.read_text() and "I've opened" not in output.read_text()
