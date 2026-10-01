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
from jev_navigator.judgments.questions import Check
from jev_navigator.judgments.thresholds import NoulVerdict, Thresholds

from .collect import Candidate, collect_candidates, head_commit
from .questions import LEADS_RECREATION, REFERS_REMOVED

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScanRequest:
    repo: Path
    names: tuple[str, ...]
    description: str
    spec_terms: tuple[str, ...]
    prefixes: tuple[str, ...]
    max_candidates: int
    context_radius: int
    max_context_chars: int
    out: Path | None
    keep_request_text: bool
    dry_run: bool

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
            spec_terms=tuple(data.get("spec_terms") or ()),
            prefixes=tuple(data.get("prefixes") or ()),
            max_candidates=int(data.get("max_candidates", 200)),
            context_radius=int(data.get("context_radius", 8)),
            max_context_chars=int(data.get("max_context_chars", 4000)),
            out=Path(data["out"]).expanduser() if data.get("out") else None,
            keep_request_text=bool(data.get("keep_request_text", False)),
            dry_run=bool(data.get("dry_run", False)),
        )


def removed_state(request: ScanRequest) -> dict:
    return {"names": list(request.names), "description": request.description}


_SIZE_REFUSALS = ("max_tokens_exceeded", "input budget exceeded")


def _refused_for_size(error: BaseException) -> bool:
    """True only when a request was refused because its input could not fit (400
    ``max_tokens_exceeded`` on the jev routes; the budget-exceeded 422 on the decider
    routes). Auth, connection, timeout and protocol errors are not size failures."""
    text = str(error)
    return any(marker in text for marker in _SIZE_REFUSALS) or getattr(error, "status", None) == 422


def _ask_check(
    judge: Judge, check: Check, items: list, list_name: str, thresholds: Thresholds
) -> tuple[list[CheckResult | None], dict[int, str]]:
    """Judge every item with one check, in as few provider requests as the provider can fit.

    Batching stays with JVN: each dispatch is one ``check_each`` call whose ``_batches`` still
    packs as many items per request as the state budget allows; jvr never packs items itself.
    Only when the provider refuses a dispatch on input size -- its per-request budget is smaller
    than JVN's state budget, and one oversized state (an uncapped ``attached_code``, say) can
    exceed both -- is the refused range bisected and re-asked, down to singletons. A singleton
    that still refuses comes back as ``None`` with its reason: honestly failed, never silently
    truncated. Any other error propagates -- an auth or connection failure must kill the run
    outright, not be recovered around.
    """
    if not items:
        return [], {}
    try:
        results = list(judge.check_each(check, items, list_name=list_name, thresholds=thresholds))
        if len(results) == len(items):
            return results, {}
        failure = f"answered {len(results)} of {len(items)} items"
    except Exception as error:
        if not _refused_for_size(error):
            raise
        failure = f"refused on input size: {type(error).__name__}: {str(error)[:120]}"
    if len(items) == 1:
        return [None], {0: f"could not fit one request ({failure})"}
    middle = len(items) // 2
    left, left_gaps = _ask_check(judge, check, items[:middle], list_name, thresholds)
    right, right_gaps = _ask_check(judge, check, items[middle:], list_name, thresholds)
    return left + right, {**left_gaps, **{p + middle: r for p, r in right_gaps.items()}}


