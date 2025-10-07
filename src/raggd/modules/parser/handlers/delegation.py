"""Shared utilities for handler delegation metadata."""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "delegated_chunk_id",
    "delegated_metadata",
]


def delegated_chunk_id(
    *,
    delegate: str,
    parent_handler: str,
    component: str,
    start_offset: int,
    end_offset: int,
    marker: str | int | None = None,
) -> str:
    """Return a deterministic chunk identifier for delegated content."""

    if not delegate:
        raise ValueError("delegate must be a non-empty string")
    if not parent_handler:
        raise ValueError("parent_handler must be a non-empty string")
    if start_offset < 0 or end_offset < 0:
        raise ValueError("start_offset and end_offset must be >= 0")
    if end_offset < start_offset:
        raise ValueError("end_offset must be >= start_offset")

    parts = [
        delegate.strip(),
        "delegate",
        parent_handler.strip(),
    ]
    normalized_component = component.strip() if component else ""
    normalized_component = normalized_component.replace(":", "-")
    if normalized_component:
        parts.append(normalized_component)
    parts.append(str(start_offset))
    parts.append(str(end_offset))
    if marker is not None:
        parts.append(str(marker))
    return ":".join(parts)


def delegated_metadata(
    *,
    delegate: str,
    parent_handler: str,
    parent_symbol_id: str,
    parent_chunk_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build metadata payload for delegated chunks with parent linkage."""

    if not delegate:
        raise ValueError("delegate must be provided")
    if not parent_handler:
        raise ValueError("parent_handler must be provided")
    if not parent_symbol_id:
        raise ValueError("parent_symbol_id must be provided")

    payload: dict[str, Any] = {
        "delegate": delegate,
        "delegate_parent_handler": parent_handler,
        "delegate_parent_symbol": parent_symbol_id,
    }
    if parent_chunk_id:
        payload["delegate_parent_chunk"] = parent_chunk_id

    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            payload[key] = value

    return payload
