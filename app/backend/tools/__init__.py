"""Tool package exports.

Use lazy imports so lightweight integrations (for example MCP catalogue APIs)
do not initialise MongoDB/Qdrant dependencies merely by importing this package.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "search_news",
    "get_full_news",
    "search_ai_analysis",
    "search_recommendations",
    "get_full_ai_analysis",
]


_EXPORT_MODULES = {
    "search_news": ".news",
    "get_full_news": ".news",
    "search_ai_analysis": ".ai_analysis",
    "search_recommendations": ".ai_analysis",
    "get_full_ai_analysis": ".ai_analysis",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
