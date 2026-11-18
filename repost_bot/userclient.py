"""Conta de usuário (MTProto/Telethon) usada SOMENTE PARA LEITURA.

A Bot API não informa em que tópico estava cada mensagem antiga nem lista os
tópicos de um fórum. Com a conta do próprio dono (que já participa do grupo),
lemos:
  - a lista de tópicos (nome, cor e ícone) da origem e do destino;
  - o histórico da origem, com o tópico de cada mensagem.

Nada é enviado pela conta de usuário: quem copia as mensagens continua sendo o
bot (copyMessage), respeitando os limites da Bot API.

A sessão gerada (StringSession) dá acesso total à conta. Ela fica no SQLite
(kv 'user:session'), nunca é mostrada na página e é removida dos logs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl import functions, types

from .db import Database, IngestItem
from .logging_setup import RedactFilter

log = logging.getLogger("repost_bot.user")
logging.getLogger("telethon").setLevel(logging.WARNING)


class AccountError(Exception):
    pass


def friendly(e: Exception) -> str:
    if isinstance(e, errors.FloodWaitError):
        return f"O Telegram pediu para esperar {e.seconds}s antes de tentar de novo."
    table = {
        errors.ApiIdInvalidError: "api_id/api_hash inválidos. Confira em my.telegram.org.",
        errors.PhoneNumberInvalidError: "Número de telefone inválido. Use o formato +5511999999999.",
        errors.PhoneNumberBannedError: "Este número está banido no Telegram.",
        errors.PhoneCodeInvalidError: "Código incorreto.",
        errors.PhoneCodeExpiredError: "Código expirado. Peça um novo.",
        errors.PasswordHashInvalidError: "Senha de duas etapas incorreta.",
        errors.AuthKeyUnregisteredError: "A sessão foi encerrada. Conecte a conta de novo.",
    }
    for cls, msg in table.items():
        if isinstance(e, cls):
            return msg
    return f"{type(e).__name__}: {e}"


class UserAccount:
    def __init__(self, db: Database, redact: RedactFilter | None = None):
        self.db = db
        self.redact = redact or RedactFilter()
        self._client: TelegramClient | None = None
        self._pending: tuple[str, str] | None = None  # (telefone, phone_code_hash)
        self._lock = asyncio.Lock()
        self.state = "conectada" if db.get_kv("user:session") else "desconectada"
        for key in ("user:api_hash", "user:session"):
            self.redact.add(db.get_kv(key) or "")

    # ------------------------------------------------------------ info
    def info(self) -> dict:
        phone = self.db.get_kv("user:phone") or ""
        return {
            "state": self.state,
            "name": self.db.get_kv("user:name"),
            "phone": (phone[:4] + "•••••" + phone[-2:]) if len(phone) > 6 else phone,
            "api_id": self.db.get_kv("user:api_id"),
            "has_api_hash": bool(self.db.get_kv("user:api_hash")),
        }

    def _new_client(self, session: str = "") -> TelegramClient:
        api_id = int(self.db.get_kv("user:api_id") or 0)
        api_hash = self.db.get_kv("user:api_hash") or ""
        if not api_id or not api_hash:
            raise AccountError("Informe api_id e api_hash.")
        return TelegramClient(StringSession(session), api_id, api_hash,
                              flood_sleep_threshold=300, device_model="Repost Bot (backup)",
                              app_version="1.0", request_retries=5, connection_retries=10,
                              auto_reconnect=True)

    async def _drop_client(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    # ------------------------------------------------------------ login
    async def send_code(self, api_id: str, api_hash: str, phone: str) -> None:
        api_id = str(api_id).strip()
        api_hash = str(api_hash).strip()
        phone = re.sub(r"[\s()-]", "", str(phone))
        if not api_id.isdigit():
            raise AccountError("api_id deve ser numérico.")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise AccountError("api_hash deve ter 32 caracteres (0-9, a-f).")
        if not re.fullmatch(r"\+?\d{8,15}", phone):
            raise AccountError("Telefone inválido. Use o formato +5511999999999.")
        if not phone.startswith("+"):
            phone = "+" + phone
        async with self._lock:
            await self._drop_client()
            self.redact.add(api_hash)
            with self.db.transaction():
                self.db.set_kv("user:api_id", api_id)
                self.db.set_kv("user:api_hash", api_hash)
                self.db.set_kv("user:phone", phone)
            client = self._new_client()
            try:
                await client.connect()
                sent = await client.send_code_request(phone)
            except Exception as e:
                await client.disconnect()
                raise AccountError(friendly(e)) from e
            self._client = client
            self._pending = (phone, sent.phone_code_hash)
            self.state = "aguardando_codigo"
            log.info("Código de login enviado para a conta %s", self.info()["phone"])

    async def sign_in_code(self, code: str) -> str:
        code = re.sub(r"\D", "", str(code))
        async with self._lock:
            if not (self._client and self._pending):
                raise AccountError("Peça o código primeiro.")
            phone, code_hash = self._pending
            try:
                await self._client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
            except errors.SessionPasswordNeededError:
                self.state = "aguardando_senha"
                return self.state
            except Exception as e:
                raise AccountError(friendly(e)) from e
            await self._finish()
            return self.state

    async def sign_in_password(self, password: str) -> str:
        async with self._lock:
            if not self._client or self.state != "aguardando_senha":
                raise AccountError("Faça a etapa do código primeiro.")
            try:
                await self._client.sign_in(password=password)
            except Exception as e:
                raise AccountError(friendly(e)) from e
            await self._finish()
            return self.state

    async def _finish(self) -> None:
        me = await self._client.get_me()
        session = self._client.session.save()
        self.redact.add(session)
        name = " ".join(x for x in (me.first_name, me.last_name) if x) or (me.username or str(me.id))
        with self.db.transaction():
            self.db.set_kv("user:session", session)
            self.db.set_kv("user:name", name)
        self._pending = None
        self.state = "conectada"
        log.info("Conta de usuário conectada (%s) — usada somente para leitura", name)

    async def logout(self) -> None:
        async with self._lock:
            client = self._client
            try:
                if client is None and self.db.get_kv("user:session"):
                    client = self._new_client(self.db.get_kv("user:session") or "")
                    await client.connect()
                if client and await client.is_user_authorized():
                    await client.log_out()  # encerra a sessão também no Telegram
            except Exception as e:
                log.warning("Falha ao encerrar a sessão no Telegram: %s", e)
            finally:
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            self._client = None
            self._pending = None
            with self.db.transaction():
                for k in ("user:session", "user:name", "user:api_hash", "user:api_id", "user:phone"):
                    self.db.conn.execute("DELETE FROM kv WHERE key=?", (k,))
            self.state = "desconectada"
            log.info("Conta de usuário desconectada e sessão removida")

    async def shutdown(self) -> None:
        await self._drop_client()

    # ------------------------------------------------------------ leitura
    async def client(self) -> TelegramClient:
        if self.state != "conectada":
            raise AccountError("Conecte sua conta do Telegram no painel primeiro.")
        if self._client and self._client.is_connected():
            return self._client
        client = self._new_client(self.db.get_kv("user:session") or "")
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
        except Exception as e:
            await client.disconnect()
            raise AccountError(friendly(e)) from e
        if not authorized:
            await client.disconnect()
            self.state = "desconectada"
            self.db.conn.execute("DELETE FROM kv WHERE key='user:session'")
            raise AccountError("A sessão da conta expirou ou foi encerrada. Conecte de novo.")
        self._client = client
        return client

    async def entity(self, chat_id: int):
        client = await self.client()
        try:
            return await client.get_input_entity(chat_id)
        except ValueError:
            pass
        async for d in client.iter_dialogs():
            if d.id == chat_id:
                return d.input_entity
        raise AccountError(f"Sua conta não participa do chat {chat_id}.")

    async def fetch_topics(self, chat_id: int) -> list[dict]:
        client = await self.client()
        peer = await self.entity(chat_id)
        out: list[dict] = []
        offset_date, offset_id, offset_topic = None, 0, 0
        for _ in range(100):  # até 10 mil tópicos
            res = await client(functions.messages.GetForumTopicsRequest(
                peer=peer, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100))
            page = [t for t in res.topics if isinstance(t, types.ForumTopic)]
            for t in page:
                out.append({"id": t.id, "title": t.title, "icon_color": t.icon_color,
                            "icon_emoji_id": str(t.icon_emoji_id) if t.icon_emoji_id else None,
                            "closed": bool(t.closed)})
            if len(res.topics) < 100 or not page:
                break
            last = page[-1]
            offset_date, offset_id, offset_topic = last.date, last.top_message, last.id
        return out

    async def sync_topics(self, source_chat_id: int, dest_chat_id: int | None) -> dict:
        counts = {}
        for label, cid in (("origem", source_chat_id), ("destino", dest_chat_id)):
            if not cid:
                continue
            topics = await self.fetch_topics(cid)
            with self.db.transaction():
                for t in topics:
                    self.db.upsert_topic(cid, t["id"], t["title"], t["icon_color"], t["icon_emoji_id"],
                                         t["closed"], found_via="conta")
            counts[label] = len(topics)
        log.info("Tópicos lidos pela conta: %s", counts)
        return counts


# --------------------------------------------------------------- histórico
def classify_mtproto(m) -> IngestItem:
    """Converte uma mensagem do Telethon em item da fila (tópico, tipo, álbum)."""
    thread = None
    rt = getattr(m, "reply_to", None)
    if isinstance(rt, types.MessageReplyHeader) and rt.forum_topic:
        thread = rt.reply_to_top_id or rt.reply_to_msg_id
    group = str(m.grouped_id) if getattr(m, "grouped_id", None) else None
    date = int(m.date.timestamp()) if getattr(m, "date", None) else None

    def item(kind: str, ok: bool = True, reason: str | None = None) -> IngestItem:
        return IngestItem(source_message_id=m.id, kind=kind, status="pending" if ok else "skipped",
                          media_group_id=group, reason=reason, source_date=date, source_thread_id=thread)

    if isinstance(m, types.MessageService):
        return item(f"service:{type(m.action).__name__}", False, "mensagem de serviço (não copiável)")
    if isinstance(m, types.MessageEmpty):
        return item("empty", False, "mensagem vazia/apagada")
    media = m.media
    if isinstance(media, types.MessageMediaPaidMedia):
        return item("paid_media", False, "paid media não pode ser copiada pela Bot API")
    if isinstance(media, (types.MessageMediaGiveaway, types.MessageMediaGiveawayResults, types.MessageMediaInvoice)):
        return item("giveaway_or_invoice", False, "tipo não copiável pela Bot API")
    for attr, kind in (("gif", "animation"), ("video_note", "video_note"), ("voice", "voice"),
                       ("sticker", "sticker"), ("video", "video"), ("audio", "audio"), ("photo", "photo"),
                       ("document", "document"), ("poll", "poll"), ("venue", "venue"), ("geo", "location"),
                       ("contact", "contact"), ("dice", "dice")):
        if getattr(m, attr, None):
            return item(kind)
    if m.message:
        return item("text")
    return item("other")


@dataclass
class ImportJob:
    running: bool = False
    read: int = 0
    queued: int = 0
    skipped: int = 0
    last_id: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    topics: dict = field(default_factory=dict)
    stop_requested: bool = False

    def public(self) -> dict:
        d = asdict(self)
        d.pop("stop_requested")
        return d


class HistoryImporter:
    """Lê o histórico da origem com a conta de usuário e enfileira para o bot copiar."""

    def __init__(self, account: UserAccount, db: Database):
        self.account = account
        self.db = db
        self.job = ImportJob()
        self._task: asyncio.Task | None = None

    def start(self, source_chat_id: int, dest_chat_id: int | None, wake: Callable[[], None]) -> None:
        if self.job.running:
            raise AccountError("A importação já está em andamento.")
        self.job = ImportJob(running=True, started_at=time.time())
        self._task = asyncio.create_task(self._run(source_chat_id, dest_chat_id, wake), name="import")

    def stop(self) -> None:
        self.job.stop_requested = True

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self.job.stop_requested = True
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self, src: int, dest: int | None, wake: Callable[[], None]) -> None:
        job = self.job
        try:
            log.info("Importação do histórico iniciada (lendo com a conta de usuário)")
            job.topics = await self.account.sync_topics(src, dest)
            client = await self.account.client()
            peer = await self.account.entity(src)
            batch: list[IngestItem] = []

            def flush():
                if not batch:
                    return
                res = self.db.ingest(src, batch, origin="import")
                job.queued += res.new_rows - res.skipped
                job.skipped += res.skipped
                batch.clear()
                wake()

            # reverse=True: da mensagem mais antiga para a mais nova (ordem de cópia).
            async for m in client.iter_messages(peer, reverse=True, wait_time=1):
                if job.stop_requested:
                    break
                batch.append(classify_mtproto(m))
                job.read += 1
                job.last_id = m.id
                if len(batch) >= 200:
                    flush()
            flush()
            if job.stop_requested:
                log.info("Importação interrompida: %d lidas, %d enfileiradas", job.read, job.queued)
            else:
                log.info("Importação concluída: %d mensagens lidas, %d enfileiradas, %d não copiáveis. "
                         "O bot está copiando no ritmo configurado.", job.read, job.queued, job.skipped)
        except asyncio.CancelledError:
            raise
        except AccountError as e:
            job.error = str(e)
            log.error("Importação falhou: %s", e)
        except Exception as e:
            job.error = friendly(e)
            log.exception("Importação falhou")
        finally:
            job.running = False
            job.finished_at = time.time()
