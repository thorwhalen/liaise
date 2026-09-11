"""Intake: which messages belong to a subject, and (0.0.x) when a partner's issue is ready.

**0.1: :func:`intake`.** It polls a subject's bindings through correspond (adopting a
repository's older open issues before its first poll), dedupes on delivery id, and
routes each message: onto the case its conversation belongs to, into a new case when a
binding matches and the sender may report, or into the unrouted queue with the reason.
It writes to the ledger and nowhere else. See its docstring.

**0.0.x** (kept until the tick replaces ``run.py``). The functions before the 0.1 section
are read-only — they answer questions about an :class:`~liaise.github.Issue` and a
:class:`~liaise.config.PartnerConfig`. Applying the answer (adding labels on first sight,
dispatching a ready issue) is `run.py`'s job; they only compute.

**A caveat about "last edit by the partner".** ``issue.updated_at`` looks like the
obvious signal for this — it is not one: GitHub bumps it on *anyone's* comment and on
every label change, including `liaise`'s own. Verified on a live issue: the issue's
`updatedAt` matched its last comment's `createdAt` to the second, regardless of who
posted the comment. Using it here would mean an owner "just thinking out loud" in the
thread, or `liaise`'s own two-label-edit-per-transition `set_state` call, resets the
partner's own quiet window — exactly the rule A.3 forbids ("the owner commenting is not
the partner writing"). There is no clean "the author edited the body at this time"
signal available from `gh`'s JSON (the GraphQL `lastEditedAt` field is not among the
fields `gh issue view --json` exposes), so :func:`last_partner_activity` only trusts the
issue's creation time and the partner's own comments — never `issue.updated_at`.
"""

from __future__ import annotations

from collections import ChainMap, Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from correspond import CorrespondError, listen, parse_ref, read
from correspond.model import Event, Message, format_time
from correspond.routing import binding_matches

from liaise.access import (
    AccessDecision,
    Resolver,
    authorize,
    is_relay,
    resolve_person,
)
from liaise.config import PartnerConfig
from liaise.github import GitHub, Issue
from liaise.ledger import Ledger
from liaise.model import (
    CASE_STATES,
    INITIAL_CASE_STATE,
    Case,
    LedgerEntry,
    require_one_of,
)
from liaise.subjects import Subject, poll_ref, ref_key


def is_partner_issue(issue: Issue, partner: PartnerConfig) -> bool:
    """True when `issue` belongs to `partner`: authored by them, or carrying their label.

    (a) the author is one of the partner's `github_logins`, or (b) the issue carries the
    partner's label (filed on their behalf through the owner's credentials, so the author
    is not the partner).
    """
    return issue.author in partner.github_logins or partner.label in issue.labels


def last_partner_activity(issue: Issue, partner: PartnerConfig) -> datetime:
    """The latest of: the issue's creation, and the partner's own comments.

    Deliberately never reads `issue.updated_at` — see the module docstring
    (H-2): that field moves on anyone's activity, not just the partner's.
    Activity by anyone else never contributes here.
    """
    candidates = [issue.created_at]
    for comment in issue.comments:
        if comment.author in partner.github_logins:
            candidates.append(comment.created_at)
    return max(candidates)


def _contains_marker(text: str, marker: str) -> bool:
    return marker.lower() in text.lower()


def _marker_events(
    issue: Issue, partner: PartnerConfig
) -> list[tuple[datetime, bool, bool]]:
    """(timestamp, has_go, has_wait) for the body and every partner comment, time-ordered."""
    events: list[tuple[datetime, bool, bool]] = []

    body_go = _contains_marker(issue.body, partner.markers.go)
    body_wait = _contains_marker(issue.body, partner.markers.wait)
    if body_go or body_wait:
        events.append((issue.created_at, body_go, body_wait))

    for comment in issue.comments:
        if comment.author not in partner.github_logins:
            continue
        go = _contains_marker(comment.body, partner.markers.go)
        wait = _contains_marker(comment.body, partner.markers.wait)
        if go or wait:
            events.append((comment.created_at, go, wait))

    events.sort(key=lambda e: e[0])
    return events


