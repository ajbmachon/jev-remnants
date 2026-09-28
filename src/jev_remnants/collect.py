"""Candidate collection: code finds every mention, ranks it and slices evidence.

No model here. The whole-root text search is cheap grep; the Jev sweep later only
sees the narrowed, ranked candidates.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

COMMENT_MARKERS = ("#", "//", "/*", "*", "<!--", ";;")

# Rank order: the costly error is a missed resurrection vector, so agent-facing
# documents and tests come first. Changelog prose is the least likely to bring
# something back.
KIND_RANK = {
    "agent_docs": 0,
    "test": 1,
    "source": 2,
    "comment": 2,
    "config": 3,
    "changelog": 4,
}

AGENT_DOC_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", "README.md"}
CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env"}


@dataclass(frozen=True)
class Candidate:
    file: str
    line: int
    text: str
    kind: str
    context: str

    def state(self, removed: dict) -> dict:
        """The neutral per-item state both questions read. No verdicts, no labels."""
        return {
            "removed": removed,
            "mention": {
                "location": f"{self.file}:{self.line}",
                "text": self.context,
            },
        }


def git_files(repo: Path, prefixes: tuple[str, ...]) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", *prefixes], cwd=repo, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def head_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def grep_mentions(repo: Path, names: tuple[str, ...]) -> list[tuple[str, int, str]]:
    """Word-boundary ripgrep over tracked files; one row per matching line."""
    pattern = "|".join(names)
    result = subprocess.run(
        ["rg", "-n", "-w", "--no-heading", "-e", pattern, "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):  # 1 means no matches
        raise RuntimeError(f"rg failed: {result.stderr.strip()}")
    rows = []
    for line in result.stdout.splitlines():
        file, number, text = line.split(":", 2)
        rows.append((file, int(number), text))
    return rows


def classify(file: str, line_text: str) -> str:
    name = Path(file).name
    path = Path(file)
    parts = path.parts
    if name.startswith(("CHANGELOG", "HISTORY")) or "migrations" in parts:
        return "changelog"
    if name in AGENT_DOC_NAMES or parts[0] in (".claude", ".cursor", "docs"):
        return "agent_docs"
    is_test = (
        "test" in parts
        or name.startswith(("test_", "conftest"))
        or name.endswith(("_test.ts", ".spec.ts"))
    )
    if is_test:
        return "test"
    if path.suffix in CONFIG_SUFFIXES:
        return "config"
    stripped = line_text.lstrip()
    if any(stripped.startswith(marker) for marker in COMMENT_MARKERS):
        return "comment"
    return "source"


def context_window(repo: Path, file: str, line: int, radius: int, max_chars: int) -> str:
    lines = (repo / file).read_text(errors="replace").splitlines()
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    window = [f"{number}: {lines[number - 1]}" for number in range(start, end + 1)]
    return "\n".join(window)[:max_chars]


def collect_candidates(
    repo: Path,
    names: tuple[str, ...],
    *,
    prefixes: tuple[str, ...] = (),
    context_radius: int = 8,
    max_context_chars: int = 4000,
    max_candidates: int = 200,
) -> tuple[list[Candidate], list[dict]]:
    """Ranked, deduplicated candidates plus the not-inspected remainder with reasons."""
    files = git_files(repo, prefixes)
    if not files:
        raise RuntimeError("the scope matched no tracked files; widen --prefix")
    rows = grep_mentions(repo, names)
    seen: set[tuple[str, int]] = set()
    candidates: list[Candidate] = []
    for file, number, text in rows:
        key = (file, number)
        if key in seen:
            continue
        seen.add(key)
        kind = classify(file, text)
        context = context_window(repo, file, number, context_radius, max_context_chars)
        candidates.append(Candidate(file, number, text, kind, context))
    candidates.sort(key=lambda item: (KIND_RANK[item.kind], item.file, item.line))
    if len(candidates) <= max_candidates:
        return candidates, []
    inspected = candidates[:max_candidates]
    remainder = [
        {"file": item.file, "line": item.line, "kind": item.kind, "reason": "beyond max_candidates"}
        for item in candidates[max_candidates:]
    ]
    return inspected, remainder
