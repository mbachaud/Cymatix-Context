"""Optional add-on (plugin) support for cymatix-context."""
from .api import CymatixPlugin, MenuItem, Panel, ServiceSpec
from .loader import LoadedPlugin, call_hook, discover_plugins, get_plugins, reset_plugins_cache

__all__ = [
    "CymatixPlugin", "MenuItem", "Panel", "ServiceSpec",
    "LoadedPlugin", "call_hook", "discover_plugins", "get_plugins", "reset_plugins_cache",
]
