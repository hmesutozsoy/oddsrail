"""Financial invariants across abrupt OS process death and a fresh worker.

These tests use real LiveEngine/ExecutionStore instances in separate Python
processes. SQLite venue history lives separately from the execution ledger.
No network or financial account is involved; all paths belong to tmp_path.
"""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from oddsrail.live.store import ExecutionStore, LEASE_MS


pytestmark = pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGKILL"),
                                reason="Abrupt SIGKILL/pipe recovery tests require POSIX")

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "helpers" / "live_crash_worker.py"
ACCOUNT = "0x" + "2" * 40
AGENT = "crash-agent"
NOW = 1_800_000_000_000
DEADLINE_SECONDS = 5


class Worker:
    def __init__(self, tmp_path, *, action="submit", checkpoint="", generation=0):
        self.stderr_path = tmp_path / f"worker-{generation}-{action}.stderr"
        self.stderr = self.stderr_path.open("wb")
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(HELPER), "--store", str(tmp_path / "execution.sqlite"),
                 "--exchange", str(tmp_path / "exchange.sqlite"), "--now", str(NOW + generation * (LEASE_MS + 1)),
                 "--worker", f"worker-{generation}", "--action", action, "--checkpoint", checkpoint],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                bufsize=0,
            )
        except BaseException:
            self.stderr.close()
            raise
        self.buffer = b""

    def message(self):
        deadline = time.monotonic() + DEADLINE_SECONDS
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(f"Worker missed IPC deadline: {self.stderr_path.read_text()}")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                pytest.fail(f"Worker missed IPC deadline: {self.stderr_path.read_text()}")
            chunk = os.read(self.process.stdout.fileno(), 65_536)
            if not chunk:
                pytest.fail(f"Worker exited before IPC message: {self.stderr_path.read_text()}")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def at(self, checkpoint):
        assert self.message() == {"event": "checkpoint", "name": checkpoint}

    def resume(self):
        self.process.stdin.write(b"continue\n")
        self.process.stdin.flush()

    def kill(self):
        assert self.process.poll() is None, "The parent must kill a live checkpointed process"
        self.process.kill()
        assert self.process.wait(timeout=DEADLINE_SECONDS) == -signal.SIGKILL

    def finished(self):
        message = self.message()
        assert message["event"] == "result"
        assert self.process.wait(timeout=DEADLINE_SECONDS) == 0, self.stderr_path.read_text()
        return message

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=DEADLINE_SECONDS)
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()


@contextmanager
def worker(tmp_path, **kwargs):
    child = Worker(tmp_path, **kwargs)
    try:
        yield child
    finally:
        child.close()


def venue_rows(tmp_path, table="attempts"):
    assert table in {"attempts", "orders", "cancels"}
    with closing(sqlite3.connect(tmp_path / "exchange.sqlite")) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def store_for(tmp_path, generation=1):
    return ExecutionStore(tmp_path / "execution.sqlite", clock=lambda: NOW + generation * (LEASE_MS + 1))


def assert_recovery_cancellations(tmp_path, known_orders, *, after=0):
    attempts = venue_rows(tmp_path, "cancels")
    canceled_hashes = {row["order_hash"] for row in attempts[after:]}
    accepted_nonterminal = {row["order_hash"] for row in known_orders if not row["final"]}
    settled = {row["order_hash"] for row in known_orders if row["final"]}
    assert accepted_nonterminal <= canceled_hashes, (
        "Recovery must attempt to cancel surviving orders, not only pause future submissions"
    )
    assert not settled.intersection(canceled_hashes), "Authoritatively finalized fills need no cancellation"
    return len(attempts)


def assert_recovered(tmp_path, *, committed, submissions):
    before = venue_rows(tmp_path)
    known_orders = venue_rows(tmp_path, "orders")
    assert len(before) == submissions
    assert venue_rows(tmp_path, "cancels") == []
    with worker(tmp_path, action="recover", generation=1) as restarted:
        result = restarted.finished()
    assert result["results"] == ["paused", "already_recorded", "already_recorded"]
    assert result["recovered_agent_state"] == result["agent_state"] == "paused"
    assert result["status"]["committed_micro"] == committed
    assert result["status"]["blocked"]
    assert result["error"] is None
    assert venue_rows(tmp_path) == before, "Restart/replayed intent must not submit another order"
    rows = store_for(tmp_path).orders(ACCOUNT)
    assert len(rows) == 2
    assert sum(row["committed"] for row in rows) == committed
    cancellation_count = assert_recovery_cancellations(tmp_path, known_orders)
    # A second independent process restart must preserve the same invariant.
    with worker(tmp_path, action="recover", generation=2) as restarted_again:
        second = restarted_again.finished()
    assert second["results"] == ["paused", "already_recorded", "already_recorded"]
    assert second["status"]["committed_micro"] == committed
    assert second["agent_state"] == "paused"
    assert venue_rows(tmp_path) == before
    # The fake venue deliberately acknowledges cancel requests without
    # proving termination. Another restart must retry those surviving orders,
    # while confirmed fills remain committed and receive no cancel requests.
    assert_recovery_cancellations(tmp_path, known_orders, after=cancellation_count)
    with closing(sqlite3.connect(tmp_path / "execution.sqlite")) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    return store_for(tmp_path, 2).orders(ACCOUNT)


