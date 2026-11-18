"""Recebimento de updates via long polling (getUpdates).

Garantias:
- O offset só avança no SQLite na mesma transação que grava as mensagens na
  fila. O Telegram só esquece um update quando chamamos getUpdates com um
  offset maior, o que só acontece depois do COMMIT.
- Updates repetidos (reentrega após queda) são ignorados pelo UNIQUE do banco.
- O Telegram guarda updates não confirmados por no máximo 24 horas. Para
  canais e supergrupos, lacunas no message_id são detectadas e enfileiradas
  para recuperação por ID (copyMessages), ver worker.py.

Descoberta de chats: a Bot API não tem método para listar os chats em que o
bot está. Cada update que menciona um chat (bot adicionado/promovido/removido,
posts, mensagens, encaminhamentos no privado) atualiza a tabela `chats`, que
alimenta a página web.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable

from .classify import classify
from .config import Config
from .db import Database, IngestItem
from .telegram_api import BotApi, NetworkError, RetryAfter, TelegramError

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ("message", "channel_post", "edited_message", "edited_channel_post", "my_chat_member")

AdminHandler = Callable[[dict], Awaitable[None]]


class FatalError(Exception):
    pass


def bot_can_post(chat_type: str | None, member: dict) -> bool:
    status = member.get("status")
    if status == "creator":
        return True
    if status == "administrator":
        return bool(member.get("can_post_messages", True)) if chat_type == "channel" else True
    if status == "member":
        return chat_type != "channel"
    if status == "restricted":
        return bool(member.get("can_send_messages", False))
    return False


class Poller:
    def __init__(self, cfg: Config, db: Database, api: BotApi, on_admin: AdminHandler,
                 wake_worker: Callable[[], None] = lambda: None, bot_id: int = 0):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.on_admin = on_admin
        self.wake_worker = wake_worker
        self.bot_id = bot_id
        self.connected = False
        self.on_chats_changed: Callable[[], None] = lambda: None

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        offset = self.db.get_offset()
        log.info("Polling iniciado (offset salvo: %s)", offset)
        while not stop.is_set():
            try:
                updates = await self.api.get_updates(offset, self.cfg.poll_timeout, ALLOWED_UPDATES)
            except RetryAfter as e:
                log.warning("Limite atingido no getUpdates (429). Aguardando %.0fs", e.retry_after)
                await _sleep_or_stop(stop, e.retry_after + 1)
                continue
            except (NetworkError, TelegramError) as e:
                if isinstance(e, TelegramError) and e.code == 401:
                    raise FatalError("Token inválido ou revogado (401). Atualize o token na página web.") from e
                if isinstance(e, TelegramError) and e.code == 409:
                    log.error("Conflito 409: outra instância do bot ou um webhook está ativo. "
                              "Rode apenas uma instância e não use webhook.")
                failures += 1
                self.connected = False
                delay = _backoff(failures)
                log.warning("Falha no getUpdates (%s). Tentativa %d, reconectando em %.0fs",
                            e, failures, delay)
                await _sleep_or_stop(stop, delay)
                continue

            if failures:
                log.info("Reconectado à Bot API após %d falha(s)", failures)
                failures = 0
            self.connected = True

            if updates:
                offset = await self.process(updates)

    # ------------------------------------------------------------------
    def _discover(self, updates: list[dict]) -> bool:
        """Atualiza a lista de chats/usuários conhecidos. Retorna True se algo mudou."""
        changed = False
        with self.db.transaction():
            for upd in updates:
                mcm = upd.get("my_chat_member")
                if mcm:
                    chat = mcm["chat"]
                    member = mcm.get("new_chat_member", {})
                    status = member.get("status", "unknown")
                    self.db.upsert_chat(self.bot_id, chat, "adicionado", bot_status=status,
                                        can_post=bot_can_post(chat.get("type"), member))
                    log.info("Status do bot no chat %s '%s': %s", chat["id"], chat.get("title"), status)
                    changed = True
                    continue

                msg = (upd.get("channel_post") or upd.get("message")
                       or upd.get("edited_channel_post") or upd.get("edited_message"))
                if not msg:
                    continue
                chat = msg.get("chat", {})
                if chat.get("type") == "private":
                    if msg.get("from") and not msg["from"].get("is_bot"):
                        self.db.upsert_user(self.bot_id, msg["from"])
                        changed = True
                    origin = msg.get("forward_origin") or {}
                    fchat = origin.get("chat") if origin.get("type") == "channel" else msg.get("forward_from_chat")
                    if fchat and fchat.get("id"):
                        self.db.upsert_chat(self.bot_id, fchat, "encaminhamento")
                        log.info("Chat descoberto por encaminhamento: %s '%s'", fchat["id"], fchat.get("title"))
                        changed = True
                elif chat.get("id"):
                    if "migrate_to_chat_id" in msg:
                        self.db.upsert_chat(self.bot_id, chat, "mensagem", bot_status="migrado", can_post=False)
                    else:
                        self.db.upsert_chat(self.bot_id, chat, "mensagem")
                    changed = True
        if changed:
            self.on_chats_changed()
        return changed

    def _learn_topics(self, msg: dict) -> None:
        """Aprende nomes de tópicos a partir de mensagens de serviço e do reply_to_message
        (em fóruns, mensagens de um tópico apontam para a mensagem de criação do tópico)."""
        chat_id = msg["chat"]["id"]
        created = msg.get("forum_topic_created")
        if created:
            self.db.upsert_topic(chat_id, msg["message_id"], created.get("name"), created.get("icon_color"),
                                 created.get("icon_custom_emoji_id"), found_via="criação")
            log.info("Tópico conhecido no chat %s: #%s '%s'", chat_id, msg["message_id"], created.get("name"))
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        if thread:
            edited = msg.get("forum_topic_edited")
            if edited:
                self.db.upsert_topic(chat_id, thread, edited.get("name"), None,
                                     edited.get("icon_custom_emoji_id"), found_via="edição")
            if "forum_topic_closed" in msg:
                self.db.upsert_topic(chat_id, thread, closed=True)
            if "forum_topic_reopened" in msg:
                self.db.upsert_topic(chat_id, thread, closed=False)
        reply = msg.get("reply_to_message") or {}
        rc = reply.get("forum_topic_created")
        if rc and not self.db.get_topic(chat_id, reply["message_id"]):
            self.db.upsert_topic(chat_id, reply["message_id"], rc.get("name"), rc.get("icon_color"),
                                 rc.get("icon_custom_emoji_id"), found_via="mensagem")

    async def process(self, updates: list[dict]) -> int:
        """Grava os updates de forma atômica e retorna o novo offset."""
        cfg = self.cfg
        src = cfg.source_chat_id
        items: list[IngestItem] = []
        admin_msgs: list[dict] = []
        ids_seen: list[int] = []

        self._discover(updates)

        for upd in updates:
            msg = upd.get("channel_post") or upd.get("message")
            edited = upd.get("edited_channel_post") or upd.get("edited_message")
            if msg is not None:
                chat = msg.get("chat", {})
                if chat.get("id") in (src, cfg.dest_chat_id) and chat.get("is_forum"):
                    self._learn_topics(msg)
                if src is not None and chat.get("id") == src:
                    if "migrate_to_chat_id" in msg:
                        log.critical("O grupo de origem foi convertido em supergrupo. Novo ID: %s. "
                                     "Selecione o novo chat como origem na página web.",
                                     msg["migrate_to_chat_id"])
                    c = classify(msg)
                    items.append(IngestItem(
                        source_message_id=msg["message_id"],
                        kind=c.kind,
                        status="pending" if c.copyable else "skipped",
                        media_group_id=msg.get("media_group_id"),
                        file_unique_id=c.file_unique_id,
                        reason=c.reason,
                        source_date=msg.get("date"),
                        source_thread_id=msg.get("message_thread_id") if msg.get("is_topic_message") else None,
                    ))
                    ids_seen.append(msg["message_id"])
                elif chat.get("type") == "private" and str(msg.get("text", "")).startswith("/"):
                    admin_msgs.append(msg)
            elif edited is not None and src is not None and edited.get("chat", {}).get("id") == src:
                log.info("Mensagem %s editada na origem (edições não são espelhadas)", edited.get("message_id"))

        gap_ids: list[int] = []
        new_max = prev_max = self.db.max_seen(src) if src is not None else None
        for mid in sorted(set(ids_seen)):
            if new_max is not None and mid > new_max + 1 and cfg.gap_recovery_supported:
                first, last = new_max + 1, mid - 1
                size = last - first + 1
                if size <= cfg.gap_auto_max:
                    gap_ids.extend(range(first, last + 1))
                    log.info("Lacuna detectada na origem: IDs %d..%d (%d). Enfileirando recuperação.",
                             first, last, size)
                else:
                    log.warning("Lacuna grande detectada: IDs %d..%d (%d > máximo automático %d). "
                                "Use o backfill na página web se quiser recuperar.",
                                first, last, size, cfg.gap_auto_max)
            new_max = mid if new_max is None else max(new_max, mid)

        new_offset = max(u["update_id"] for u in updates) + 1
        res = self.db.ingest(src or 0, items, gap_ids=gap_ids, new_offset=new_offset,
                             max_seen=new_max if new_max != prev_max else None)
        if items or gap_ids:
            log.info("Recebidas %d mensagem(ns) da origem (%d novas, %d não copiáveis, %d IDs de lacuna)",
                     len(items), res.new_rows, res.skipped, res.gap_rows)
            self.wake_worker()

        for msg in admin_msgs:
            try:
                await self.on_admin(msg)
            except Exception:  # um comando com erro não pode derrubar o polling
                log.exception("Erro ao processar comando administrativo")
        return new_offset


def _backoff(failures: int) -> float:
    base = min(2 ** min(failures, 9), 300)
    return base * random.uniform(0.8, 1.2)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
