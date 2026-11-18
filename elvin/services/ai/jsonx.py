"""Pull a JSON value out of model output that may contain code fences, prose,
<think> sections or trailing commas."""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_THINK = re.compile(r"<think>.*?</think>", re.S)


def _balanced(text: str, start: int) -> str | None:
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1] if ch == close_ch else None
    return None


def _loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    try:
        return json.loads(_TRAILING_COMMA.sub(r"\1", candidate))
    except ValueError:
        return None


def extract(text: str | None, *, want: type | None = dict) -> Any:
    if not text:
        return None
    body = _THINK.sub("", text).strip()
    candidates = [body]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(body)]
    for cand in candidates:
        value = _loads(cand)
        if value is not None and (want is None or isinstance(value, want)):
            return value
    for idx, ch in enumerate(body):
        if ch in "{[":
            chunk = _balanced(body, idx)
            if chunk:
                value = _loads(chunk)
                if value is not None and (want is None or isinstance(value, want)):
                    return value
    return None


def as_list(value: Any, key: str) -> list:
    if isinstance(value, dict):
        v = value.get(key)
        return v if isinstance(v, list) else []
    if isinstance(value, list):
        return value
    return []
