"""Motor do bot: liga/desliga/reinicia poller e worker conforme a configuração salva.

Estados:
    sem_token  – nenhum token salvo ainda
    conectando – validando token / aguardando rede
    descoberta – bot conectado, coletando chats; origem/destino ainda não definidos
    ativo      – copiando mensagens
    erro       – problema que exige ação (token inválido, webhook, chat inacessível...)

Mesmo em 'erro' por causa de origem/destino, o polling continua rodando para
que a lista de chats siga sendo atualizada na página.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .admin import AdminCommands
from .config import Bootstrap, Config
from .db import Database
from .logging_setup import RedactFilter
from .poller import FatalError, Poller, bot_can_post
from .ratelimit import RateLimiter
from .telegram_api import BotApi, NetworkError, RetryAfter, TelegramError
from .worker import Worker

log = logging.getLogger("repost_bot.engine")


class Stopped(Exception):
    pass


class Engine:
    def __init__(self, db: Database, boot: Bootstrap, redact: RedactFilter | None = None,
                 api_factory=None):
        self.db = db
        self.boot = boot
        self.redact = redact or RedactFilter()
        self._api_factory = api_factory or (lambda token, cfg: BotApi(token, cfg.api_base, cfg.request_timeout))
        self.started_at = time.time()
        self.state = "sem_token"
        self.error: str | None = None
        self.warnings: list[str] = []
        self.bot: dict | None = None
        self.cfg: Config = Config.load(db, boot)
        self.api: BotApi | None = None
        self.poller: Poller | None = None
        self.worker: Worker | None = None
        self.admin: AdminCommands | None = None
        self._stop = asyncio.Event()
        self._main: asyncio.Task | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self.chats_version = 0

    # ------------------------------------------------------------ ciclo
    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def restart(self) -> None:
        async with self._lock:
            await self._shutdown()
            await self._start()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._shutdown()

    async def _start(self) -> None:
        self.cfg = Config.load(self.db, self.boot)
        self.error = None
        self.warnings = []
        self._stop = asyncio.Event()
        if not self.cfg.bot_token:
            self.state = "sem_token"
            log.info("Nenhum token configurado. Abra a página web para configurar o bot.")
            return
        self.redact.add(self.cfg.bot_token)
        self.api = self._api_factory(self.cfg.bot_token, self.cfg)
        self.state = "conectando"
        self._main = asyncio.create_task(self._run(), name="engine")

    async def _shutdown(self) -> None:
        self._stop.set()
        worker = self._tasks.get("worker")
        if worker and not worker.done():
            # Deixa o worker terminar a cópia em andamento para não gerar entrega incerta.
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=60)
            except (asyncio.TimeoutError, Exception):
                worker.cancel()
        for t in self._tasks.values():
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks = {}
        if self._main and not self._main.done():
            self._main.cancel()
            await asyncio.gather(self._main, return_exceptions=True)
        self._main = None
        if self.api:
            await self.api.aclose()
            self.api = None
        self.poller = self.worker = None

    # ------------------------------------------------------------- run
    async def _retry(self, factory, what: str):
        """Repete chamadas de inicialização em falhas de rede/5xx com backoff."""
        delay = 2.0
        while not self._stop.is_set():
            try:
                return await factory()
            except RetryAfter as e:
                log.warning("%s: 429, aguardando %.0fs", what, e.retry_after)
                await self._sleep(e.retry_after + 1)
            except (NetworkError, TelegramError) as e:
                if isinstance(e, TelegramError) and e.code < 500:
                    raise
                log.warning("%s falhou (%s); nova tentativa em %.0fs", what, e, delay)
                await self._sleep(delay)
                delay = min(delay * 2, 300)
        raise Stopped()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        cfg, api, db = self.cfg, self.api, self.db
        assert api is not None
        try:
            try:
                me = await self._retry(api.get_me, "getMe")
            except TelegramError as e:
                self._fail(f"Token rejeitado pelo Telegram ({e.code} {e.description}). Gere/cole um token válido.")
                return
            self.bot = me
            log.info("Bot autenticado: @%s (id %s)", me.get("username"), me.get("id"))
            if db.get_kv("bot_id") != str(me["id"]):
                if db.get_kv("bot_id") is not None:
                    log.warning("Token de outro bot detectado: o offset de updates foi reiniciado.")
                db.reset_offset()
                db.set_kv("bot_id", str(me["id"]))

            hook = await self._retry(api.get_webhook_info, "getWebhookInfo")
            if hook.get("url"):
                self._fail("Este bot tem um webhook ativo; o modo de polling não funciona com webhook. "
                           "Use o botão 'Remover webhook' ou um bot dedicado ao backup.")
                return

            recovered = db.recover_on_startup(cfg.uncertain_policy)
            if recovered:
                log.warning("%d mensagem(ns) estavam sendo enviadas quando o processo parou: marcadas como %s",
                            recovered, "incertas" if cfg.uncertain_policy == "hold" else "pendentes")

            limiter = RateLimiter(cfg.dest_messages_per_minute)
            worker = Worker(cfg, db, api, limiter) if cfg.ready else None
            self.worker = worker
            self.admin = AdminCommands(cfg, db, api, self)
            self.poller = Poller(cfg, db, api, self.admin.handle,
                                 worker.wake if worker else (lambda: None), bot_id=int(me["id"]))
            self.poller.on_chats_changed = self._chats_changed
            self._tasks["poller"] = asyncio.create_task(self.poller.run(self._stop), name="poller")
            self._tasks["heartbeat"] = asyncio.create_task(self._heartbeat(), name="heartbeat")

            if not cfg.ready:
                self.state = "descoberta"
                log.info("Aguardando configuração na página web (falta: %s). "
                         "Os chats onde o bot está aparecem conforme houver atividade.",
                         ", ".join(cfg.missing()))
            else:
                problem = await self._check_copy_chats()
                if problem:
                    self.worker = None
                    self._fail(problem)
                else:
                    self._tasks["worker"] = asyncio.create_task(worker.run(self._stop), name="worker")
                    self.state = "ativo"
                    s = db.stats(cfg.source_chat_id)
                    log.info("Backup ATIVO: %s -> %s | fila=%s | pausado=%s", cfg.source_chat_id,
                             cfg.dest_chat_id, s["counts"], db.is_paused())

            done, _ = await asyncio.wait(list(self._tasks.values()), return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if not t.cancelled() and t.exception():
                    exc = t.exception()
                    if isinstance(exc, FatalError):
                        self._fail(str(exc))
                    else:
                        log.error("Tarefa %s terminou com erro: %r", t.get_name(), exc)
                        self._fail(f"Erro interno em {t.get_name()}: {exc}")
                    self._stop.set()
        except Stopped:
            return

    def _fail(self, msg: str) -> None:
        self.state = "erro"
        self.error = msg
        log.error("%s", msg)

    def _chats_changed(self) -> None:
        self.chats_version += 1

    async def _heartbeat(self) -> None:
        interval = max(self.cfg.heartbeat_minutes, 1) * 60
        while not self._stop.is_set():
            await self._sleep(interval)
            if self._stop.is_set() or not self.cfg.source_chat_id:
                continue
            c = self.db.counts(self.cfg.source_chat_id)
            log.info("Heartbeat: estado=%s conectado=%s pausado=%s copiadas=%d pendentes=%d falhas=%d incertas=%d",
                     self.state, self.poller.connected if self.poller else False, self.db.is_paused(),
                     c["done"], c["pending"], c["failed"], c["uncertain"])

    # ------------------------------------------------------- checagens
    async def inspect_chat(self, ref: int | str, found_via: str = "verificado") -> dict:
        """getChat + getChatMember(bot) e grava o resultado na lista de chats."""
        if not (self.api and self.bot):
            raise RuntimeError("Bot ainda não conectado")
        bot_id = int(self.bot["id"])
        chat = await self._retry(lambda: self.api.get_chat(ref), "getChat")
        status, can_post = "unknown", None
        try:
            member = await self._retry(lambda: self.api.get_chat_member(chat["id"], bot_id), "getChatMember")
            status = member.get("status", "unknown")
            can_post = bot_can_post(chat.get("type"), member)
        except TelegramError as e:
            log.debug("getChatMember falhou em %s: %s", chat.get("id"), e)
            status = "left"
            can_post = False
        self.db.upsert_chat(bot_id, chat, found_via, bot_status=status, can_post=can_post,
                            protected=bool(chat.get("has_protected_content")))
        self._chats_changed()
        return {"chat": chat, "status": status, "can_post": can_post}

    async def _check_copy_chats(self) -> str | None:
        cfg = self.cfg
        for label, chat_id in (("origem", cfg.source_chat_id), ("destino", cfg.dest_chat_id)):
            try:
                info = await self.inspect_chat(chat_id)
            except TelegramError as e:
                if self.bot:
                    self.db.upsert_chat(int(self.bot["id"]), {"id": chat_id}, "verificado", bot_status="left",
                                        can_post=False)
                return (f"Não foi possível acessar o chat de {label} ({chat_id}): {e.description}. "
                        f"Adicione o bot ao chat (como administrador em canais).")
            chat, status = info["chat"], info["status"]
            name = chat.get("title") or chat_id
            log.info("Chat de %s: %s '%s' (%s), bot=%s", label, chat.get("type"), name, chat_id, status)
            if status in ("left", "kicked"):
                return f"O bot não está no chat de {label} '{name}'. Adicione-o e clique em 'Aplicar'."
            if label == "destino" and info["can_post"] is False:
                return (f"O bot não tem permissão para publicar no destino '{name}'. "
                        f"Dê permissão de postar mensagens ao bot.")
            if chat.get("is_forum"):
                self.db.set_kv(f"forum:{chat_id}", "1")
            if label == "origem":
                if chat.get("has_protected_content"):
                    self.warnings.append("A origem tem 'conteúdo protegido': o Telegram bloqueia a cópia "
                                         "dessas mensagens; elas ficarão como não copiáveis.")
                if chat.get("type") in ("group", "supergroup") and status != "administrator" \
                        and status != "creator" and not (self.bot or {}).get("can_read_all_group_messages"):
                    self.warnings.append("Origem é grupo, o bot não é admin e o modo de privacidade está ativo: "
                                         "ele não verá todas as mensagens. Torne-o admin ou use /setprivacy "
                                         "no @BotFather.")
                if chat.get("type") == "group":
                    self.warnings.append("Origem é grupo básico: recuperação de lacunas por ID indisponível.")
        for w in self.warnings:
            log.warning("%s", w)
        return None

    async def refresh_chats(self) -> int:
        """Reconsulta todos os chats conhecidos (status do bot, título, permissões)."""
        if not self.bot:
            return 0
        n = 0
        for c in self.db.list_chats(int(self.bot["id"])):
            try:
                await self.inspect_chat(c["chat_id"], c["found_via"] or "verificado")
            except TelegramError as e:
                self.db.upsert_chat(int(self.bot["id"]), {"id": c["chat_id"]}, "verificado",
                                    bot_status="left", can_post=False)
                log.info("Chat %s inacessível: %s", c["chat_id"], e.description)
            n += 1
            await asyncio.sleep(0.3)
        self._chats_changed()
        return n

    async def add_chat(self, ref: str) -> dict:
        ref = ref.strip()
        m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?$", ref)
        if m:
            ref = "@" + m.group(1)
        if re.fullmatch(r"-?\d+", ref):
            target: int | str = int(ref)
        elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", ref):
            target = ref if ref.startswith("@") else "@" + ref
        else:
            raise ValueError("Informe @username, link t.me/... ou ID numérico.")
        return await self.inspect_chat(target, "manual")

    async def delete_webhook(self) -> None:
        if not self.api:
            raise RuntimeError("Sem token configurado")
        await self.api.delete_webhook()
        log.info("Webhook removido a pedido do admin")

    # -------------------------------------------------------- web
    def links(self) -> dict:
        if not self.bot or not self.bot.get("username"):
            return {}
        u = self.bot["username"]
        return {
            "private": f"https://t.me/{u}?start=admin",
            # Links oficiais de "adicionar bot": abrem o seletor de grupo/canal no Telegram.
            "group": f"https://t.me/{u}?startgroup=backup",
            "channel": f"https://t.me/{u}?startchannel&admin=post_messages",
        }