def _marker_state(
    issue: Issue, partner: PartnerConfig
) -> tuple[Optional[datetime], bool]:
    """Return (timestamp of the last go marker, whether a later wait marker pauses it)."""
    last_go: Optional[datetime] = None
    last_wait: Optional[datetime] = None
    for ts, go, wait in _marker_events(issue, partner):
        if go:
            last_go = ts
        if wait:
            last_wait = ts
    paused = last_wait is not None and (last_go is None or last_wait > last_go)
    return last_go, paused


@dataclass(frozen=True)
class Readiness:
    """The result of :func:`compute_readiness`."""

    ready: bool
    paused: bool
    last_activity: datetime
    countdown: timedelta  # zero once ready or paused
    reason: str


def compute_readiness(
    issue: Issue, partner: PartnerConfig, *, now: Optional[datetime] = None
) -> Readiness:
    """Is `issue` ready to dispatch, right now?

    Ready when `now - last_activity >= quiet_minutes`, or when a go marker appeared and
    `now - marker_time >= go_minutes`. A wait marker later than the last go marker
    suspends the issue until the next go marker, overriding both.
    """
    now = now if now is not None else datetime.now(timezone.utc)

    last_go, paused = _marker_state(issue, partner)
    activity = last_partner_activity(issue, partner)

    if paused:
        return Readiness(
            ready=False,
            paused=True,
            last_activity=activity,
            countdown=timedelta(0),
            reason="partner asked to wait",
        )

    quiet_deadline = activity + timedelta(minutes=partner.quiet_minutes)
    go_deadline = (
        last_go + timedelta(minutes=partner.go_minutes) if last_go is not None else None
    )

    ready_by_quiet = now >= quiet_deadline
    ready_by_go = go_deadline is not None and now >= go_deadline

    if ready_by_quiet or ready_by_go:
        reason = (
            "go marker"
            if ready_by_go and not ready_by_quiet
            else "quiet window elapsed"
        )
        return Readiness(
            ready=True,
            paused=False,
            last_activity=activity,
            countdown=timedelta(0),
            reason=reason,
        )

    deadlines = [quiet_deadline] + ([go_deadline] if go_deadline is not None else [])
    countdown = min(deadlines) - now
    return Readiness(
        ready=False,
        paused=False,
        last_activity=activity,
        countdown=countdown,
        reason="waiting",
    )


def find_partner_issues(
    gh: GitHub, partner: PartnerConfig, *, state: str = "open"
) -> list[Issue]:
    """Every open issue belonging to `partner`, oldest first.

    Queries `gh` once per `github_login` plus once by label (the union `is_partner_issue`
    describes), then de-duplicates by issue number.
    """
    seen: dict[int, Issue] = {}
    for login in partner.github_logins:
        for issue in gh.list_issues(partner.repo, author=login, state=state):
            seen[issue.number] = issue
    for issue in gh.list_issues(partner.repo, label=partner.label, state=state):
        seen[issue.number] = issue
    return sorted(
        (i for i in seen.values() if is_partner_issue(i, partner)),
        key=lambda i: i.created_at,
    )


# ---- 0.1: intake over correspond (listen, dedupe, route, authorize) ----

