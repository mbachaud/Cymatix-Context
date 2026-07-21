"""Backward-compat namespace: ``helix_context`` -> ``cymatix_context``.

The project was renamed to cymatix-context (July 2026). Every
``helix_context[.sub]`` import resolves to the *identical*
``cymatix_context`` module object — no copies — so isinstance checks and
module singletons keep working across old and new import paths. This
package will be removed after a deprecation window.
"""
import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_OLD = "helix_context"
_NEW = "cymatix_context"
# Real files shipped in this shim dir (needed for ``python -m``): let the
# normal path finder handle them instead of aliasing.
_REAL_FILES = {f"{_OLD}.mcp_server"}

warnings.warn(
    "'helix_context' has been renamed to 'cymatix_context'; the old import "
    "path will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real_name, real_spec):
        self._real_name = real_name
        self._real_spec = real_spec
        self._saved = None

    def create_module(self, spec):
        real = importlib.import_module(self._real_name)
        # module_from_spec will stamp the alias spec onto this shared
        # object; stash the canonical identity so exec_module can restore it.
        self._saved = (
            real.__name__,
            real.__spec__,
            real.__package__,
            getattr(real, "__loader__", None),
        )
        sys.modules[spec.name] = real
        return real

    def exec_module(self, module):
        name, spec, package, loader = self._saved
        module.__name__ = name
        module.__spec__ = spec
        module.__package__ = package
        module.__loader__ = loader

    def get_code(self, fullname):
        # Lets runpy (``python -m helix_context.submodule``) execute the
        # real module's code under the old dotted name.
        return self._real_spec.loader.get_code(self._real_name)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(_OLD + ".") and fullname not in _REAL_FILES:
            real_name = _NEW + fullname[len(_OLD):]
            real_spec = importlib.util.find_spec(real_name)
            if real_spec is None:
                return None
            loader = _AliasLoader(real_name, real_spec)
            return importlib.util.spec_from_loader(
                fullname,
                loader,
                is_package=real_spec.submodule_search_locations is not None,
            )
        return None


if not any(type(f).__name__ == "_AliasFinder" for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

_pkg = importlib.import_module(_NEW)


def __getattr__(name):
    return getattr(_pkg, name)


def __dir__():
    return dir(_pkg)