def judge_candidates(
    judge: Judge, candidates: list[Candidate], removed: dict, thresholds: Thresholds
) -> dict[str, list[CheckResult | None] | dict[int, str]]:
    """Two independent judgments per candidate, each dispatched in as few requests as the
    provider can fit (see ``_ask_check``). A candidate that no request could carry has ``None``
    in both result lists and its reason in ``unjudged``, so the report lists it under
    ``budget.not_inspected`` instead of inventing a verdict for it."""
    items = [candidate.state(removed) for candidate in candidates]
    refers, refer_gaps = _ask_check(judge, REFERS_REMOVED, items, "items", thresholds)
    leads, lead_gaps = _ask_check(judge, LEADS_RECREATION, items, "items", thresholds)
    unjudged = {
        index: refer_gaps.get(index) or lead_gaps.get(index) or "could not fit one request"
        for index in set(refer_gaps) | set(lead_gaps)
    }
    return {"refers": refers, "leads": leads, "unjudged": unjudged}


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
    request: ScanRequest, judge: Judge | None, thresholds: Thresholds | None = None
) -> dict:
    thresholds = thresholds or (judge.thresholds if judge else Thresholds())
    repo = request.repo
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repository")
    commit = head_commit(repo)
    removed = removed_state(request)
    candidates, remainder = collect_candidates(
        repo,
        request.names,
        spec_terms=request.spec_terms,
        prefixes=request.prefixes,
        context_radius=request.context_radius,
        max_context_chars=request.max_context_chars,
        max_candidates=request.max_candidates,
    )
    if request.dry_run:
        # No Jev spend: the ranked candidates and the remainder are the whole report.
        report = {
            "schema_version": SCHEMA_VERSION,
            "dry_run": True,
            "repo": str(repo),
            "commit": commit,
            "removed": removed,
            "budget": {"max_candidates": request.max_candidates, "not_inspected": remainder},
            "counts": {"total": len(candidates)},
            "candidates": [
                {
                    "location": f"{candidate.file}:{candidate.line}",
                    "kind": candidate.kind,
                    "comment_kind": candidate.comment_kind,
                    "mention": candidate.text,
                    "has_attached_code": bool(candidate.attached_code),
                }
                for candidate in candidates
            ],
        }
        return persist_or_replay(request, report)

    judged = (
        judge_candidates(judge, candidates, removed, thresholds)
        if candidates
        else {"refers": [], "leads": [], "unjudged": {}}
    )

    findings = []
    not_inspected = list(remainder)
    counts: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        refers, leads = judged["refers"][index], judged["leads"][index]
        if refers is None or leads is None:
            # A state that fitted no request is honestly failed, with its reason, not truncated.
            not_inspected.append(
                {
                    "file": candidate.file,
                    "line": candidate.line,
                    "kind": candidate.kind,
                    "reason": judged["unjudged"].get(index, "could not be judged"),
                }
            )
            continue
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
            "not_inspected": not_inspected,
            "calls": judge.calls if judge else None,
            "input_tokens": judge.input_tokens if judge else None,
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
    return persist_or_replay(request, report)


def persist_or_replay(request: ScanRequest, report: dict) -> dict:
    """Write the pack once, atomically; a retry against an existing pack reports the saved run.

    The report is written to a partial file and renamed into place, so a crash mid-write never
    leaves a truncated ``report.json`` for the next run to choke on: a partial file means the
    run never finished, and the caller sees the crash instead of silently reporting stale data.
    """
    if request.out is None:
        return report
    pack_written = write_report(request.out, report, request.keep_request_text)
    if pack_written:
        return report
    saved = json.loads(request.out.expanduser().joinpath("report.json").read_text())
    saved["already_run"] = True
    return saved


def write_report(out: Path, report: dict, keep_request_text: bool = False) -> bool:
    """Write the pack once, atomically. Returns False when the pack already existed: a retry then
    leaves it untouched, and the caller reports the saved run instead of duplicating.

    Every file lands through ``_atomic_write_json``: bytes go to ``<name>.partial`` first and a
    rename completes the file, so a reader never sees a half-written JSON document.
    """
    out = out.expanduser()
    pack_existed = out.exists()
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(out / "report.json", report)
    if pack_existed:
        return False
    _atomic_write_text(out / "report.md", markdown_report(report))
    if not report.get("dry_run"):
        _atomic_write_json(
            out / "manifest.json",
            {
                "tool": "jev-remnants",
                "schema_version": SCHEMA_VERSION,
                "commit": report["commit"],
                "question_ids": report.get("question_ids", {}),
                "counts": report.get("counts", {}),
                "model_calls": report.get("budget", {}).get("calls"),
            },
        )
    return True


def _atomic_write_json(path: Path, value: dict) -> None:
    _atomic_write_text(path, json.dumps(value, indent=2) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text)
    partial.replace(path)


def markdown_report(report: dict) -> str:
    calls = report.get("budget", {}).get("calls")
    calls_text = "dry run, no Jev calls" if calls is None else f"{calls} Jev calls"
    lines = [
        f"# Residual mentions: {', '.join(report['removed']['names'])}",
        "",
        f"Repository `{report['repo']}` at `{report['commit'][:12]}`; "
        f"{report['counts']['total']} mentions, {calls_text}.",
        "",
        "| Action | Count |",
        "|---|---|",
    ]
    if report.get("dry_run"):
        lines += [
            "",
            "## Candidates (dry run, not judged)",
            "",
        ]
        lines += [
            f"- `{candidate['location']}` ({candidate['kind']}"
            + (f", {candidate['comment_kind']}" if candidate["comment_kind"] else "")
            + f"): {candidate['mention'].splitlines()[0] if candidate['mention'] else ''}"
            for candidate in report.get("candidates", [])
        ]
        if report.get("budget", {}).get("not_inspected"):
            lines += ["", "## Not inspected", ""]
            lines += [
                f"- `{entry['file']}:{entry['line']}` ({entry['reason']})"
                for entry in report["budget"]["not_inspected"]
            ]
        return "\n".join(lines) + "\n"
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
