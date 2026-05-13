"""Telemetry sub-package.

Re-exports everything from otel.py (the former helix_context.telemetry module)
so that ``from helix_context.telemetry import setup_telemetry`` keeps working.
Also exports metrics.py as a sibling module.
"""
from . import otel as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
