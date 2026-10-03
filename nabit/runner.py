"""Repeatable, schedulable audit runs — with backfill and dashboard controls.

Define audits once, run them on a schedule:

    # nabit_audits.py
    from nabit import audit, check_claims, run_mode

    @audit(schedule="15 7 * * *")            # metadata shown in the dashboard
    def captions_daily():
        '''every claim the captions pipeline makes, against real state'''
        if run_mode() == "backfill":
            rows = all_rows()                 # startup: audit the full history
        else:
            rows = recent_rows(hours=48)      # recurring: recent window only
        check_claims(check, rows, name="...")

Then:

    python -m nabit run captions_daily --backfill      # once, at setup:
                                                       # historical census becomes
                                                       # the first trend point
    python -m nabit crontab                            # ready-to-paste crontab
    python -m nabit serve /data/nabit-history          # local UI with enable/
                                                       # disable + run-now controls

Each run writes a timestamped JSONL (full results), a compact line to
history.jsonl (trends), and updates schedules.json (last run + enabled state,
which `nabit run` respects — so the dashboard's toggle actually controls cron).
"""

from __future__ import annotations

import contextvars
import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_AUDITS: dict[str, Callable] = {}

# 'scheduled' (recurring runs) or 'backfill' (full historical census at setup)
_run_mode_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nabit_run_mode", default="scheduled")


def run_mode() -> str:
    """The current run's mode: 'backfill' widens claim windows to the full
    history; 'scheduled' keeps them to the recent window. Audit functions
    branch on this so one definition serves both purposes."""
    return _run_mode_var.get()


def audit(fn: Optional[Callable] = None, *, schedule: Optional[str] = None,
          backfill: bool = False):
    """Register a function as a named audit.

    @audit                          # bare
    @audit(schedule="15 7 * * *")   # cron metadata, surfaced in the dashboard
                                    # and `nabit crontab`
    """
    def deco(f: Callable) -> Callable:
        f._nabit_schedule = schedule
        f._nabit_backfill = backfill
        _AUDITS[f.__name__] = f
        return f
    return deco(fn) if fn is not None else deco


def get_audits() -> dict[str, Callable]:
    return dict(_AUDITS)


def load_audits(path: str | Path) -> dict[str, Callable]:
    """Import a python file and return the audits it registered (they are
    removed from the in-process registry afterwards, so re-loading is clean)."""
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"_nabit_audits_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load audits from {path}")
    mod = importlib.util.module_from_spec(spec)
    before = set(_AUDITS)
    try:
        spec.loader.exec_module(mod)
        return {k: v for k, v in _AUDITS.items() if k not in before}
    finally:
        for k in list(_AUDITS):
            if k not in before:
                del _AUDITS[k]


# --- schedules.json (shared state between cron, the runner, and the UI) -------
def _schedules_path(history_dir: str | Path) -> Path:
    return Path(history_dir) / "schedules.json"


