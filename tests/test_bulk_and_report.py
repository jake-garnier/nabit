import json

import pytest

import nabit
from nabit import (
    verify, run, summary, check_claims, Mode, VerificationError,
    get_results, clear_results, clear_sinks, add_sink, jsonl_sink,
    report, report_from_jsonl,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_results()
    clear_sinks()
    yield
    clear_results()
    clear_sinks()


# --- check_claims ------------------------------------------------------------
def test_check_claims_bare_dicts_ctx_is_result():
    ok_ids = {1, 2, 3, 4}
    claims = ({"video_id": i} for i in [1, 2, 3, 99, 4, 100])  # 99,100 fail
    s = check_claims(lambda r, ctx: r["video_id"] in ok_ids, claims,
                     name="A:captions")
    assert s.total == 6
    assert s.passed == 4
    assert s.failed == 2
    assert [r.result["video_id"] for r in s.failed_results] == [99, 100]
    # recorded through the normal machinery:
    assert summary()["total"] == 6


def test_check_claims_pairs_use_explicit_ctx():
    claims = [( {"id": 10}, {"expected": 10} ), ( {"id": 11}, {"expected": 99} )]
    s = check_claims(lambda r, ctx: r["id"] == ctx["expected"], claims)
    assert (s.passed, s.failed) == (1, 1)


def test_check_claims_raising_check_is_failure():
    s = check_claims(lambda r, ctx: r["missing"], [{"a": 1}])
    assert s.failed == 1
    assert "check raised" in (s.failed_results[0].error or "")


def test_check_claims_run_id_stamped():
    with run("bulk-run") as rid:
        s = check_claims(lambda r, ctx: True, [{"x": 1}, {"x": 2}])
    assert get_results(run_id=rid) and summary(run_id=rid)["total"] == 2


def test_check_claims_raise_mode_after_loop():
    with pytest.raises(VerificationError):
        check_claims(lambda r, ctx: False, [{"a": 1}, {"b": 2}], mode=Mode.RAISE)
    # everything still recorded before the raise
    assert summary()["total"] == 2


def test_check_claims_str():
    s = check_claims(lambda r, ctx: True, [{"a": 1}])
    assert "1/1 passed" in str(s)


# --- report ------------------------------------------------------------------
def test_report_renders_table():
    @verify(lambda r, c: r > 0, mode=Mode.SILENT)
    def f(n):
        return n

    f(1); f(2); f(-1)
    out = report()
    assert "f" in out
    assert "TOTAL" in out
    assert "66.7%" in out  # 2/3 pass rate


def test_report_empty():
    assert report() == "no verification results"


def test_report_scoped_to_run():
    @verify(lambda r, c: True, mode=Mode.SILENT)
    def ok():
        return 1

    with run("r1") as rid:
        ok()
    ok()  # outside the run
    out = report(run_id=rid)
    assert "TOTAL" in out
    # exactly one row counted in this run
    assert out.count("\n") >= 2


def test_report_from_jsonl_sees_everything(tmp_path):
    # > ring size: jsonl must capture all 1200, report() only sees 1000
    path = str(tmp_path / "audit.jsonl")
    add_sink(jsonl_sink(path))

    @verify(lambda r, c: r % 10 != 0, mode=Mode.SILENT)
    def mostly_ok(i):
        return i

    with run("big") as rid:
        for i in range(1200):
            mostly_ok(i)

    out = report_from_jsonl(path)
    assert "1200" in out          # exact total from the file
    assert "mostly_ok" in out
    assert "120" in out           # 120 failures (i % 10 == 0)
    # summary() (lifetime counters) agrees:
    s = summary(run_id=rid)
    assert s["total"] == 1200 and s["failed"] == 120


def test_report_from_jsonl_missing_file(tmp_path):
    with pytest.raises(OSError):
        report_from_jsonl(str(tmp_path / "nope.jsonl"))
