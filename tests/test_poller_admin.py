import asyncio

from conftest import ADMIN, SRC, FakeApi, make_cfg

from repost_bot.admin import AdminCommands
from repost_bot.db import Database
from repost_bot.poller import Poller


def post(update_id, mid, **extra):
    msg = {"message_id": mid, "chat": {"id": SRC, "type": "channel"}, "date": 1}
    msg.update(extra or {"text": f"m{mid}"})
    return {"update_id": update_id, "channel_post": msg}


def make(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    db = Database(cfg.db_path)
    handled = []

    async def on_admin(m):
        handled.append(m)

    return cfg, db, Poller(cfg, db, FakeApi(), on_admin), handled


def test_process_enqueues_and_advances_offset(tmp_path):
    cfg, db, p, _ = make(tmp_path)
    off = asyncio.run(p.process([post(100, 1), post(101, 2, photo=[{"file_unique_id": "x"}])]))
    assert off == 102 and db.get_offset() == 102
    assert db.get(SRC, 2).kind == "photo" and db.get(SRC, 2).file_unique_id == "x"


def test_gap_detected_after_downtime(tmp_path):
    cfg, db, p, _ = make(tmp_path)
    asyncio.run(p.process([post(1, 10)]))
    asyncio.run(p.process([post(2, 15)]))
    gaps = [m for m in range(11, 15) if db.get(SRC, m) and db.get(SRC, m).origin == "gap"]
    assert gaps == [11, 12, 13, 14]


def test_first_run_does_not_backfill_history(tmp_path):
    cfg, db, p, _ = make(tmp_path)
    asyncio.run(p.process([post(1, 5000)]))
    assert db.counts(SRC)["pending"] == 1


def test_large_gap_not_auto_filled(tmp_path):
    cfg, db, p, _ = make(tmp_path, gap_auto_max=5)
    asyncio.run(p.process([post(1, 10)]))
    asyncio.run(p.process([post(2, 100)]))
    assert db.counts(SRC)["pending"] == 2


def test_service_message_recorded_as_skipped(tmp_path):
    cfg, db, p, _ = make(tmp_path)
    asyncio.run(p.process([post(1, 3, pinned_message={"message_id": 1})]))
    assert db.get(SRC, 3).status == "skipped"


def test_other_chats_ignored_and_private_commands_dispatched(tmp_path):
    cfg, db, p, handled = make(tmp_path)
    other = {"update_id": 5, "message": {"message_id": 1, "chat": {"id": -999, "type": "group"}, "text": "hi"}}
    cmd = {"update_id": 6, "message": {"message_id": 2, "chat": {"id": ADMIN, "type": "private"},
                                       "from": {"id": ADMIN}, "text": "/status"}}
    asyncio.run(p.process([other, cmd]))
    assert db.counts(SRC)["pending"] == 0
    assert len(handled) == 1


def test_admin_authorization(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    api = FakeApi()
    adm = AdminCommands(cfg, db, api)
    intruder = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "/pause"}
    asyncio.run(adm.handle(intruder))
    assert not db.is_paused() and api.sent == []

    in_group = {"chat": {"id": -5, "type": "supergroup"}, "from": {"id": ADMIN}, "text": "/pause"}
    asyncio.run(adm.handle(in_group))
    assert not db.is_paused()

    me = {"chat": {"id": ADMIN, "type": "private"}, "from": {"id": ADMIN}, "text": "/pause"}
    asyncio.run(adm.handle(me))
    assert db.is_paused() and api.sent


def test_admin_commands_text(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    adm = AdminCommands(cfg, db, FakeApi())
    assert "Estado" in adm.execute("/status", [])
    assert "Total registrado" in adm.execute("/stats", [])
    assert "enfileirado" in adm.execute("/backfill", ["1", "10"])
    assert db.counts(SRC)["pending"] == 10
    assert "BACKFILL_MAX" in adm.execute("/backfill", ["1", "999999"])
    assert adm.execute("/retry", ["all"]).startswith("0")
