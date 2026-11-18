"""Consumidor da fila persistente: copia mensagens da origem para o destino.

- Ordem: sempre a menor source_message_id pendente primeiro (head-of-line).
- Álbuns: as partes chegam como updates separados com o mesmo media_group_id;
  aguardamos ALBUM_SETTLE_SECONDS e copiamos tudo numa única chamada
  copyMessages, que mantém o agrupamento.
- Mídia: copyMessage/copyMessages reutilizam o arquivo nos servidores do
  Telegram. Nada é baixado nem reenviado pelo bot.
- Lacunas/backfill: IDs sem conteúdo conhecido são copiados em lotes
  consecutivos com copyMessages (IDs inexistentes são pulados pela API).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .config import Config
from .db import Database, Unit
from .ratelimit import RateLimiter
from .telegram_api import BotApi, NetworkError, RetryAfter, TelegramError

log = logging.getLogger(__name__)

NOT_FOUND = ("message to copy not found", "message to forward not found", "message not found",
             "message_id_invalid", "messages to copy not found")
NOT_COPYABLE = ("can't be copied", "can't be forwarded", "cannot be copied", "protected",
                "message_ids_empty")
THREAD_GONE = ("message thread not found", "thread not found", "topic_deleted", "topic deleted",
               "topic_id_invalid")
# Cores aceitas pelo createForumTopic.
TOPIC_COLORS = {7322096, 16766590, 13338331, 9367192, 16749490, 16478047}
CONFIG_ERRORS = ("chat not found", "not enough rights", "chat_write_forbidden", "chat_admin_required",
                 "have no rights", "need administrator rights", "peer_id_invalid", "bot is not a member",
                 "bot was kicked", "chat_restricted", "topic_closed")


class Worker:
    def __init__(self, cfg: Config, db: Database, api: BotApi, limiter: RateLimiter,
                 clock=time.time):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.limiter = limiter
        self.clock = clock
        self._wake = asyncio.Event()
        self.last_activity: float | None = None

    def wake(self) -> None:
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        log.info("Worker de cópia iniciado")
        while not stop.is_set():
            if self.db.is_paused():
                await self._idle(stop, 5)
                continue
            unit = self.db.next_unit(self.cfg.source_chat_id, self.cfg.album_settle_seconds,
                                     self.cfg.gap_settle_seconds)
            if unit is None:
                await self._idle(stop, 30)
                continue
            wait = unit.ready_at - self.clock()
            if wait > 0:
                await self._idle(stop, min(wait, 30))
                continue
            try:
                await self.process(unit)
            except Exception:
                # Erro inesperado: não perde a mensagem, devolve para a fila com backoff.
                log.exception("Erro inesperado ao copiar %s", unit.message_ids)
                self.db.mark_retry(unit.row_ids, "erro interno inesperado", self.clock() + 60)

    async def _idle(self, stop: asyncio.Event, seconds: float) -> None:
        self._wake.clear()
        stop_task = asyncio.ensure_future(stop.wait())
        wake_task = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({stop_task, wake_task}, timeout=seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
            wake_task.cancel()

    # ------------------------------------------------------------------
    async def resolve_thread(self, source_thread_id: int | None) -> int | None:
        """Tópico de destino para um tópico da origem (None = tópico Geral / chat sem tópicos)."""
        cfg = self.cfg
        if not source_thread_id:
            return None
        m = self.db.get_topic_map(cfg.source_chat_id, source_thread_id, cfg.dest_chat_id)
        if m:
            return m["dest_thread_id"]
        if not cfg.auto_create_topics:
            return None
        t = self.db.get_topic(cfg.source_chat_id, source_thread_id) or {}
        name = t.get("title") or f"Tópico {source_thread_id}"
        color = t.get("icon_color") if t.get("icon_color") in TOPIC_COLORS else None
        emoji = t.get("icon_emoji_id")
        await self.limiter.acquire(1)  # criar tópico gera uma mensagem de serviço no destino
        try:
            res = await self.api.create_forum_topic(cfg.dest_chat_id, name, color, emoji)
        except TelegramError as e:
            if not emoji or e.code != 400:
                raise
            # Emoji personalizado não permitido para bots: cria sem ele.
            res = await self.api.create_forum_topic(cfg.dest_chat_id, name, color, None)
        dest_thread = int(res["message_thread_id"])
        with self.db.transaction():
            self.db.set_topic_map(cfg.source_chat_id, source_thread_id, cfg.dest_chat_id, dest_thread,
                                  created_by_bot=True)
            self.db.upsert_topic(cfg.dest_chat_id, dest_thread, res.get("name", name), res.get("icon_color"),
                                 res.get("icon_custom_emoji_id"), found_via="criado pelo bot")
        log.info("Tópico criado no destino: '%s' (#%d) para o tópico #%d da origem", name, dest_thread,
                 source_thread_id)
        return dest_thread

    async def process(self, unit: Unit) -> None:
        cfg = self.cfg
        ids = unit.message_ids
        # Resolve o tópico de destino antes de marcar 'sending': falhas aqui não
        # envolvem a mensagem e podem ser repetidas com segurança.
        try:
            thread = await self.resolve_thread(unit.thread_id)
        except RetryAfter as e:
            self.limiter.penalize(e.retry_after)
            self.db.mark_retry(unit.row_ids, "429 ao criar tópico",self.clock() + e.retry_after + 1)
            log.warning("Limite atingido ao criar tópico (429). Aguardando %.0fs", e.retry_after)
            return
        except TelegramError as e:
            reason = f"não foi possível criar o tópico no destino: {e.code} {e.description}"
            self.db.mark_retry(unit.row_ids, reason, 0)
            await self._auto_pause(reason + ". Dê ao bot a permissão 'Gerenciar tópicos' ou mapeie o tópico no painel.")
            return
        except NetworkError as e:
            self.db.mark_retry(unit.row_ids, f"rede ao criar tópico: {e}", self.clock() + 30)
            log.warning("Falha de rede ao criar tópico: %s", e)
            return

        await self.limiter.acquire(len(ids))
        # Marca 'sending' ANTES de chamar a API: se o processo cair durante a
        # chamada, a linha fica identificável como entrega incerta.
        self.db.mark_sending(unit.row_ids)
        try:
            if len(ids) == 1:
                dest_id = await self.api.copy_message(cfg.dest_chat_id, cfg.source_chat_id, ids[0],
                                                      message_thread_id=thread)
                self.db.mark_done(unit.rows[0].id, cfg.dest_chat_id, dest_id)
                log.info("Copiada msg %d -> %d (%s%s)", ids[0], dest_id, unit.rows[0].kind,
                         f", tópico #{thread}" if thread else "")
            else:
                dest_ids = await self.api.copy_messages(cfg.dest_chat_id, cfg.source_chat_id, ids,
                                                        message_thread_id=thread)
                if not dest_ids:
                    self.db.mark_skipped(unit.row_ids, "nenhuma mensagem do lote existia ou era copiável")
                    log.info("Lote %d..%d: nada a copiar (IDs apagados/inexistentes)", ids[0], ids[-1])
                    return
                self.db.mark_done_batch(unit.rows, cfg.dest_chat_id, dest_ids)
                log.info("Copiado %s %d..%d (%d msgs) -> %d mensagens no destino",
                         "álbum" if unit.kind == "album" else "lote", ids[0], ids[-1], len(ids), len(dest_ids))
            self.last_activity = self.clock()
        except RetryAfter as e:
            self.limiter.penalize(e.retry_after)
            self.db.mark_retry(unit.row_ids, f"429 retry_after={e.retry_after:.0f}s",
                               self.clock() + e.retry_after + 1, refund_attempt=True)
            log.warning("Limite de envio atingido (429). Pausando envios por %.0fs", e.retry_after)
        except TelegramError as e:
            await self._handle_api_error(unit, e)
        except NetworkError as e:
            await self._handle_network_error(unit, e)

    async def _handle_api_error(self, unit: Unit, e: TelegramError) -> None:
        d = e.desc
        ids = unit.message_ids
        if e.migrate_to_chat_id:
            reason = (f"chat migrado para supergrupo (novo ID {e.migrate_to_chat_id}); "
                      f"atualize a configuração e use /resume")
            self.db.mark_retry(unit.row_ids, reason, 0, refund_attempt=True)
            await self._auto_pause(reason)
            return

        if any(s in d for s in THREAD_GONE) and unit.thread_id:
            # Tópico de destino apagado: esquece o mapeamento e tenta de novo (recria ou usa o novo mapa).
            self.db.delete_topic_map(self.cfg.source_chat_id, unit.thread_id, self.cfg.dest_chat_id)
            self.db.mark_retry(unit.row_ids, f"tópico de destino não existe mais ({e.description})",
                               self.clock() + 5, refund_attempt=True)
            log.warning("Tópico de destino do tópico #%d da origem não existe mais; mapeamento removido",
                        unit.thread_id)
            return

        if e.code == 403 or any(s in d for s in CONFIG_ERRORS):
            reason = f"erro de permissão/configuração: {e.code} {e.description}"
            self.db.mark_retry(unit.row_ids, reason, 0, refund_attempt=True)
            await self._auto_pause(reason)
            return

        if e.code == 400 and (any(s in d for s in NOT_FOUND) or any(s in d for s in NOT_COPYABLE)):
            if len(ids) > 1:
                # Um lote inteiro recusado: processa uma a uma para isolar o problema.
                self.db.mark_split(unit.row_ids, f"lote recusado ({e.description}); dividindo")
                log.info("Lote %d..%d recusado (%s); processando individualmente", ids[0], ids[-1], e.description)
            else:
                label = "não encontrada (apagada/inexistente)" if any(s in d for s in NOT_FOUND) \
                    else "não copiável"
                self.db.mark_skipped(unit.row_ids, f"{label}: {e.description}")
                log.info("Msg %d ignorada: %s (%s)", ids[0], label, e.description)
            return

        if e.code in (500, 504):
            await self._uncertain_or_retry(unit, f"{e.code} {e.description}")
            return

        self._retry_or_fail(unit, f"{e.code} {e.description}")

    async def _handle_network_error(self, unit: Unit, e: NetworkError) -> None:
        if not e.maybe_delivered:
            # A conexão nem foi aberta: seguro tentar de novo.
            self._retry_or_fail(unit, str(e))
            return
        await self._uncertain_or_retry(unit, str(e))

    async def _uncertain_or_retry(self, unit: Unit, err: str) -> None:
        ids = unit.message_ids
        if self.cfg.uncertain_policy == "resend":
            log.warning("Entrega incerta de %s (%s); reenviando (UNCERTAIN_POLICY=resend)", ids, err)
            self._retry_or_fail(unit, err)
            return
        self.db.mark_uncertain(unit.row_ids, f"entrega não confirmada: {err}")
        log.warning("Entrega INCERTA de %s (%s). Retida para não duplicar; use /uncertain e /resend.",
                    ids, err)
        await self.notify_admin(
            f"⚠️ Entrega incerta das mensagens {ids} ({err}).\n"
            f"Verifique o destino e use /resend {ids[0]} ou /skip {ids[0]}.")

    def _retry_or_fail(self, unit: Unit, err: str) -> None:
        attempts = max(r.attempts for r in unit.rows) + 1  # incluindo a que acabou de falhar
        ids = unit.message_ids
        if attempts >= self.cfg.max_attempts:
            self.db.mark_failed(unit.row_ids, err)
            log.error("Falha definitiva em %s após %d tentativas: %s", ids, attempts, err)
            return
        delay = min(5 * 2 ** (attempts - 1), 900) * random.uniform(0.8, 1.2)
        self.db.mark_retry(unit.row_ids, err, self.clock() + delay)
        log.warning("Erro ao copiar %s (tentativa %d/%d): %s. Nova tentativa em %.0fs",
                    ids, attempts, self.cfg.max_attempts, err, delay)

    async def _auto_pause(self, reason: str) -> None:
        self.db.set_paused(True, reason)
        log.critical("Backup PAUSADO automaticamente: %s", reason)
        await self.notify_admin(f"⛔ Backup pausado automaticamente:\n{reason}\n\nCorrija e envie /resume.")

    async def notify_admin(self, text: str) -> None:
        if not self.cfg.admin_user_id:
            return
        try:
            await self.api.send_message(self.cfg.admin_user_id, text)
        except Exception as e:  # o admin pode não ter iniciado conversa com o bot
            log.debug("Não foi possível notificar o admin: %s", e)
