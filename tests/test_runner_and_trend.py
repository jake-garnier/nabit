import json

import pytest

import nabit
from nabit import audit, check_claims, get_audits, load_audits, run_audit
from nabit.dashboard import (build_html, load_history, load_records,
                             _latest_run_file, write_dashboard)


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(get_audits())
    yield
    for k in set(get_audits()) - before:
        nabit.runner._AUDITS.pop(k, None)


# --- audit registration -------------------------------------------------------
def test_audit_decorator_registers():
    @audit
    def my_check():
        """does a thing"""
        check_claims(lambda r, ctx: True, [{"a": 1}])

    assert "my_check" in get_audits()
    assert get_audits()["my_check"] is my_check


def test_load_audits_from_file(tmp_path):
    mod = tmp_path / "nabit_audits.py"
    mod.write_text(
        "from nabit import audit, check_claims\n"
        "@audit\n"
        "def alpha():\n"
        "    check_claims(lambda r, ctx: True, [{'x': 1}])\n"
        "@audit\n"
        "def beta():\n"
        "    check_claims(lambda r, ctx: False, [{'x': 2}])\n"
    )
    loaded = load_audits(mod)
    assert sorted(loaded) == ["alpha", "beta"]
    # loading doesn't leave them in the host process registry
    assert "alpha" not in get_audits()


# --- run_audit records history -------------------------------------------------
def test_run_audit_writes_run_file_and_history(tmp_path):
    hdir = tmp_path / "hist"

    @audit
    def mixed():
        check_claims(lambda r, ctx: r["ok"], [
            {"ok": True, "i": 1}, {"ok": True, "i": 2}, {"ok": False, "i": 3}],
            name="thing -> is ok")

    res = run_audit(mixed, history_dir=hdir)
    assert res["total"] == 3 and res["passed"] == 2 and res["failed"] == 1
    assert res["audit"] == "mixed"
    assert res["run_id"].startswith("mixed-")

    # full results file exists with the records
    run_file = tmp_path / res["run_file"] if not res["run_file"].startswith("/") else None
    rf = res["run_file"]
    recs = load_records([rf])
    assert len(recs) == 3
    assert all(r["run_id"] == res["run_id"] for r in recs)

    # compact history line appended
    lines = load_history(hdir)
    assert len(lines) == 1
    h = lines[0]
    assert h["audit"] == "mixed" and h["failed"] == 1
    assert h["checks"]["thing -> is ok"] == {"total": 3, "passed": 2, "failed": 1}
    assert h["_source"] == "hist"

    # latest-run discovery for the dashboard
    assert _latest_run_file(hdir) == rf


def test_run_audit_second_run_appends_history(tmp_path):
    hdir = tmp_path / "hist"

    @audit
    def repeated():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    run_audit(repeated, history_dir=hdir)
    run_audit(repeated, history_dir=hdir)
    assert len(load_history(hdir)) == 2
    # two distinct run files
    files = list(hdir.glob("*.jsonl"))
    files = [f for f in files if f.name != "history.jsonl"]
    assert len(files) == 2


def test_run_audit_does_not_leak_sinks(tmp_path):
    import nabit as n

    @audit
    def plain():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    before = len(nabit.core._SINKS)
    run_audit(plain, history_dir=tmp_path)
    assert len(nabit.core._SINKS) == before


# --- dashboard trend mode -------------------------------------------------------
def test_dashboard_from_history_dir(tmp_path):
    hdir = tmp_path / "captions-daily"

    @audit
    def dash_audit():
        check_claims(lambda r, ctx: r["ok"], [
            {"ok": True}, {"ok": False, "why": "broken"}], name="check -> ok")

    run_audit(dash_audit, history_dir=hdir)

    out = tmp_path / "dash.html"
    write_dashboard([str(hdir)], str(out))
    html = out.read_text()
    assert "historywrap" in html
    # latest run records embedded + history embedded
    import re
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                        html, re.S).group(1)
    data = json.loads(payload.replace("<\\/", "</"))
    assert len(data["history"]) == 1
    assert data["history"][0]["checks"]["check -> ok"]["failed"] == 1
    assert len(data["records"]) == 2


def test_dashboard_mixed_files_and_dirs(tmp_path):
    hdir = tmp_path / "daily"
    (hdir).mkdir()

    @audit
    def one():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    run_audit(one, history_dir=hdir)
    loose = tmp_path / "loose.jsonl"
    loose.write_text(json.dumps(
        {"name": "loose", "passed": True, "duration_ms": 1, "result": None,
         "error": None, "attempts": 1, "run_id": None, "timestamp": 1.0}) + "\n")

    out = tmp_path / "d.html"
    write_dashboard([str(hdir), str(loose)], str(out))
    payload = json.loads(
        out.read_text().split('<script id="data" type="application/json">')[1]
            .split("</script>")[0].replace("<\\/", "</"))
    assert len(payload["history"]) == 1
    # records from a history dir are labeled by the dir (audit) name
    assert {r["_source"] for r in payload["records"]} == {"daily", "loose"}
