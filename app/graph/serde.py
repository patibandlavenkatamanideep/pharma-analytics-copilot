"""Restrictive checkpoint loading for the application's primitive turn state.

The pinned JsonPlusSerializer otherwise warns and invokes arbitrary msgpack
constructors. Its strict mode alone returns blocked constructors' arguments
as data. Preflight refuses those records instead of resuming altered state.
Only LangGraph's Interrupt wrapper is needed by this graph; plans are dicts.
"""
from __future__ import annotations

import json

import ormsgpack
from langgraph.checkpoint.serde.jsonplus import (
    EXT_CONSTRUCTOR_KW_ARGS, JsonPlusSerializer,
)
from langgraph.types import Interrupt


class CheckpointError(ValueError):
    """Unreadable workflow state; never include persisted content in errors."""


def _plain(value, depth=0):
    if depth > 64:
        raise CheckpointError("Checkpoint nesting exceeds the limit")
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) in (list, tuple):
        for item in value:
            _plain(item, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        if value.get("type") == "constructor" and "lc" in value:
            raise CheckpointError("Checkpoint constructor is not supported")
        for item in value.values():
            _plain(item, depth + 1)
        return
    raise CheckpointError("Checkpoint contains unsupported data")


def _extension(code, data):
    # Do not import or invoke anything named by persisted bytes. Recursively
    # inspect nested extensions before handing bytes to the library loader.
    parts = ormsgpack.unpackb(data, ext_hook=_extension)
    if (code != EXT_CONSTRUCTOR_KW_ARGS or type(parts) is not list or
            len(parts) != 3 or parts[:2] != ["langgraph.types", "Interrupt"]):
        raise CheckpointError("Checkpoint extension is not supported")
    fields = parts[2]
    if (type(fields) is not dict or not {"value", "id"} <= fields.keys() or
            fields.keys() - {"value", "id", "response_schema"} or
            type(fields["id"]) is not str or fields.get("response_schema") is not None):
        raise CheckpointError("Checkpoint interrupt is invalid")
    _plain(fields)
    return fields


class TurnSerializer(JsonPlusSerializer):
    def __init__(self):
        # Explicit constructor settings, independent of import-time environment.
        super().__init__(pickle_fallback=False, allowed_json_modules=None,
                         allowed_msgpack_modules=[Interrupt])

    def loads_typed(self, data):
        try:
            kind, blob = data
            if len(blob) > 8 * 1024 * 1024:
                raise CheckpointError("Checkpoint exceeds the size limit")
            if kind == "msgpack":
                _plain(ormsgpack.unpackb(blob, ext_hook=_extension))
            elif kind == "json":
                # Legacy plain JSON is readable, but its object constructors
                # are not. Return plain data without a constructor reviver.
                value = json.loads(blob)
                _plain(value)
                return value
            elif kind != "null":
                raise CheckpointError("Checkpoint encoding is not supported")
            return super().loads_typed(data)
        except Exception:
            raise CheckpointError("Saved workflow state could not be read") from None
