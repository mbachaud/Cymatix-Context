"""A sqlite3 connection that only one thread may be inside at a time.

Issue #505. A ``:memory:`` knowledge store has a single connection: a second
one would be a different database, so reads cannot move to per-thread readers
the way they do for file-backed stores. Threads sharing that connection hung
in two ways:

- two threads inside the connection at once (``query_docs`` on a request
  thread while the packet-trace fingerprint thread runs ``COUNT(*)``) never
  returned;
- two cursors running the same SQL share one cached prepared statement, so a
  second ``execute`` reset the first cursor's half-read result and its loop
  started over, indefinitely under enough threads.

So every call takes ``serial_lock``, and ``execute`` reads the whole result
under that lock: no statement stays active once the lock is released. Rows are
then served from the buffer. The lock is an RLock and the store uses the same
object as its write lock, so a writer that holds it across a multi-statement
transaction stays atomic and can re-enter.

File-backed stores do not use this class. Results are buffered in full, which
suits the small stores ``:memory:`` is used for.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import deque


class SerializedCursor(sqlite3.Cursor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buffered: deque = deque()

    def _run(self, method, args, kwargs):
        with self.connection.serial_lock:
            self._buffered = deque()
            method(*args, **kwargs)
            if self.description is not None:
                self._buffered = deque(super().fetchall())
        return self

    def execute(self, *args, **kwargs):
        return self._run(super().execute, args, kwargs)

    def executemany(self, *args, **kwargs):
        return self._run(super().executemany, args, kwargs)

    def executescript(self, *args, **kwargs):
        return self._run(super().executescript, args, kwargs)

    def fetchone(self):
        return self._buffered.popleft() if self._buffered else None

    def fetchmany(self, size=None):
        size = self.arraysize if size is None else size
        return [self._buffered.popleft() for _ in range(min(size, len(self._buffered)))]

    def fetchall(self):
        rows = list(self._buffered)
        self._buffered = deque()
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        if not self._buffered:
            raise StopIteration
        return self._buffered.popleft()

    def close(self):
        with self.connection.serial_lock:
            self._buffered = deque()
            return super().close()


class SerializedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        self.serial_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def cursor(self, factory=SerializedCursor):
        with self.serial_lock:
            return super().cursor(factory)

    # sqlite3.Connection.execute* build a plain Cursor in C, bypassing
    # cursor() above, so route them through the serialized cursor explicitly.
    def execute(self, *args, **kwargs):
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)

    def commit(self):
        with self.serial_lock:
            return super().commit()

    def rollback(self):
        with self.serial_lock:
            return super().rollback()

    def backup(self, *args, **kwargs):
        with self.serial_lock:
            return super().backup(*args, **kwargs)

    def create_function(self, *args, **kwargs):
        with self.serial_lock:
            return super().create_function(*args, **kwargs)

    def close(self):
        with self.serial_lock:
            return super().close()
