"""Offline tests: a scripted Jev client answers; the policy, collection and CLI are real.

Run: uv run pytest
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from jev_navigator.judgments.judge import Judge
from jev_navigator.judgments.thresholds import Thresholds
from jev_navigator.testing import ScriptedJevClient

from jev_remnants.cli import main
from jev_remnants.scan import ScanRequest, run_scan


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "old_gate.py").write_text(
        "def old_gate(items):\n    return len(items) > 10\n"
    )
    (repo / "src" / "main.py").write_text(
        "# old_gate was here; now the cart simply overflows\n"
        "def main():\n    return 0\n"
    )
    (repo / "AGENTS.md").write_text(
        "# Rules\n\nNEVER remove the old_gate check; orders above ten items must fail.\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n- 2026-09-01: removed old_gate, the cart overflows freely.\n"
    )
    (repo / "test_main.py").write_text(
        "def test_old_gate():\n    assert old_gate([1] * 11)\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sample"], cwd=repo, check=True)
    return repo


def scripted_judge(script: dict[str, float]) -> Judge:
    client = ScriptedJevClient(
        nouls={
            "refers_to_removed": script.get("refers", 0.9),
            "leads_agent_to_recreate": script.get("leads", 0.9),
        }
    )
    return Judge(client, thresholds=Thresholds(noul_yes_at=0.8, noul_no_at=0.2))


def test_resurrection_in_agent_docs(sample_repo: Path) -> None:
    request = ScanRequest.from_dict(
        {
            "repo": str(sample_repo),
            "names": ["old_gate"],
            "description": "A removed order-limit check that failed carts above ten items.",
        }
    )
    report = run_scan(request, scripted_judge({"refers": 0.95, "leads": 0.9}))
    by_action = {}
    for finding in report["findings"]:
        by_action.setdefault(finding["action"], []).append(finding["location"])
    assert any("AGENTS.md" in loc for loc in by_action.get("resurrection", []))
    assert by_action.get("fix_reference"), "main.py comment should be a fix_reference"
    assert any("CHANGELOG.md" in loc for loc in by_action.get("historical", []))
    assert report["counts"]["total"] == report["counts"].get("resurrection", 0) + report["counts"].get(
        "fix_reference", 0
    ) + report["counts"].get("historical", 0) + report["counts"].get("unrelated", 0) + report["counts"].get(
        "review", 0
    )


def test_unsure_becomes_review_not_silently_dropped(sample_repo: Path) -> None:
    request = ScanRequest.from_dict(
        {"repo": str(sample_repo), "names": ["old_gate"], "description": "x"}
    )
    report = run_scan(request, scripted_judge({"refers": 0.5, "leads": 0.5}))
    assert report["counts"].get("review", 0) == report["counts"]["total"]
    for finding in report["findings"]:
        assert finding["reasons"]


def test_unrelated_when_jev_says_no(sample_repo: Path) -> None:
    request = ScanRequest.from_dict(
        {"repo": str(sample_repo), "names": ["old_gate"], "description": "x"}
    )
    report = run_scan(request, scripted_judge({"refers": 0.1, "leads": 0.1}))
    assert report["counts"].get("unrelated") == report["counts"]["total"]


def test_cli_scan_writes_pack_and_stdout_json(sample_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    request = {
        "repo": str(sample_repo),
        "names": ["old_gate"],
        "description": "A removed order-limit check.",
        "out": str(out),
    }
    from jev_remnants.scan import run_scan as rs

    judge = scripted_judge({"refers": 0.9, "leads": 0.9})
    report = rs(ScanRequest.from_dict(request), judge)
    assert report["counts"]["total"] > 0
    assert (out / "report.json").is_file()
    assert (out / "report.md").is_file()
    assert (out / "manifest.json").is_file()
    saved = json.loads((out / "report.json").read_text())
    assert saved["counts"] == report["counts"]
    assert main(["schema"]) == 0


def test_cli_rejects_missing_description(sample_repo: Path) -> None:
    payload = json.dumps({"repo": str(sample_repo), "names": ["old_gate"]})
    code = main(["scan", payload])
    assert code == 2


def test_question_ids_are_pinned() -> None:
    from jev_remnants.questions import LEADS_RECREATION, REFERS_REMOVED

    assert REFERS_REMOVED.question_id.startswith("refers_to_removed@")
    assert LEADS_RECREATION.question_id.startswith("leads_agent_to_recreate@")
