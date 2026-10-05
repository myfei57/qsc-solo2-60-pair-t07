"""JSON list and object helpers used by flow calibration aggregates."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from waterplant.store.store import Store


def load_json_list(store: Store, key: str) -> list[dict[str, Any]]:
    """Load a list of JSON objects, ignoring unrelated legacy values."""

    raw, present = store.get(key)
    if not present:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def has_json(store: Store, key: str, predicate: Callable[[dict[str, Any]], bool]) -> bool:
    return any(predicate(item) for item in load_json_list(store, key))


def append_json(store: Store, key: str, item: dict[str, Any]) -> None:
    """Append one immutable JSON object."""

    items = load_json_list(store, key)
    items.append(dict(item))
    store.put(key, json.dumps(items, ensure_ascii=False))


def update_json_list(
    store: Store, key: str, mutate: Callable[[list[dict[str, Any]]], None]
) -> list[dict[str, Any]]:
    """Replace a JSON list after running an in-place aggregate mutation."""

    items = load_json_list(store, key)
    mutate(items)
    store.put(key, json.dumps(items, ensure_ascii=False))
    return items


def load_json_object(store: Store, key: str) -> dict[str, Any]:
    """Load a JSON object, returning an empty mapping when absent or corrupt."""

    raw, present = store.get(key)
    if not present:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def save_json_object(store: Store, key: str, value: dict[str, Any]) -> None:
    store.put(key, json.dumps(value, ensure_ascii=False))
