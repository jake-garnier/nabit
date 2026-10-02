"""Core verification primitives for nabit.

The whole idea: an LLM agent (or any tool it calls) returns a result that
*claims* something happened. We don't trust the claim. We run a read-only
post-condition against the actual system state and record whether reality
matched the claim.

What sets nabit apart from other verifiers:
  * self-heal — on a failed check it can re-run the action (optionally with
    feedback), so it corrects silent failures instead of just reporting them.
  * run-ID correlation — scope a batch of actions under one run so their
    verifications group together (the "nothing shares a run id" problem).
  * pluggable sinks — tee every result to JSONL / OpenTelemetry / your logger
    without nabit taking on a single runtime dependency.

Zero dependencies. Works with sync or async functions. Framework-agnostic —
LangGraph, LangChain, CrewAI, or a plain function all look the same here.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger("nabit")


class Mode(str, Enum):
    """What to do when a post-condition fails (after retries are exhausted).

    RAISE  — raise VerificationError (fail loud, good for tests / CI).
    WARN   — log at WARNING level and return the result anyway (good for prod
             rollout: you get the signal without changing behavior).
    LOG    — log at INFO level only.
    SILENT — record the result but emit nothing (inspect via get_results()).
    """

    RAISE = "raise"
    WARN = "warn"
    LOG = "log"
    SILENT = "silent"


class VerificationError(AssertionError):
    """Raised (in Mode.RAISE) when an agent's claimed outcome does not match
    actual system state, after any retries are exhausted."""


@dataclass
class VerificationResult:
    """One verification event, kept in an in-memory log for reporting."""

    name: str
    passed: bool
    duration_ms: float
    result: Any = None
    error: Optional[str] = None
    attempts: int = 1
    run_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        # `result` may not be JSON-serializable; stringify defensively.
        try:
            import json

            json.dumps(d["result"])
        except (TypeError, ValueError):
            d["result"] = repr(d["result"])
        return d


# --- run-ID correlation -----------------------------------------------------
# A context var so concurrent/async runs don't clobber each other's run id.
_current_run: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "nabit_run_id", default=None
)


@contextlib.contextmanager
def run(run_id: Optional[str] = None):
    """Scope a batch of verified actions under a single run id, so their
    results group together.

        with nabit.run() as rid:
            create_customer(...)
            charge_card(...)
        print(nabit.summary(run_id=rid))

    If `run_id` is omitted a short unique id is generated and yielded.
    """
    rid = run_id or uuid.uuid4().hex[:12]
    token = _current_run.set(rid)
    try:
        yield rid
    finally:
        _current_run.reset(token)


# --- results log + pluggable sinks ------------------------------------------
_RESULTS: list[VerificationResult] = []
_MAX_RESULTS = 1000
_SINKS: list[Callable[[VerificationResult], None]] = []


def add_sink(fn: Callable[[VerificationResult], None]) -> None:
    """Register a callback invoked with every VerificationResult as it happens.
    Use it to tee results to JSONL, OpenTelemetry, Datadog, etc. Zero deps:
    nabit never imports your sink's backend."""
    _SINKS.append(fn)


def clear_sinks() -> None:
    _SINKS.clear()


def get_results(run_id: Optional[str] = None) -> list[VerificationResult]:
    """Return recorded verification results (most recent last). Optionally
    filter to a single run id."""
    if run_id is None:
        return list(_RESULTS)
    return [r for r in _RESULTS if r.run_id == run_id]


def clear_results() -> None:
    """Clear the in-memory results log (useful between tests)."""
    _RESULTS.clear()


def summary(run_id: Optional[str] = None) -> dict:
    """Aggregate pass/fail stats, optionally scoped to one run id."""
    rs = get_results(run_id)
    total = len(rs)
    passed = sum(1 for r in rs if r.passed)
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total) if total else 1.0,
        "failures": [r.name for r in rs if not r.passed],
    }


def _record(result: VerificationResult) -> None:
    _RESULTS.append(result)
    if len(_RESULTS) > _MAX_RESULTS:
        del _RESULTS[0]
    for sink in _SINKS:
        try:
            sink(result)
        except Exception:  # noqa: BLE001 — a broken sink must never break the app
            logger.exception("nabit: sink raised; continuing")


# A post-condition receives (result, context) and returns a truthy value for
# "reality matches the claim". `context` is the bound call arguments, so you
# can check the inputs against the outputs. May be sync or async.
PostCondition = Callable[[Any, dict], Union[bool, Awaitable[bool]]]
# A retry/feedback hook: (attempt, last_result, context) -> None. Use it to
# nudge the agent before the next attempt (e.g. append a corrective message).
RetryHook = Callable[[int, Any, dict], Any]


def _build_context(func: Callable, args: tuple, kwargs: dict) -> dict:
    """Bind call args to parameter names so post-conditions can read inputs."""
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return {"args": args, "kwargs": kwargs}