#: What intake does with an event it takes in (each marks the event seen)...
TAKEN_ACTIONS = ("opened", "appended", "unrouted")
#: ...and with one it passes over (neither is marked seen).
SKIPPED_ACTIONS = ("duplicate", "ignored")
INTAKE_ACTIONS = TAKEN_ACTIONS + SKIPPED_ACTIONS
#: The actor of an entry for a message the channel's own account wrote.
SELF_ACTOR = "self"
#: The ``detail["role"]`` of an entry for a message the channel's own account wrote, and
#: of one a relay wrote. Neither is attributed to a person, so neither is partner activity.
SELF_ROLE = "self"
RELAY_ROLE = "relay"
#: The actor of the entries intake records itself (an adoption's transition).
LIAISE_ACTOR = "liaise"
#: The permission an inbound message implies, whether it opens a case or adds to one.
REPORT_PERMISSION = "report"
#: Where an issue carrying several state labels is adopted: the operator sorts it out.
AMBIGUOUS_ADOPTION_STATE = "needs-owner"
#: An issue opening in this ``native`` state opens no case.
CLOSED_STATE = "closed"
#: Conversation kinds whose every message is a thread of its own (a web inbox's reports).
PER_MESSAGE_CONVERSATION_KINDS = ("inbox",)
#: Channels whose first poll looks back only so far (correspond's GitHub adapter: a day),
#: so intake reads a repository's open issues before polling it for the first time.
ADOPTING_CHANNELS = ("github",)
#: The kind of conversation whose open issues are read for adoption.
ADOPTING_CONVERSATION_KIND = "repository"
#: The most open issues one adoption read asks for: one page of GitHub's issue list.
ADOPTION_READ_LIMIT = 100
#: The ``native`` state of an open issue, and how the id of its opening post starts.
OPEN_STATE = "open"
OPENING_ID_PREFIX = "issue-"
#: The kind of event listen gives a new post, and so the event of an adopted one.
CREATED_EVENT_KIND = "message.created"
#: What the ``opened`` event of an adopted issue adds to its reason.
ADOPTED_REASON = "open before the first poll, adopted"


@dataclass(frozen=True)
class IntakeEvent:
    """What :func:`intake` did with one channel event, and why.

    ``action`` is one of :data:`INTAKE_ACTIONS`. ``conversation`` is the one the case is
    keyed on (see :func:`case_conversation`), ``author`` the sender's address.
    """

    delivery_id: str
    action: str
    conversation: str = ""
    author: str = ""
    case_id: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        require_one_of(self.action, INTAKE_ACTIONS, what="intake action")


@dataclass(frozen=True)
class IntakeReport:
    """What one :func:`intake` of a subject took in.

    ``new_cases`` and ``updated_cases`` are the cases as intake left them (a case opened
    this time is only in ``new_cases``). ``unrouted`` holds the fields each unrouted
    message was queued with; ``problems`` what stopped a binding or a label from being
    read as it should.
    """

    subject: str
    events: tuple[IntakeEvent, ...] = ()
    new_cases: tuple[Case, ...] = ()
    updated_cases: tuple[Case, ...] = ()
    unrouted: tuple[Mapping[str, Any], ...] = ()
    problems: tuple[str, ...] = ()
    dry_run: bool = False

    def plan_lines(self) -> list[str]:
        """One line per event taken in, per case, and per problem, under a summary line."""
        counts = Counter(item.action for item in self.events)
        taken = [item for item in self.events if item.action in TAKEN_ACTIONS]
        head = f"intake {self.subject}: {len(taken)} new event{'' if len(taken) == 1 else 's'}"
        skipped = [f"{counts[a]} {a}" for a in SKIPPED_ACTIONS if counts[a]]
        if skipped:
            head += f" ({', '.join(skipped)})"
        lines = [head + (" [dry run]" if self.dry_run else "")]
        for item in taken:
            outcome = (
                f"unrouted: {item.reason}"
                if item.action == "unrouted"
                else f"{item.action} {item.case_id} ({item.reason})"
            )
            lines.append(f"  {item.conversation} from {item.author}: {outcome}")
        lines += [
            f"  case {case.id}: created, state {case.state}, reporter {case.reporter}"
            for case in self.new_cases
        ]
        lines += [
            f"  case {case.id}: updated, state {case.state}"
            for case in self.updated_cases
        ]
        lines += [f"  problem: {problem}" for problem in self.problems]
        return lines


def case_conversation(message: Message) -> str:
    """The encoded conversation ``message``'s case is keyed on.

    Its own conversation (``github:example/app#12``), except where each message is a
    thread of its own. A web inbox's reports all arrive on ``webinbox:<site>``, so each is
    keyed ``webinbox:<site>#<report id>`` and opens a case of its own.
    """
    conversation = message.conversation
    if conversation.kind in PER_MESSAGE_CONVERSATION_KINDS:
        return f"{conversation.encoded}#{message.thread_root or message.id}"
    return conversation.encoded


