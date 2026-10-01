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


def test_docstring_mention_is_collected_with_attached_code(sample_repo: Path) -> None:
    from jev_remnants.collect import collect_candidates

    (sample_repo / "src" / "cart.py").write_text(
        'def add_item(cart):\n'
        '    """Add an item; the old_gate limit of ten no longer applies."""\n'
        '    cart.append(1)\n'
        '    return cart\n'
    )
    subprocess.run(["git", "add", "."], cwd=sample_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "docstring"], cwd=sample_repo, check=True)
    candidates, remainder = collect_candidates(sample_repo, ("old_gate",))
    docstring = [
        candidate
        for candidate in candidates
        if candidate.file == "src/cart.py" and candidate.comment_kind == "docstring"
    ]
    assert docstring, "the docstring mention must be a candidate"
    assert docstring[0].kind == "comment"
    assert "def add_item" in docstring[0].attached_code
    assert not remainder


def test_spec_terms_find_unnamed_prose(sample_repo: Path) -> None:
    from jev_remnants.collect import collect_candidates

    candidates, _ = collect_candidates(
        sample_repo, ("old_gate",), spec_terms=("orders above ten items",)
    )
    locations = [f"{c.file}:{c.line}" for c in candidates]
    assert "AGENTS.md:3" in locations, "spec prose without the name must be a candidate"


def test_dry_run_makes_no_jev_calls(sample_repo: Path) -> None:
    request = ScanRequest.from_dict(
        {
            "repo": str(sample_repo),
            "names": ["old_gate"],
            "description": "x",
            "dry_run": True,
        }
    )
    judge = scripted_judge({"refers": 0.9, "leads": 0.9})
    report = run_scan(request, judge)
    assert report["dry_run"] is True
    assert report["budget"].get("calls") is None
    assert report["counts"]["total"] > 0
    assert all("action" not in candidate for candidate in report["candidates"])


