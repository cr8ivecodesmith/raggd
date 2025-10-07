"""Parser handler scaffolding and factory utilities."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable

from .base import (
    HandlerCache,
    HandlerChunk,
    HandlerFile,
    HandlerResult,
    HandlerSymbol,
    ParseContext,
    ParserHandler,
)
from .css import CSSHandler
from .html import HTMLHandler
from .javascript import JavaScriptHandler, TypeScriptHandler
from .python import PythonHandler

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .base import ParseContext

ParserHandlerFactory = Callable[["ParseContext"], ParserHandler]

__all__ = [
    "HandlerCache",
    "HandlerChunk",
    "HandlerFile",
    "HandlerResult",
    "HandlerSymbol",
    "ParseContext",
    "ParserHandler",
    "ParserHandlerFactory",
    "load_factory",
    "PythonHandler",
    "HTMLHandler",
    "JavaScriptHandler",
    "TypeScriptHandler",
    "CSSHandler",
]


def load_factory(reference: str) -> ParserHandlerFactory:
    """Load a handler factory or class from ``reference`` string.

    Args:
        reference: Dotted path in ``module:attr`` form.

    Returns:
        Callable returning a :class:`ParserHandler` when provided a
        :class:`~raggd.modules.parser.handlers.base.ParseContext`.

    Raises:
        ImportError: If ``reference`` cannot be imported.
        AttributeError: If the attribute does not exist on the target module.
        TypeError: If the resolved attribute is not callable.
    """

    if "" == reference or ":" not in reference:
        raise ImportError(
            f"Handler factory reference must contain ':' (got {reference!r})."
        )
    module_name, attr_name = reference.split(":", 1)
    module = import_module(module_name)
    try:
        resolved = getattr(module, attr_name)
    except AttributeError as exc:  # pragma: no cover - defensive branch
        raise AttributeError(
            "Handler factory attribute "
            f"{attr_name!r} not found in {module_name!r}."
        ) from exc

    if callable(resolved):
        return _wrap_callable(resolved)

    raise TypeError(
        "Handler factory reference "
        f"{reference!r} must resolve to a callable, got {type(resolved)!r}."
    )


def _wrap_callable(value: Any) -> ParserHandlerFactory:
    """Return a handler factory wrapping ``value`` appropriately."""

    from inspect import isclass

    if isclass(value):

        def _factory(context: "ParseContext", _cls=value):
            return _cls(context=context)  # type: ignore[arg-type]

        return _factory

    def _factory(context: "ParseContext"):
        try:
            return value(context=context)
        except TypeError:
            return value(context)

    return _factory
