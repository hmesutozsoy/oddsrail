"""Hourly runs for kept agents: due agents run once, results land on the
record, failures are recorded rather than raised."""

import time

from oddsrail.cloud import scheduler
from oddsrail.cloud.db import DB


async def test_tick_runs_due_agents_and_records_results(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.sqlite3")
    ledgers = tmp_path / "ledgers"; ledgers.mkdir()
    u1, u2, u3 = db.login_user("a@x.io"), db.login_user("b@x.io"), db.login_user("c@x.io")
    db.agent_put(u1["id"], "hourly one", "", {"on": {"report": True}}, "hourly")
    db.agent_put(u2["id"], "off", "", {"on": {}}, "off")
    db.agent_put(u3["id"], "ran recently", "", {"on": {}}, "hourly")
    db.agent_ran(u3["id"], {"ok": True})                     # last_run = now

    calls = []

    async def fake_run(cfg, ledger):
        calls.append((cfg, ledger.name))
        if cfg.get("boom"):
            raise RuntimeError("venue down")
        return {"ok": True, "started": "2026-09-07T10:00:00Z", "seconds": 1.2, "orders_placed": 2,
                "decisions": [1, 2, 3], "ledger": {"equity": 1001.5}, "halted": None}
    monkeypatch.setattr(scheduler.runner, "run_pass", fake_run)

    assert await scheduler.tick(db, ledgers) == 1
    assert calls == [({"on": {"report": True}}, f"{u1['id']}.json")]
    a = db.agent_get(u1["id"])
    assert a["runs"] == 1 and a["last_result"]["orders"] == 2 and a["last_result"]["equity"] == 1001.5
    assert await scheduler.tick(db, ledgers) == 0, "nothing is due twice within the hour"

    db.agent_put(u1["id"], "hourly one", "", {"boom": True}, "hourly")
    assert await scheduler.tick(db, ledgers, now=time.time() + 7200) == 2, "an hour later both hourly agents are due"
    a = db.agent_get(u1["id"])
    assert a["runs"] == 2 and a["last_result"]["ok"] is False and "venue down" in a["last_result"]["error"]
    assert db.agent_get(u3["id"])["runs"] == 2 and db.agent_get(u2["id"])["runs"] == 0
