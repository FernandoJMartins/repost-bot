import asyncio

from aiohttp.test_utils import TestClient, TestServer
from conftest import TelegramError

from repost_bot.config import Bootstrap, Config, save_token
from repost_bot.db import Database
from repost_bot.engine import Engine
from repost_bot.logging_setup import MemoryLogHandler
from repost_bot.poller import Poller
from repost_bot.web import create_app

BOT = {"id": 777, "is_bot": True, "username": "meu_backup_bot", "first_name": "Backup",
       "can_read_all_group_messages": False}
CHANNEL = {"id": -1001, "type": "channel", "title": "Canal Fonte"}
DEST = {"id": -1002, "type": "channel", "title": "Canal Backup"}
H = {"X-Requested-With": "fetch"}


class FakeBot:
    """Bot API falsa usada pelo Engine nos testes."""

    def __init__(self, chats=None, webhook=""):
        self.chats = {c["id"]: c for c in (chats or [CHANNEL, DEST])}
        self.webhook = webhook
        self.updates: list[dict] = []

    async def get_me(self):
        return BOT

    async def get_webhook_info(self):
        return {"url": self.webhook}

    async def delete_webhook(self):
        self.webhook = ""
        return True

    async def get_chat(self, ref):
        for c in self.chats.values():
            if ref == c["id"] or ref == "@" + str(c.get("username")):
                return c
        raise TelegramError("getChat", 400, "Bad Request: chat not found")

    async def get_chat_member(self, chat_id, user_id):
        return {"status": "administrator", "can_post_messages": True}

    async def get_updates(self, offset, timeout, allowed):
        await asyncio.sleep(0.02)
        out, self.updates = self.updates, []
        return out

    async def send_message(self, chat_id, text):
        return {}

    async def copy_message(self, *a):
        return 1

    async def copy_messages(self, *a):
        return [1]

    async def aclose(self):
        pass


def boot_for(tmp_path):
    return Bootstrap(db_path=tmp_path / "w.db", log_level="INFO", log_file=None,
                     web_host="127.0.0.1", web_port=0, api_base="http://invalid")


async def wait_state(eng, *states, timeout=3.0):
    t = 0.0
    while eng.state not in states and t < timeout:
        await asyncio.sleep(0.02)
        t += 0.02
    assert eng.state in states, (eng.state, eng.error)


def test_discovery_records_chats_users_and_forwards(tmp_path):
    cfg = Config(bot_token="1:x", db_path=tmp_path / "d.db")
    db = Database(cfg.db_path)

    async def noop(m):
        pass

    p = Poller(cfg, db, None, noop, bot_id=777)
    updates = [
        {"update_id": 1, "my_chat_member": {"chat": DEST, "new_chat_member": {
            "status": "administrator", "can_post_messages": True}}},
        {"update_id": 2, "channel_post": {"message_id": 5, "chat": CHANNEL, "text": "oi"}},
        {"update_id": 3, "message": {"message_id": 1, "chat": {"id": 42, "type": "private"},
                                     "from": {"id": 42, "first_name": "Ana"}, "text": "/start",
                                     "forward_origin": {"type": "channel",
                                                        "chat": {"id": -1003, "type": "channel", "title": "Outro"}}}},
        {"update_id": 4, "my_chat_member": {"chat": {"id": -1004, "type": "supergroup", "title": "Saí"},
                                            "new_chat_member": {"status": "left"}}},
    ]
    off = asyncio.run(p.process(updates))
    assert off == 5
    chats = {c["chat_id"]: c for c in db.list_chats(777)}
    assert chats[-1002]["bot_status"] == "administrator" and chats[-1002]["can_post"] == 1
    assert chats[-1001]["title"] == "Canal Fonte"
    assert chats[-1003]["found_via"] == "encaminhamento"
    assert chats[-1004]["bot_status"] == "left" and chats[-1004]["can_post"] == 0
    assert 42 not in chats  # chats privados não aparecem como grupo/canal
    assert [u["user_id"] for u in db.list_users(777)] == [42]
    # nada é enfileirado sem origem configurada
    assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_engine_without_token_waits(tmp_path):
    async def go():
        boot = boot_for(tmp_path)
        db = Database(boot.db_path)
        eng = Engine(db, boot, api_factory=lambda t, c: FakeBot())
        await eng.start()
        assert eng.state == "sem_token"
        await eng.shutdown()
        db.close()
    asyncio.run(go())


