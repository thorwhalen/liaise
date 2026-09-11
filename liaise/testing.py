"""Fakes shipped with liaise: for its tests, and for the one-command smoke test.

:class:`FakeGitHubChannel` is a correspond channel adapter held in memory. Its references,
messages, delivery ids and events have the shapes correspond's GitHub adapter gives them,
so :func:`liaise.intake.intake` cannot tell the two apart, and nothing it does leaves the
process. :func:`add_webinbox_report` stores a report in correspond's own ``WebInbox``
exactly as its collector stores one. :func:`demo_registry` puts the two together as the
channel registry ``correspond.listen`` and ``correspond.send`` take.

Every value here is fictional: the partner ``pat``, the repository ``example/app``, the
site ``example-site`` and the relay ``example-bot``.

>>> import correspond
>>> github = FakeGitHubChannel()
>>> _ = github.add_issue(
...     "example/app", 12, author="pat", title="Export", body="The export drops a row.",
...     labels=["partner:pat"], created_at="2026-09-11T09:00:00Z",
... )
>>> events = correspond.listen(
...     "github:example/app", cursors={}, registry=demo_registry(github=github)
... )
>>> [(e.delivery_id, e.message.native["labels"]) for e in events]
[('github:example/app:issue-12@2026-09-11T09:00:00Z', ['partner:pat'])]
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from correspond.channels.github import MAX_BODY_CHARS, MAX_TITLE_CHARS, REF_RE
from correspond.channels.webinbox import Site, WebInbox, sign_identity, verify_identity
from correspond.errors import ChannelError, InvalidRef, NotSupported
from correspond.model import (
    Account,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    SendResult,
    Support,
    format_time,
    parse_time,
)
from correspond.ops import window, with_final_cursor

from liaise.github import FakeGitHub

#: The name a fake GitHub channel registers under: correspond's own adapter's.
DFLT_GITHUB_CHANNEL = "github"
#: The login a fake GitHub channel's own sends go out under (``FakeGitHub``'s, too).
SELF_LOGIN = FakeGitHub.SELF_AUTHOR
#: What the fake web inbox's host application signs its logged-in users with.
DFLT_SITE_SECRET = "example-site-secret"
#: The ``native`` fields an issue opening carries. Comments carry none.
NATIVE_FIELDS = ("number", "title", "labels", "state")
#: The states an issue is in, as GitHub names them (see :meth:`FakeGitHubChannel.set_state`).
ISSUE_STATES = ("open", "closed")

_URL_ROOT = "https://github.com"
_OPENING, _COMMENT = "issue", "issuecomment"
_CURSOR_PREFIX = "seq-"
_EVIDENCE = {"attested_by": "liaise.testing, in memory"}
#: How many hex digits of a digest end a seeded report's id, as the collector's random ones do.
_REPORT_SUFFIX_DIGITS = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class _Post:
    """A seeded or sent post: its place in arrival order, how its delivery id ends, and when it changed."""

    seq: int
    repo: str
    number: int
    key: str
    message: Message
    changed_at: datetime


class FakeGitHubChannel:
    """GitHub issues held in memory, as a correspond channel that reads, listens and sends.

    Seed it with :meth:`add_issue` and :meth:`add_comment`; :meth:`poll` yields what was
    added after its cursor, in the order it was added. A real send is recorded in ``sent``
    as ``(ref, draft)`` and posted as the channel's own (``is_self``) at ``clock()``, so
    the next poll yields it, as GitHub's would. Set ``send_error`` to a
    ``correspond.ChannelError`` and every real send raises it, which ``correspond.send``
    turns into a failed result.

    ``lookback`` makes a first poll (one without a cursor) skip what last changed more
    than that long before ``clock()``, as correspond's GitHub adapter looks back only
    ``LISTEN_LOOKBACK``. With None, the default, a first poll yields everything.
    """

    def __init__(
        self,
        *,
        name: str = DFLT_GITHUB_CHANNEL,
        lookback: Optional[timedelta] = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.name = name
        self.lookback = lookback
        self.clock = clock
        self.sent: list[tuple[ConversationRef, Draft]] = []
        self.send_error: Optional[ChannelError] = None
        self._posts: list[_Post] = []
        self._openings: dict[tuple[str, int], Message] = {}
        self._comment_ids = itertools.count(1)

    @property
    def capabilities(self) -> Capabilities:
        """Read, listen and send on issues, graded ``platform``, as GitHub's are."""
        return Capabilities(
            channel=self.name,
            read=Support.FULL,
            listen=Support.FULL,
            send=Support.FULL,
            initiate=Support.FULL,
            reply=Support.PARTIAL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.PLATFORM,),
            max_text_length=MAX_BODY_CHARS,
            max_title_length=MAX_TITLE_CHARS,
            formats=("markdown",),
            native_fields=NATIVE_FIELDS,
            notes=(
                "in memory: nothing leaves the process",
                "reply_to is refused on issues, whose comments are flat, as on GitHub",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """``owner/repo`` or ``owner/repo#N``, lower-cased, as correspond's GitHub adapter parses them."""
        match = REF_RE.match(id or "")
        if not match:
            raise InvalidRef(
                f"{self.name} references are {self.name}:owner/repo or "
                f"{self.name}:owner/repo#number, not {self.name}:{id}"
            )
        repository = ConversationRef(
            channel=self.name,
            id=f"{match['owner']}/{match['repo']}".lower(),
            kind="repository",
        )
        if match["number"] is None:
            return repository
        return ConversationRef(
            channel=self.name,
            id=f"{repository.id}#{int(match['number'])}",
            parent=repository,
        )

    # ---- seeding ----

    def add_issue(
        self,
        repo: str,
        number: int,
        *,
        author: str,
        title: str,
        body: str,
        labels: Sequence[str] = (),
        created_at: Union[datetime, str],
        grade: Union[Grade, str] = Grade.PLATFORM,
        state: str = "open",
        is_self: bool = False,
    ) -> Message:
        """Seed the opening post of issue ``repo#number``; the next poll yields it.

        The message carries ``native`` ``number``, ``title``, ``labels`` and ``state``.
        Raises ``ValueError`` for an issue already seeded.
        """
        repository = self._repository(repo)
        if (repository.id, number) in self._openings:
            raise ValueError(f"{self.name}:{repository.id}#{number} is already seeded")
        created = parse_time(created_at)
        message = Message(
            id=f"{_OPENING}-{number}",
            conversation=ConversationRef(
                channel=self.name,
                id=f"{repository.id}#{number}",
                kind="issue",
                parent=repository,
            ),
            author=self._identity(author, is_self=is_self),
            authenticity=Authenticity(grade=grade, evidence=_EVIDENCE),
            sent_at=created,
            text=body,
            body=body,
            body_format="markdown",
            url=f"{_URL_ROOT}/{repository.id}/issues/{number}",
            native={
                "number": number,
                "title": title,
                "labels": list(labels),
                "state": state,
            },
        )
        self._openings[(repository.id, number)] = message
        self._post(repository.id, number, message, changed_at=created)
        return message

    def add_comment(
        self,
        repo: str,
        number: int,
        *,
        author: str,
        body: str,
        created_at: Union[datetime, str],
        edited_at: Optional[Union[datetime, str]] = None,
        is_self: bool = False,
    ) -> Message:
        """Seed a comment on the seeded issue ``repo#number``; the next poll yields it.

        ``edited_at``, when the comment was edited, becomes its ``edited_at``, and its
        delivery id ends with it, as correspond's GitHub adapter ends one with the
        comment's ``updated_at``. Raises ``ValueError`` when that issue was never seeded.
        """
        repository = self._repository(repo)
        opening = self._openings.get((repository.id, number))
        if opening is None:
            raise ValueError(
                f"no issue {self.name}:{repository.id}#{number} to comment on; "
                f"seed it with add_issue first"
            )
        comment_id = next(self._comment_ids)
        created = parse_time(created_at)
        edited = parse_time(edited_at) if edited_at is not None else None
        message = Message(
            id=f"{_COMMENT}-{comment_id}",
            conversation=ConversationRef(
                channel=self.name, id=opening.conversation.id, parent=repository
            ),
            author=self._identity(author, is_self=is_self),
            authenticity=Authenticity(grade=Grade.PLATFORM, evidence=_EVIDENCE),
            sent_at=created,
            edited_at=edited,
            text=body,
            body=body,
            body_format="markdown",
            url=f"{opening.url}#{_COMMENT}-{comment_id}",
        )
        self._post(repository.id, number, message, changed_at=edited or created)
        return message

    def set_state(self, repo: str, number: int, state: str) -> Message:
        """Close or reopen the seeded issue ``repo#number``, as someone on GitHub would.

        ``state`` is one of :data:`ISSUE_STATES`. The opening's ``native["state"]``
        changes, so a read of the issue, or of the repository's open issues, sees it. A
        poll yields nothing for it, as correspond's GitHub adapter hears an older issue's
        changes only through its comments. Returns the opening as it is now; raises
        ``ValueError`` for an unknown state or an issue never seeded.
        """
        if state not in ISSUE_STATES:
            raise ValueError(
                f"issue state {state!r} is not one of: {', '.join(ISSUE_STATES)}"
            )
        repository = self._repository(repo)
        key = (repository.id, number)
        opening = self._openings.get(key)
        if opening is None:
            raise ValueError(
                f"no issue {self.name}:{repository.id}#{number} to set the state of; "
                f"seed it with add_issue first"
            )
        changed = replace(opening, native={**opening.native, "state": state})
        self._openings[key] = changed
        self._posts = [
            replace(post, message=changed) if post.message is opening else post
            for post in self._posts
        ]
        return changed

    # ---- reading and listening ----

    def read(
        self,
        ref: ConversationRef,
        *,
        since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[Message]:
        """An issue's opening and comments; for a repository, its open issues' openings."""
        repo, _, number = ref.id.partition("#")
        if not number:
            messages = [
                message
                for (owner_repo, _), message in self._openings.items()
                if owner_repo == repo and message.native["state"] == "open"
            ]
        elif (repo, int(number)) not in self._openings:
            raise self._not_found(repo, number)
        else:
            messages = [
                post.message
                for post in self._posts
                if post.repo == repo and post.number == int(number)
            ]
        return window(messages, since=since, limit=limit)

    def poll(
        self,
        ref: ConversationRef,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Event]:
        """Events for what was added after ``cursor`` (opaque), in the order it was added.

        A repository's poll yields its issue openings and comments; an issue's poll only
        that issue's comments, as correspond's GitHub adapter does. Delivery ids are
        ``<name>:owner/repo:issue-<N>@<created_at>`` and
        ``<name>:owner/repo:issuecomment-<id>@<updated_at>``. A first poll with a
        ``lookback`` yields only what changed within it.
        """
        after = self._position(cursor)
        horizon = (
            self.clock() - self.lookback
            if self.lookback is not None and not cursor
            else None
        )
        repo, _, number = ref.id.partition("#")
        posts = [
            post
            for post in self._posts
            if post.seq > after
            and (horizon is None or post.changed_at >= horizon)
            and post.repo == repo
            and (
                not number
                or (
                    post.number == int(number)
                    and post.message.id.startswith(f"{_COMMENT}-")
                )
            )
        ]
        limited = bool(limit) and len(posts) > limit
        if limit:
            posts = posts[:limit]
        events = [
            Event(
                kind="message.created",
                channel=self.name,
                delivery_id=f"{self.name}:{post.repo}:{post.key}",
                cursor=f"{_CURSOR_PREFIX}{post.seq}",
                message=post.message,
            )
            for post in posts
        ]
        latest = max([after, *(post.seq for post in self._posts)])
        final = None if limited else f"{_CURSOR_PREFIX}{latest}"
        return with_final_cursor(events, final)

    # ---- writing ----

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Comment on an issue; on a repository, open an issue (``title`` required).

        A dry run returns the plan and records nothing.
        """
        repo, _, number = ref.id.partition("#")
        if not number:
            return self._open_issue(ref, repo, draft, dry_run=dry_run)
        if draft.title:
            raise ChannelError(
                "a comment has no title; leave it out", kind="validation"
            )
        plan = {
            "action": "comment",
            "target": f"{repo}#{number}",
            "request": f"POST repos/{repo}/issues/{number}/comments",
            "reply_to": draft.reply_to,
            "body": draft.text,
        }
        if dry_run:
            return self._planned(ref, plan)
        self._raise_send_error()
        if (repo, int(number)) not in self._openings:
            raise self._not_found(repo, number)
        if draft.reply_to:
            raise NotSupported(
                "reply",
                self.name,
                alternatives=(
                    "issue and pull request comments are flat: quote the message you answer",
                ),
            )
        message = self.add_comment(
            repo,
            int(number),
            author=SELF_LOGIN,
            body=draft.text,
            created_at=self.clock(),
            is_self=True,
        )
        return self._sent(ref, draft, message, plan)

    def _open_issue(
        self, ref: ConversationRef, repo: str, draft: Draft, *, dry_run: bool
    ) -> SendResult:
        if not (draft.title or "").strip():
            raise ChannelError(
                f"opening an issue in {repo} needs a title", kind="validation"
            )
        if draft.reply_to:
            raise NotSupported(
                "reply",
                self.name,
                alternatives=(f"comment on the issue: {self.name}:owner/repo#N",),
            )
        plan = {
            "action": "open an issue",
            "request": f"POST repos/{repo}/issues",
            "title": draft.title,
            "body": draft.text,
        }
        if dry_run:
            return self._planned(ref, plan)
        self._raise_send_error()
        number = 1 + max((n for (r, n) in self._openings if r == repo), default=0)
        message = self.add_issue(
            repo,
            number,
            author=SELF_LOGIN,
            title=draft.title,
            body=draft.text,
            created_at=self.clock(),
            is_self=True,
        )
        return self._sent(
            ref, draft, message, plan, conversation=message.conversation.encoded
        )

    # ---- plumbing ----

    def _repository(self, repo: str) -> ConversationRef:
        ref = self.parse_ref(repo)
        if ref.parent is not None:
            raise ValueError(
                f"name the repository as owner/repo, not {repo!r}: the issue number "
                f"is an argument of its own"
            )
        return ref

    def _identity(self, login: str, *, is_self: bool) -> ChannelIdentity:
        return ChannelIdentity(
            channel=self.name,
            native_id=login,
            handle=login,
            is_bot=login.endswith("[bot]"),
            is_self=is_self,
        )

    def _post(
        self, repo: str, number: int, message: Message, *, changed_at: datetime
    ) -> None:
        key = f"{message.id}@{format_time(changed_at)}"
        post = _Post(len(self._posts) + 1, repo, number, key, message, changed_at)
        self._posts.append(post)

    def _position(self, cursor: Optional[str]) -> int:
        if not cursor:
            return 0
        digits = cursor.removeprefix(_CURSOR_PREFIX)
        if digits == cursor or not digits.isdigit():
            raise ChannelError(
                f"{cursor!r} is not a {self.name} listen cursor", kind="validation"
            )
        return int(digits)

    def _raise_send_error(self) -> None:
        if self.send_error is not None:
            raise self.send_error

    @staticmethod
    def _not_found(repo: str, number: Union[int, str]) -> ChannelError:
        return ChannelError(
            f"no issue, pull request or discussion #{number} in {repo} that the gh "
            f"account can see",
            kind="not_found",
        )

    def _planned(self, ref: ConversationRef, plan: Mapping[str, Any]) -> SendResult:
        return SendResult(
            ok=True,
            channel=self.name,
            conversation=ref.encoded,
            operation="send",
            dry_run=True,
            plan=plan,
        )

    def _sent(
        self,
        ref: ConversationRef,
        draft: Draft,
        message: Message,
        plan: Mapping[str, Any],
        *,
        conversation: Optional[str] = None,
    ) -> SendResult:
        self.sent.append((ref, draft))
        return SendResult(
            ok=True,
            channel=self.name,
            conversation=conversation or ref.encoded,
            operation="send",
            message_id=message.id,
            url=message.url,
            account=Account(channel=self.name, id=SELF_LOGIN, acts_as="user"),
            plan=plan,
        )


def demo_registry(
    *,
    github: Optional[FakeGitHubChannel] = None,
    webinbox: Optional[WebInbox] = None,
) -> dict[str, Any]:
    """A correspond channel registry of fakes: ``{"github": ..., "webinbox": ...}``.

    ``github`` defaults to an empty :class:`FakeGitHubChannel`, ``webinbox`` to
    correspond's ``WebInbox(store={}, blobs={})``: both in memory.
    """
    return {
        "github": FakeGitHubChannel() if github is None else github,
        "webinbox": WebInbox(store={}, blobs={}) if webinbox is None else webinbox,
    }


def add_webinbox_report(
    inbox: WebInbox,
    site: str,
    *,
    text: str,
    received_at: Union[datetime, str],
    user: Optional[str] = None,
    name: Optional[str] = None,
    email: Optional[str] = None,
    page: Optional[str] = None,
    context: Optional[Mapping[str, Any]] = None,
    secret: str = DFLT_SITE_SECRET,
) -> dict[str, Any]:
    """Store a report for ``site`` in ``inbox`` exactly as correspond's collector stores one.

    With ``user``, the report carries an identity the site's host application signed with
    ``secret`` (correspond's ``sign_identity``), checked as the collector checks it, so it
    is ``bound`` and its author's address is ``webinbox:<user>``. Without ``user`` it is
    ``claimed``, and its author is only the ``name`` typed. The report's id starts with
    ``received_at``, so reports poll in time order; its event's delivery id is
    ``webinbox:<site>:<id>``. Returns the stored record.
    """
    site_config = Site(name=site, secret=secret)  # refuses a malformed site name
    received = parse_time(received_at).astimezone(timezone.utc)
    moment = received.timestamp()
    if user is not None:
        identity = sign_identity(
            secret, site, user, name=name, email=email, issued_at=moment
        )
        authenticity = verify_identity(identity, site=site_config, now=moment)
        author = ChannelIdentity(
            channel=WebInbox.name, native_id=user, display_name=name or None
        )
        contact = {"name": name, "email": email, "signed": True}
    else:
        authenticity = Authenticity(
            grade=Grade.CLAIMED, evidence={"reason": "no signed identity"}
        )
        author = ChannelIdentity(channel=WebInbox.name, native_id="", display_name=name)
        contact = {"name": name, "email": email, "signed": False}
    stamp = format_time(received)
    digest = hashlib.sha1(f"{site}\n{stamp}\n{text}".encode()).hexdigest()
    record = {
        "id": f"{received:%Y%m%dT%H%M%S%fZ}-{digest[:_REPORT_SUFFIX_DIGITS]}",
        "site": site,
        "received_at": stamp,
        "text": text,
        "page": page,
        "context": None if context is None else dict(context),
        "contact": contact,
        "author": author.to_dict(),
        "authenticity": authenticity.to_dict(),
        "attachments": [],
    }
    inbox.store[f"{site}/{record['id']}.json"] = record
    return record
