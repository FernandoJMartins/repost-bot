import asyncio

from conftest import DST, SRC, FakeApi, FakeLimiter, NetworkError, RetryAfter, TelegramError, make_cfg

from repost_bot.db import Database, IngestItem
from repost_bot.worker import Worker


def setup(tmp_path, script=None, **cfgkw):
    cfg = make_cfg(tmp_path, **cfgkw)
    db = Database(cfg.db_path)
    api = FakeApi(script)
    lim = FakeLimiter()
    return cfg, db, api, lim, Worker(cfg, db, api, lim, clock=lambda: 1e12)


def run_all(db, w, cfg, max_steps=50):
    """Processa a fila até esvaziar, ignorando os tempos de espera."""
    for _ in range(max_steps):
        unit = db.next_unit(cfg.source_chat_id, 0, 0)
        if unit is None:
            return
        asyncio.run(w.process(unit))


def ingest(db, *mids, **kw):
    db.ingest(SRC, [IngestItem(m, kw.get("kind", "text"), "pending", kw.get("group")) for m in mids])


def test_single_copy_maps_ids(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [555])
    ingest(db, 10)
    run_all(db, w, cfg)
    r = db.get(SRC, 10)
    assert r.status == "done" and r.dest_message_id == 555
    assert api.calls == [("copyMessage", [10])]


def test_album_copied_in_single_call(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path)
    ingest(db, 1, 2, 3, kind="photo", group="G")
    run_all(db, w, cfg)
    assert api.calls == [("copyMessages", [1, 2, 3])]
    assert lim.acquired == [3]
    assert db.counts(SRC)["done"] == 3


def test_429_retries_without_counting_attempt_and_no_duplicate(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [RetryAfter("copyMessage", 429, "Too Many Requests", 7), 900])
    ingest(db, 1)
    run_all(db, w, cfg)
    assert lim.penalties == [7]
    assert [c[0] for c in api.calls] == ["copyMessage", "copyMessage"]
    r = db.get(SRC, 1)
    assert r.status == "done" and r.dest_message_id == 900 and r.attempts == 1


def test_not_found_is_skipped(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [TelegramError("copyMessage", 400,
                                                          "Bad Request: message to copy not found")])
    ingest(db, 1)
    run_all(db, w, cfg)
    assert db.get(SRC, 1).status == "skipped"


def test_read_timeout_is_held_as_uncertain_and_not_resent(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [NetworkError("copyMessage", "ReadTimeout", True)])
    ingest(db, 1, 2)
    run_all(db, w, cfg)
    assert db.get(SRC, 1).status == "uncertain"
    assert db.get(SRC, 2).status == "done"
    assert api.calls.count(("copyMessage", [1])) == 1
    assert api.sent and "incerta" in api.sent[0][1]


def test_connect_error_is_retried(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [NetworkError("copyMessage", "ConnectError", False), 77])
    ingest(db, 1)
    run_all(db, w, cfg)
    assert db.get(SRC, 1).dest_message_id == 77


def test_gives_up_after_max_attempts(tmp_path):
    err = TelegramError("copyMessage", 400, "Bad Request: something odd")
    cfg, db, api, lim, w = setup(tmp_path, [err, err, err], max_attempts=3)
    ingest(db, 1)
    run_all(db, w, cfg)
    r = db.get(SRC, 1)
    assert r.status == "failed" and r.attempts == 3


def test_permission_error_pauses_and_keeps_message(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [TelegramError("copyMessage", 403, "Forbidden: bot was kicked")])
    ingest(db, 1)
    asyncio.run(w.process(db.next_unit(SRC, 0, 0)))
    assert db.is_paused()
    assert db.get(SRC, 1).status == "pending"


def test_gap_batch_partial_result(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path, [[901, 902]])
    db.ingest(SRC, [], gap_ids=[5, 6, 7])
    run_all(db, w, cfg)
    assert api.calls == [("copyMessages", [5, 6, 7])]
    assert all(db.get(SRC, m).status == "done" for m in (5, 6, 7))


def test_gap_batch_rejected_is_split(tmp_path):
    bad = TelegramError("copyMessages", 400, "Bad Request: message can't be copied")
    cfg, db, api, lim, w = setup(tmp_path, [bad, 11, bad])
    db.ingest(SRC, [], gap_ids=[1, 2])
    run_all(db, w, cfg)
    assert api.calls == [("copyMessages", [1, 2]), ("copyMessage", [1]), ("copyMessage", [2])]
    assert db.get(SRC, 1).status == "done"
    assert db.get(SRC, 2).status == "skipped"


def test_restart_after_crash_mid_send_does_not_duplicate(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path)
    ingest(db, 1)
    unit = db.next_unit(SRC, 0, 0)
    db.mark_sending(unit.row_ids)  # processo morre aqui
    db.close()

    db2 = Database(cfg.db_path)
    db2.recover_on_startup("hold")
    api2 = FakeApi()
    w2 = Worker(cfg, db2, api2, FakeLimiter(), clock=lambda: 1e12)
    run_all(db2, w2, cfg)
    assert api2.calls == []
    assert db2.get(SRC, 1).status == "uncertain"


def test_worker_respects_pause(tmp_path):
    cfg, db, api, lim, w = setup(tmp_path)
    ingest(db, 1)
    db.set_paused(True, "teste")

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await t

    asyncio.run(go())
    assert api.calls == []
