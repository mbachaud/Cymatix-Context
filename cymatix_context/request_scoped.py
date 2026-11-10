"""Per-thread view of the "last query" values a store publishes.

``query_docs`` reports its score maps by assigning attributes on the store
(``last_query_scores``, ``last_tier_contributions``, ...). The caller reads
them back after the call returns. With several request threads on one store,
another thread's query can assign in between, and the caller then pairs its
own candidates with foreign scores.

``LastQueryValue`` keeps the attribute's meaning (the most recent publication
by any thread, which diagnostics read) and also remembers each thread's own
most recent assignment. ``own_last`` reads that: the value this thread
published, which is the one that belongs to the query it just ran.
"""

from __future__ import annotations

import threading
from typing import Any

_OWN = "_request_scoped_own"


class LastQueryValue:
    """Attribute that is global on read and also recorded per thread on write."""

    def __set_name__(self, owner, name: str) -> None:
        self._name = name
        self._slot = "_shared_" + name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        try:
            return obj.__dict__[self._slot]
        except KeyError:
            raise AttributeError(self._name) from None

    def __set__(self, obj, value) -> None:
        obj.__dict__[self._slot] = value
        own = obj.__dict__.get(_OWN)
        if own is None:
            own = obj.__dict__.setdefault(_OWN, threading.local())
        setattr(own, self._name, value)


def own_last(obj: Any, name: str, default: Any = None) -> Any:
    """This thread's last publication of ``name`` on ``obj``.

    Falls back to the plain attribute when this thread has published nothing
    (or ``obj`` does not use ``LastQueryValue`` at all, e.g. a test fake).
    """
    own = getattr(obj, "__dict__", {}).get(_OWN)
    if own is not None:
        try:
            return getattr(own, name)
        except AttributeError:
            pass
    return getattr(obj, name, default)