def load_schedules(history_dir: str | Path) -> dict:
    p = _schedules_path(history_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _save_schedules(history_dir: str | Path, schedules: dict) -> None:
    _schedules_path(history_dir).write_text(
        json.dumps(schedules, indent=2, default=str), encoding="utf-8")


def set_enabled(history_dir: str | Path, audit_name: str, enabled: bool) -> dict:
    """Enable/disable an audit. `nabit run` skips disabled audits — this is
    the switch the dashboard's toggle flips."""
    schedules = load_schedules(history_dir)
    entry = schedules.setdefault(audit_name, {})
    entry["enabled"] = enabled
    _save_schedules(history_dir, schedules)
    return entry


def is_enabled(history_dir: str | Path, audit_name: str) -> bool:
    return load_schedules(history_dir).get(audit_name, {}).get("enabled", True)


def crontab_lines(audits: dict[str, Callable], audits_path: str | Path,
                  history_dir: str | Path = "nabit-history") -> list[str]:
    """Ready-to-paste crontab entries for every audit carrying schedule
    metadata."""
    out = []
    apath = Path(audits_path).resolve()
    for name in sorted(audits):
        cron = getattr(audits[name], "_nabit_schedule", None)
        if not cron:
            continue
        out.append(
            f"{cron} cd {apath.parent} && python -m nabit run {name} "
            f"--audits {apath} --out {history_dir} # nabit {name}"
        )
    return out


def run_audit(
    fn: Callable,
    history_dir: str | Path = "nabit-history",
    run_id: Optional[str] = None,
    mode: str = "scheduled",
    skip_if_disabled: bool = False,
) -> Optional[dict]:
    """Run one audit and record history. Writes:
      <history_dir>/<UTC timestamp>.jsonl   full results (dashboard drill-down)
      <history_dir>/history.jsonl           compact aggregate line (trends)
      <history_dir>/schedules.json          last-run + enabled state (the UI)
    mode='backfill' marks the run as the historical baseline (see run_mode()).
    Returns the summary dict, or None if skipped as disabled.
    """
    from .core import _SINKS, clear_results, run, summary  # late: import cycle

    name = fn.__name__
    if skip_if_disabled and not is_enabled(history_dir, name):
        return None

    ts = datetime.now(timezone.utc)
    hdir = Path(history_dir)
    hdir.mkdir(parents=True, exist_ok=True)
    base = ts.strftime("%Y%m%dT%H%M%SZ")
    run_file = hdir / f"{base}.jsonl"
    i = 1
    while run_file.exists():  # same-second runs must not clobber each other
        run_file = hdir / f"{base}-{i}.jsonl"
        i += 1
    rid = run_id or f"{name}-{run_file.stem}"

    clear_results()
    per_check: dict[str, dict] = {}

    def _tally(result) -> None:
        a = per_check.setdefault(result.name, {"total": 0, "passed": 0, "failed": 0})
        a["total"] += 1
        if result.passed:
            a["passed"] += 1
        else:
            a["failed"] += 1

    sink = jsonl_sink(str(run_file))
    _SINKS.append(sink)
    _SINKS.append(_tally)
    t0 = time.perf_counter()
    token = _run_mode_var.set(mode)
    try:
        with run(rid):
            fn()
        s = summary()
    finally:
        _run_mode_var.reset(token)
        _SINKS.remove(_tally)
        _SINKS.remove(sink)

    line = {
        "audit": name,
        "run_id": rid,
        "mode": mode,
        "ts": ts.timestamp(),
        "ts_str": ts.strftime("%Y-%m-%d %H:%M UTC"),
        "duration_s": round(time.perf_counter() - t0, 3),
        "total": s["total"],
        "passed": s["passed"],
        "failed": s["failed"],
        "file": run_file.name,
        "checks": per_check,
    }
    with open(hdir / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str) + "\n")

    # update the schedule entry (preserving the dashboard's enabled toggle)
    schedules = load_schedules(hdir)
    entry = schedules.setdefault(name, {})
    entry.setdefault("enabled", True)
    if getattr(fn, "_nabit_schedule", None):
        entry["cron"] = fn._nabit_schedule
    if mode == "backfill":
        entry["last_backfill_ts"] = line["ts"]
        entry["last_backfill_failed"] = line["failed"]
    else:
        entry["last_ts"] = line["ts"]
        entry["last_failed"] = line["failed"]
        entry["last_total"] = line["total"]
        entry["last_file"] = line["file"]
    _save_schedules(hdir, schedules)

    out = dict(line)
    out["run_file"] = str(run_file)
    return out


def run_named(
    audit_name: str,
    audits_path: str | Path,
    history_dir: str | Path = "nabit-history",
    mode: str = "scheduled",
) -> Optional[dict]:
    """Load audits from a file and run one by name (the CLI path)."""
    audits = load_audits(audits_path)
    if audit_name not in audits:
        raise SystemExit(
            f"unknown audit {audit_name!r}; available: {', '.join(sorted(audits)) or 'none'}")
    return run_audit(audits[audit_name], history_dir=history_dir, mode=mode,
                     skip_if_disabled=True)


# imported late to avoid a circular import at module load
from .sinks import jsonl_sink  # noqa: E402
