"""In-process delta sync of tracked source folders ([sync] in cymatix.toml).

``SyncWorker.run_pass`` walks the configured roots and keeps the knowledge
store in step with them: new files are ingested, changed files re-ingested
with their stale chunks tombstoned, and deleted files tombstoned. Tracking
state lives in the knowledge store itself (``sync_tracked``), so it travels
with the database.
"""

from .worker import PassReport, SyncWorker

__all__ = ["PassReport", "SyncWorker"]
