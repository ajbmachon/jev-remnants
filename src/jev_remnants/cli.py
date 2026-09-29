"""The jvr command: find the residual mentions of a removed thing that bring it back.

Code collects and ranks every mention; Jev answers two closed questions per mention;
code applies the action policy. JSON request in, JSON report out.
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
        "spec_terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Phrases that mark spec or doc prose as a candidate even without a name, "
                'for example "orders above ten items".'
            ),
        },
        "prefixes": {"type": "array", "items": {"type": "string"}, "default": []},
        "max_candidates": {"type": "integer", "default": 200},
        "context_radius": {"type": "integer", "default": 8},
        "max_context_chars": {"type": "integer", "default": 4000},
        "out": {"type": "string", "description": "Directory for the evidence pack."},
        "keep_request_text": {"type": "boolean", "default": False},
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "Collect and rank only, no Jev calls; the candidate list is the report.",
        },
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

EXAMPLE_REQUEST = {
    "repo": "~/Projects/my-repo",
    "names": ["old_gate"],
    "description": "A removed order-limit check that failed carts above ten items.",
}

SCAN_EXAMPLES = (
    "Examples:\n"
    '  echo \'{"repo": "~/x", "names": ["oldGate"], "description": "Removed gate"}\' | jvr scan\n'
    "  jvr scan request.json\n"
    '  jvr scan \'{"repo": "~/x", "names": ["oldGate"], "description": "d", "dry_run": true}\'\n'
    '  jvr scan \'{"repo": "~/x", "names": ["oldGate"], "description": "d", "out": "~/pack"}\'\n'
    "  jvr schema   # every parameter with its default\n"
)


def _load_typesafe_environment(environment: dict[str, str]) -> None:
    """The family chain: process values win, then the checkout `.env`, then `~/.config/jvn/env`."""
    from .environment import load_typesafe_environment

    load_typesafe_environment(environment)


def _live_judge(request: ScanRequest, thresholds: Thresholds, journal) -> Judge:
    from jev_navigator.adapters.typesafe import TypeSafeJevClient

    _load_typesafe_environment(dict(os.environ))
    client = TypeSafeJevClient()  # model=None resolves TYPESAFE_DEFAULT_MODEL in the adapter
    return Judge(client, thresholds=thresholds, journal=journal)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jvr",
        description="Find the residual mentions of a removed thing that bring it back.",
        epilog=(
            "Examples:\n"
            '  echo \'{"repo": "~/x", "names": ["oldGate"], "description": "Removed gate"}\' | jvr scan\n'
            "  jvr scan --help   # every parameter and its defaults\n"
            "  jvr schema        # the request schema as JSON\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser(
        "scan",
        help="scan a repository for residual mentions of a removed thing",
        epilog=SCAN_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan_parser.add_argument(
        "request", nargs="?", help="request JSON, or a path to a .json file; stdin when omitted"
    )

    sub.add_parser(
        "schema",
        help="print the request schema as JSON",
        epilog="Examples:\n  jvr schema\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

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
        print(
            json.dumps(
                {
                    "error": f"bad request: {error}",
                    "example": EXAMPLE_REQUEST,
                    "hint": "jvr schema prints every parameter with its default",
                }
            ),
            file=sys.stderr,
        )
        return 2

    journal = None
    if request.out is not None:
        journal = JsonlJournal(request.out / "journal.jsonl", keep_request_text=request.keep_request_text)
    try:
        # A dry run never touches the provider, so it needs no API key.
        judge = None if request.dry_run else _live_judge(request, thresholds, journal)
        report = run_scan(request, judge, thresholds)
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
