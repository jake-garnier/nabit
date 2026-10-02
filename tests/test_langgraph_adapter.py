"""Tests for the LangGraph adapter.

These run without LangChain installed: NabitCallback falls back to subclassing
object, and we drive its on_tool_start/on_tool_end hooks directly the same way
LangChain would. verify_node is just the decorator applied to a node fn.
"""

import pytest

from nabit import get_results, clear_results, clear_sinks, run, Mode
from nabit.checks import has_keys, truthy
from nabit.adapters.langgraph import NabitCallback, verify_node


@pytest.fixture(autouse=True)
def _clear():
    clear_results()
    clear_sinks()
    yield
    clear_results()
    clear_sinks()


def _drive_tool(cb, name, output, run_id="r1", inputs=None):
    """Simulate LangChain invoking a tool through the callback."""
    cb.on_tool_start({"name": name}, inputs or "", run_id=run_id, inputs=inputs)
    cb.on_tool_end(output, run_id=run_id)


def test_callback_verifies_registered_tool():
    cb = NabitCallback(checks={"create_customer": has_keys("id")}, mode=Mode.SILENT)
    _drive_tool(cb, "create_customer", {"id": 7, "status": "ok"})
    results = get_results()
    assert len(results) == 1
    assert results[0].name == "create_customer"
    assert results[0].passed is True


def test_callback_catches_silent_failure():
    cb = NabitCallback(checks={"search": truthy()}, mode=Mode.SILENT)
    _drive_tool(cb, "search", [])  # empty list == silent failure
    assert get_results()[0].passed is False


def test_callback_ignores_unregistered_tools():
    cb = NabitCallback(checks={"known": truthy()}, mode=Mode.SILENT)
    _drive_tool(cb, "unknown_tool", {"whatever": 1})
    assert get_results() == []  # no check registered → not recorded


def test_callback_default_check_applies_to_all():
    cb = NabitCallback(default_check=truthy(), mode=Mode.SILENT)
    _drive_tool(cb, "tool_a", {"x": 1}, run_id="a")
    _drive_tool(cb, "tool_b", None, run_id="b")
    results = {r.name: r.passed for r in get_results()}
    assert results == {"tool_a": True, "tool_b": False}


def test_callback_extracts_toolmessage_content():
    class FakeToolMessage:
        def __init__(self, content):
            self.content = content

    cb = NabitCallback(checks={"t": truthy()}, mode=Mode.SILENT)
    _drive_tool(cb, "t", FakeToolMessage(""))  # .content is empty -> fail
    assert get_results()[0].passed is False


def test_callback_run_id_and_summary():
    cb = NabitCallback(checks={"t": truthy()}, mode=Mode.SILENT, run_id="graph-run-1")
    _drive_tool(cb, "t", {"ok": 1})
    assert get_results()[0].run_id == "graph-run-1"
    assert cb.summary()["total"] == 1


def test_verify_node_self_heals():
    # a node that fails to write on the first pass, writes on the second
    store = set()
    attempts = {"n": 0}

    def book_node(state):
        attempts["n"] += 1
        bid = state["req_id"]
        if attempts["n"] >= 2:
            store.add(bid)
        return {"booking_id": bid}

    wrapped = verify_node(
        book_node,
        lambda update, ctx: update["booking_id"] in store,
        mode=Mode.SILENT,
        retries=2,
    )
    out = wrapped({"req_id": "abc"})
    assert out == {"booking_id": "abc"}
    r = get_results()[0]
    assert r.name == "book_node"
    assert r.passed is True
    assert r.attempts == 2


def test_verify_node_context_has_incoming_state():
    seen = {}

    def node(state):
        return {"doubled": state["n"] * 2}

    def check(update, ctx):
        seen.update(ctx)
        return update["doubled"] == ctx["state"]["n"] * 2

    wrapped = verify_node(node, check, mode=Mode.SILENT)
    wrapped({"n": 5})
    assert seen["state"] == {"n": 5}
    assert get_results()[0].passed is True
