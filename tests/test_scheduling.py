import json

import pytest

import nabit
from nabit import (audit, check_claims, get_audits, is_enabled, load_audits,
                   load_schedules, run_audit, run_mode, set_enabled)
from nabit.runner import crontab_lines
from nabit.dashboard import build_html, gather_inputs


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(get_audits())
    yield
    for k in set(get_audits()) - before:
        nabit.runner._AUDITS.pop(k, None)


# --- decorator metadata -------------------------------------------------------
def test_audit_bare_still_works():
    @audit
    def plain():
        pass

    assert plain._nabit_schedule is None
    assert plain._nabit_backfill is False


def test_audit_with_schedule_metadata():
    @audit(schedule="15 7 * * *")
    def scheduled():
        pass

    assert scheduled._nabit_schedule == "15 7 * * *"
    assert get_audits()["scheduled"] is scheduled


# --- run_mode ------------------------------------------------------------------
def test_run_audit_sets_mode(tmp_path):
    seen = []

    @audit
    def probe2():
        seen.append(run_mode())
        check_claims(lambda r, ctx: True, [{"a": 1}])

    run_audit(probe2, history_dir=tmp_path)
    run_audit(probe2, history_dir=tmp_path, mode="backfill")
    assert seen == ["scheduled", "backfill"]
    # modes recorded in history lines
    modes = [json.loads(l)["mode"] for l in
             (tmp_path / "history.jsonl").read_text().splitlines()]
    assert modes == ["scheduled", "backfill"]


# --- schedules.json state --------------------------------------------------------
def test_run_audit_writes_schedules_and_preserves_enabled(tmp_path):
    @audit(schedule="15 7 * * *")
    def job():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    run_audit(job, history_dir=tmp_path)
    sched = load_schedules(tmp_path)
    assert sched["job"]["cron"] == "15 7 * * *"
    assert sched["job"]["enabled"] is True
    assert sched["job"]["last_total"] == 1

    set_enabled(tmp_path, "job", False)
    assert is_enabled(tmp_path, "job") is False
    run_audit(job, history_dir=tmp_path)  # enabled state survives a real run
    assert load_schedules(tmp_path)["job"]["enabled"] is False


def test_run_audit_skip_if_disabled(tmp_path):
    @audit
    def skippable():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    res1 = run_audit(skippable, history_dir=tmp_path, skip_if_disabled=True)
    assert res1 is not None
    set_enabled(tmp_path, "skippable", False)
    res2 = run_audit(skippable, history_dir=tmp_path, skip_if_disabled=True)
    assert res2 is None
    lines = (tmp_path / "history.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1  # the disabled run never happened


def test_backfill_recorded_in_schedule_entry(tmp_path):
    @audit
    def historical():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    run_audit(historical, history_dir=tmp_path, mode="backfill")
    e = load_schedules(tmp_path)["historical"]
    assert "last_backfill_ts" in e
    assert "last_ts" not in e  # backfill doesn't count as a scheduled run


# --- crontab generator -------------------------------------------------------------
def test_crontab_lines(tmp_path):
    mod = tmp_path / "nabit_audits.py"
    mod.write_text(
        "from nabit import audit\n"
        "@audit(schedule='15 7 * * *')\n"
        "def morning(): pass\n"
        "@audit\n"
        "def manual(): pass\n"
    )
    audits = load_audits(mod)
    lines = crontab_lines(audits, mod, history_dir="/data/h")
    assert len(lines) == 1
    assert lines[0].startswith("15 7 * * * ")
    assert "run morning" in lines[0]
    assert "--audits" in lines[0] and "--out /data/h" in lines[0]


# --- dashboard: schedules panel data + backfill in history -------------------------
def test_dashboard_embeds_schedules_and_history(tmp_path):
    hdir = tmp_path / "captions_daily"

    @audit(schedule="15 7 * * *")
    def dash_audit():
        check_claims(lambda r, ctx: r["ok"],
                     [{"ok": True}, {"ok": False}], name="check -> ok")

    run_audit(dash_audit, history_dir=hdir)
    run_audit(dash_audit, history_dir=hdir, mode="backfill")

    html = build_html([], history=nabit.dashboard.load_history(hdir),
                      schedules=load_schedules(hdir))
    import re
    payload = json.loads(
        re.search(r'<script id="data" type="application/json">(.*?)</script>',
                  html, re.S).group(1).replace("<\\/", "</"))
    assert payload["served"] is False
    assert payload["schedules"]["dash_audit"]["cron"] == "15 7 * * *"
    assert len(payload["history"]) == 2
    assert {h["mode"] for h in payload["history"]} == {"scheduled", "backfill"}


def test_gather_inputs_merges_schedules(tmp_path):
    hdir = tmp_path / "daily"

    @audit
    def merged():
        check_claims(lambda r, ctx: True, [{"a": 1}])

    run_audit(merged, history_dir=hdir)
    records, history, schedules = gather_inputs([str(hdir)])
    assert len(records) == 1
    assert records[0]["_source"] == "daily"
    assert schedules["merged"]["history_dir"] == str(hdir)
