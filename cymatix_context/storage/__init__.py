"""cymatix_context.storage — KnowledgeStore split into focused modules.

Re-exports ``KnowledgeStore`` and ``Genome`` (legacy alias) so that::

    from cymatix_context.storage import KnowledgeStore

works identically to::

    from cymatix_context.knowledge_store import KnowledgeStore

Sub-modules:
    ddl            — schema creation, migrations, indexes
    co_activation  — co-activation graph updates + queries
    indexes        — promoter_index, path_key_index, filename_index sync
"""

from ..knowledge_store import KnowledgeStore, Genome

__all__ = ["KnowledgeStore", "Genome"]
