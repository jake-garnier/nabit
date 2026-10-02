"""Runnable tour of nabit.

    python examples/basic.py

Shows: catching a silent failure, self-healing a flaky action, grouping
verifications under a run id, and the composable check library.
"""

import logging

import nabit
from nabit import verify, run, summary, Mode, get_results
from nabit.checks import all_of, has_keys, field_equals

logging.basicConfig(level=logging.WARNING, format="%(message)s")

DB = set()  # a fake database so the example is self-contained


# ---- 1. Catch a silent failure ---------------------------------------------
@verify(lambda result, ctx: result["id"] in DB, mode=Mode.WARN)
def create_customer_lying(name):
    cid = abs(hash(name)) % 1000
    # oops — forgot to write to DB, but still reports success
    return {"id": cid, "status": "created"}


# ---- 2. Self-heal: a flaky action that succeeds on retry --------------------
_attempts = {"n": 0}


@verify(lambda result, ctx: result["id"] in DB, mode=Mode.WARN, retries=3)
def create_customer_flaky(name):
    _attempts["n"] += 1
    cid = abs(hash(name)) % 1000
    if _attempts["n"] >= 2:        # fails first time, writes on the 2nd attempt
        DB.add(cid)
    return {"id": cid, "status": "created"}


# ---- 3. Composable checks instead of hand-written lambdas -------------------
@verify(all_of(has_keys("id", "status"), field_equals("status", "created")),
        mode=Mode.SILENT)
def create_customer_shaped(name):
    cid = abs(hash(name)) % 1000
    DB.add(cid)
    return {"id": cid, "status": "created"}


if __name__ == "__main__":
    with run("signup-batch") as rid:
        print("1. lying tool   -> nabit should flag it")
        create_customer_lying("Ada Lovelace")

        print("2. flaky tool   -> nabit should self-heal and pass")
        create_customer_flaky("Alan Turing")

        print("3. shaped check -> composable, no lambda")
        create_customer_shaped("Grace Hopper")

    print("\n--- verification log ---")
    for r in get_results(run_id=rid):
        status = "PASS" if r.passed else "FAIL <-- silent failure caught"
        print(f"  {r.name:26} {status:34} attempts={r.attempts}")

    print("\n--- run summary ---")
    print(" ", summary(run_id=rid))
