"""Repeatable, schedulable audit runs.

Define audits once, run them on a schedule:

    # nabit_audits.py
    from nabit import audit, check_claims

    @audit
    def captions_daily():
        check_claims(caption_ok, claims, name="caption_extracted -> ...")
        check_claims(file_exists, paths, name="downloaded -> file on disk")

Then cron does the rest:

    */30 * * * * cd /srv/app && python -m nabit run captions_daily --out /data/nabit-history

Each run writes a timestamped JSONL (full results, for drill-down) plus one
compact aggregate line appended to history.jsonl (for the trend dashboard):

    python -m nabit dashboard /data/nabit-history      # trend + latest drill-down

`--strict` exits 1 if anything failed, so CI or cron mail catches regressions.
"""

from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_AUDITS: dict[str, Callable] = {}


def audit(fn: Callable) -> Callable:
    """Register a function as a named audit. The function body should call
    check_claims()/verify()-decorated functions; the runner provides the
    run id, sinks, and history bookkeeping around it."""
    _AUDITS[fn.__name__] = fn
    return fn


def get_audits() -> dict[str, Callable]:
    """All audits registered in this process."""
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


def run_audit(
    fn: Callable,
    history_dir: str | Path = "nabit-history",
    run_id: Optional[str] = None,
) -> dict:
    """Run one audit and record history. Writes:
      <history_dir>/<UTC timestamp>.jsonl   full results (dashboard drill-down)
      <history_dir>/history.jsonl           one compact aggregate line (trends)
    Returns the summary dict, including the run file path.
    """
    from .core import _SINKS, clear_results, get_results, run, summary  # local: late import cycle

    name = fn.__name__
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
    try:
        with run(rid):
            fn()
        s = summary()
    finally:
        _SINKS.remove(_tally)
        _SINKS.remove(sink)

    line = {
        "audit": name,
        "run_id": rid,
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

    out = dict(line)
    out["run_file"] = str(run_file)
    return out


def run_named(
    audit_name: str,
    audits_path: str | Path,
    history_dir: str | Path = "nabit-history",
) -> dict:
    """Load audits from a file and run one by name (the CLI path)."""
    audits = load_audits(audits_path)
    if audit_name not in audits:
        raise SystemExit(
            f"unknown audit {audit_name!r}; available: {', '.join(sorted(audits)) or 'none'}")
    return run_audit(audits[audit_name], history_dir=history_dir)


# imported late to avoid a circular import at module load
from .sinks import jsonl_sink  # noqa: E402
