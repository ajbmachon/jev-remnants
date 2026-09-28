"""Candidate collection: code finds every mention, ranks it and slices evidence.

No model here. The whole-root text search is cheap grep; the Jev sweep later only
sees the narrowed, ranked candidates. Comment and documentation blocks come from
jev-navigator's parser-backed finder, so a docstring or JSDoc mention arrives as one
candidate with the code it documents, and spec prose without a name can be caught
through the request's spec_terms.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from jev_navigator.comments import CommentBlock, CommentKind, find_comments
from jev_navigator.index.code_index import CodeIndex

# Rank order: the costly error is a missed resurrection vector, so agent-facing
# documents and tests come first. Changelog prose is the least likely to bring
# something back.
KIND_RANK = {
    "agent_docs": 0,
    "test": 1,
    "comment": 2,
    "source": 2,
    "config": 3,
    "changelog": 4,
}

AGENT_DOC_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", "README.md"}
CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env"}
# Comments inside these languages are collected through the parser-backed finder.
COMMENT_LANGUAGE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


@dataclass(frozen=True)
class Candidate:
    file: str
    line: int
    text: str
    kind: str
    context: str
    comment_kind: str = ""
    attached_code: str = ""

    def state(self, removed: dict) -> dict:
        """The neutral per-item state both questions read. No verdicts, no labels."""
        mention: dict = {
            "location": f"{self.file}:{self.line}",
            "text": self.context,
        }
        if self.attached_code:
            mention["attached_code"] = self.attached_code
        return {"removed": removed, "mention": mention}


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


def grep_matches(repo: Path, names: tuple[str, ...]) -> list[tuple[str, int, str]]:
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
    return "source"


def context_window(repo: Path, file: str, line: int, radius: int, max_chars: int) -> str:
    lines = (repo / file).read_text(errors="replace").splitlines()
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    window = [f"{number}: {lines[number - 1]}" for number in range(start, end + 1)]
    return "\n".join(window)[:max_chars]


def attached_code_above(repo: Path, file: str, start: int, max_chars: int) -> str:
    """The code a comment block sits on: the declaration or statement below it."""
    lines = (repo / file).read_text(errors="replace").splitlines()
    end = start
    while end < len(lines) and not lines[end].strip():
        end += 1
    body: list[str] = []
    while end < len(lines):
        stripped = lines[end].strip()
        if not stripped:
            break
        body.append(lines[end])
        if stripped.endswith((":", ";", "{")):
            break
        end += 1
    return "\n".join(body)[:max_chars]


def comment_candidates(
    index: CodeIndex, repo: Path, names: tuple[str, ...], spec_terms: tuple[str, ...]
) -> dict[tuple[str, int], tuple[CommentBlock, str]]:
    """Parser-backed comment and docstring blocks that name the removed thing or a spec term."""
    hits: dict[tuple[str, int], tuple[CommentBlock, str]] = {}
    needles = tuple(name for name in (*names, *spec_terms) if name)
    for block in find_comments(index).kept:
        if not any(needle in block.text for needle in needles):
            continue
        attached = ""
        if block.attached is not None:
            attached = index.read_slice(block.attached).text
        elif block.kind in (CommentKind.INLINE, CommentKind.BLOCK):
            attached = attached_code_above(repo, block.span.file, block.span.end, 2000)
        hits[(block.span.file, block.span.start)] = (block, attached)
    return hits


def collect_candidates(
    repo: Path,
    names: tuple[str, ...],
    *,
    spec_terms: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
    context_radius: int = 8,
    max_context_chars: int = 4000,
    max_candidates: int = 200,
) -> tuple[list[Candidate], list[dict]]:
    """Ranked, deduplicated candidates plus the not-inspected remainder with reasons."""
    files = git_files(repo, prefixes)
    if not files:
        raise RuntimeError(
            "the scope matched no tracked files; add prefixes to the request, "
            'for example "prefixes": ["src", "docs"]'
        )
    seen: set[tuple[str, int]] = set()
    candidates: list[Candidate] = []

    # Parser-backed comment and documentation blocks first: their kind and attached
    # code are exact, and a match inside a docstring is the classic stale-documentation case.
    code_files = [
        file for file in files if Path(file).suffix in COMMENT_LANGUAGE_SUFFIXES
    ]
    index = CodeIndex(repo, code_files)
    blocks = comment_candidates(index, repo, names, spec_terms)
    for (file, start), (block, attached) in blocks.items():
        seen.add((file, start))
        # A match inside a docstring or comment is a documentation mention, even in
        # a source file; tests, docs and changelog keep their file-based kind.
        kind = classify(file, block.text)
        if kind == "source":
            kind = "comment"
        candidates.append(
            Candidate(
                file=file,
                line=start,
                text=block.text,
                kind=kind,
                context=context_window(repo, file, start, context_radius, max_context_chars),
                comment_kind=block.kind.value,
                attached_code=attached,
            )
        )

    for file, number, text in grep_matches(repo, names):
        if (file, number) in seen or (file, number) in blocks:
            continue
        seen.add((file, number))
        candidates.append(
            Candidate(
                file=file,
                line=number,
                text=text,
                kind=classify(file, text),
                context=context_window(repo, file, number, context_radius, max_context_chars),
            )
        )

    # Spec prose without a name: a spec term appearing in a document, test or config
    # file is a candidate even when no identifier from names appears.
    if spec_terms:
        doc_suffixes = (".md", ".txt", ".rst", ".adoc")
        prose_files = [
            f for f in files if f.endswith(doc_suffixes) or Path(f).suffix in CONFIG_SUFFIXES
        ]
        for file in prose_files:
            lines = (repo / file).read_text(errors="replace").splitlines()
            for number, line in enumerate(lines, start=1):
                if (file, number) in seen:
                    continue
                if any(term in line for term in spec_terms):
                    seen.add((file, number))
                    candidates.append(
                        Candidate(
                            file=file,
                            line=number,
                            text=line,
                            kind=classify(file, line),
                            context=context_window(repo, file, number, context_radius, max_context_chars),
                        )
                    )

    candidates.sort(key=lambda item: (KIND_RANK[item.kind], item.file, item.line))
    if len(candidates) <= max_candidates:
        return candidates, []
    inspected = candidates[:max_candidates]
    remainder = [
        {"file": item.file, "line": item.line, "kind": item.kind, "reason": "beyond max_candidates"}
        for item in candidates[max_candidates:]
    ]
    return inspected, remainder


def _agent_or_changelog(file: str) -> set[str]:
    parts = Path(file).parts
    name = Path(file).name
    if name.startswith(("CHANGELOG", "HISTORY")) or "migrations" in parts:
        return {file}
    if name in AGENT_DOC_NAMES or parts[0] in (".claude", ".cursor", "docs"):
        return {file}
    return set()
