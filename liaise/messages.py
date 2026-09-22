"""Messages outside a case: what an agent says to a person on its own initiative, through the gate.

A case is a unit of feedback work, with a reporter, a state machine, runs and a delivery. A
question an agent decides to ask has none of those, so it is not a case, and ``liaise
message send`` opens none (liaise #28). It is an :class:`~liaise.model.OutboundMessage` in
the ledger, judged by the same gate as every message liaise sends, with no case on the
gate's context.

- :func:`send_message` takes a GitHub issue, or a repository and a title to open an issue,
  and the subject from the bindings that take it in
  (:func:`liaise.subjects.subject_for_ref`), so the caller cannot pick a laxer policy. The
  gate judges it with that subject's policy and no case on its context, which in 0.1 means
  it is held for the operator: its sender chose where it goes, and only the operator's
  release lets such a message out (see :func:`liaise.gate.reply_mode`). The operator is
  told, without its text, when a subject's held queue stops being empty. A hold on the
  subject, the recipient, the repository or the checkout keeps it before the gate judges
  it. Every message is recorded.
- :func:`send_held_message` is how the operator sends a held one, through
  :func:`liaise.release.release_draft`, as a case's draft is sent.
- :func:`reject_message` records that the operator declined one, and why.
- :func:`message_lines` and :func:`message_show_lines` are ``liaise message list`` and
  ``liaise message show``.

A message has no label, so nothing here writes to a repository beyond the message itself.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional, Union

from correspond.errors import ERROR_KINDS

from liaise.cases import (
    DFLT_SHOW_ENTRIES,
    NONE_SHOWN,
    entry_line,
    gate_summary,
    held_lines,
)
from liaise.detect import visible
from liaise.gate import DFLT_OUTBOUND_FILTERS, GateContext, Outbound, OutboundFilter
from liaise.holds import DFLT_SET_BY, blocking_hold, scopes_for
from liaise.ledger import Ledger
from liaise.model import (
    MESSAGE_HELD,
    MESSAGE_REJECTED,
    MESSAGE_SENT,
    Approval,
    Hold,
    LedgerEntry,
    OutboundMessage,
    require_one_of,
)
from liaise.notify import NOTICE_MESSAGE_HELD, notice_body, notice_title, notify
from liaise.outcomes import DFLT_OPERATOR_PRIORITY, HELD_REASON_PREFIX, make_draft
from liaise.policy import Provenance
from liaise.release import (
    CASELESS_PROVENANCE,
    DRAFT_ENTRY_KIND,
    GITHUB_CHANNEL,
    DraftSentNotRecorded,
    SendAttempt,
    failure_reason,
    gate_and_send,
    github_repo,
    release_draft,
    sendable_ref,
)
from liaise.subjects import Subject, subject_for_ref

#: What a message outside a case may carry out: a question, a reply or a proposal.
MESSAGE_PURPOSES = ("ask", "reply", "propose")
DFLT_MESSAGE_PURPOSE = "ask"
#: Who a message is recorded as sent by when its sender does not say: an agent, never the
#: operator, whose word is what releases a held one.
DFLT_SENDER = "agent"
#: Who a held message is released or rejected by.
OPERATOR_ACTOR = DFLT_SET_BY
#: What a notification names a failed send by when the channel gave no error kind.
SEND_REFUSED_CAUSE = "refused"


@dataclass(frozen=True)
class MessageSent:
    """What :func:`send_message` did: the message as recorded, the gate's attempt, any hold.

    ``message`` is the record (as it would be, in a dry run). ``attempt`` is None when
    ``hold`` kept the message before the gate judged it, and ``filters`` is how many
    filters the gate had.
    """

    message: OutboundMessage
    attempt: Optional[SendAttempt]
    filters: int
    hold: Optional[Hold] = None

    @property
    def sent(self) -> bool:
        """Whether the message went out; in a dry run, whether it would have."""
        return self.message.state == MESSAGE_SENT


@dataclass(frozen=True)
class MessageRelease:
    """What :func:`send_held_message` did, as :class:`liaise.cases.DraftRelease` does for a case.

    ``draft`` is the held message as the operator saw it, ``attempt`` the gate's decision
    and the send, ``filters`` how many filters ran, ``edited`` whether the operator's text
    replaced the message's, and ``message`` the record as the release left it.
    ``approval`` is the approval the gate was given.
    """

    draft: Mapping[str, Any]
    attempt: SendAttempt
    filters: int
    edited: bool
    message: OutboundMessage
    approval: Optional[Approval] = None


def _no_message(message_id: str) -> str:
    return (
        f"no message {message_id!r} in the ledger; liaise message list shows the "
        f"messages there are"
    )


def _utc_now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def message_draft(message: OutboundMessage) -> dict[str, Any]:
    """``message`` as a held draft: the shape :func:`liaise.release.release_draft` releases."""
    return make_draft(
        at=message.updated_at,
        outcome=message.purpose,
        recipient=message.recipient,
        ref=message.ref,
        text=message.text,
        reason=message.reason or "",
        notes=message.notes,
        title=message.title,
        send_key=message.send_key or message.id,
    )


def find_message(ledger: Ledger, message_id: str) -> OutboundMessage:
    """The message ``message_id``. Raises ``ValueError`` for one the ledger does not hold."""
    message = ledger.get_message(message_id)
    if message is None:
        raise ValueError(_no_message(message_id))
    return message


def held_message(
    ledger: Ledger, message_id: str, *, verb: str = "send"
) -> OutboundMessage:
    """The held message ``message_id``. Raises ``ValueError`` for one missing or not held.

    ``verb`` says, in the error, what there is nothing to do.
    """
    message = find_message(ledger, message_id)
    if message.state != MESSAGE_HELD:
        raise ValueError(
            f"message {message_id} is {message.state}, not held: there is nothing to {verb}"
        )
    return message


def send_message(
    ledger: Ledger,
    subjects: Mapping[str, Subject],
    recipient: str,
    *,
    ref: str,
    text: str,
    title: Optional[str] = None,
    purpose: str = DFLT_MESSAGE_PURPOSE,
    by: str = DFLT_SENDER,
    now: Optional[datetime] = None,
    registry: Optional[Mapping[str, Any]] = None,
    notify_fn: Optional[Callable[..., Any]] = None,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
    fingerprint_key: Union[bytes, Callable[[], bytes], None] = None,
    sends: Optional[MutableMapping[str, Any]] = None,
) -> MessageSent:
    """Send ``text`` to ``recipient`` (a person id) at ``ref`` outside any case, or hold it.

    ``ref`` is a GitHub issue a subject binds (``github:example/app#12``), or a repository
    it binds with a ``title``, to open an issue there; it is kept as
    :func:`~liaise.release.sendable_ref` gives it. Its subject is the one
    :func:`~liaise.subjects.subject_for_ref` names. The message is judged by the gate
    (``outbound_filters``) with no case on the context, then sent through correspond on
    ``registry``, or held:

    - **Held**, because the gate diverted it (in 0.1 it always does, for the operator to
      release), its channel refused it, or a hold on the subject, the recipient, the
      repository or the checkout keeps effects waiting. It is recorded as held, with the
      reason and the text as given. When no other message of the subject was held yet,
      the operator is told through ``notify_fn`` (:func:`liaise.notify.notify` when None)
      that a message on the subject waits. The notification carries neither the text nor
      the recipient.
    - **Sent**, when a gate without that rule passes it: recorded as sent, with the text
      as it went out and its url.

    Each record's one entry is by ``by``, at ``now``, with the gate's audit record. The gate
    judges it with its provenance unknown (nobody can say what its sender read), and
    ``fingerprint_key`` is as :class:`~liaise.gate.GateContext` has it. A dry run judges
    and plans the same, and records and tells nothing. The send's idempotency key is the
    message's id, kept in ``sends`` (correspond's own store when None), and a held
    message keeps it, so its release never posts it twice.

    Raises ``ValueError``, sending and recording nothing, for any of these:

    - a purpose outside :data:`MESSAGE_PURPOSES`, or a blank recipient or text;
    - a ``ref`` that is not a GitHub issue or repository, or that no subject binds;
    - a title on an issue, or a repository with no title.

    Raises :class:`~liaise.release.DraftSentNotRecorded` when the message went out and
    the ledger failed to record it.
    """
    require_one_of(purpose, MESSAGE_PURPOSES, what="message purpose")
    if not recipient.strip():
        raise ValueError("a message needs a recipient: the id of the person it is for")
    if not text.strip():
        raise ValueError("a message needs text to send")
    ref, title = sendable_ref(ref), (title or "").strip() or None
    opens_an_issue = github_repo(ref) is not None and "#" not in ref
    if title and not opens_an_issue:
        raise ValueError(
            f"a title opens an issue, and {ref} is one already: leave the title out, or "
            f"give its repository"
        )
    if opens_an_issue and not title:
        raise ValueError(
            f"{ref} is a repository: a message there opens an issue, which needs a title"
        )
    subject = subject_for_ref(subjects, ref)
    message_id = ledger.new_message_id(subject.slug)
    channel = GITHUB_CHANNEL
    at = _utc_now(now)
    filters = tuple(outbound_filters)
    scopes = scopes_for(
        subject=subject.slug,
        person=recipient,
        repo=github_repo(ref),
        checkout=subject.workspace.path or None,
        processor=False,
    )
    hold = blocking_hold(ledger, scopes, for_="effect")
    detail: dict[str, Any] = {"purpose": purpose, "ref": ref, "recipient": recipient}
    attempt, state, recorded_text = None, MESSAGE_HELD, text
    if hold is not None:
        reason, notes = f"{HELD_REASON_PREFIX}{hold.scope}", ()
        detail.update(decision="hold", reason=reason)
        cause = hold.scope.partition(":")[0]
    else:
        outbound = Outbound(
            ref=ref,
            channel=channel,
            recipient=recipient,
            purpose=purpose,
            text=text,
            title=title,
        )
        context = GateContext(
            subject=subject,
            now=at,
            provenance=Provenance.unknown(CASELESS_PROVENANCE),
            fingerprint_key=fingerprint_key,
        )
        attempt = gate_and_send(
            outbound,
            context,
            registry=registry,
            dry_run=dry_run,
            outbound_filters=filters,
            idempotency_key=message_id,
            sends=sends,
        )
        decision, notes = attempt.decision, attempt.decision.notes
        detail["notes"] = list(notes)
        detail.update(decision.record())
        if attempt.sent:
            state, reason, cause = MESSAGE_SENT, None, None
            recorded_text, title = attempt.outbound.text, attempt.outbound.title
            detail.update(decision="send", url=getattr(attempt.result, "url", None))
        elif decision.send is None:
            reason, cause = decision.diverted, decision.diverted_by
            detail.update(decision="divert", reason=reason)
        else:
            reason = failure_reason(attempt, ref)
            known = attempt.failure_kind in ERROR_KINDS  # never an adapter's own words
            cause = attempt.failure_kind if known else SEND_REFUSED_CAUSE
            detail.update(decision="send", error=attempt.failure)
    tried = attempt.outbound if attempt is not None else None
    entry = LedgerEntry(
        at=at,
        kind=DRAFT_ENTRY_KIND,
        actor=by,
        text=tried.text if tried is not None else text,
        detail=detail,
    )
    message = OutboundMessage(
        id=message_id,
        subject=subject.slug,
        recipient=recipient,
        ref=ref,
        purpose=purpose,
        text=recorded_text,
        state=state,
        created_at=at,
        updated_at=at,
        title=title,
        reason=reason,
        notes=tuple(notes),
        entries=(entry,),
    )
    if not dry_run:
        try:
            waiting = next(
                ledger.messages(subject=subject.slug, state=MESSAGE_HELD), None
            )
        except (
            ValueError,
            TypeError,
            KeyError,
        ):  # an unreadable record: tell the operator
            waiting = None
        try:
            ledger.save_message(message)
        except Exception as error:
            if state != MESSAGE_SENT:
                raise
            label = f"the message to {recipient} on {ref}"
            raise DraftSentNotRecorded.after(
                label, attempt, error, reject=None
            ) from error
        if (
            state == MESSAGE_HELD and waiting is None
        ):  # once, when the queue stops being empty
            notice = dict(subject=subject.slug, cause=cause)
            (notify_fn or notify)(
                notice_title(NOTICE_MESSAGE_HELD, **notice),
                notice_body(NOTICE_MESSAGE_HELD, **notice),
                priority=DFLT_OPERATOR_PRIORITY,
            )
    return MessageSent(
        message=message, attempt=attempt, filters=len(filters), hold=hold
    )


def send_held_message(
    ledger: Ledger,
    subjects: Mapping[str, Subject],
    message_id: str,
    *,
    by: str,
    text: Optional[str] = None,
    title: Optional[str] = None,
    seen: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    registry: Optional[Mapping[str, Any]] = None,
    send: bool = True,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
    approval: Optional[Approval] = None,
    approve_shown: bool = False,
    justification: str = "",
    fingerprint_key: Union[bytes, Callable[[], bytes], None] = None,
    sends: Optional[MutableMapping[str, Any]] = None,
    new_attempt: bool = False,
) -> MessageRelease:
    """Send the held message ``message_id`` as ``by``, through the gate again.

    It goes out through :func:`liaise.release.release_draft` with ``by``'s approval on the
    context, as a case's draft does (:func:`liaise.cases.send_draft`), with ``text`` and
    ``title`` when the operator edited them. ``by`` has no default: this function asks no
    one, and ``liaise message send-draft`` passes the operator only after asking at a
    terminal. Sent, the message is recorded as sent. Diverted or refused,
    it stays held with the text that was judged and the new reason. Either way an entry
    by ``by`` records the attempt. ``seen`` is the message as the operator saw it
    (:func:`message_draft`): one that changed since is not sent. ``send=False``,
    ``dry_run``, ``approval`` (the dry run's, bound to what the operator was shown),
    ``approve_shown``, ``justification``, ``fingerprint_key``, ``sends`` and
    ``new_attempt`` are as :func:`~liaise.release.release_draft` has them: with neither an
    approval nor ``approve_shown``, nothing is settled and a held message stays held. The
    message keeps the idempotency key its release used.

    Raises ``ValueError``, sending and writing nothing, for a message the ledger does not
    hold, one that is not held, one whose subject is not in ``subjects``, one that changed
    since ``seen``, and anything :func:`~liaise.release.release_draft` refuses.
    """
    message = held_message(ledger, message_id, verb="send")
    subject = subjects.get(message.subject)
    if subject is None:
        raise ValueError(
            f"message {message_id} belongs to the subject {message.subject!r}, which is "
            f"not configured, so there is no policy to judge it by"
        )
    draft, label = message_draft(message), f"message {message_id}"
    if seen is not None and dict(draft) != dict(seen):
        raise ValueError(
            f"{label} changed while you had it open, so nothing was sent; read it again "
            f"with liaise message show {message_id}"
        )
    reject = f"liaise message reject-draft {message_id}"
    outcome = release_draft(
        draft,
        subject=subject,
        ledger=ledger,
        label=label,
        reject=reject,
        by=by,
        now=_utc_now(now),
        text=text,
        title=title,
        registry=registry,
        send=send,
        dry_run=dry_run,
        outbound_filters=outbound_filters,
        approval=approval,
        approve_shown=approve_shown,
        justification=justification,
        fingerprint_key=fingerprint_key,
        sends=sends,
        new_attempt=new_attempt,
    )
    release = functools.partial(
        MessageRelease,
        draft=draft,
        attempt=outcome.attempt,
        filters=outcome.filters,
        edited=outcome.edited,
        approval=outcome.approval,
    )
    if outcome.entry is None:
        return release(message=message)  # only a plan: nothing went out
    if outcome.kept is None:
        went = outcome.attempt.outbound
        after = replace(
            message,
            state=MESSAGE_SENT,
            text=went.text,
            title=went.title,
            reason=None,
            notes=tuple(outcome.attempt.decision.notes),
            send_key=outcome.entry.detail.get("send_key"),
        )
    else:
        kept = outcome.kept
        after = replace(
            message,
            text=kept["text"],
            title=kept.get("title"),
            reason=kept["reason"],
            notes=tuple(kept["notes"]),
            send_key=kept.get("send_key"),
        )
    after = after.with_entry(outcome.entry)
    if not dry_run:
        try:
            ledger.save_message(after)
        except Exception as error:
            if outcome.kept is not None:
                raise
            raise DraftSentNotRecorded.after(
                label, outcome.attempt, error, reject=reject
            ) from error
    return release(message=after)


def reject_message(
    ledger: Ledger,
    message_id: str,
    *,
    reason: str,
    by: str = OPERATOR_ACTOR,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> OutboundMessage:
    """Decline the held message ``message_id`` as ``by``, recording ``reason``; return it.

    Nothing is sent. The message is recorded as rejected, with an entry keeping its text,
    where it would have gone, why it was held and ``reason``. A dry run writes nothing.

    Raises ``ValueError``, writing nothing, for a blank ``reason``, a message the ledger
    does not hold, and one that is not held.
    """
    if not reason.strip():
        raise ValueError(
            "a rejected message needs a reason, so the ledger says why it was not sent "
            "(--reason)"
        )
    message = held_message(ledger, message_id, verb="reject")
    entry = LedgerEntry(
        at=_utc_now(now),
        kind=DRAFT_ENTRY_KIND,
        actor=by,
        text=message.text,
        detail={
            "decision": "reject",
            "reason": reason.strip(),
            "purpose": message.purpose,
            "ref": message.ref,
            "held_for": message.reason,
        },
    )
    after = replace(message, state=MESSAGE_REJECTED).with_entry(entry)
    if not dry_run:
        ledger.save_message(after)
    return after


def message_lines(
    store: MutableMapping[str, Any], *, state: Optional[str] = None
) -> list[str]:
    """What ``liaise message list`` prints: ``<id>\\t<state>\\t<purpose> to <person> on <ref>``.

    Every message outside a case in the ledger ``store``, or only those in ``state``,
    oldest first. Reads only. Raises ``ValueError`` for a state outside
    :data:`~liaise.model.MESSAGE_STATES`.
    """
    found = sorted(
        Ledger(store).messages(state=state), key=lambda m: (m.created_at, m.id)
    )
    if not found:
        return [f"(no {state} messages)" if state else "(no messages)"]
    return [
        f"{m.id}\t{m.state}\t{m.purpose} to {m.recipient} on {m.ref}" for m in found
    ]


def message_show_lines(
    store: MutableMapping[str, Any],
    message_id: str,
    *,
    entries: int = DFLT_SHOW_ENTRIES,
) -> list[str]:
    """What ``liaise message show`` prints: the message ``message_id``, with its text.

    Its subject, state, recipient, reference, purpose and title; why it is held, with the
    gate's notes; the gate's last flow and the audience in words, its whole text with
    invisible characters made visible, and every link in full
    (:func:`liaise.cases.held_lines`); and its ``entries`` latest entries. Reads only.
    Raises ``ValueError`` for a message the ledger ``store`` does not hold.
    """
    message = find_message(Ledger(store), message_id)
    judged = next(
        filter(
            None,
            (
                gate_summary(entry.detail)
                for entry in reversed(message.entries)
                if entry.kind == DRAFT_ENTRY_KIND
            ),
        ),
        None,
    )
    lines = [
        f"message: {message.id}",
        f"  subject: {message.subject}",
        f"  state: {message.state}",
        f"  recipient: {message.recipient}",
        f"  ref: {message.ref}",
        f"  purpose: {message.purpose}",
        f"  title: {visible(message.title) if message.title else NONE_SHOWN}",
        f"  created: {message.created_at.isoformat(timespec='seconds')}",
        f"  updated: {message.updated_at.isoformat(timespec='seconds')}",
        f"held for: {message.reason or NONE_SHOWN}",
        *(f"  note: {note}" for note in message.notes),
        "text:",
        *held_lines(message.text, gate=judged),
    ]
    shown = message.entries[-entries:] if entries > 0 else ()
    lines.append(f"latest entries: {len(shown)} of {len(message.entries)}")
    lines += [f"  {entry_line(entry)}" for entry in shown]
    return lines
