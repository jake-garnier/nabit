"""Tests for nabit.contrib.celery run-id propagation.

Drives the real Celery signals directly (no broker, no worker needed): we
publish into headers the way `apply_async` would, then fire task_prerun /
task_postrun the way a worker would around task execution.
"""

import pytest

celery = pytest.importorskip("celery")  # dev extra: pip install nabit[dev]

import nabit
from nabit import verify, run, summary, get_results, clear_results, Mode
from nabit.core import _current_run
from nabit.contrib.celery import install
from celery.signals import before_task_publish, task_prerun, task_postrun


class FakeTask:
    """Mimics the slice of a Celery task the signal handlers touch."""

    def __init__(self, headers=None):
        self.request = type("R", (), {"headers": headers or {}})()


@pytest.fixture(autouse=True)
def _reset():
    clear_results()
    yield
    clear_results()


def test_install_is_idempotent():
    install()
    install()  # second call must not double-register handlers
    headers = {}
    with run("abc") as rid:
        before_task_publish.send(sender="t", headers=headers)
    assert headers.get("nabit_run_id") == "abc"


def test_run_id_rides_publish_headers():
    install()
    headers = {}
    with run("signup-123"):
        before_task_publish.send(sender="t", headers=headers)
    assert headers["nabit_run_id"] == "signup-123"


def test_no_active_run_leaves_headers_alone():
    install()
    headers = {}
    before_task_publish.send(sender="t", headers=headers)
    assert "nabit_run_id" not in headers


def test_worker_restores_and_resets_run_id():
    install()
    task = FakeTask({"nabit_run_id": "signup-123"})

    assert _current_run.get() is None
    task_prerun.send(sender=task, task=task)
    assert _current_run.get() == "signup-123"   # task body sees the run id
    task_postrun.send(sender=task, task=task)
    assert _current_run.get() is None           # cleaned up afterwards


def test_worker_without_header_untouched():
    install()
    task = FakeTask({})  # no nabit header
    task_prerun.send(sender=task, task=task)
    assert _current_run.get() is None
    task_postrun.send(sender=task, task=task)


def test_end_to_end_publisher_to_worker():
    """The full story: run() at publish -> header -> restored in the worker,
    and verifications inside the 'task' carry the run id."""
    install()

    # --- publisher process ---
    headers = {}
    with run("order-42"):
        before_task_publish.send(sender="t", headers=headers)

    # --- (message crosses a process boundary here) ---

    # --- worker process ---
    task = FakeTask(headers)
    task_prerun.send(sender=task, task=task)

    @verify(lambda r, ctx: r is True, mode=Mode.SILENT)
    def charge():
        return True

    charge()

    s = summary(run_id="order-42")
    assert s["total"] == 1 and s["passed"] == 1
    assert get_results()[0].run_id == "order-42"

    task_postrun.send(sender=task, task=task)
    assert _current_run.get() is None