def _emit(result: VerificationResult, mode: Mode) -> None:
    """Record a final result and raise/log according to mode."""
    _record(result)
    if result.passed:
        logger.debug("nabit: %s verified OK (%.1fms, %d attempt(s))",
                     result.name, result.duration_ms, result.attempts)
        return
    msg = (f"nabit: {result.name} FAILED verification after {result.attempts} "
           f"attempt(s) — agent claimed success, reality disagreed")
    if result.error:
        msg += f" ({result.error})"
    if mode is Mode.RAISE:
        raise VerificationError(msg)
    elif mode is Mode.WARN:
        logger.warning(msg)
    elif mode is Mode.LOG:
        logger.info(msg)
    # SILENT: recorded only.


def verify(
    postcondition: PostCondition,
    *,
    mode: Union[Mode, str] = Mode.WARN,
    name: Optional[str] = None,
    on_error: bool = True,
    retries: int = 0,
    backoff: float = 0.0,
    on_retry: Optional[RetryHook] = None,
) -> Callable:
    """Decorator: run the wrapped function, then check its claimed outcome
    against actual system state via `postcondition`. On failure, optionally
    self-heal by re-running the action.

    Args:
        postcondition: callable (result, context) -> bool. Truthy == reality
            matched the claim. May be sync or async.
        mode: what to do once retries are exhausted. See Mode. Default WARN.
        name: label for the result log. Defaults to the function name.
        on_error: if True (default), also run the post-condition when the
            wrapped function raises — so "it threw but the side effect still
            happened" (or vice versa) is handled. On the final failed attempt
            the original exception is re-raised.
        retries: how many times to re-run the action if verification fails
            (0 = no self-heal, just verify once). This is the closed loop:
            detect AND correct.
        backoff: seconds to sleep between retries, multiplied by attempt number
            (linear backoff). Ignored for async if 0.
        on_retry: optional (attempt, last_result, context) -> None hook invoked
            before each retry — use it to feed the discrepancy back to the agent.

    Works transparently on both sync and async functions.
    """
    mode = Mode(mode)

    def decorator(func: Callable) -> Callable:
        label = name or getattr(func, "__name__", "anonymous")
        pc_is_async = inspect.iscoroutinefunction(postcondition)
        func_is_async = inspect.iscoroutinefunction(func)

        async def _run_pc_async(result: Any, ctx: dict) -> bool:
            out = postcondition(result, ctx)
            if inspect.isawaitable(out):
                out = await out
            return bool(out)

        def _run_pc_sync(result: Any, ctx: dict) -> bool:
            if pc_is_async:
                raise RuntimeError(
                    f"nabit: async post-condition used on sync function '{label}'. "
                    "Make the wrapped function async, or the post-condition sync."
                )
            return bool(postcondition(result, ctx))

        def _finalize(result, passed, pc_err, last_exc, attempt, start):
            """Record the final attempt, then raise/return per semantics:
            a real exception always propagates (more informative than a
            VerificationError); otherwise _emit handles raise/warn/log/silent."""
            err = pc_err or (f"function raised: {last_exc}" if last_exc else None)
            vr = VerificationResult(
                name=label, passed=passed,
                duration_ms=(time.perf_counter() - start) * 1000,
                result=result, error=err, attempts=attempt,
                run_id=_current_run.get(),
            )
            if last_exc is not None:
                _record(vr)
                if not passed and mode in (Mode.WARN, Mode.LOG):
                    logger.warning("nabit: %s FAILED (function raised after %d "
                                   "attempt(s)): %s", label, attempt, last_exc)
                raise last_exc
            _emit(vr, mode)
            return result

        if func_is_async:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                import asyncio

                ctx = _build_context(func, args, kwargs)
                start = time.perf_counter()
                result: Any = None
                for attempt in range(1, retries + 2):  # 1 initial + `retries`
                    last_exc: Optional[BaseException] = None
                    try:
                        result = await func(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        result = None
                        if not on_error:
                            raise  # don't verify, just propagate
                    try:
                        passed = await _run_pc_async(result, ctx)
                        pc_err = None
                    except Exception as pc_exc:  # noqa: BLE001
                        passed = False
                        pc_err = f"postcondition raised: {pc_exc}"
                    if passed or attempt == retries + 1:
                        return _finalize(result, passed, pc_err, last_exc, attempt, start)
                    logger.debug("nabit: %s attempt %d failed, retrying", label, attempt)
                    if on_retry is not None:
                        maybe = on_retry(attempt, result, ctx)
                        if inspect.isawaitable(maybe):
                            await maybe
                    if backoff:
                        await asyncio.sleep(backoff * attempt)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            ctx = _build_context(func, args, kwargs)
            start = time.perf_counter()
            result: Any = None
            for attempt in range(1, retries + 2):
                last_exc: Optional[BaseException] = None
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    result = None
                    if not on_error:
                        raise
                try:
                    passed = _run_pc_sync(result, ctx)
                    pc_err = None
                except Exception as pc_exc:  # noqa: BLE001
                    passed = False
                    pc_err = f"postcondition raised: {pc_exc}"
                if passed or attempt == retries + 1:
                    return _finalize(result, passed, pc_err, last_exc, attempt, start)
                logger.debug("nabit: %s attempt %d failed, retrying", label, attempt)
                if on_retry is not None:
                    on_retry(attempt, result, ctx)
                if backoff:
                    time.sleep(backoff * attempt)

        return sync_wrapper

    return decorator
