"""Triage: sort a batch of ready cases before dispatch (liaise #19).

A cheap headless pass (its own call, a small fixed prompt and a hard turn cap) reads every
case that became ready in the same run, plus the partner's open cases for context, and
returns one verdict per ready case: ``kind``, ``duplicate_of``, ``group``, ``size``,
``priority``. From the verdicts this module builds the *groups* the tick dispatches (one job
per group, lead first, groups ordered by priority then age) and the labels and the one
plain-language comment that make the verdict visible on the issues.

The pass is a seam: an :data:`Ask` (``prompt -> structured answer``), so tests use a fake and
:func:`headless_ask` is the real one. It stays optional and off by default
(``triage = true`` in the subject). A pass that fails never blocks dispatch: the tick falls
back to one case at a time, in its own order, and says so.

The prompt (``data/triage_prompt.md``) and :data:`TRIAGE_SCHEMA` are generic: nothing in
them names a partner, a repository or a product.

Example:
    >>> verdict = parse_verdicts({"verdicts": [{"id": "a-1", "kind": "problem",
    ...     "duplicate_of": None, "group": "login", "size": "small",
    ...     "priority": "blocking"}]}, ["a-1"])["a-1"]
    >>> (verdict.kind, verdict.priority)
    ('problem', 'blocking')
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Optional

from liaise.model import Case

KINDS = ("problem", "request", "question", "comment")
SIZES = ("small", "medium", "large")
PRIORITIES = ("blocking", "important", "nice")

#: Verdict applied to a case the pass said nothing usable about.
DFLT_KIND = "request"
DFLT_SIZE = "medium"
DFLT_PRIORITY = "important"

#: The turn cap of the triage call: it only classifies, so it needs very few.
DFLT_TRIAGE_MAX_TURNS = 3
#: Seconds the triage call may take before it counts as failed.
DFLT_TRIAGE_TIMEOUT = 300
#: Characters of a case's first message shown to the pass.
DFLT_EXCERPT_CHARS = 600

PROMPT_RESOURCE = "triage_prompt.md"

#: The label every duplicate carries, besides its ``kind:`` / ``size:`` / ``priority:`` ones.
DUPLICATE_LABEL = "duplicate"
#: The one comment on a duplicate; ``{ref}`` is the issue it duplicates.
DUPLICATE_COMMENT = (
    "This looks like the same thing as {ref}. I'll handle them together, "
    "so there is nothing more you need to do."
)
#: Ledger purpose of that comment.
DUPLICATE_PURPOSE = "triage_duplicate"
#: Ledger entry kind detail marking a case grouped under a lead: ``{"lead": case_id}``.
GROUPED_EVENT = "grouped"

_SLUG = re.compile(r"[^a-z0-9-]+")

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "duplicate_of": {"type": ["string", "null"]},
                    "group": {"type": "string"},
                    "size": {"type": "string", "enum": list(SIZES)},
                    "priority": {"type": "string", "enum": list(PRIORITIES)},
                },
                "required": ["id", "kind", "group", "size", "priority"],
            },
        }
    },
    "required": ["verdicts"],
}

#: ``prompt -> structured answer`` (a mapping, or JSON text): the triage call seam.
Ask = Callable[[str], Any]


@dataclass(frozen=True)
class TriageVerdict:
    """What the pass decided about one case. ``duplicate_of`` and ``group`` name case ids/slugs."""

    case_id: str
    kind: str = DFLT_KIND
    duplicate_of: Optional[str] = None
    group: str = ""
    size: str = DFLT_SIZE
    priority: str = DFLT_PRIORITY

    def labels(self, prefix: str = "") -> tuple[str, ...]:
        """The labels that show this verdict, each ``<prefix>kind:...`` style, plus ``duplicate``."""
        names = [
            f"{prefix}kind:{self.kind}",
            f"{prefix}size:{self.size}",
            f"{prefix}priority:{self.priority}",
        ]
        if self.duplicate_of:
            names.append(f"{prefix}{DUPLICATE_LABEL}")
        return tuple(names)


def label_specs(prefix: str = "") -> list[tuple[str, str]]:
    """Every label a verdict can carry, as ``(name, description)``, so they can be created."""
    specs = [(f"{prefix}kind:{k}", f"Triage: this is a {k}.") for k in KINDS]
    specs += [(f"{prefix}size:{s}", f"Triage: {s} piece of work.") for s in SIZES]
    specs += [(f"{prefix}priority:{p}", f"Triage: {p}.") for p in PRIORITIES]
    specs.append(
        (f"{prefix}{DUPLICATE_LABEL}", "Triage: the same thing as another issue.")
    )
    return specs


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in allowed else default


def _slug(value: Any) -> str:
    return _SLUG.sub("-", str(value or "").strip().lower()).strip("-")


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, Mapping) else {}


def parse_verdicts(raw: Any, case_ids: Iterable[str]) -> dict[str, TriageVerdict]:
    """A verdict for each of ``case_ids`` from the pass's answer; tolerant of bad entries.

    An id the pass did not name, or named with unusable values, gets the defaults (a
    ``request``, ``medium``, ``important``, alone in its own group). A ``duplicate_of`` that
    is not another id of the batch or the open context, or names the case itself, is dropped,
    and so is one that would make a cycle.
    """
    ids = list(dict.fromkeys(case_ids))
    items = _as_mapping(raw).get("verdicts")
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and str(item.get("id", "")) in ids:
            by_id.setdefault(str(item["id"]), item)
    verdicts: dict[str, TriageVerdict] = {}
    for case_id in ids:
        item = by_id.get(case_id, {})
        dup = item.get("duplicate_of")
        verdicts[case_id] = TriageVerdict(
            case_id=case_id,
            kind=_choice(item.get("kind"), KINDS, DFLT_KIND),
            duplicate_of=str(dup) if dup else None,
            group=_slug(item.get("group")) or case_id,
            size=_choice(item.get("size"), SIZES, DFLT_SIZE),
            priority=_choice(item.get("priority"), PRIORITIES, DFLT_PRIORITY),
        )
    return verdicts


def _resolve_duplicates(
    verdicts: Mapping[str, TriageVerdict], known: Iterable[str]
) -> dict[str, TriageVerdict]:
    """Drop a ``duplicate_of`` that points outside ``known``, at itself, or into a cycle."""
    known_ids = set(known)
    fixed: dict[str, TriageVerdict] = {}
    for case_id, verdict in verdicts.items():
        target = verdict.duplicate_of
        seen, node = {case_id}, target
        while node and node in verdicts and node not in seen:
            seen.add(node)
            node = verdicts[node].duplicate_of
        cyclic = node is not None and node in seen
        if target and (target == case_id or target not in known_ids or cyclic):
            verdict = TriageVerdict(**{**verdict.__dict__, "duplicate_of": None})
        fixed[case_id] = verdict
    return fixed


def _root(case_id: str, verdicts: Mapping[str, TriageVerdict]) -> str:
    """The case a chain of duplicates ends at (itself when it duplicates nothing in the batch)."""
    seen = {case_id}
    while True:
        target = verdicts[case_id].duplicate_of if case_id in verdicts else None
        if not target or target not in verdicts or target in seen:
            return case_id
        seen.add(target)
        case_id = target


def group_cases(
    cases: Sequence[Case], verdicts: Mapping[str, TriageVerdict]
) -> list[list[Case]]:
    """The dispatch groups: cases sharing a ``group`` slug or a duplicate chain, together.

    Within a group the lead is the highest-priority, then oldest case (it is the one that is
    dispatched; the rest ride along in its prompt). Groups are ordered by their best priority,
    then the age of their lead. Every case is in exactly one group.
    """
    rank = {name: i for i, name in enumerate(PRIORITIES)}
    by_id = {case.id: case for case in cases}
    parent: dict[str, str] = {case_id: case_id for case_id in by_id}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    slugs: dict[str, str] = {}
    for case_id in by_id:
        verdict = verdicts.get(case_id)
        if verdict is None:
            continue
        if verdict.group:
            union(case_id, slugs.setdefault(verdict.group, case_id))
        root = _root(case_id, verdicts)
        if root in by_id:
            union(case_id, root)

    def order(case: Case) -> tuple[int, Any, str]:
        verdict = verdicts.get(case.id)
        return (
            rank.get(verdict.priority if verdict else DFLT_PRIORITY, 1),
            case.created_at,
            case.id,
        )

    members: dict[str, list[Case]] = {}
    for case in cases:
        members.setdefault(find(case.id), []).append(case)
    groups = [sorted(group, key=order) for group in members.values()]
    return sorted(groups, key=lambda group: order(group[0]))


def _excerpt(case: Case, limit: int) -> str:
    first = next((e.text for e in case.entries if e.kind == "message" and e.text), "")
    text = " ".join(str(first).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compose_triage_prompt(
    cases: Sequence[Case],
    context: Sequence[Case] = (),
    *,
    excerpt_chars: int = DFLT_EXCERPT_CHARS,
) -> str:
    """The prompt: the packaged text, the batch, and the partner's open cases as context."""
    rules = (
        resources.files("liaise.data")
        .joinpath(PROMPT_RESOURCE)
        .read_text(encoding="utf-8")
    )
    batch = "\n".join(
        f"- id: {case.id}\n  text: {json.dumps(_excerpt(case, excerpt_chars))}"
        for case in cases
    )
    parts = [rules.strip(), "## Batch\n\n" + batch]
    if context:
        opened = "\n".join(
            f"- id: {case.id}\n  state: {case.state}\n  text: "
            f"{json.dumps(_excerpt(case, excerpt_chars))}"
            for case in context
        )
        parts.append("## Already open\n\n" + opened)
    return "\n\n".join(parts) + "\n"