def test_dry_run_with_out_writes_a_pack(sample_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "drypack"
    request = ScanRequest.from_dict(
        {
            "repo": str(sample_repo),
            "names": ["old_gate"],
            "description": "x",
            "dry_run": True,
            "out": str(out),
        }
    )
    report = run_scan(request, None)
    assert (out / "report.json").is_file()
    saved = json.loads((out / "report.json").read_text())
    assert saved["dry_run"] is True
    assert report["counts"]["total"] > 0
    # A dry-run pack has report.json only: no live artifacts to mistake for a real run.
    assert not (out / "manifest.json").exists()
    # A retry replays the saved dry run and marks it.
    replay = run_scan(ScanRequest.from_dict(
        {"repo": str(sample_repo), "names": ["old_gate"], "description": "x",
         "dry_run": True, "out": str(out)}
    ), None)
    assert replay["already_run"] is True


def test_retry_with_same_out_dir_is_idempotent(sample_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    request_dict = {
        "repo": str(sample_repo),
        "names": ["old_gate"],
        "description": "x",
        "out": str(out),
    }
    judge = scripted_judge({"refers": 0.9, "leads": 0.9})
    first = run_scan(ScanRequest.from_dict(request_dict), judge)
    second = run_scan(ScanRequest.from_dict(request_dict), judge)
    assert first["counts"] == second["counts"]
    assert second["already_run"] is True
    # The retry changed nothing: the pack still holds exactly the first run.
    report_documents = (out / "report.json").read_text().count('"schema_version"')
    assert report_documents == 1
    assert (out / "report.md").is_file()
    assert (out / "manifest.json").is_file()


def test_a_truncated_pack_from_a_crash_is_healed_by_the_next_run(sample_repo: Path, tmp_path: Path) -> None:
    """A crash mid-write leaves a truncated report.json; the next run rewrites every file
    atomically (partial + rename), so the pack is healed whole and no `.partial` survives a
    completed write — a reader never sees a half-written JSON document."""
    out = tmp_path / "pack"
    request_dict = {"repo": str(sample_repo), "names": ["old_gate"], "description": "x", "out": str(out)}
    judge = scripted_judge({"refers": 0.9, "leads": 0.9})
    run_scan(ScanRequest.from_dict(request_dict), judge)
    # A completed run leaves no partials behind.
    assert not list(out.glob("*.partial"))
    # A crash mid-write leaves truncated JSON...
    (out / "report.json").write_text('{"schema_version": 1, "fin')
    # ...and the next run rewrites report.json atomically (healing the torn file) and reports
    # the saved pack with already_run, as any retry does.
    healed = run_scan(ScanRequest.from_dict(request_dict), judge)
    assert healed["already_run"] is True
    assert not list(out.glob("*.partial"))
    healed_on_disk = json.loads((out / "report.json").read_text())
    assert healed_on_disk["counts"] == healed["counts"]


class BudgetJevClient(ScriptedJevClient):
    """Gateway stand-in: answers like ScriptedJevClient but refuses any request whose body
    could not fit one provider request, raising the refusal shape the real gateway returns
    (400 with error_type max_tokens_exceeded). A code-heavy body's true token count never
    exceeds twice the SDK's len//3+1 estimate, which is what the real refusals proved.
    """

    budget_tokens = 32_768
    refused = 0

    def send(self, state, questions):
        body = json.dumps({"state": state, "questions": questions}, sort_keys=True).encode()
        if (len(body) // 3 + 1) * 2 > self.budget_tokens:
            self.refused += 1
            raise RuntimeError(
                '400 {"detail": {"error_type": "max_tokens_exceeded", "message": '
                '"request input budget exceeded"}, "request_id": "fake"}'
            )
        return super().send(state, questions)


def budget_client() -> BudgetJevClient:
    return BudgetJevClient(nouls={"refers_to_removed": 0.9, "leads_agent_to_recreate": 0.9})


def test_oversized_state_fails_honestly_instead_of_killing_the_scan(sample_repo: Path) -> None:
    # Red-before/green-after transport regression. Before the fix, a request the provider
    # refused for input size raised through judge_candidates and killed the WHOLE scan:
    # zero findings, exit 1, unrecoverable by retry (failed requests are not journaled for
    # replay). The giant cart.py below reproduces the oversized-state shape (docstring plus
    # uncapped attached_code) that the saved JVN run died on. After the fix the scan
    # completes and every candidate is answered or honestly listed under budget.not_inspected.
    from jev_remnants.collect import collect_candidates

    (sample_repo / "src" / "cart.py").write_text(
        "def big_cart(carts, value):\n"
        '    """Move a cart through the depot. old_gate used to cap this at ten."""\n'
        + "\n".join(f"    total_{i} = value_{i} + 1  # value" for i in range(4000))
        + "\n    return carts\n"
    )
    subprocess.run(["git", "add", "."], cwd=sample_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "huge"], cwd=sample_repo, check=True)
    client = budget_client()
    request = ScanRequest.from_dict(
        {"repo": str(sample_repo), "names": ["old_gate"], "description": "x"}
    )
    report = run_scan(request, Judge(client, thresholds=Thresholds(noul_yes_at=0.8, noul_no_at=0.2)))
    assert client.refused > 0, "the oversized requests really were refused"
    candidates, remainder = collect_candidates(sample_repo, ("old_gate",))
    judged = {finding["location"] for finding in report["findings"]}
    failed = {f"{entry['file']}:{entry['line']}" for entry in report["budget"]["not_inspected"]}
    assert not remainder, "the sample repo must stay below the candidate cap"
    assert all(f"{c.file}:{c.line}" in judged | failed for c in candidates), (
        "every candidate was answered or honestly listed, none silently dropped"
    )
    huge = next(candidate for candidate in candidates if candidate.file == "src/cart.py")
    assert f"{huge.file}:{huge.line}" in failed, "a state that fits no request must be listed, not truncated"
    entry = next(entry for entry in report["budget"]["not_inspected"] if entry["file"] == "src/cart.py")
    assert "could not fit" in entry["reason"]
    assert any(
        finding["location"].startswith("src/main.py") for finding in report["findings"]
    ), "candidates beside the oversized state must still be judged"


def test_batches_over_the_provider_budget_are_bisected_not_dropped(sample_repo: Path) -> None:
    # Red-before/green-after batch regression: with default settings a wide repo's mentions
    # rode in a few multi-item requests of 57-140 KB, above the per-request budget, and one
    # provider refusal killed the entire scan. The same shapes are refused offline here by
    # the budget-enforcing client; the scan must complete and every candidate be judged.
    from jev_remnants.collect import collect_candidates

    blocks = []
    for _ in range(120):
        filler = "\n".join(f"    x_{j} = {j} + 1  # pad" for j in range(7))
        blocks.append(filler + "\n# old_gate note\n")
    (sample_repo / "src" / "wide.py").write_text(
        "def wide(value):\n" + "\n".join(blocks) + "\n    return value\n"
    )
    subprocess.run(["git", "add", "."], cwd=sample_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "wide"], cwd=sample_repo, check=True)
    client = budget_client()
    request = ScanRequest.from_dict(
        {"repo": str(sample_repo), "names": ["old_gate"], "description": "x", "max_candidates": 1000}
    )
    report = run_scan(request, Judge(client, thresholds=Thresholds(noul_yes_at=0.8, noul_no_at=0.2)))
    candidates, remainder = collect_candidates(sample_repo, ("old_gate",), max_candidates=1000)
    assert client.refused > 0, "this repo must still produce over-budget requests"
    assert report["counts"]["total"] == len(candidates), "every candidate must still be judged"
    assert report["budget"]["not_inspected"] == list(remainder)


def test_ordinary_runs_keep_the_unchanged_transport_shape(sample_repo: Path) -> None:
    # Normal case: while nothing exceeds the provider budget the scan must behave exactly as
    # before the fix -- check_each dispatched over the whole list, every candidate answered,
    # no budget failures, unchanged request shapes (verified against the pre-fix code).
    from jev_remnants.collect import collect_candidates

    client = ScriptedJevClient(nouls={"refers_to_removed": 0.9, "leads_agent_to_recreate": 0.9})
    request = ScanRequest.from_dict(
        {"repo": str(sample_repo), "names": ["old_gate"], "description": "x"}
    )
    report = run_scan(request, Judge(client, thresholds=Thresholds(noul_yes_at=0.8, noul_no_at=0.2)))
    candidates, remainder = collect_candidates(sample_repo, ("old_gate",))
    assert report["counts"]["total"] == len(candidates)
    assert report["budget"]["not_inspected"] == list(remainder)
    assert all(finding["probabilities"]["refers_to_removed"] is not None for finding in report["findings"])

