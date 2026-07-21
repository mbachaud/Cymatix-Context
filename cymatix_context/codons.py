"""Backward-compat shim -- canonical module at cymatix_context.encoding.fragments."""
from cymatix_context.encoding import fragments as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