class LlmTriage:
    """The :data:`~liaise.tick.Triage` seam backed by an :data:`Ask`.

    Calling it with the ready cases returns the dispatch groups (lead first); ``verdicts``
    keeps the verdicts of the latest call, which the tick turns into labels and comments.
    An ``ask`` that raises propagates: the tick treats that as a failed triage and falls
    back to its own order.
    """

    #: The tick passes the partner's open cases as ``context=`` to a seam that says so.
    accepts_context = True
    #: The tick starts each multi-case group as one job, its lead carrying the rest.
    merges_groups = True

    def __init__(self, ask: Ask, *, excerpt_chars: int = DFLT_EXCERPT_CHARS):
        self.ask = ask
        self.excerpt_chars = excerpt_chars
        self.verdicts: dict[str, TriageVerdict] = {}

    def __call__(
        self, cases: Sequence[Case], *, context: Sequence[Case] = ()
    ) -> list[list[Case]]:
        self.verdicts = {}
        if not cases:
            return []
        prompt = compose_triage_prompt(cases, context, excerpt_chars=self.excerpt_chars)
        raw = self.ask(prompt)
        ids = [case.id for case in cases]
        parsed = parse_verdicts(raw, [*ids, *(case.id for case in context)])
        known = _resolve_duplicates(parsed, [*ids, *(case.id for case in context)])
        self.verdicts = {case_id: known[case_id] for case_id in ids}
        return group_cases(cases, self.verdicts)


def headless_ask(
    prompt: str,
    *,
    max_turns: int = DFLT_TRIAGE_MAX_TURNS,
    claude_bin: str = "claude",
    timeout: int = DFLT_TRIAGE_TIMEOUT,
    cwd: Optional[str] = None,
) -> Any:
    """Run the triage prompt as one headless, turn-capped call and return its structured answer.

    No tools are granted beyond what the call needs to answer: it classifies text it was
    handed. Raises ``RuntimeError`` if the call fails or returns nothing structured.
    """
    from liaise.processor import _resolve_bin, _structured_output, child_env
    import os

    argv = [
        _resolve_bin(claude_bin) or claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--json-schema",
        json.dumps(TRIAGE_SCHEMA),
        "--permission-mode",
        "plan",
    ]
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=child_env(os.environ),
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"the triage call exited {done.returncode}: {done.stderr[-200:]}"
        )
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"the triage call returned no JSON: {error}") from error
    structured = _structured_output(result) if isinstance(result, Mapping) else {}
    if not structured:
        raise RuntimeError("the triage call returned no structured verdicts")
    return structured