def _poll_refs(bindings: tuple[str, ...], *, where: str) -> tuple[list[str], list[str]]:
    """The conversations to poll for ``bindings``, once each, and the bindings that cannot be.

    Bindings on one conversation (compared as :func:`~liaise.subjects.ref_key` does) are
    polled once, on the first binding's spelling.
    """
    refs: dict[str, str] = {}
    problems = []
    for pattern in bindings:
        ref = poll_ref(pattern)
        if ref is None:
            problems.append(
                f"{where}: binding {pattern!r} has a wildcard in its conversation "
                f"part, which intake cannot poll in v0.1; name the conversation "
                f"instead. Skipped."
            )
        else:
            refs.setdefault(ref_key(ref), ref)
    return list(refs.values()), problems


def _label_states(message: Message, label_prefix: str) -> list[str]:
    """The case states ``message``'s ``<label_prefix><state>`` labels name, in label order."""
    labels = message.native.get("labels") or ()
    suffixes = (
        label[len(label_prefix) :] for label in labels if label.startswith(label_prefix)
    )
    return list(dict.fromkeys(s for s in suffixes if s in CASE_STATES))


def _message_entry(
    event: Event,
    *,
    actor: Optional[str],
    decision: Optional[AccessDecision] = None,
    role: Optional[str] = None,
) -> LedgerEntry:
    """The ``message`` entry for ``event``: attributed by ``decision``, or to ``actor`` alone.

    ``role`` (:data:`SELF_ROLE` or :data:`RELAY_ROLE`) says who an unattributed actor is.
    """
    message = event.message
    detail = {
        "event": event.kind,
        "channel": event.channel,
        "message_id": message.id,
        "url": message.url,
    }
    if decision is not None:
        detail["via"] = decision.via
    if role is not None:
        detail["role"] = role
    return LedgerEntry(
        at=message.edited_at or message.sent_at,
        kind="message",
        actor=actor,
        grade=decision.grade if decision else message.authenticity.grade.value,
        permission=decision.permission if decision else None,
        delivery_id=event.delivery_id,
        text=message.text,
        detail=detail,
    )


def _opening_delivery_id(message: Message) -> str:
    """The delivery id correspond's GitHub listen gives the opening post ``message``.

    ``github:owner/repo:issue-<N>@<created_at>``, built as the adapter builds it (GitHub's
    ``created_at`` is whole seconds in UTC, as ``format_time`` writes it), so an opening
    adopted from a read is the very delivery a poll would hear.
    """
    repository = message.conversation.parent or message.conversation
    return f"{repository.encoded}:{message.id}@{format_time(message.sent_at)}"


