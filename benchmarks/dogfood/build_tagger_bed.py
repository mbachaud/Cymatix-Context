"""build_tagger_bed.py — dogfood bed re-ingested through the LLM tagger.

Same curated walk, same exclusions, same repos as
``scripts/build_dogfood_genome.py`` — the ONLY variable is the ingest
tagger: chunks route through ``ribosome.encode`` (Ollama + gemma) instead
of the spaCy/heuristic CpuTagger. Target is a separate genome
(``genomes/taggerdogfood/genome.db``) so the clean CPU-tagged bed stays
untouched and the two can be baselined against each other.

Wiring note (same receipt caveat as run_expression_arms.py): since the
explicit-opt-in change, ``[ingestion] backend="ollama"`` is unreachable
from config alone — the coherence guard flips it to "cpu" while the
ribosome is disabled, and ribosome construction only honors litellm /
deberta. This wrapper patches ``open_session`` to wire the shipped
``OllamaBackend`` onto the session's manager and force
``ingestion.backend="ollama"`` after the guard has run.

The build script's per-50-file rate lines give a live ETA; per-file
failures (e.g. malformed tagger JSON) are tolerated and reported by the
underlying script.

Usage::

    python benchmarks/dogfood/build_tagger_bed.py                 # e4b, full
    python benchmarks/dogfood/build_tagger_bed.py --model gemma4:e2b
    python benchmarks/dogfood/build_tagger_bed.py --dry-run
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUT = "genomes/taggerdogfood/genome.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="gemma4:e4b")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import cymatix_context.api as api
    from cymatix_context.ribosome import OllamaBackend, Ribosome

    real_open_session = api.open_session

    def tagged_open_session(*a, **kw):
        sess = real_open_session(*a, **kw)
        mgr = sess._manager  # noqa: SLF001 - bench wrapper, documented above
        mgr.config.ribosome.enabled = True
        mgr.config.ingestion.backend = "ollama"  # post-guard, deliberate
        mgr.ribosome = Ribosome(
            backend=OllamaBackend(model=args.model, timeout=args.timeout,
                                  keep_alive="30m", warmup=True),
            encoder=mgr.encoder,
            splice_aggressiveness=mgr.config.budget.splice_aggressiveness,
        )
        print(f"[tagger-bed] session wired: ingestion.backend=ollama "
              f"model={args.model}", flush=True)
        return sess

    api.open_session = tagged_open_session

    spec = importlib.util.spec_from_file_location(
        "build_dogfood_genome", REPO_ROOT / "scripts/build_dogfood_genome.py")
    bdg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bdg)

    argv = ["build_dogfood_genome.py", "--out", args.out]
    if args.dry_run:
        argv.append("--dry-run")
    sys.argv = argv
    return bdg.main()


if __name__ == "__main__":
    raise SystemExit(main())
