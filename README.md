# jev-remnants

**jvr**: find the residual mentions of a removed function, feature or banned pattern that bring it
back. Removed things keep coming back because a stale test, an agent-facing document or a comment
still describes them, and the next agent obeys. jvr finds those mentions after a removal, so the
change becomes durable.

Built with [Jev / TypeSafe System One](https://docs.typesafe.ai): code collects every mention,
ranks it and slices the evidence; Jev answers two closed yes/no questions per mention; code applies
the action policy. Code owns goals, loops and policy; Jev makes only narrow, typed judgments.

## Use it as an agent

JSON in, JSON out. One tool, `scan`, with configurable parameters:

```sh
echo '{
  "repo": "~/Projects/my-repo",
  "names": ["old_gate", "legacyCheck"],
  "description": "A removed order-limit check that failed carts above ten items.",
  "prefixes": ["src", "docs"],
  "max_candidates": 200,
  "out": "~/.local/share/jev-remnants/my-repo-old-gate"
}' | jvr scan
```

The report is one JSON document on stdout (`jvr scan` prints it) and, with `out`, an evidence pack
(`report.json`, `report.md`, `manifest.json`, `journal.jsonl`). `jvr schema` prints the request
schema. Every parameter except `repo`, `names` and `description` is optional.

### The actions the report assigns

| Action | Meaning | The calling agent does |
|---|---|---|
| `resurrection` | An agent-facing doc or test still presents the removed thing as usable | Fix the doc or test, or the removal is wrong |
| `fix_reference` | Source or a comment still describes the removed thing as present | Update or delete the mention |
| `review` | Jev was unsure; the reasons say which question and why | Decide by hand |
| `historical` | Changelog or decision prose; records the removal, causes nothing | Nothing |
| `unrelated` | Same words, different thing | Nothing |

`not_inspected` in the report's budget block lists every mention the run never judged, with the
reason. A durable-removal claim is only proven once that list is empty or explicitly accepted.

## The judgments

Both questions run over the same per-mention state and cannot see one another's answers:

- `refers_to_removed`: does this mention refer to the removed thing, not an unrelated same-named thing?
- `leads_agent_to_recreate`: would an agent reading it be led to use, call, configure or re-create it?

Question ids carry a wording hash; a reworded question is a new question. Answers are journaled with
their exact request hashes; thresholds (default: yes at 0.80, no at 0.20) are per-request overridable
via the `thresholds` request field or `JEV_NAVIGATOR_NOUL_*` environment variables.

Live scans use JVN's existing batcher. If the provider refuses input that is too large, jvr
splits the candidate range and asks again, down to single candidates. A candidate that still
cannot fit appears in `budget.not_inspected` with the provider's reason; its evidence is never
truncated to make it fit. Recovery can repeat successful requests from the failed range, so an
oversized run can use more calls than an ordinary run.

Only documented size refusals qualify. JVN decodes Jev's HTTP 400
`detail.error_type=max_tokens_exceeded`; jvr consumes its `InputBudgetExceededError`.
jvr also recognizes Drex's HTTP 422 `invalid_request_error` messages for state, row or
serialized-body limits.
Other validation errors, missing answers, authentication and transport failures fail the scan
and make `jvr scan` exit 1. A malformed response never becomes a successful partial report.

## Install and test

```sh
uv sync            # installs the locked jev-navigator revision and creates the venv
uv run pytest      # offline; a scripted Jev client answers
uv tool install -e .  # the jvr command; live scans need TYPESAFE_API_KEY in ~/.config/jvn/env
```

Live calls go through the same configuration as `jvn` (explicit `TYPESAFE_API_KEY` /
`TYPESAFE_BASE_URL` process values win, else `~/.config/jvn/env`), so a local jeview gateway at
`127.0.0.1:4777` works unchanged.

## Scope discipline

The grep over tracked files is cheap and whole-root by design; the Jev sweep only ever sees the
narrowed, ranked candidates (default cap 200, remainder reported as `not_inspected`). Never widen
the sweep to a per-file model scan: narrow candidates in code first.