def test_engine_webhook_blocks_until_removed(tmp_path):
    async def go():
        boot = boot_for(tmp_path)
        db = Database(boot.db_path)
        save_token(db, "1:abc")
        fake = FakeBot(webhook="https://example.com/hook")
        eng = Engine(db, boot, api_factory=lambda t, c: fake)
        await eng.start()
        await wait_state(eng, "erro")
        assert "webhook" in eng.error
        await eng.delete_webhook()
        await eng.restart()
        await wait_state(eng, "descoberta")
        await eng.shutdown()
        db.close()
    asyncio.run(go())


def test_web_flow_setup_login_select_chats(tmp_path):
    async def go():
        boot = boot_for(tmp_path)
        db = Database(boot.db_path)
        fake = FakeBot()
        eng = Engine(db, boot, api_factory=lambda t, c: fake)
        app = create_app(eng, db, MemoryLogHandler())
        async with TestClient(TestServer(app)) as client:
            # sem sessão: API fechada
            assert (await client.get("/api/state")).status == 401
            # POST sem cabeçalho anti-CSRF é recusado
            assert (await client.post("/api/auth/login", json={"password": "x"})).status == 403

            st = await (await client.get("/api/auth/state")).json()
            assert st["data"] == {"logged_in": False, "default_password": True}
            bad = await client.post("/api/auth/login", json={"user": "admin", "password": "errada"}, headers=H)
            assert bad.status == 403
            bad = await client.post("/api/auth/login", json={"user": "outro", "password": "admin"}, headers=H)
            assert bad.status == 403
            r = await client.post("/api/auth/login", json={"user": "admin", "password": "admin"}, headers=H)
            assert r.status == 200
            assert (await (await client.get("/api/state")).json())["data"]["default_password"] is True

            # troca de senha remove o aviso de senha padrão
            r = await client.post("/api/auth/password",
                                  json={"current": "admin", "new": "senhaforte"}, headers=H)
            assert r.status == 200
            assert (await (await client.get("/api/state")).json())["data"]["default_password"] is False

            # token: precisa de rede real -> testamos direto no engine
            save_token(db, "1:abc")
            await eng.restart()
            await wait_state(eng, "descoberta")

            fake.updates = [{"update_id": 10, "my_chat_member": {"chat": CHANNEL, "new_chat_member": {
                "status": "administrator"}}}]
            for _ in range(100):
                st = (await (await client.get("/api/state")).json())["data"]
                if st["chats"]:
                    break
                await asyncio.sleep(0.02)
            assert [c["chat_id"] for c in st["chats"]] == [-1001]
            assert "token" in st and "abc" not in str(st["token"])
            assert st["links"]["channel"].startswith("https://t.me/meu_backup_bot?startchannel")

            # adicionar por @username / ID
            r = await client.post("/api/chats/add", json={"ref": "-1002"}, headers=H)
            assert r.status == 200

            # selecionar origem e destino com "clique" (sem digitar -100)
            r = await client.post("/api/settings", json={"source_chat_id": -1001}, headers=H)
            assert (await r.json())["data"]["config"]["source_chat_id"] == -1001
            r = await client.post("/api/settings", json={"dest_chat_id": -1002}, headers=H)
            await wait_state(eng, "ativo")

            # validação de configuração
            r = await client.post("/api/settings", json={"dest_messages_per_minute": 500}, headers=H)
            assert r.status == 400

            # pausa / retomada
            await client.post("/api/pause", json={}, headers=H)
            assert db.is_paused()
            await client.post("/api/resume", json={}, headers=H)
            assert not db.is_paused()

            # backfill
            r = await client.post("/api/backfill", json={"first": 1, "last": 3}, headers=H)
            assert (await r.json())["data"]["count"] == 3

            # logout encerra a sessão
            await client.post("/api/auth/logout", json={}, headers=H)
            assert (await client.get("/api/state")).status == 401
            r = await client.post("/api/auth/login", json={"user": "admin", "password": "admin"}, headers=H)
            assert r.status == 403
            r = await client.post("/api/auth/login", json={"user": "Admin", "password": "senhaforte"}, headers=H)
            assert r.status == 200
        await eng.shutdown()
        db.close()
    asyncio.run(go())


def test_engine_reports_missing_bot_in_source(tmp_path):
    async def go():
        boot = boot_for(tmp_path)
        db = Database(boot.db_path)
        save_token(db, "1:abc")
        from repost_bot.config import save_settings
        save_settings(db, {"source_chat_id": -1009, "dest_chat_id": -1002})
        eng = Engine(db, boot, api_factory=lambda t, c: FakeBot())
        await eng.start()
        await wait_state(eng, "erro")
        assert "origem" in eng.error and eng.poller is not None  # descoberta continua
        await eng.shutdown()
        db.close()
    asyncio.run(go())
