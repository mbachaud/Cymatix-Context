"""
Cymatix Context — KnowledgeStore-based context compression for local LLMs.

Makes 9k tokens of context window feel like 600k by treating
context like a knowledge store instead of a flat text buffer.
"""

import os as _os

# Single source of truth is pyproject.toml; tests/test_version.py pins
# this string to it so the two can't drift.
__version__ = "0.8.0"


def _mirror_env() -> None:
    """Accept CYMATIX_* env vars while internal reads still use HELIX_*.

    CYMATIX_* is the canonical user-facing prefix as of the 0.8.0 rename;
    each CYMATIX_X is mirrored to HELIX_X unless HELIX_X is already set
    (an explicit old-name setting wins, so existing deployments are
    untouched). Internal call sites migrate to CYMATIX_* reads in a
    follow-up PR, after this PR has survived its rebases.
    """
    for _k, _v in list(_os.environ.items()):
        if _k.startswith("CYMATIX_"):
            _os.environ.setdefault("HELIX_" + _k[len("CYMATIX_"):], _v)


_mirror_env()

# GB10 / Grace+Blackwell (aarch64, sm_121) platform handshake — default-OFF.
# When HELIX_CUDA_LAUNCH_BLOCKING=1, force synchronous CUDA launches BEFORE any
# torch/CUDA import to dodge the sm_121 async-dispatch livelock (see
# docs/hardware/grace-blackwell.md). Byte-identical for everyone who leaves it
# unset; never overrides an operator-exported CUDA_LAUNCH_BLOCKING.
import os
if os.environ.get("HELIX_CUDA_LAUNCH_BLOCKING", "0") == "1":
    os.environ.setdefault("CUDA_LAUNCH_BLOCKING", "1")

from .accel import accel_info, JSON_BACKEND
from .config import HelixConfig, load_config
from .schemas import Gene, ContextWindow, ContextHealth, ChromatinState, PromoterTags, EpigeneticMarkers
from .genome import Genome
from .ribosome import Ribosome, OllamaBackend
from .codons import CodonChunker, CodonEncoder, RawStrand, Codon
from .context_manager import HelixContextManager
from .exceptions import (
    HelixError,
    CodonAlignmentError,
    PromoterMismatch,
    FoldingError,
    TranscriptionError,
    GenomeFullError,
)

# CpuTagger is optional (requires spacy)
try:
    from .tagger import CpuTagger
except ImportError:
    CpuTagger = None

from .replication import ReplicationManager

# ΣĒMA is optional (requires sentence-transformers)
try:
    from .backends.sema import SemaCodec, SemaPrime, PRIMES, PRIME_COUNT
except ImportError:
    SemaCodec = None
    SemaPrime = None
    PRIMES = None
    PRIME_COUNT = None


def create_app(*args, **kwargs):
    """Lazy server import so package import has no HTTP/app side effects."""
    from .server import create_app as _create_app

    return _create_app(*args, **kwargs)

__all__ = [
    "__version__",
    "accel_info",
    "JSON_BACKEND",
    "HelixConfig",
    "load_config",
    "Gene",
    "ContextWindow",
    "ContextHealth",
    "ChromatinState",
    "PromoterTags",
    "EpigeneticMarkers",
    "Genome",
    "Ribosome",
    "OllamaBackend",
    "CodonChunker",
    "CodonEncoder",
    "RawStrand",
    "Codon",
    "HelixContextManager",
    "create_app",
    "HelixError",
    "CodonAlignmentError",
    "PromoterMismatch",
    "FoldingError",
    "TranscriptionError",
    "GenomeFullError",
    "SemaCodec",
    "SemaPrime",
    "PRIMES",
    "PRIME_COUNT",
    "CpuTagger",
]