def intake(
    subject: Subject,
    ledger: Ledger,
    *,
    registry: Optional[Mapping[str, Any]] = None,
    resolver: Resolver = resolve_person,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> IntakeReport:
    """Take in what arrived on ``subject``'s bindings since the ledger's cursors.

    **Polling.** Each conversation the bindings name is polled once, however many
    bindings name it, with the ``?conditions`` stripped
    (``github:example/app?labels=partner:pat`` polls ``github:example/app``), through
    ``correspond.listen`` over the ledger's cursors and ``registry`` (correspond's own
    when None). Each message it yields meets every binding. A binding with a wildcard in
    its conversation part cannot be polled in v0.1: it is reported in ``problems`` and
    skipped. A channel that fails is a problem too, and the other conversations are
    still polled.

    **Adoption, before a first poll.** correspond's GitHub adapter looks back only a day
    the first time it polls a repository, so an older open issue would never be heard.
    When the ledger holds no cursor for a repository on one of :data:`ADOPTING_CHANNELS`,
    intake first reads its open issues (``correspond.read``, the
    :data:`ADOPTION_READ_LIMIT` most recent) and takes in each opening post a binding
    matches, exactly as the poll would: the same routing, authorization and legacy
    adoption, marked seen under the delivery id listen gives that post, so no poll takes
    it in again. When the read fails, that is a problem and the repository is not polled,
    so its cursor stays unset and the next intake tries the adoption again.

    **Routing.** An event with no message is passed over, as is a delivery this intake
    already dealt with (a poll hearing an issue adopted moments before). So is one whose
    delivery id the ledger has already seen (``duplicate``). Otherwise:

    - Its conversation belongs to one of this subject's cases: the message becomes a
      ``message`` entry on it. A message by the channel's own account (``is_self``) is
      recorded with the actor ``self``, and one by a ``policy.relays`` author with its
      address as the actor. Each carries its ``detail["role"]`` (``self`` or ``relay``)
      and no permission: neither is partner activity, and neither is unrouted. Anyone
      else must be authorized to ``report``, or the message is queued as unrouted with
      the reason, and no entry.
    - A binding matches it (``correspond.routing.binding_matches``): when its sender may
      ``report``, a case opens on its conversation with that person as reporter and the
      message as its first entry, in ``intake``. Otherwise it is unrouted with the reason.
      A closed issue's opening opens no case.
    - Neither: it is not this subject's (``ignored``), and it is not marked seen.

    **Legacy adoption.** An issue opening that already carries a
    ``<label_prefix><state>`` label opens its case in that state rather than ``intake``,
    recorded as a transition. One carrying several state labels opens in
    ``needs-owner``, with a problem saying why. The 0.0.x session id (the
    ``sessions__<repo>__<n>`` key) is not carried over: that is out of scope here.

    **Dry run.** Cursors are not committed, and nothing reaches the ledger's store,
    adoptions included: unless the store is already a ``ChainMap`` overlay (as the tick
    passes it), intake works on one of its own. Since no cursor is committed, every dry
    run before the first real one adopts again.

    Cursors are kept per conversation, not per subject, so two subjects polling one
    conversation would starve each other: :func:`~liaise.subjects.load_subjects` refuses
    such a configuration.

    ``resolver`` maps a sender's address to a person (see :mod:`liaise.access`). ``now``
    stamps the inbox records and adoption transitions (default: the current UTC time).
    """
    now = now if now is not None else datetime.now(timezone.utc)
    if dry_run and not isinstance(ledger.store, ChainMap):
        ledger = Ledger(ChainMap({}, ledger.store))
    where = subject.source or subject.slug
    refs, problems = _poll_refs(subject.bindings, where=where)
    events: list[IntakeEvent] = []
    unrouted: list[dict[str, Any]] = []
    opened: dict[str, None] = {}
    touched: dict[str, None] = {}
    taken: set[str] = set()  # the delivery ids this intake has already dealt with

    def record(event: Event, action: str, **fields: Any) -> None:
        message = event.message
        taken.add(event.delivery_id)
        events.append(
            IntakeEvent(
                delivery_id=event.delivery_id,
                action=action,
                conversation=case_conversation(message),
                author=message.author.address,
                **fields,
            )
        )

    def refuse(event: Event, decision: AccessDecision) -> None:
        message = event.message
        fields = {
            "delivery_id": event.delivery_id,
            "subject": subject.slug,
            "author": message.author.address,
            "grade": decision.grade,
            "reason": decision.reason,
            "url": message.url,
            "conversation": case_conversation(message),
            "at": message.sent_at,
        }
        ledger.add_unrouted(**fields)
        unrouted.append(fields)
        record(event, "unrouted", reason=decision.reason)

    def append(case: Case, event: Event) -> None:
        message = event.message
        address = message.author.address
        if message.author.is_self:
            entry = _message_entry(event, actor=SELF_ACTOR, role=SELF_ROLE)
            reason = "the channel's own account"
        elif is_relay(address, subject):
            entry = _message_entry(event, actor=address, role=RELAY_ROLE)
            reason = "a relay"
        else:
            decision = authorize(message, subject, REPORT_PERMISSION, resolver=resolver)
            if not decision.allowed:
                refuse(event, decision)
                return
            entry = _message_entry(event, actor=decision.person, decision=decision)
            reason = f"{decision.person} via {decision.via}"
        ledger.append(case.id, entry)
        touched[case.id] = None
        record(event, "appended", case_id=case.id, reason=reason)

    def open_case(pattern: str, event: Event, *, adopted: bool) -> None:
        message = event.message
        decision = authorize(message, subject, REPORT_PERMISSION, resolver=resolver)
        if not decision.allowed:
            refuse(event, decision)
            return
        conversation = case_conversation(message)
        case = ledger.new_case(
            subject.slug, conversation, reporter=decision.person, at=message.sent_at
        )
        entry = _message_entry(event, actor=decision.person, decision=decision)
        ledger.append(case.id, entry)
        opened[case.id] = None
        states = _label_states(message, subject.label_prefix)
        labels = ", ".join(subject.label_prefix + state for state in states)
        if len(states) > 1:
            ledger.transition(
                case.id,
                AMBIGUOUS_ADOPTION_STATE,
                at=now,
                actor=LIAISE_ACTOR,
                reason=f"adopted with several state labels: {labels}",
            )
            problems.append(
                f"{conversation} carries several state labels ({labels}); case "
                f"{case.id} opened in {AMBIGUOUS_ADOPTION_STATE} for the operator"
            )
        elif states and states[0] != INITIAL_CASE_STATE:
            ledger.transition(
                case.id,
                states[0],
                at=now,
                actor=LIAISE_ACTOR,
                reason=f"adopted from the label {labels}",
            )
        reason = f"{pattern} matched; {decision.person} via {decision.via}"
        if adopted:
            reason += f"; {ADOPTED_REASON}"
        record(event, "opened", case_id=case.id, reason=reason)

    def take(event: Event, *, adopted: bool = False) -> None:
        message = event.message
        if message is None or event.delivery_id in taken:
            return
        if ledger.seen(event.delivery_id):
            record(event, "duplicate")
            return
        case = ledger.case_for_conversation(case_conversation(message))
        if case is not None and case.subject != subject.slug:
            reason = f"its case belongs to {case.subject}"
            record(event, "ignored", case_id=case.id, reason=reason)
            return
        pattern = None
        if case is None:
            pattern = next(
                (p for p in subject.bindings if binding_matches(p, message)), None
            )
            if pattern is None:
                record(event, "ignored", reason="matches no binding")
                return
            if message.native.get("state") == CLOSED_STATE:
                record(event, "ignored", reason="a closed issue opens no case")
                return
        ledger.mark_seen(
            event.delivery_id, channel=event.channel, kind=event.kind, at=now
        )
        if case is not None:
            append(case, event)
        else:
            open_case(pattern, event, adopted=adopted)

    def adopt(ref: str) -> None:
        conversation = parse_ref(ref, registry=registry)
        if (
            conversation.channel not in ADOPTING_CHANNELS
            or conversation.kind != ADOPTING_CONVERSATION_KIND
            or conversation.encoded in ledger.cursors
        ):
            return
        messages = read(ref, limit=ADOPTION_READ_LIMIT, registry=registry)
        if len(messages) >= ADOPTION_READ_LIMIT:
            problems.append(
                f"{where}: {conversation.encoded} has {ADOPTION_READ_LIMIT} or more open "
                f"issues; only the {ADOPTION_READ_LIMIT} most recent were read for adoption"
            )
        for message in messages:
            is_opening = message.id.startswith(OPENING_ID_PREFIX)
            if not is_opening or message.native.get("state") != OPEN_STATE:
                continue
            if any(binding_matches(pattern, message) for pattern in subject.bindings):
                event = Event(
                    kind=CREATED_EVENT_KIND,
                    channel=conversation.channel,
                    delivery_id=_opening_delivery_id(message),
                    message=message,
                )
                take(event, adopted=True)

    for ref in refs:
        try:
            adopt(ref)
            stream = listen(
                ref, cursors=ledger.cursors, registry=registry, commit=not dry_run
            )
            for event in stream:
                take(event)
        except CorrespondError as error:
            problems.append(f"{where}: polling {ref} failed: {error}")

    return IntakeReport(
        subject=subject.slug,
        events=tuple(events),
        new_cases=tuple(map(ledger.get_case, opened)),
        updated_cases=tuple(
            ledger.get_case(case_id) for case_id in touched if case_id not in opened
        ),
        unrouted=tuple(unrouted),
        problems=tuple(problems),
        dry_run=dry_run,
    )
