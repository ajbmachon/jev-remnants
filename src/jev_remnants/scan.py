"""The scan workflow: collect candidates, judge each, apply the action policy in code.

Consumer policy (prerequisites, never a weighted sum):
  source/comment + refers            -> fix_reference   (the code no longer compiles with reality)
  agent_docs/test + refers + leads   -> resurrection    (an agent or test will bring the thing back)
  refers, otherwise                  -> historical      (changelog/decision prose; no action)
  no + no                            -> unrelated       (same words, different thing)
  anything unsure                    -> review, with the reasons
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jev_navigator.judgments.judge import CheckResult, Judge
from jev_navigator.judgments.thresholds import NoulVerdict, Thresholds

from .collect import Candidate, collect_candidates, head_commit
from .questions import LEADS_RECREATION, REFERS_REMOVED

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScanRequest:
    repo: Path
    names: tuple[str, ...]
    description: str
    prefixes: tuple[str, ...]
    max_candidates: int
    context_radius: int
    max_context_chars: int
    out: Path | None
    keep_request_text: bool

    @classmethod
    def from_dict(cls, data: dict) -> ScanRequest:
        repo = Path(data.get("repo", ".")).expanduser().resolve()
        names = tuple(data.get("names") or [])
        if not names:
            raise ValueError("names is required: the identifiers, keys or phrases of the removed thing")
        description = (data.get("description") or "").strip()
        if not description:
            raise ValueError(
                "description is required: what the removed thing was, so Jev can tell "
                "same-name-different-thing apart"
            )
        return cls(
            repo=repo,
            names=names,
            description=description,
            prefixes=tuple(data.get("prefixes") or ()),
            max_candidates=int(data.get("max_candidates", 200)),
            context_radius=int(data.get("context_radius", 8)),
            max_context_chars=int(data.get("max_context_chars", 4000)),
            out=Path(data["out"]).expanduser() if data.get("out") else None,
            keep_request_text=bool(data.get("keep_request_text", False)),
        )


def removed_state(request: ScanRequest) -> dict:
    return {"names": list(request.names), "description": request.description}


def judge_candidates(
    judge: Judge, candidates: list[Candidate], removed: dict, thresholds: Thresholds
) -> dict[str, list[CheckResult]]:
    """Two independent judgments per candidate, batched over the same state."""
    items = [candidate.state(removed) for candidate in candidates]
    refers = judge.check_each(REFERS_REMOVED, items, thresholds=thresholds)
    leads = judge.check_each(LEADS_RECREATION, items, thresholds=thresholds)
    return {"refers": refers, "leads": leads}


def decide(refers: NoulVerdict, leads: NoulVerdict, kind: str) -> tuple[str, list[str]]:
    """The action policy. Returns (action, reasons). Never a weighted sum."""
    if refers is NoulVerdict.UNSURE:
        # refers is a prerequisite for every action: an unsure reference is never historical.
        return "review", ["refers_to_removed unsure"]
    if refers is NoulVerdict.NO:
        return "unrelated", []
    if leads is NoulVerdict.UNSURE and kind in ("agent_docs", "test"):
        return "review", ["leads_agent_to_recreate unsure"]
    if kind in ("agent_docs", "test") and leads is NoulVerdict.YES:
        return "resurrection", []
    if kind in ("source", "comment"):
        return "fix_reference", []
    return "historical", []


def run_scan(
    request: ScanRequest, judge: Judge, thresholds: Thresholds | None = None
) -> dict:
    thresholds = thresholds or judge.thresholds
    repo = request.repo
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repository")
    commit = head_commit(repo)
    removed = removed_state(request)
    candidates, remainder = collect_candidates(
        repo,
        request.names,
        prefixes=request.prefixes,
        context_radius=request.context_radius,
        max_context_chars=request.max_context_chars,
        max_candidates=request.max_candidates,
    )
    judged = judge_candidates(judge, candidates, removed, thresholds) if candidates else {
        "refers": [], "leads": []
    }

    findings = []
    counts: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        refers, leads = judged["refers"][index], judged["leads"][index]
        action, reasons = decide(refers.verdict, leads.verdict, candidate.kind)
        counts[action] = counts.get(action, 0) + 1
        findings.append(
            {
                "location": f"{candidate.file}:{candidate.line}",
                "kind": candidate.kind,
                "mention": candidate.text,
                "action": action,
                "reasons": reasons,
                "probabilities": {
                    "refers_to_removed": refers.probability,
                    "leads_agent_to_recreate": leads.probability,
                },
                "from_store": [refers.from_store, leads.from_store],
                "request_sha256": refers.request_sha256,
            }
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repo": str(repo),
        "commit": commit,
        "removed": removed,
        "scope_prefixes": list(request.prefixes) or ["(all tracked files)"],
        "budget": {
            "max_candidates": request.max_candidates,
            "not_inspected": remainder,
            "calls": judge.calls,
            "input_tokens": judge.input_tokens,
        },
        "question_ids": {
            "refers_to_removed": REFERS_REMOVED.question_id,
            "leads_agent_to_recreate": LEADS_RECREATION.question_id,
        },
        "thresholds": {
            "noul_yes_at": thresholds.noul_yes_at,
            "noul_no_at": thresholds.noul_no_at,
        },
        "counts": counts | {"total": len(findings)},
        "findings": findings,
    }
    if request.out is not None:
        write_report(request.out, report, request.keep_request_text)
    return report


def write_report(out: Path, report: dict, keep_request_text: bool) -> None:
    out = out.expanduser()
    out.mkdir(parents=True, exist_ok=False)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "report.md").write_text(markdown_report(report))
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "tool": "jev-remnants",
                "schema_version": SCHEMA_VERSION,
                "commit": report["commit"],
                "question_ids": report["question_ids"],
                "counts": report["counts"],
                "model_calls": report["budget"]["calls"],
            },
            indent=2,
        )
        + "\n"
    )


def markdown_report(report: dict) -> str:
    lines = [
        f"# Residual mentions: {', '.join(report['removed']['names'])}",
        "",
        f"Repository `{report['repo']}` at `{report['commit'][:12]}`; "
        f"{report['counts']['total']} mentions, {report['budget']['calls']} Jev calls.",
        "",
        "| Action | Count |",
        "|---|---|",
    ]
    for action in ("resurrection", "fix_reference", "review", "historical", "unrelated"):
        if report["counts"].get(action):
            lines.append(f"| {action} | {report['counts'][action]} |")
    lines += ["", "## Act on", ""]
    acted = [f for f in report["findings"] if f["action"] in ("resurrection", "fix_reference", "review")]
    if not acted:
        lines.append("_None found._")
    for finding in acted:
        reasons = f" ({'; '.join(finding['reasons'])})" if finding["reasons"] else ""
        lines.append(f"- `{finding['location']}` **{finding['action']}**{reasons}: {finding['mention']}")
    if report["budget"]["not_inspected"]:
        lines += ["", "## Not inspected", ""]
        for entry in report["budget"]["not_inspected"]:
            lines.append(f"- `{entry['file']}:{entry['line']}` ({entry['reason']})")
    return "\n".join(lines) + "\n"
