"""Owner notification (A.7): one function, posting to ntfy, never raising.

`liaise` has no owner-facing UI; ntfy (a plain HTTP POST) is the whole channel.
Silent when unconfigured, because a package should not require a notification
service just to run its tests or a first `liaise poll`.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

from liaise.config import DFLT_NTFY_TOPIC_ENV

#: M-4: this used to be a second, independent definition of the same default
#: env var name as config.py's — the two could drift silently. config.py is
#: the SSOT (it's what a partner/global config resolves against); this module
#: just uses it.
DFLT_TOPIC_ENV = DFLT_NTFY_TOPIC_ENV
DFLT_NTFY_BASE_URL = "https://ntfy.sh"


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
    that otherwise succeeded.
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
