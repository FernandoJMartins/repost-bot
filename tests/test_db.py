from conftest import SRC

from repost_bot.db import Database, IngestItem


def item(mid, kind="text", **kw):
    return IngestItem(source_message_id=mid, kind=kind, status=kw.pop("status", "pending"), **kw)


def test_ingest_is_idempotent_and_saves_offset(db):
    db.ingest(SRC, [item(10), item(11)], new_offset=501, max_seen=11)
    res = db.ingest(SRC, [item(10), item(11)], new_offset=501)  # reentrega após queda
    assert res.new_rows == 0
    assert db.counts(SRC)["pending"] == 2
    assert db.get_offset() == 501
    assert db.max_seen(SRC) == 11


def test_offset_persists_across_reopen(cfg):
    d = Database(cfg.db_path)
    d.ingest(SRC, [item(1)], new_offset=77)
    d.close()
    d2 = Database(cfg.db_path)
    assert d2.get_offset() == 77
    assert d2.counts(SRC)["pending"] == 1
    d2.close()


def test_done_message_is_never_reset_by_redelivery(db):
    db.ingest(SRC, [item(5)])
    u = db.next_unit(SRC, 0, 0)
    db.mark_sending(u.row_ids)
    db.mark_done(u.rows[0].id, -100, 900)
    db.ingest(SRC, [item(5)])
    assert db.get(SRC, 5).status == "done"
    assert db.next_unit(SRC, 0, 0) is None


def test_gap_placeholder_upgraded_by_real_update(db):
    db.ingest(SRC, [], gap_ids=[7])
    assert db.get(SRC, 7).kind == "unknown"
    db.ingest(SRC, [item(7, kind="photo", media_group_id=None)])
    r = db.get(SRC, 7)
    assert r.kind == "photo" and r.origin == "update"


def test_order_and_album_grouping(db):
    db.ingest(SRC, [item(3), item(1, kind="photo", media_group_id="A"),
                    item(2, kind="video", media_group_id="A")])
    u = db.next_unit(SRC, 0, 0)
    assert u.kind == "album" and u.message_ids == [1, 2]


def test_album_waits_for_settle(db):
    db.ingest(SRC, [item(1, kind="photo", media_group_id="A")], now=1000.0)
    u = db.next_unit(SRC, album_settle=3, gap_settle=0)
    assert u.ready_at == 1003.0


def test_gap_chunk_stops_at_discontinuity(db):
    db.ingest(SRC, [], gap_ids=[1, 2, 3, 5])
    u = db.next_unit(SRC, 0, 0)
    assert u.kind == "gap" and u.message_ids == [1, 2, 3]


def test_recover_sending_becomes_uncertain(db):
    db.ingest(SRC, [item(1)])
    u = db.next_unit(SRC, 0, 0)
    db.mark_sending(u.row_ids)
    assert db.recover_on_startup("hold") == 1
    assert db.get(SRC, 1).status == "uncertain"
    assert db.next_unit(SRC, 0, 0) is None
    assert db.requeue(SRC, "uncertain", 1) == 1
    assert db.next_unit(SRC, 0, 0).message_ids == [1]


def test_backfill_does_not_touch_existing(db):
    db.ingest(SRC, [item(3)])
    assert db.enqueue_backfill(SRC, 1, 5) == 4
    assert db.get(SRC, 3).kind == "text"


def test_pause_flag(db):
    assert not db.is_paused()
    db.set_paused(True, "x")
    assert db.is_paused() and db.pause_reason() == "x"
    db.set_paused(False)
    assert not db.is_paused()