@pytest.mark.parametrize("checkpoint,submissions,committed,precrash_states", [
    ("pair_reserved", 0, 0, ["reserved", "reserved"]),
    ("hashes_bound", 0, 0, ["reserved", "reserved"]),
    ("dispatch_committed", 0, 4_800_000, ["dispatching", "reserved"]),
    ("exchange_accepted", 1, 4_800_000, ["dispatching", "reserved"]),
    ("acknowledgment_committed", 1, 4_800_000, ["open", "reserved"]),
])
def test_sigkill_at_submission_boundaries_preserves_money_and_never_resubmits(
        tmp_path, checkpoint, submissions, committed, precrash_states):
    with worker(tmp_path, checkpoint=checkpoint) as interrupted:
        interrupted.at(checkpoint)
        store = store_for(tmp_path)
        before = store.orders(ACCOUNT)
        assert sorted(row["state"] for row in before) == sorted(precrash_states)
        assert store.status(ACCOUNT)["committed_micro"] == 9_600_000
        if checkpoint == "pair_reserved":
            assert all(row["order_hash"] is None for row in before)
        else:
            assert all(row["order_hash"] for row in before)
        assert len(venue_rows(tmp_path)) == submissions
        interrupted.kill()
        assert store.orders(ACCOUNT) == before, "SIGKILL must not run the engine's graceful cleanup"
        assert store.agent(ACCOUNT, AGENT)["state"] == "running"

    after = assert_recovered(tmp_path, committed=committed, submissions=submissions)
    if checkpoint == "dispatch_committed":
        unknown = [row for row in after if not row["terminal"]]
        assert len(unknown) == 1 and unknown[0]["state"] == "unknown"
        assert unknown[0]["committed"] == 4_800_000, "Missing venue lookup is not proof of rejection"
    if committed == 0:
        assert all(row["terminal"] and row["state"] == "rejected" for row in after)


@pytest.mark.parametrize("state,matched,confirmed,final,committed", [
    ("matched", "10", "4", False, 4_800_000),
    ("filled", "10", "10", True, 4_800_000),
    ("canceled", "4", "4", True, 1_920_000),
])
def test_fill_evidence_survives_process_death_and_retains_committed_capital(
        tmp_path, state, matched, confirmed, final, committed):
    with worker(tmp_path, checkpoint="exchange_accepted") as interrupted:
        interrupted.at("exchange_accepted")
        interrupted.kill()
    # The fake exchange evolves independently while the OddsRail worker is
    # dead. Settlement evidence must survive in its own durable database.
    with closing(sqlite3.connect(tmp_path / "exchange.sqlite")) as db:
        db.execute("UPDATE orders SET state=?,matched=?,confirmed=?,final=?",
                   (state, matched, confirmed, int(final)))
        db.commit()
    rows = assert_recovered(tmp_path, committed=committed, submissions=1)
    filled = next(row for row in rows if row["ever_accepted"])
    assert filled["matched"] == matched
    assert filled["confirmed"] == confirmed
    assert bool(filled["terminal"]) is final
    assert filled["committed"] == committed


def test_stale_process_cannot_submit_or_release_the_replacement_workers_lease(tmp_path):
    with worker(tmp_path, checkpoint="hashes_bound") as stale:
        stale.at("hashes_bound")
        # Expire the lease logically in another process, without sleeping for
        # the production 30-second lease or racing a heartbeat timer.
        with worker(tmp_path, action="takeover", checkpoint="takeover_holds_lease", generation=1) as replacement:
            replacement.at("takeover_holds_lease")
            with closing(sqlite3.connect(tmp_path / "execution.sqlite")) as db:
                expected_lease = db.execute("SELECT worker,fence,lease_until FROM live_accounts WHERE account=?",
                                            (ACCOUNT,)).fetchone()
            assert expected_lease[0] == "worker-1"
            stale.resume()
            old_result = stale.finished()
            assert old_result["error"] == "lease_lost"
            assert old_result["agent_state"] == "paused"
            assert venue_rows(tmp_path) == []
            with closing(sqlite3.connect(tmp_path / "execution.sqlite")) as db:
                assert db.execute("SELECT worker,fence,lease_until FROM live_accounts WHERE account=?",
                                  (ACCOUNT,)).fetchone() == expected_lease
            replacement.resume()
            assert replacement.finished()["agent_state"] == "paused"
    assert store_for(tmp_path).status(ACCOUNT)["committed_micro"] == 0


def test_sqlite_write_failure_rolls_back_both_orders_and_their_reservations(tmp_path):
    with worker(tmp_path, checkpoint="before_submit") as submitting:
        submitting.at("before_submit")
        # Trigger an actual SQLite statement failure after the first order and
        # its audit event have been written inside reserve_pair's transaction.
        # This works under privileged CI users, unlike chmod/disk-permission tests.
        with closing(sqlite3.connect(tmp_path / "execution.sqlite")) as db:
            db.execute("""CREATE TRIGGER reject_second_order BEFORE INSERT ON live_orders
                          WHEN NEW.token_id='456'
                          BEGIN SELECT RAISE(ABORT, 'injected second order write failure'); END""")
            db.commit()
        submitting.resume()
        result = submitting.finished()
    assert result["write_failures"] == ["SQLITE_CONSTRAINT_TRIGGER"]
    assert result["results"] == ["running", "paused"]
    assert result["error"] == "venue_operation_failed"
    store = store_for(tmp_path)
    assert store.orders(ACCOUNT) == []
    assert store.status(ACCOUNT)["committed_micro"] == 0
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert not any(event["kind"] == "order_reserved" for event in store.history(ACCOUNT))
    assert venue_rows(tmp_path) == []
    with closing(sqlite3.connect(tmp_path / "execution.sqlite")) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
