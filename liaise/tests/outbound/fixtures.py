"""Fixtures for the outbound scenario suite: invented people, audiences per channel, a disclosure builder.

Everything here is invented: Ada, Bram and Cy, the project Heron, the repository
``example/app``. The shapes are those of the design of record (liaise discussion 32):
:func:`audience_for` writes a correspond ``Audience`` record for a channel, and
:func:`disclosure_for` an ``acquaint.disclosure`` record for a set of readers, by the
rules of §4.1, §4.4 and §4.5 (tiers to clearances, involvement, seals, the vocabulary of
every entity some reader is not cleared for). Neither package is imported: the suite
runs offline against these fakes.

Addresses are written with ``{at}`` for ``@`` in the scenario file, and the operator's
address is built by concatenation here, so the repository holds no email address.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from liaise.detect import LABELS

KEY = b"k" * 32
AT = "@"
TODAY = "2026-09-15"
#: The operator's own address and handle: what the ``personal`` detector looks for.
OPERATOR_ADDRESS = "ops" + AT + "example.org"
OPERATOR_TERMS = (OPERATOR_ADDRESS, "ops-owner")
CANARY = "zq-canary-7731"
ALLOWLIST = ("example.org", "github.com")
#: A GitHub token shape, built so no scanner reads a token in this repository.
TOKEN = "ghp_" + "a" * 36

TIER_CLEARANCE = {
    "open": "amber",
    "involved": "green",
    "need-to-know": "clear",
    "reviewed": "clear",
}
INVOLVED_CLEARANCE = "red"

#: The people (research §9.3): Ada is open until her review date and a public figure (her
#: name is ``clear``); Bram is reviewed, and his own dealings are his (``red``); Cy is
#: involved in Heron, and Cy's news is Cy's (``red``).
PEOPLE: dict[str, dict[str, Any]] = {
    "ada": {
        "tier": "open",
        "review_by": "2026-11-01",
        "label": "clear",
        "terms": ["Ada", "Ada Lorne"],
        "involved_in": [],
        "address": "email:ada" + AT + "example.org",
        "github": "github:ada-lorne",
    },
    "bram": {
        "tier": "reviewed",
        "review_by": None,
        "label": "red",
        "terms": ["Bram", "Bramwell"],
        "involved_in": [],
        "address": "email:bram" + AT + "example.org",
        "github": "github:bramwell-k",
    },
    "cy": {
        "tier": "involved",
        "review_by": "2026-12-01",
        "label": "red",
        "terms": ["Cy", "Cyrus"],
        "involved_in": ["project:heron"],
        "address": "email:cy" + AT + "example.org",
        "github": "github:cy-v",
    },
}
#: The projects: Heron is amber, sealed from Bram, with its codenames.
ENTITIES: dict[str, dict[str, Any]] = {
    "project:heron": {
        "label": "amber",
        "terms": ["Heron", "the bird project", "H."],
        "sealed_from": ["bram"],
    },
}
#: Aliases the mutation generator substitutes for a sensitive term.
ALIASES = {
    "Heron": ["the bird project"],
    "the bird project": ["Heron"],
    "Cy": ["Cyrus"],
    "Bram": ["Bramwell"],
}
#: Address -> person id, what ``acquaint.resolve`` answers (``identities`` in evaluate).
IDENTITIES: dict[str, Optional[str]] = {}
for _person, _record in PEOPLE.items():
    IDENTITIES[_record["address"]] = _person
    IDENTITIES[_record["github"]] = _person

PUBLIC_ISSUE = "github:example/app#12"
ORG_ISSUE = "github:example/app-internal#3"
PLANNED = {
    "discord_dm": "discord:dm/ada-lorne",
    "discord_channel": "discord:heron-core",
    "slack_channel": "slack:shared-with-partner",
}
CHANNELS = ("email", "public_issue", "org_repo", "operator", *PLANNED)


def address_of(recipient: str) -> str:
    """The email address a recipient id or address is written to."""
    if recipient in PEOPLE:
        return PEOPLE[recipient]["address"]
    if ":" in recipient:
        return recipient
    return f"email:{recipient}" + AT + "unknown.example"


def _reader(address: str) -> dict:
    channel, _, handle = address.partition(":")
    return {"channel": channel, "native_id": handle, "handle": handle}


def audience_for(
    channel: str,
    *,
    recipient: str,
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    as_of: str = TODAY + "T12:00:00Z",
) -> dict:
    """A correspond ``Audience`` record for ``channel``, as its adapter would compute it."""
    if channel == "email":
        readers = [_reader(address_of(recipient))] + [
            _reader(address_of(c)) for c in cc
        ]
        classes = ["copies delivered to each address"]
        if bcc:
            classes.append("a blind copy to an address the other readers do not see")
        return {
            "ref": address_of(recipient),
            "scope": "named",
            "readers": readers,
            "complete": False,
            "classes": classes,
            "external": True,
            "retractable": False,
            "durability": ["copies_pushed"],
            "widening": ["forwarding"],
            "as_of": as_of,
            "evidence": ["addresses classified against the operator's domains"],
            "defaulted": False,
        }
    if channel == "public_issue":
        return {
            "ref": PUBLIC_ISSUE,
            "scope": "public",
            "readers": [],
            "complete": False,
            "classes": ["watchers and participants receive the body by email"],
            "external": True,
            "retractable": False,
            "durability": [
                "indexed",
                "archived_by_others",
                "copies_pushed",
                "edit_history_visible",
            ],
            "widening": ["forks", "visibility_flip"],
            "as_of": as_of,
            "evidence": ["gh api repos/example/app: visibility public"],
            "defaulted": False,
        }
    if channel == "org_repo":
        return {
            "ref": ORG_ISSUE,
            "scope": "org",
            "readers": [_reader(PEOPLE["ada"]["github"])],
            "complete": False,
            "classes": [
                "every member of the organisation through its base permission (documented default: read)",
                "apps and webhooks with access to the repository, which can copy its conversations elsewhere",
            ],
            "external": None,
            "retractable": False,
            "durability": ["copies_pushed", "edit_history_visible"],
            "widening": ["joiners_read_history", "forks", "visibility_flip"],
            "as_of": as_of,
            "evidence": [
                "gh api repos/example/app-internal: visibility private, owner an organisation"
            ],
            "defaulted": False,
        }
    if channel == "operator":
        return {
            "ref": "macos:notify",
            "scope": "operator",
            "readers": [],
            "complete": True,
            "classes": [],
            "external": False,
            "retractable": True,
            "durability": [],
            "widening": [],
            "as_of": as_of,
            "evidence": ["the operator's own device"],
            "defaulted": False,
        }
    if channel in PLANNED:
        return {
            "ref": PLANNED[channel],
            "scope": "public",
            "readers": [],
            "complete": False,
            "classes": [],
            "external": None,
            "retractable": False,
            "durability": [
                "indexed",
                "archived_by_others",
                "copies_pushed",
                "edit_history_visible",
            ],
            "widening": [
                "visibility_flip",
                "joiners_read_history",
                "forwarding",
                "forks",
                "list_expansion",
            ],
            "as_of": as_of,
            "evidence": [
                f"{channel.partition('_')[0]}: a channel not built",
                "unknown resolves to public",
            ],
            "defaulted": True,
        }
    raise ValueError(f"no fixture audience for channel {channel!r}; one of {CHANNELS}")


def _label_rank(label: str) -> int:
    return LABELS.index(label)


def may_see(clearance: Optional[str], label: str) -> bool:
    """Whether a reader cleared to ``clearance`` may be told about a ``label`` entity."""
    return clearance is None or _label_rank(clearance) >= _label_rank(label)


def _resolve(text: str) -> Optional[str]:
    if text in PEOPLE:
        return text
    return IDENTITIES.get(text)


def _person_entry(person: str, record: Mapping, override: Mapping) -> dict:
    tier = override.get("tier", record["tier"])
    entry = {
        "tier": tier,
        "recorded_tier": override.get("recorded_tier", record["tier"]),
        "lapsed": override.get("lapsed", False),
        "review_by": override.get("review_by", record["review_by"]),
        "source": override.get("source", "operator"),
        "clearance": TIER_CLEARANCE[tier],
        "involved_in": list(override.get("involved_in", record["involved_in"])),
        "already_told": [],
        "via": "named",
    }
    if "review" in override:
        entry["review"] = list(override["review"])
    return entry


def _scope_ceiling(audience: Optional[Mapping]) -> Optional[str]:
    """What the unlisted readers of an audience may hear (§4.4): ``None`` for no such class."""
    if audience is None:
        return None
    if audience.get("defaulted") or audience.get("scope") == "public":
        return "clear"
    if audience.get("scope") in ("org", "group") and not audience.get("complete"):
        return "clear"  # no organisation record carries a clearance in these fixtures
    return None


def disclosure_for(
    readers: Sequence[str],
    *,
    audience: Optional[Mapping] = None,
    today: str = TODAY,
    overrides: Optional[Mapping[str, Mapping]] = None,
    gaps: Optional[Mapping[str, Sequence[str]]] = None,
) -> dict:
    """An ``acquaint.disclosure`` record for ``readers`` (ids or addresses) and ``audience``.

    ``overrides`` change a person's entry (``{"ada": {"tier": "need-to-know"}}``);
    ``gaps`` add to the gap lists. The vocabulary holds the terms of every entity at
    least one reader is not cleared for, or that is sealed from a reader, or whose label
    is above what the audience's unlisted readers may hear.
    """
    overrides = overrides or {}
    people: dict[str, dict] = {}
    gap_lists: dict[str, list[str]] = {
        name: []
        for name in (
            "unrecorded",
            "ambiguous",
            "not_a_person",
            "no_tier",
            "organisation",
            "unreadable",
            "unresolved_seals",
        )
    }
    for name, values in (gaps or {}).items():
        gap_lists[name] = list(values)
    listed = list(readers)
    if audience is not None:
        listed += [
            f"{r['channel']}:{r.get('handle') or r.get('native_id')}"
            for r in audience.get("readers") or ()
            if not r.get("is_self")
        ]
    for text in listed:
        person = _resolve(text)
        if person is None:
            gap_lists["unrecorded"].append(text)
            continue
        people[person] = _person_entry(
            person, PEOPLE[person], overrides.get(person, {})
        )
    ceiling = _scope_ceiling(audience)
    strangers = ["clear"] * len(gap_lists["unrecorded"])
    clearances = (
        [p["clearance"] for p in people.values()]
        + strangers
        + ([ceiling] if ceiling else [])
    )
    least = min(clearances, key=_label_rank) if clearances else "red"

    entities: dict[str, dict] = {}
    seals: list[dict] = []
    vocabulary: list[dict] = []
    subjects = {
        **{ref: record for ref, record in ENTITIES.items()},
        **{f"person:{person}": record for person, record in PEOPLE.items()},
    }
    for ref, record in subjects.items():
        label = record["label"]
        sealed_from = list(record.get("sealed_from", ()))
        cleared, not_cleared, sealed = [], [], []
        for person, entry in people.items():
            if person in sealed_from:
                sealed.append(person)
                not_cleared.append(person)
            elif (
                ref == f"person:{person}"
                or ref in entry["involved_in"]
                or may_see(entry["clearance"], label)
            ):
                cleared.append(person)
            else:
                not_cleared.append(person)
        above_unlisted = any(
            not may_see(c, label) for c in strangers + ([ceiling] if ceiling else [])
        )
        seals += [{"entity": ref, "from": person} for person in sealed]
        entities[ref] = {
            "label": label,
            "cleared": cleared,
            "not_cleared": not_cleared,
            "sealed_from": sealed_from,
            "above_unlisted_readers": above_unlisted,
        }
        if not_cleared or above_unlisted:
            vocabulary += [
                {"term": term, "entity": ref, "label": label, "sealed_from": sealed}
                for term in record["terms"]
            ]
    about = None
    if audience is not None:
        about = {
            "scope": "public" if audience.get("defaulted") else audience["scope"],
            "complete": bool(audience.get("complete")),
            "ref": audience["ref"],
            "organisation": None,
            "ceiling": ceiling,
        }
    return {
        "as_of": today,
        "people": people,
        "least_clearance": least,
        "audience": about,
        "entities": entities,
        "seals": seals,
        "vocabulary": vocabulary,
        "gaps": gap_lists,
        "warnings": [],
    }


def deep_merge(base: Mapping, changes: Mapping) -> dict:
    """``base`` with ``changes`` merged in, mapping by mapping; lists replace."""
    merged = copy.deepcopy(dict(base))
    for key, value in changes.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
