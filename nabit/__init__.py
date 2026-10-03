"""nabit — did it actually work?

A dead-simple verification layer for LLM agents. Your agent says it's done;
nabit checks the real system state and tells you whether that was true — and
can re-run the action to fix it when it wasn't.

Basic usage:

    from nabit import verify
    from nabit.checks import has_keys

    @verify(lambda result, ctx: db.exists("customers", result["id"]),
            retries=2)                       # self-heal: re-run on failure
    def create_customer(name):
        # ... agent / tool does the work, claims success ...
        return {"id": 42, "status": "created"}

If the post-condition returns False, nabit records a failed verification,
re-runs the action up to `retries` times, and (depending on mode) warns or
raises — so a lying "success" can't slip through silently.

What sets it apart: self-heal retries, run-ID correlation, a composable check
library (nabit.checks), and pluggable sinks — all with zero dependencies.
"""

from .core import (
    verify,
    run,
    summary,
    failures,
    check_claims,
    BulkSummary,
    VerificationError,
    VerificationResult,
    get_results,
    clear_results,
    add_sink,
    clear_sinks,
    Mode,
)
from .sinks import jsonl_sink
from .report import report, report_from_jsonl
from .runner import (audit, run_audit, run_named, load_audits, get_audits,
                     run_mode, load_schedules, set_enabled, is_enabled)

from . import checks

__all__ = [
    "verify",
    "run",
    "summary",
    "failures",
    "check_claims",
    "BulkSummary",
    "VerificationError",
    "VerificationResult",
    "get_results",
    "clear_results",
    "add_sink",
    "clear_sinks",
    "jsonl_sink",
    "report",
    "report_from_jsonl",
    "audit",
    "run_audit",
    "run_named",
    "load_audits",
    "get_audits",
    "run_mode",
    "load_schedules",
    "set_enabled",
    "is_enabled",
    "checks",
    "Mode",
]

__version__ = "0.7.2"
