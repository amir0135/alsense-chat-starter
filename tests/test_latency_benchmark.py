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
