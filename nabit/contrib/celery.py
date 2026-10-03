"""Run-id propagation across the Celery process boundary.

nabit's `run()` uses a contextvar, which dies at a process boundary — and a
Celery task runs in a *different process* than the code that queued it. So
this doesn't correlate:

    with run("signup-123"):
        process.delay(user_id)     # worker sees no run id

This module fixes it by riding the run id in the Celery message headers:
`before_task_publish` (publisher process) injects the current run id;
`task_prerun` (worker process) restores it; `task_postrun` resets it.

Usage — call once at startup, next to your Celery app config:

    # pip install nabit[celery]
    from nabit.contrib.celery import install

    install()          # wire the signals

    # publisher side — just keep using run():
    with run("signup-123"):
        process.delay(user_id)

    # worker side — verifications inside the task now carry run "signup-123":
    @app.task
    def process(user_id):
        do_work(user_id)           # @verify results get run_id=signup-123
        return summary()           # scoped to this task's inherited run id

No Celery models or config are touched; if no `run()` is active when a task
is published, nothing is injected and behavior is unchanged.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

try:
    from celery.signals import before_task_publish, task_postrun, task_prerun
except ImportError as exc:  # pragma: no cover — opt-in extra
    raise ImportError(
        "nabit.contrib.celery requires celery. Install it with: pip install nabit[celery]"
    ) from exc

from ..core import _current_run

_HEADER = "nabit_run_id"
_local = threading.local()
_installed = False


def _inject_run_id(sender: Any = None, headers: Optional[dict] = None, **kwargs) -> None:
    """Publisher side: stamp the current run id into the outgoing message."""
    rid = _current_run.get()
    if rid and isinstance(headers, dict):
        headers.setdefault(_HEADER, rid)


def _restore_run_id(sender: Any = None, task: Any = None, **kwargs) -> None:
    """Worker side: restore the run id from the message headers for this task."""
    rid = None
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", None)
    if isinstance(headers, dict):
        rid = headers.get(_HEADER)
    if rid:
        _local.token = _current_run.set(rid)


def _reset_run_id(sender: Any = None, task: Any = None, **kwargs) -> None:
    """Worker side: clear the contextvar after the task finishes."""
    token = getattr(_local, "token", None)
    if token is not None:
        _current_run.reset(token)
        _local.token = None


def install() -> None:
    """Wire nabit run-id propagation onto Celery's signals. Idempotent —
    call it once at startup (import side effects in a celery config module
    are the usual spot)."""
    global _installed
    if _installed:
        return
    _installed = True
    before_task_publish.connect(_inject_run_id)
    task_prerun.connect(_restore_run_id)
    task_postrun.connect(_reset_run_id)


__all__ = ["install"]
