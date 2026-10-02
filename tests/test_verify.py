import logging

import pytest

from nabit import (
    verify, run, summary, Mode, VerificationError,
    get_results, clear_results, add_sink, clear_sinks,
)
from nabit import checks


@pytest.fixture(autouse=True)
def _clear():
    clear_results()
    clear_sinks()
    yield
    clear_results()
    clear_sinks()


# --- core behavior ----------------------------------------------------------
def test_passing_postcondition_records_pass():
    @verify(lambda result, ctx: result == 42, mode=Mode.SILENT)
    def f():
        return 42

    assert f() == 42
    results = get_results()
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].name == "f"
    assert results[0].attempts == 1


def test_failing_postcondition_raises_in_raise_mode():
    @verify(lambda result, ctx: False, mode=Mode.RAISE)
    def f():
        return "claimed success"

    with pytest.raises(VerificationError):
        f()
    assert get_results()[0].passed is False


def test_warn_mode_returns_result_and_records(caplog):
    @verify(lambda result, ctx: False, mode=Mode.WARN)
    def f():
        return "ok"

    with caplog.at_level(logging.WARNING, logger="nabit"):
        assert f() == "ok"  # does not raise
    assert "FAILED verification" in caplog.text
    assert get_results()[0].passed is False


def test_context_exposes_bound_args():
    seen = {}

    def pc(result, ctx):
        seen.update(ctx)
        return ctx["name"] == "bob" and result["id"] == 1

    @verify(pc, mode=Mode.SILENT)
    def create(name):
        return {"id": 1}

    create("bob")
    assert seen["name"] == "bob"
    assert get_results()[0].passed is True


def test_postcondition_runs_when_function_raises():
    state = {"written": True}

    @verify(lambda result, ctx: state["written"], mode=Mode.SILENT)
    def f():
        raise RuntimeError("boom after writing")

    with pytest.raises(RuntimeError):
        f()
    results = get_results()
    assert len(results) == 1
    assert results[0].passed is True
    assert "function raised" in (results[0].error or "")


def test_on_error_false_skips_verification():
    @verify(lambda result, ctx: True, mode=Mode.SILENT, on_error=False)
    def f():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        f()
    assert get_results() == []  # no verification recorded


def test_postcondition_that_raises_is_treated_as_failure():
    @verify(lambda result, ctx: result["missing_key"], mode=Mode.WARN)
    def f():
        return {}

    f()
    r = get_results()[0]
    assert r.passed is False
    assert "postcondition raised" in (r.error or "")


# --- self-heal / retries ----------------------------------------------------
def test_retries_until_success():
    calls = {"n": 0}

    @verify(lambda result, ctx: result >= 3, mode=Mode.SILENT, retries=5)
    def f():
        calls["n"] += 1
        return calls["n"]  # 1, 2, 3 ... passes on the 3rd attempt

    assert f() == 3
    r = get_results()[0]
    assert r.passed is True
    assert r.attempts == 3


def test_retries_exhausted_records_failure():
    @verify(lambda result, ctx: False, mode=Mode.WARN, retries=2)
    def f():
        return "nope"

    f()
    r = get_results()[0]
    assert r.passed is False
    assert r.attempts == 3  # 1 initial + 2 retries


def test_on_retry_hook_is_called():
    attempts_seen = []

    def on_retry(attempt, last_result, ctx):
        attempts_seen.append(attempt)

    @verify(lambda result, ctx: False, mode=Mode.SILENT, retries=2, on_retry=on_retry)
    def f():
        return "x"

    f()
    assert attempts_seen == [1, 2]  # hook fires before each retry, not the final


# --- run-id correlation -----------------------------------------------------
def test_run_id_groups_results():
    @verify(lambda result, ctx: True, mode=Mode.SILENT)
    def ok():
        return 1

    @verify(lambda result, ctx: False, mode=Mode.SILENT)
    def bad():
        return 0

    with run() as rid:
        ok()
        bad()

    # both carry the run id
    grouped = get_results(run_id=rid)
    assert len(grouped) == 2
    s = summary(run_id=rid)
    assert s == {"total": 2, "passed": 1, "failed": 1, "pass_rate": 0.5,
                 "failures": ["bad"]}


def test_results_outside_run_have_no_run_id():
    @verify(lambda result, ctx: True, mode=Mode.SILENT)
    def f():
        return 1

    f()
    assert get_results()[0].run_id is None


# --- sinks ------------------------------------------------------------------
def test_sink_receives_results():
    received = []
    add_sink(received.append)

    @verify(lambda result, ctx: True, mode=Mode.SILENT)
    def f():
        return 1

    f()
    assert len(received) == 1
    assert received[0].name == "f"


def test_broken_sink_does_not_break_app(caplog):
    def bad_sink(r):
        raise ValueError("sink boom")

    add_sink(bad_sink)

    @verify(lambda result, ctx: True, mode=Mode.SILENT)
    def f():
        return 1

    # must not raise despite the broken sink
    assert f() == 1


# --- checks library ---------------------------------------------------------
def test_checks_has_keys_and_combinators():
    pc = checks.all_of(checks.has_keys("id", "status"),
                       checks.field_equals("status", "created"))
    assert pc({"id": 1, "status": "created"}, {}) is True
    assert pc({"id": 1, "status": "failed"}, {}) is False
    assert pc({"id": 1}, {}) is False


def test_checks_truthy_catches_empty():
    assert checks.truthy()([], {}) is False        # empty list == silent failure
    assert checks.truthy()([1], {}) is True
    assert checks.non_empty()("", {}) is False
    assert checks.not_(checks.truthy())(0, {}) is True


def test_checks_file_exists(tmp_path):
    p = tmp_path / "report.txt"
    pc = checks.file_exists(str(p))
    assert pc(None, {}) is False
    p.write_text("done")
    assert pc(None, {}) is True


def test_checks_file_exists_derives_path_from_result(tmp_path):
    p = tmp_path / "out.json"
    p.write_text("{}")
    pc = checks.file_exists(lambda result, ctx: result["path"])
    assert pc({"path": str(p)}, {}) is True


# --- async ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_async_function_and_async_postcondition():
    async def pc(result, ctx):
        return result == "done"

    @verify(pc, mode=Mode.SILENT)
    async def f():
        return "done"

    assert await f() == "done"
    assert get_results()[0].passed is True


@pytest.mark.asyncio
async def test_async_retries():
    calls = {"n": 0}

    @verify(lambda result, ctx: result >= 2, mode=Mode.SILENT, retries=3)
    async def f():
        calls["n"] += 1
        return calls["n"]

    assert await f() == 2
    assert get_results()[0].attempts == 2
