"""
Helix error types — every error has a fallback, the pipeline never crashes.

Biology → Error map:
    CodonAlignmentError  — chunker produces un-processable fragment
    PromoterMismatch     — query activates zero genes
    FoldingError         — ribosome JSON output unparseable
    TranscriptionError   — ribosome model call fails entirely
    GenomeFullError      — storage limit hit (future)
"""


class HelixError(Exception):
    """Base class for all Helix errors."""


class CodonAlignmentError(HelixError):
    """Chunker produced a fragment that can't be processed."""


class PromoterMismatch(HelixError):
    """Query matched zero genes in the genome."""


class FoldingError(HelixError):
    """Ribosome returned unparseable JSON."""


class TranscriptionError(HelixError):
    """Ribosome model call failed entirely (network, OOM, etc.)."""


class GenomeFullError(HelixError):
    """Genome storage limit reached."""


class ConfigError(HelixError):
    """helix.toml is structurally invalid.

    Raised by the Stage 4 loader when ``[abstain].mode = "per_classifier"``
    is set but a required per-class block is missing. See
    ``docs/specs/2026-05-08-stage-4-threshold-calibration.md`` §6.
    """
