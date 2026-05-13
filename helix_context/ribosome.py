"""Backward-compat shim -- canonical module at helix_context.backends.compressor."""
from helix_context.backends import compressor as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
