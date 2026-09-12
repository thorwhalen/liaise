"""Owner notification (A.7): one function posting to ntfy, and the one builder of what it says.

`liaise` has no owner-facing UI; ntfy (a plain HTTP POST) is the whole channel.
Silent when unconfigured, because a package should not require a notification
service just to run its tests or a first `liaise run --once --dry-run`.

**No case content goes to the notifier.** Anyone who knows an ntfy topic's name can read
it, so :func:`notice_body` is the only builder of a notification's body. It names the
subject, the case, the event and what liaise calls its cause (an error class, a gate
filter, an exit code), and points at ``liaise case show``. It never carries what an agent
wrote (a draft, a reason, a summary), a message, or a command's output: those stay on the
case, where the operator reads them on their own machine. :func:`notice_title` is the only
builder of a title, which goes out with the body: fixed text, the subject's slug and the
case ids, never a person's name or address, an issue's title or a report's subject.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from types import MappingProxyType
from typing import Optional

from liaise.config import DFLT_NTFY_TOPIC_ENV
from liaise.errors import ERROR_CLASSES
from liaise.model import require_one_of

#: M-4: this used to be a second, independent definition of the same default
#: env var name as config.py's — the two could drift silently. config.py is
#: the SSOT (it's what a partner/global config resolves against); this module
#: just uses it.
DFLT_TOPIC_ENV = DFLT_NTFY_TOPIC_ENV
DFLT_NTFY_BASE_URL = "https://ntfy.sh"

#: The events an operator notification is about, as its body names them.
NOTICE_ESCALATION = "escalation"
NOTICE_NO_CHANNEL = "no channel to reach the reporter"
NOTICE_DIVERTED = "message diverted by the gate"
NOTICE_SEND_FAILED = "send failed"
NOTICE_EFFECTS_HELD = "effects held"
NOTICE_DEPLOY_FAILED = "deploy failed"
NOTICE_DELIVERY_FAILED = "delivery raised an exception"
NOTICE_RUN_LOST = "run lost"
NOTICE_RUN_CANCELLED = "run cancelled for a hold"
NOTICE_ERROR = "error"
NOTICE_START_REFUSED = "start refused"
NOTICE_DAILY_CAP = "daily cap reached"
NOTICE_ISSUE_UNREADABLE = "issue state unreadable"
NOTICE_EVENTS = (
    NOTICE_ESCALATION,
    NOTICE_NO_CHANNEL,
    NOTICE_DIVERTED,
    NOTICE_SEND_FAILED,
    NOTICE_EFFECTS_HELD,
    NOTICE_DEPLOY_FAILED,
    NOTICE_DELIVERY_FAILED,
    NOTICE_RUN_LOST,
    NOTICE_RUN_CANCELLED,
    NOTICE_ERROR,
    NOTICE_START_REFUSED,
    NOTICE_DAILY_CAP,
    NOTICE_ISSUE_UNREADABLE,
)
#: Each event's notification title, in :func:`notice_title`'s fields: ``{cases}`` (the case
#: ids, else the subject's slug), ``{subject}`` (the slug) and, for an error alone,
#: ``{error_class}``. Nothing else ever fills a title.
NOTICE_TITLES = MappingProxyType(
    {
        NOTICE_ESCALATION: "{cases} needs you",
        NOTICE_NO_CHANNEL: "no channel to reach the reporter of {cases}",
        NOTICE_DIVERTED: "liaise: a draft for {cases} waits for you",
        NOTICE_SEND_FAILED: "liaise: a message for {cases} was not sent",
        NOTICE_EFFECTS_HELD: "liaise: {cases}'s effects are held",
        NOTICE_DEPLOY_FAILED: "liaise: {cases} landed but did not deploy",
        NOTICE_DELIVERY_FAILED: "liaise: delivering {cases} failed",
        NOTICE_RUN_LOST: "liaise: {cases} run lost",
        NOTICE_RUN_CANCELLED: "liaise: {cases}'s run was cancelled",
        NOTICE_ERROR: "liaise: {cases} {error_class}",
        NOTICE_START_REFUSED: "liaise: {cases} needs you before work starts",
        NOTICE_DAILY_CAP: "liaise: {subject} reached its daily cap",
        NOTICE_ISSUE_UNREADABLE: "liaise: {cases}'s issue cannot be read",
    }
)
#: Where a notification points the operator for what it leaves out: each case it names,
#: or the status when it names none.
CASE_SHOW_POINTER = "see liaise case show {case_id}"
STATUS_POINTER = "see liaise status"


def notice_title(
    event: str,
    *,
    subject: str,
    case_ids: Iterable[str] = (),
    cause: Optional[str] = None,
) -> str:
    """The title of an operator notification about ``event``: fixed text, the slug, the case ids.

    A title is pushed with its body, and is held to the body's rule and to a narrower one:
    it never names a person or an address, an issue's title or a report's subject, whatever
    the event. It is :data:`NOTICE_TITLES`'s text for ``event``, filled with the case ids,
    or the subject's slug without any. ``cause`` is the one :func:`notice_body` gets: a title
    names it only for :data:`NOTICE_ERROR`, where it must be one of
    :data:`liaise.errors.ERROR_CLASSES`, and ignores it for every other event.

    >>> notice_title(NOTICE_ERROR, subject="example-app", case_ids=["example-app-1"],
    ...     cause="timed_out")
    'liaise: example-app-1 timed_out'

    Raises ``ValueError`` for an event outside :data:`NOTICE_EVENTS`, and for an error whose
    cause is not an error class.
    """
    require_one_of(event, NOTICE_EVENTS, what="notice event")
    cases = ", ".join(dict.fromkeys(case_ids)) or subject
    fields = {"cases": cases, "subject": subject}
    if event == NOTICE_ERROR:
        fields["error_class"] = require_one_of(cause, ERROR_CLASSES, what="error class")
    return NOTICE_TITLES[event].format(**fields)


def notice_body(
    event: str,
    *,
    subject: str,
    case_ids: Iterable[str] = (),
    cause: Optional[str] = None,
) -> str:
    """The body of an operator notification about ``event``: nothing a case holds.

    A line each for the subject's slug, the case ids (a batch deploy has several), the
    event (one of :data:`NOTICE_EVENTS`) and its ``cause`` when there is one, then a
    :data:`CASE_SHOW_POINTER` per case, or :data:`STATUS_POINTER` without one. ``cause`` is
    a name liaise gives the cause: an error class, a gate filter, an exit code, a hold's
    scope kind. Callers never pass text an agent wrote, a message, or a command's output.

    >>> print(notice_body(NOTICE_ERROR, subject="example-app",
    ...     case_ids=["example-app-1"], cause="timed_out"))
    subject: example-app
    case: example-app-1
    event: error
    cause: timed_out
    see liaise case show example-app-1

    Raises ``ValueError`` for an event outside :data:`NOTICE_EVENTS`.
    """
    require_one_of(event, NOTICE_EVENTS, what="notice event")
    ids = list(dict.fromkeys(case_ids))
    lines = [f"subject: {subject}"]
    if ids:
        lines.append(f"case: {', '.join(ids)}")
    lines.append(f"event: {event}")
    if cause:
        lines.append(f"cause: {cause}")
    lines += [CASE_SHOW_POINTER.format(case_id=case_id) for case_id in ids] or [
        STATUS_POINTER
    ]
    return "\n".join(lines)


def notify(
    title: str,
    body: str,
    *,
    priority: str = "default",
    topic_env: str = DFLT_NTFY_TOPIC_ENV,
    base_url: str = DFLT_NTFY_BASE_URL,
) -> bool:
    """POST `body` to the ntfy topic named by the `topic_env` environment variable.

    Returns whether it actually sent. No-ops (returns False) when `topic_env`
    is unset. Never raises — a notification failure must not take down a run
    that otherwise succeeded. Build `body` with :func:`notice_body`.
    """
    topic = os.environ.get(topic_env)
    if not topic:
        return False
    try:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10):
            pass
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
