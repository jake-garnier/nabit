"""Composable post-condition builders.

Most verifications are the same handful of shapes: "the result has these keys",
"this file now exists", "this URL returns 2xx", "this row is in the DB". Instead
of hand-writing a lambda every time (what every other verifier makes you do),
compose these.

Every builder returns a post-condition `(result, context) -> bool` that plugs
straight into @verify:

    from nabit import verify
    from nabit.checks import has_keys, all_of, file_exists

    @verify(all_of(has_keys("id", "status"), lambda r, c: r["status"] == "created"))
    def create(...): ...

Zero dependencies. `http_ok` uses only the stdlib (urllib).
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping

PostCondition = Callable[[Any, dict], bool]


# --- combinators ------------------------------------------------------------
def all_of(*checks: PostCondition) -> PostCondition:
    """Pass only if every check passes (logical AND)."""
    def _pc(result: Any, ctx: dict) -> bool:
        return all(bool(c(result, ctx)) for c in checks)
    return _pc


def any_of(*checks: PostCondition) -> PostCondition:
    """Pass if any check passes (logical OR)."""
    def _pc(result: Any, ctx: dict) -> bool:
        return any(bool(c(result, ctx)) for c in checks)
    return _pc


def not_(check: PostCondition) -> PostCondition:
    """Invert a check."""
    def _pc(result: Any, ctx: dict) -> bool:
        return not bool(check(result, ctx))
    return _pc


# --- result-shape checks ----------------------------------------------------
def has_keys(*keys: str) -> PostCondition:
    """Result is a mapping containing all of `keys`."""
    def _pc(result: Any, ctx: dict) -> bool:
        if not isinstance(result, Mapping):
            return False
        return all(k in result for k in keys)
    return _pc


def equals(expected: Any) -> PostCondition:
    """Result equals `expected`."""
    return lambda result, ctx: result == expected


def field_equals(key: str, expected: Any) -> PostCondition:
    """Result is a mapping where result[key] == expected."""
    def _pc(result: Any, ctx: dict) -> bool:
        return isinstance(result, Mapping) and result.get(key) == expected
    return _pc


def truthy(key: str | None = None) -> PostCondition:
    """Result (or result[key]) is truthy. Catches empty lists/strings/None —
    the classic 'returned [] instead of failing' silent failure."""
    def _pc(result: Any, ctx: dict) -> bool:
        if key is None:
            return bool(result)
        return isinstance(result, Mapping) and bool(result.get(key))
    return _pc


def non_empty() -> PostCondition:
    """Result is non-empty (len > 0). Catches truncated/empty responses."""
    def _pc(result: Any, ctx: dict) -> bool:
        try:
            return len(result) > 0
        except TypeError:
            return result is not None
    return _pc


# --- real-world side-effect checks (the whole point of nabit) ----------------
def file_exists(path_or_fn: str | Callable[[Any, dict], str]) -> PostCondition:
    """A file exists on disk. `path_or_fn` is a literal path or a callable
    (result, ctx) -> path, so you can derive the path from the agent's output."""
    def _pc(result: Any, ctx: dict) -> bool:
        path = path_or_fn(result, ctx) if callable(path_or_fn) else path_or_fn
        return bool(path) and os.path.exists(path)
    return _pc


def file_fresh(path_or_fn: str | Callable[[Any, dict], str], max_age_s: float) -> PostCondition:
    """A file exists AND was modified within the last `max_age_s` seconds.
    Catches the 'cron job exited 0 but wrote nothing new' failure."""
    import time

    def _pc(result: Any, ctx: dict) -> bool:
        path = path_or_fn(result, ctx) if callable(path_or_fn) else path_or_fn
        if not path or not os.path.exists(path):
            return False
        return (time.time() - os.path.getmtime(path)) <= max_age_s
    return _pc


def predicate(fn: Callable[[Any, dict], bool]) -> PostCondition:
    """Wrap an arbitrary check against real state — a DB lookup, API call, etc.
    Just sugar for readability / composition:

        predicate(lambda r, c: db.exists("customers", r["id"]))
    """
    return lambda result, ctx: bool(fn(result, ctx))


def http_ok(url_or_fn: str | Callable[[Any, dict], str], timeout: float = 5.0) -> PostCondition:
    """An HTTP GET to the URL returns a 2xx status. Stdlib only (urllib).
    `url_or_fn` is a literal URL or (result, ctx) -> url."""
    from urllib.request import urlopen

    def _pc(result: Any, ctx: dict) -> bool:
        url = url_or_fn(result, ctx) if callable(url_or_fn) else url_or_fn
        if not url:
            return False
        try:
            with urlopen(url, timeout=timeout) as resp:  # noqa: S310 — caller-supplied URL
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            return False
    return _pc
