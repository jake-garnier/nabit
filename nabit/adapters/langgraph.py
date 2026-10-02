"""LangGraph / LangChain adapter for nabit.

Two ways to plug in, depending on how much you want:

1. NabitCallback — a passive callback handler. Add it once to your graph and it
   verifies every tool result against the checks you register. Zero restructure.
   Detection only (a callback can't re-run a tool), but you still get recording,
   run-id grouping, sinks, and summaries.

       from nabit.adapters.langgraph import NabitCallback
       from nabit.checks import truthy, has_keys

       cb = NabitCallback(checks={
           "create_customer": has_keys("id"),
           "search_hotels": truthy(),          # catches the empty-list silent fail
       })
       graph.invoke(state, config={"callbacks": [cb]})
       print(cb.summary())

2. verify_node — wrap a single graph node to get the FULL nabit treatment,
   including self-heal retries (a node is just a function, so it can be re-run):

       from nabit.adapters.langgraph import verify_node
       from nabit.checks import predicate

       builder.add_node("book", verify_node(
           book_node,
           predicate(lambda update, state: db.has_booking(update["booking_id"])),
           retries=2,
       ))

This module lazily imports LangChain — if it isn't installed, `verify_node` and
the plain decorator still work; only the live callback hookup needs it.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Union

from ..core import (
    Mode,
    VerificationResult,
    _current_run,
    _emit,
    verify as _verify,
)

try:  # lazy / optional — nabit never hard-depends on LangChain
    from langchain_core.callbacks import BaseCallbackHandler
    _HAS_LC = True
except Exception:  # noqa: BLE001
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAS_LC = False


PostCondition = Callable[[Any, dict], bool]


def _extract_output(output: Any) -> Any:
    """LangChain tool output may be a ToolMessage, a str, or a raw value.
    Pull out the payload we should verify."""
    if hasattr(output, "content"):
        return output.content
    return output


class NabitCallback(BaseCallbackHandler):
    """A LangChain/LangGraph callback handler that verifies tool results.

    Register a `checks` mapping of ``tool_name -> post_condition``. After each
    tool runs, the matching post-condition is checked against the tool's output;
    results are recorded through nabit's normal machinery (so get_results(),
    sinks, and run ids all work). Provide `default_check` to verify every tool.

    Args:
        checks: {tool_name: (output, ctx) -> bool}. ctx carries {"tool", "inputs"}.
        default_check: applied to any tool without a specific entry.
        mode: Mode.WARN (default), LOG, SILENT, or RAISE. Note RAISE inside a
            callback may be swallowed by the framework — prefer WARN/SILENT and
            inspect summary()/get_results().
        run_id: optional id stamped on every result for grouping.
    """

    def __init__(
        self,
        checks: Optional[Dict[str, PostCondition]] = None,
        *,
        default_check: Optional[PostCondition] = None,
        mode: Union[Mode, str] = Mode.WARN,
        run_id: Optional[str] = None,
    ) -> None:
        self.checks = checks or {}
        self.default_check = default_check
        self.mode = Mode(mode)
        self.run_id = run_id
        self._names: Dict[str, str] = {}
        self._starts: Dict[str, float] = {}

    # -- LangChain callback hooks --------------------------------------------
    def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs):  # noqa: ANN001
        name = None
        if isinstance(serialized, dict):
            name = serialized.get("name")
        name = name or kwargs.get("name") or "tool"
        key = str(run_id)
        self._names[key] = name
        self._starts[key] = time.perf_counter()
        # stash inputs for the post-condition context
        self._names[key + ":inputs"] = kwargs.get("inputs") or input_str  # type: ignore[assignment]

    def on_tool_end(self, output, *, run_id=None, **kwargs):  # noqa: ANN001
        key = str(run_id)
        name = self._names.pop(key, "tool")
        inputs = self._names.pop(key + ":inputs", None)
        start = self._starts.pop(key, None)
        check = self.checks.get(name, self.default_check)
        if check is None:
            return  # nothing registered for this tool — ignore it
        self._run_check(name, check, _extract_output(output), inputs, start)

    # -- shared logic (framework-independent, so it's unit-testable) ----------
    def _run_check(self, name: str, check: PostCondition, output: Any,
                   inputs: Any, start: Optional[float]) -> None:
        ctx = {"tool": name, "inputs": inputs}
        try:
            passed = bool(check(output, ctx))
            err = None
        except Exception as exc:  # noqa: BLE001
            passed = False
            err = f"postcondition raised: {exc}"
        dur = (time.perf_counter() - start) * 1000 if start else 0.0
        _emit(
            VerificationResult(
                name=name, passed=passed, duration_ms=dur, result=output,
                error=err, attempts=1,
                run_id=self.run_id or _current_run.get(),
            ),
            self.mode,
        )

    def summary(self) -> dict:
        """Pass/fail summary scoped to this handler's run_id (if set)."""
        from ..core import summary as _summary

        return _summary(run_id=self.run_id) if self.run_id else _summary()


def verify_node(
    node: Callable,
    postcondition: PostCondition,
    *,
    mode: Union[Mode, str] = Mode.WARN,
    name: Optional[str] = None,
    retries: int = 0,
    backoff: float = 0.0,
    on_retry: Optional[Callable] = None,
):
    """Wrap a LangGraph node function with nabit verification + self-heal.

    A node is ``(state) -> state_update``. The post-condition receives
    ``(state_update, ctx)`` where ctx holds the bound node args (so ctx["state"]
    is the incoming state). Because a node is a plain function, this path gets
    the full retry/self-heal behavior that the passive callback can't offer.
    """
    return _verify(
        postcondition,
        mode=mode,
        name=name or getattr(node, "__name__", "node"),
        retries=retries,
        backoff=backoff,
        on_retry=on_retry,
    )(node)


__all__ = ["NabitCallback", "verify_node", "_HAS_LC"]
