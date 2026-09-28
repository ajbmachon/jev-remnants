"""The jvr command: one tool, JSON request in, JSON report out. Agent-first.

    echo '{"repo": "~/x", "names": ["oldGate"], "description": "..."}' | jvr scan

Every parameter has a default except repo, names and description. The report goes
to stdout as one JSON document; with "out" it is also written as a pack
(report.json, report.md, manifest.json). `jvr schema` prints the request schema.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from jev_navigator.judgments.journal import JsonlJournal
from jev_navigator.judgments.judge import Judge
from jev_navigator.judgments.thresholds import Thresholds

from .scan import ScanRequest, run_scan

REQUEST_SCHEMA = {
    "type": "object",
    "required": ["names", "description"],
    "properties": {
        "repo": {"type": "string", "default": "."},
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Identifiers, config keys or phrases of the removed thing.",
        },
        "description": {
            "type": "string",
            "description": "What the removed thing was and what it did, in one or two sentences.",
        },
        "prefixes": {"type": "array", "items": {"type": "string"}, "default": []},
        "max_candidates": {"type": "integer", "default": 200},
        "context_radius": {"type": "integer", "default": 8},
        "max_context_chars": {"type": "integer", "default": 4000},
        "out": {"type": "string", "description": "Directory for the evidence pack; must be new."},
        "keep_request_text": {"type": "boolean", "default": False},
        "thresholds": {
            "type": "object",
            "properties": {
                "noul_yes_at": {"type": "number"},
                "noul_no_at": {"type": "number"},
            },
            "default": {},
        },
    },
}


def _load_typesafe_environment(environment: dict[str, str]) -> None:
    """Same contract as jvn: explicit process values win, else ~/.config/jvn/env."""
    path = Path("~/.config/jvn/env").expanduser()
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if not environment.get(name, "").strip():
                environment[name] = value
    if not environment.get("TYPESAFE_API_KEY", "").strip():
        raise RuntimeError("TYPESAFE_API_KEY is unset and ~/.config/jvn/env does not provide it")


def _live_judge(request: ScanRequest, thresholds: Thresholds, journal) -> Judge:
    from jev_navigator.adapters.typesafe import TypeSafeJevClient

    _load_typesafe_environment(dict(os.environ))
    client = TypeSafeJevClient()
    return Judge(client, thresholds=thresholds, journal=journal)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jvr", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="print the request schema as JSON")
    scan_parser = sub.add_parser("scan", help="scan a repository for residual mentions")
    scan_parser.add_argument("request", nargs="?", help="request JSON; stdin when omitted")
    args = parser.parse_args(argv)

    if args.command == "schema":
        print(json.dumps(REQUEST_SCHEMA, indent=2))
        return 0

    raw = args.request or sys.stdin.read()
    request_path = Path(args.request).expanduser() if args.request else None
    if request_path and request_path.is_file():
        raw = request_path.read_text()
    try:
        data = json.loads(raw)
        request = ScanRequest.from_dict(data)
        overrides = data.get("thresholds") or {}
        thresholds = Thresholds.from_env().updated(
            {key: float(value) for key, value in overrides.items()}
        )
    except (ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": f"bad request: {error}"}), file=sys.stderr)
        return 2

    journal = None
    if request.out is not None:
        journal = JsonlJournal(request.out / "journal.jsonl", keep_request_text=request.keep_request_text)
    try:
        judge = _live_judge(request, thresholds, journal)
        report = run_scan(request, judge, thresholds)
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
