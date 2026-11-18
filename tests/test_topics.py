import asyncio
from datetime import datetime, timezone

from conftest import DST, SRC, FakeApi, FakeLimiter, TelegramError, make_cfg
from telethon.tl import types

from repost_bot.db import Database, IngestItem
from repost_bot.poller import Poller
from repost_bot.userclient import classify_mtproto
from repost_bot.worker import Worker


def setup(tmp_path, script=None, **kw):
    cfg = make_cfg(tmp_path, **kw)
    db = Database(cfg.db_path)
    api = FakeApi(script)
    return cfg, db, api, Worker(cfg, db, api, FakeLimiter(), clock=lambda: 1e12)


def run_all(db, w):
    for _ in range(50):
        u = db.next_unit(SRC, 0, 0)
        if u is None:
            return
        asyncio.run(w.process(u))


def test_auto_creates_topic_with_same_name_once(tmp_path):
    cfg, db, api, w = setup(tmp_path)
    db.upsert_topic(SRC, 77, "Filmes", 7322096, None)
    db.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=77),
                    IngestItem(2, "text", "pending", source_thread_id=77),
                    IngestItem(3, "text", "pending")])
    run_all(db, w)
    assert api.topics_created == [("Filmes", 7322096, None)]
    assert api.threads == [501, 501, None]  # tópico Geral não leva message_thread_id
    assert db.get_topic_map(SRC, 77, DST)["dest_thread_id"] == 501


def test_manual_map_and_general(tmp_path):
    cfg, db, api, w = setup(tmp_path)
    db.set_topic_map(SRC, 10, DST, 900)
    db.set_topic_map(SRC, 11, DST, None)
    db.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=10),
                    IngestItem(2, "text", "pending", source_thread_id=11)])
    run_all(db, w)
    assert api.threads == [900, None] and api.topics_created == []


def test_unknown_topic_name_and_auto_off(tmp_path):
    cfg, db, api, w = setup(tmp_path)
    db.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=5)])
    run_all(db, w)
    assert api.topics_created[0][0] == "Tópico 5"

    cfg2, db2, api2, w2 = setup(tmp_path / "b", auto_create_topics=False)
    db2.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=5)])
    run_all(db2, w2)
    assert api2.topics_created == [] and api2.threads == [None]


def test_album_in_topic_single_call(tmp_path):
    cfg, db, api, w = setup(tmp_path)
    db.set_topic_map(SRC, 3, DST, 30)
    db.ingest(SRC, [IngestItem(i, "photo", "pending", media_group_id="g", source_thread_id=3) for i in (4, 5, 6)])
    run_all(db, w)
    assert api.calls == [("copyMessages", [4, 5, 6])] and api.threads == [30]


def test_deleted_dest_topic_is_recreated(tmp_path):
    gone = TelegramError("copyMessage", 400, "Bad Request: message thread not found")
    cfg, db, api, w = setup(tmp_path, [gone])
    db.set_topic_map(SRC, 8, DST, 999)
    db.upsert_topic(SRC, 8, "Séries")
    db.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=8)])
    run_all(db, w)
    assert db.get(SRC, 1).status == "done"
    assert api.threads == [999, 501] and api.topics_created[0][0] == "Séries"


def test_topic_creation_without_permission_pauses(tmp_path):
    cfg, db, api, w = setup(tmp_path)

    async def deny(*a, **k):
        raise TelegramError("createForumTopic", 400, "Bad Request: not enough rights to create a topic")

    api.create_forum_topic = deny
    db.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=8)])
    asyncio.run(w.process(db.next_unit(SRC, 0, 0)))
    assert db.is_paused() and db.get(SRC, 1).status == "pending" and api.calls == []


def test_poller_learns_topics_and_thread(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)

    async def noop(m):
        pass

    p = Poller(cfg, db, None, noop, bot_id=1)
    chat = {"id": SRC, "type": "supergroup", "is_forum": True, "title": "x"}
    dchat = {"id": DST, "type": "supergroup", "is_forum": True, "title": "y"}
    ups = [
        {"update_id": 1, "message": {"message_id": 40, "chat": chat, "message_thread_id": 40, "is_topic_message": True,
                                     "forum_topic_created": {"name": "Novos", "icon_color": 16766590}}},
        {"update_id": 2, "message": {"message_id": 41, "chat": chat, "message_thread_id": 7, "is_topic_message": True,
                                     "text": "oi", "reply_to_message": {"message_id": 7, "chat": chat,
                                     "forum_topic_created": {"name": "Antigo", "icon_color": 7322096}}}},
        {"update_id": 3, "message": {"message_id": 42, "chat": chat, "text": "geral"}},
        {"update_id": 4, "message": {"message_id": 9, "chat": dchat, "message_thread_id": 9, "is_topic_message": True,
                                     "forum_topic_created": {"name": "Destino X", "icon_color": 7322096}}},
    ]
    asyncio.run(p.process(ups))
    assert db.get_topic(SRC, 40)["title"] == "Novos"
    assert db.get_topic(SRC, 7)["title"] == "Antigo"
    assert db.get_topic(DST, 9)["title"] == "Destino X"
    assert db.get(SRC, 41).source_thread_id == 7
    assert db.get(SRC, 42).source_thread_id is None
    assert db.get(SRC, 40).status == "skipped"  # criação de tópico é mensagem de serviço


def _msg(mid, **kw):
    return types.Message(id=mid, peer_id=types.PeerChannel(1), date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                         message=kw.pop("message", ""), **kw)


def test_classify_mtproto_topics_and_media():
    in_topic = _msg(10, message="oi", reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=5))
    reply_in_topic = _msg(11, message="re", reply_to=types.MessageReplyHeader(
        forum_topic=True, reply_to_msg_id=10, reply_to_top_id=5))
    general = _msg(12, message="geral")
    photo = _msg(13, media=types.MessageMediaPhoto(photo=types.Photo(
        id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)), grouped_id=99)
    service = types.MessageService(id=5, peer_id=types.PeerChannel(1), date=None,
                                   action=types.MessageActionTopicCreate(title="T", icon_color=1))
    a, b, c, d, e = map(classify_mtproto, (in_topic, reply_in_topic, general, photo, service))
    assert (a.kind, a.source_thread_id, a.status) == ("text", 5, "pending")
    assert b.source_thread_id == 5
    assert c.source_thread_id is None
    assert d.kind == "photo" and d.media_group_id == "99"
    assert e.status == "skipped" and e.kind.startswith("service:")


def test_import_does_not_touch_done_rows(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    db.ingest(SRC, [IngestItem(1, "text", "pending")])
    u = db.next_unit(SRC, 0, 0)
    db.mark_sending(u.row_ids)
    db.mark_done(u.rows[0].id, DST, 55)
    db.enqueue_backfill(SRC, 2, 2)
    db.ingest(SRC, [IngestItem(1, "text", "pending", source_thread_id=3),
                    IngestItem(2, "photo", "pending", source_thread_id=3)], origin="import")
    assert db.get(SRC, 1).status == "done" and db.get(SRC, 1).source_thread_id is None
    r2 = db.get(SRC, 2)
    assert r2.kind == "photo" and r2.source_thread_id == 3 and r2.origin == "import"
