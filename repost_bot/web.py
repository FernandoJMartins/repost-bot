"""Página web de configuração e acompanhamento (aiohttp).

Segurança:
- Escuta em 127.0.0.1 por padrão (WEB_HOST). Exponha só via túnel/VPN/proxy com HTTPS.
- Login obrigatório. Padrão: usuário 'admin', senha 'admin' (ou WEB_USER /
  WEB_PASSWORD no primeiro início). A página avisa enquanto a senha padrão
  não for trocada.
- Sessão em cookie HttpOnly + SameSite=Strict; POSTs exigem o cabeçalho
  X-Requested-With (bloqueia CSRF de outros sites).
- O token do bot nunca é devolvido à página, apenas uma versão mascarada.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import EDITABLE, ConfigError, is_channel_or_supergroup_id, save_settings, save_token
from .db import STATUSES, Database
from .engine import Engine
from .logging_setup import MemoryLogHandler
from .telegram_api import BotApi, NetworkError, TelegramError
from .userclient import AccountError, HistoryImporter, UserAccount

log = logging.getLogger("repost_bot.web")

STATIC = Path(__file__).parent / "static"
COOKIE = "rb_session"
SESSION_TTL = 7 * 86400
PBKDF2_ITERS = 390_000

ENGINE: web.AppKey[Engine] = web.AppKey("engine", Engine)
DB: web.AppKey[Database] = web.AppKey("db", Database)
LOGS: web.AppKey[MemoryLogHandler] = web.AppKey("logs", MemoryLogHandler)
AUTH: web.AppKey["AuthState"] = web.AppKey("auth", object)  # type: ignore[arg-type]
ACCOUNT: web.AppKey[UserAccount] = web.AppKey("account", UserAccount)
IMPORTER: web.AppKey[HistoryImporter] = web.AppKey("importer", HistoryImporter)


# ------------------------------------------------------------------ senha
def hash_password(password: str, salt: bytes | None = None, iters: int = PBKDF2_ITERS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"


class AuthState:
    def __init__(self, db: Database):
        self.db = db
        self.failures: dict[str, tuple[int, float]] = {}
        if not db.get_kv("web:pwhash"):
            user = os.environ.get("WEB_USER", "").strip() or DEFAULT_USER
            password = os.environ.get("WEB_PASSWORD", "") or DEFAULT_PASSWORD
            db.set_kv("web:user", user)
            db.set_kv("web:pwhash", hash_password(password))
            db.set_kv("web:default_pw", "1" if password == DEFAULT_PASSWORD else "0")
        if self.default_password:
            log.warning("Página web com login padrão (usuário '%s', senha '%s'). Troque a senha na página.",
                        self.user, DEFAULT_PASSWORD)

    @property
    def user(self) -> str:
        return self.db.get_kv("web:user") or DEFAULT_USER

    @property
    def default_password(self) -> bool:
        return self.db.get_kv("web:default_pw") == "1"

    def throttled(self, ip: str) -> float:
        n, until = self.failures.get(ip, (0, 0.0))
        return max(0.0, until - time.time()) if n >= 5 else 0.0

    def fail(self, ip: str) -> None:
        n, _ = self.failures.get(ip, (0, 0.0))
        n += 1
        self.failures[ip] = (n, time.time() + min(30 * 2 ** max(n - 5, 0), 3600))
        log.warning("Falha de login na página web (ip=%s, tentativa %d)", ip, n)

    def ok(self, ip: str) -> None:
        self.failures.pop(ip, None)


# --------------------------------------------------------------- utilidades
def ok(data: Any = None, status: int = 200) -> web.Response:
    return web.json_response({"ok": True, "data": data}, status=status)


def err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def mask_token(token: str) -> str:
    if not token:
        return ""
    bot_id, _, rest = token.partition(":")
    # Só revela os 4 últimos caracteres se o segredo for longo (tokens reais têm ~35).
    return f"{bot_id}:••••••{rest[-4:]}" if len(rest) >= 20 else f"{bot_id}:••••••"


def _row(r) -> dict:
    return dataclasses.asdict(r)


PUBLIC = {"/", "/api/auth/state", "/api/auth/login"}


@web.middleware
async def security_mw(request: web.Request, handler):
    path = request.path
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "fetch":
        return err("Requisição recusada", 403)
    if path.startswith("/api/") and path not in PUBLIC:
        token = request.cookies.get(COOKIE)
        if not token or not request.app[DB].session_valid(_sha(token)):
            return err("Não autenticado", 401)
    resp = await handler(request)
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _client_ip(request: web.Request) -> str:
    return request.remote or "?"


def _start_session(request: web.Request, resp: web.Response) -> None:
    token = secrets.token_urlsafe(32)
    request.app[DB].add_session(_sha(token), time.time() + SESSION_TTL)
    resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Strict", path="/")


# --------------------------------------------------------------- handlers
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def auth_state(request: web.Request) -> web.Response:
    auth = request.app[AUTH]
    token = request.cookies.get(COOKIE)
    logged = bool(token and request.app[DB].session_valid(_sha(token)))
    return ok({"logged_in": logged, "default_password": auth.default_password})


async def auth_login(request: web.Request) -> web.Response:
    auth = request.app[AUTH]
    ip = _client_ip(request)
    if wait := auth.throttled(ip):
        return err(f"Muitas tentativas. Aguarde {wait:.0f}s.", 429)
    data = await body(request)
    stored = request.app[DB].get_kv("web:pwhash") or ""
    user_ok = hmac.compare_digest(str(data.get("user", "")).strip().lower(), auth.user.lower())
    pw_ok = bool(stored) and verify_password(str(data.get("password", "")), stored)
    if not (user_ok and pw_ok):
        auth.fail(ip)
        return err("Usuário ou senha incorretos.", 403)
    auth.ok(ip)
    resp = ok()
    _start_session(request, resp)
    return resp


async def auth_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE)
    if token:
        request.app[DB].delete_session(_sha(token))
    resp = ok()
    resp.del_cookie(COOKIE, path="/")
    return resp


async def change_password(request: web.Request) -> web.Response:
    data = await body(request)
    db = request.app[DB]
    if not verify_password(str(data.get("current", "")), db.get_kv("web:pwhash") or ""):
        return err("Senha atual incorreta.", 403)
    new = str(data.get("new", ""))
    if len(new) < 4:
        return err("A nova senha precisa ter pelo menos 4 caracteres.")
    new_user = str(data.get("user", "")).strip()
    if new_user:
        db.set_kv("web:user", new_user)
    db.set_kv("web:pwhash", hash_password(new))
    db.set_kv("web:default_pw", "1" if new == DEFAULT_PASSWORD else "0")
    db.delete_all_sessions()
    resp = ok()
    _start_session(request, resp)
    log.info("Senha da página web alterada; demais sessões encerradas.")
    return resp


def _is_forum(db: Database, bot_id: int | None, src: int | None) -> bool:
    if not src:
        return False
    return (db.get_kv(f"forum:{src}") == "1" or bool(db.list_topics(src))
            or bool(db.source_threads_in_queue(src)))


def _topics_view(db: Database, cfg) -> list[dict]:
    """Tópicos da origem (conhecidos por nome ou vistos em mensagens) com o mapeamento atual."""
    src, dst = cfg.source_chat_id, cfg.dest_chat_id
    known = {t["thread_id"]: t for t in db.list_topics(src)}
    for tid in db.source_threads_in_queue(src):
        known.setdefault(tid, {"thread_id": tid, "title": None, "icon_color": None, "closed": 0})
    maps = db.list_topic_map(src, dst) if dst else {}
    dest_titles = {t["thread_id"]: t["title"] for t in db.list_topics(dst)} if dst else {}
    out = []
    for tid, t in sorted(known.items()):
        m = maps.get(tid)
        if m is None:
            mode, dest_thread = "auto", None
        elif m["dest_thread_id"] is None:
            mode, dest_thread = "general", None
        else:
            mode, dest_thread = "topic", m["dest_thread_id"]
        out.append({
            "thread_id": tid, "title": t.get("title"), "icon_color": t.get("icon_color"),
            "closed": bool(t.get("closed")), "mode": mode, "dest_thread_id": dest_thread,
            "dest_title": dest_titles.get(dest_thread) if dest_thread else None,
            "created_by_bot": bool(m and m["created_by_bot"]),
        })
    return out


def build_state(app: web.Application) -> dict:
    eng, db = app[ENGINE], app[DB]
    cfg = eng.cfg
    bot = eng.bot
    bot_id = int(bot["id"]) if bot else None
    src = cfg.source_chat_id
    return {
        "state": eng.state,
        "error": eng.error,
        "warnings": eng.warnings,
        "connected": bool(eng.poller and eng.poller.connected),
        "bot": {
            "id": bot.get("id"), "username": bot.get("username"), "name": bot.get("first_name"),
            "can_read_all_group_messages": bot.get("can_read_all_group_messages"),
        } if bot else None,
        "links": eng.links(),
        "token": {"set": bool(cfg.bot_token), "masked": mask_token(cfg.bot_token)},
        "config": {k: getattr(cfg, k) for k in EDITABLE},
        "missing": cfg.missing(),
        "paused": db.is_paused(),
        "pause_reason": db.pause_reason(),
        "stats": db.stats(src) if src else None,
        "chats": db.list_chats(bot_id) if bot_id else [],
        "users": db.list_users(bot_id) if bot_id else [],
        "chats_version": eng.chats_version,
        "uptime": int(time.time() - eng.started_at),
        "default_password": app[AUTH].default_password,
        "web_user": app[AUTH].user,
        "is_forum": _is_forum(db, bot_id, src),
        "account": app[ACCOUNT].info(),
        "import": app[IMPORTER].job.public(),
        "topics": _topics_view(db, cfg) if src else [],
        "dest_topics": db.list_topics(cfg.dest_chat_id) if cfg.dest_chat_id else [],
        "gap_supported": is_channel_or_supergroup_id(src),
    }


async def get_state(request: web.Request) -> web.Response:
    return ok(build_state(request.app))


async def set_token(request: web.Request) -> web.Response:
    data = await body(request)
    token = str(data.get("token", "")).strip()
    try:
        from .config import coerce
        coerce("bot_token", token)
    except ConfigError as e:
        return err(str(e))
    eng = request.app[ENGINE]
    api = BotApi(token, eng.boot.api_base, 20)
    try:
        me = await api.get_me()
    except TelegramError as e:
        return err(f"O Telegram recusou o token ({e.code} {e.description}).")
    except NetworkError as e:
        return err(f"Sem conexão com a API do Telegram: {e}", 502)
    finally:
        await api.aclose()
    eng.redact.add(token)
    save_token(request.app[DB], token)
    log.info("Token atualizado pela página web (bot @%s)", me.get("username"))
    await eng.restart()
    return ok(build_state(request.app))


async def set_settings(request: web.Request) -> web.Response:
    data = await body(request)
    updates = {k: v for k, v in data.items() if k in EDITABLE}
    if not updates:
        return err("Nada para salvar.")
    try:
        save_settings(request.app[DB], updates)
    except ConfigError as e:
        return err(str(e))
    log.info("Configuração alterada pela página web: %s", ", ".join(sorted(updates)))
    await request.app[ENGINE].restart()
    return ok(build_state(request.app))


async def restart(request: web.Request) -> web.Response:
    await request.app[ENGINE].restart()
    return ok(build_state(request.app))


async def chats_refresh(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    if not eng.bot:
        return err("Bot ainda não conectado.", 409)
    n = await eng.refresh_chats()
    return ok({"refreshed": n, **build_state(request.app)})


async def chats_add(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    if not eng.bot:
        return err("Bot ainda não conectado.", 409)
    data = await body(request)
    try:
        info = await eng.add_chat(str(data.get("ref", "")))
    except ValueError as e:
        return err(str(e))
    except TelegramError as e:
        return err(f"Chat não encontrado ou inacessível para o bot: {e.description}")
    except NetworkError as e:
        return err(f"Sem conexão com o Telegram: {e}", 502)
    return ok({"chat": info["chat"], "status": info["status"], **build_state(request.app)})


async def chats_forget(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    data = await body(request)
    try:
        chat_id = int(data.get("chat_id"))
    except (TypeError, ValueError):
        return err("chat_id inválido")
    if eng.bot:
        request.app[DB].forget_chat(int(eng.bot["id"]), chat_id)
    return ok(build_state(request.app))


async def pause(request: web.Request) -> web.Response:
    request.app[DB].set_paused(True, "pausado pela página web")
    log.info("Backup pausado pela página web")
    return ok(build_state(request.app))


async def resume(request: web.Request) -> web.Response:
    request.app[DB].set_paused(False)
    eng = request.app[ENGINE]
    if eng.worker:
        eng.worker.wake()
    log.info("Backup retomado pela página web")
    return ok(build_state(request.app))


async def queue(request: web.Request) -> web.Response:
    status = request.query.get("status", "failed")
    if status not in STATUSES:
        return err("status inválido")
    src = request.app[ENGINE].cfg.source_chat_id
    rows = request.app[DB].list_by_status(src, status, 100) if src else []
    return ok([_row(r) for r in rows])


async def queue_requeue(request: web.Request) -> web.Response:
    data = await body(request)
    status = data.get("status")
    if status not in ("failed", "uncertain"):
        return err("status deve ser failed ou uncertain")
    mid = data.get("id")
    eng = request.app[ENGINE]
    src = eng.cfg.source_chat_id
    if not src:
        return err("Origem não configurada.")
    n = request.app[DB].requeue(src, status, int(mid) if mid not in (None, "", "all") else None)
    if eng.worker:
        eng.worker.wake()
    log.info("Página web recolocou %d mensagem(ns) '%s' na fila", n, status)
    return ok({"count": n})


async def queue_skip(request: web.Request) -> web.Response:
    data = await body(request)
    src = request.app[ENGINE].cfg.source_chat_id
    try:
        mid = int(data.get("id"))
    except (TypeError, ValueError):
        return err("id inválido")
    n = request.app[DB].skip_manual(src, mid) if src else 0
    eng = request.app[ENGINE]
    if eng.worker:
        eng.worker.wake()
    return ok({"count": n})


async def backfill(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    cfg = eng.cfg
    if not is_channel_or_supergroup_id(cfg.source_chat_id):
        return err("Backfill por ID só funciona quando a origem é canal ou supergrupo.")
    data = await body(request)
    try:
        first, last = int(data.get("first")), int(data.get("last"))
    except (TypeError, ValueError):
        return err("Informe os IDs inicial e final.")
    if first < 1 or last < first:
        return err("Intervalo inválido.")
    if last - first + 1 > cfg.backfill_max:
        return err(f"Intervalo maior que o máximo por vez ({cfg.backfill_max}). Divida em partes.")
    n = request.app[DB].enqueue_backfill(cfg.source_chat_id, first, last)
    if eng.worker:
        eng.worker.wake()
    log.info("Backfill enfileirado pela página web: IDs %d..%d (%d novos)", first, last, n)
    return ok({"count": n, "minutes": round(n / cfg.dest_messages_per_minute)})


async def webhook_delete(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    try:
        await eng.delete_webhook()
    except (TelegramError, NetworkError, RuntimeError) as e:
        return err(f"Não foi possível remover o webhook: {e}")
    await eng.restart()
    return ok(build_state(request.app))


# ---------------------------------------------------- conta de usuário / tópicos
async def _account_call(request: web.Request, coro) -> web.Response:
    try:
        await coro
    except AccountError as e:
        return err(str(e))
    return ok(build_state(request.app))


async def account_send_code(request: web.Request) -> web.Response:
    data = await body(request)
    acc = request.app[ACCOUNT]
    api_id = str(data.get("api_id", "")).strip() or (request.app[DB].get_kv("user:api_id") or "")
    api_hash = str(data.get("api_hash", "")).strip() or (request.app[DB].get_kv("user:api_hash") or "")
    return await _account_call(request, acc.send_code(api_id, api_hash, str(data.get("phone", ""))))


async def account_code(request: web.Request) -> web.Response:
    data = await body(request)
    return await _account_call(request, request.app[ACCOUNT].sign_in_code(str(data.get("code", ""))))


async def account_password(request: web.Request) -> web.Response:
    data = await body(request)
    return await _account_call(request, request.app[ACCOUNT].sign_in_password(str(data.get("password", ""))))


async def account_logout(request: web.Request) -> web.Response:
    request.app[IMPORTER].stop()
    return await _account_call(request, request.app[ACCOUNT].logout())


async def topics_fetch(request: web.Request) -> web.Response:
    cfg = request.app[ENGINE].cfg
    if not cfg.source_chat_id:
        return err("Defina a origem primeiro.")
    return await _account_call(request, request.app[ACCOUNT].sync_topics(cfg.source_chat_id, cfg.dest_chat_id))


async def topics_map(request: web.Request) -> web.Response:
    data = await body(request)
    eng = request.app[ENGINE]
    cfg = eng.cfg
    if not (cfg.source_chat_id and cfg.dest_chat_id):
        return err("Defina origem e destino primeiro.")
    try:
        src_thread = int(data.get("source_thread_id"))
    except (TypeError, ValueError):
        return err("Tópico de origem inválido.")
    mode = data.get("mode")
    db = request.app[DB]
    if mode == "auto":
        db.delete_topic_map(cfg.source_chat_id, src_thread, cfg.dest_chat_id)
    elif mode == "general":
        db.set_topic_map(cfg.source_chat_id, src_thread, cfg.dest_chat_id, None)
    elif mode == "topic":
        try:
            dest_thread = int(data.get("dest_thread_id"))
        except (TypeError, ValueError):
            return err("Escolha o tópico de destino.")
        db.set_topic_map(cfg.source_chat_id, src_thread, cfg.dest_chat_id, dest_thread)
    else:
        return err("Modo inválido.")
    log.info("Mapeamento de tópico alterado: origem #%d -> %s", src_thread,
             {"auto": "criar automaticamente", "general": "Geral"}.get(mode, f"destino #{data.get('dest_thread_id')}"))
    if eng.worker:
        eng.worker.wake()
    return ok(build_state(request.app))


async def import_start(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    cfg = eng.cfg
    if not (cfg.source_chat_id and cfg.dest_chat_id):
        return err("Defina origem e destino primeiro.")
    if request.app[ACCOUNT].state != "conectada":
        return err("Conecte sua conta do Telegram primeiro.")
    try:
        request.app[IMPORTER].start(cfg.source_chat_id, cfg.dest_chat_id,
                                    lambda: eng.worker.wake() if eng.worker else None)
    except AccountError as e:
        return err(str(e))
    return ok(build_state(request.app))


async def import_stop(request: web.Request) -> web.Response:
    request.app[IMPORTER].stop()
    return ok(build_state(request.app))


async def logs(request: web.Request) -> web.Response:
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    return ok(request.app[LOGS].since(since))


def create_app(engine: Engine, db: Database, logs_handler: MemoryLogHandler) -> web.Application:
    app = web.Application(middlewares=[security_mw], client_max_size=64 * 1024)
    app[ENGINE] = engine
    app[DB] = db
    app[LOGS] = logs_handler
    app[AUTH] = AuthState(db)
    app[ACCOUNT] = account = UserAccount(db, engine.redact)
    app[IMPORTER] = importer = HistoryImporter(account, db)

    async def _cleanup(_app):
        await importer.shutdown()
        await account.shutdown()

    app.on_cleanup.append(_cleanup)
    r = app.router
    r.add_get("/", index)
    r.add_static("/static/", STATIC, show_index=False)
    r.add_get("/api/auth/state", auth_state)
    r.add_post("/api/auth/login", auth_login)
    r.add_post("/api/auth/logout", auth_logout)
    r.add_post("/api/auth/password", change_password)
    r.add_get("/api/state", get_state)
    r.add_post("/api/token", set_token)
    r.add_post("/api/settings", set_settings)
    r.add_post("/api/restart", restart)
    r.add_post("/api/chats/refresh", chats_refresh)
    r.add_post("/api/chats/add", chats_add)
    r.add_post("/api/chats/forget", chats_forget)
    r.add_post("/api/pause", pause)
    r.add_post("/api/resume", resume)
    r.add_get("/api/queue", queue)
    r.add_post("/api/queue/requeue", queue_requeue)
    r.add_post("/api/queue/skip", queue_skip)
    r.add_post("/api/backfill", backfill)
    r.add_post("/api/webhook/delete", webhook_delete)
    r.add_get("/api/logs", logs)
    r.add_post("/api/account/send_code", account_send_code)
    r.add_post("/api/account/code", account_code)
    r.add_post("/api/account/password", account_password)
    r.add_post("/api/account/logout", account_logout)
    r.add_post("/api/topics/fetch", topics_fetch)
    r.add_post("/api/topics/map", topics_map)
    r.add_post("/api/import/start", import_start)
    r.add_post("/api/import/stop", import_stop)
    return app
