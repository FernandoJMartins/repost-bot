"""Comandos administrativos, aceitos apenas em chat privado com ADMIN_USER_ID."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .config import Config, is_channel_or_supergroup_id
from .db import Database
from .telegram_api import BotApi

if TYPE_CHECKING:
    from .engine import Engine as Runtime

log = logging.getLogger(__name__)

HELP = """Comandos disponíveis:
/status – estado do backup
/stats – estatísticas
/pause – pausa as cópias (a fila continua recebendo)
/resume – retoma as cópias
/failed – lista mensagens com falha
/retry <id|all> – recoloca falhas na fila
/uncertain – lista entregas incertas
/resend <id|all> – reenvia entregas incertas (pode duplicar se já tiverem chegado)
/skip <id> – ignora uma mensagem pendente/falha/incerta
/backfill <de> <até> – copia um intervalo de IDs antigos (canal/supergrupo)
/help – esta ajuda"""


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class AdminCommands:
    def __init__(self, cfg: Config, db: Database, api: BotApi, runtime: "Runtime | None" = None):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.runtime = runtime

    def authorized(self, msg: dict) -> bool:
        return (msg.get("chat", {}).get("type") == "private"
                and msg.get("from", {}).get("id") == self.cfg.admin_user_id
                and not msg.get("from", {}).get("is_bot", False))

    def _wake(self) -> None:
        if self.runtime and self.runtime.worker:
            self.runtime.worker.wake()

    async def handle(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if self.cfg.admin_user_id is None and msg.get("chat", {}).get("type") == "private":
            # Ainda sem admin: ajuda a identificar o usuário, sem executar nada.
            if cmd == "/start":
                uid = msg.get("from", {}).get("id")
                reply = (f"Olá! Seu ID de usuário é {uid}.\n"
                         f"Na página web, em 'Administrador', clique em 'Sou eu' ao lado do seu nome.")
                try:
                    await self.api.send_message(msg["chat"]["id"], reply)
                except Exception as e:
                    log.warning("Falha ao responder /start: %s", e)
            return
        if not self.authorized(msg):
            log.warning("Comando ignorado de usuário não autorizado: id=%s",
                        msg.get("from", {}).get("id"))
            return
        if self.cfg.source_chat_id is None and cmd not in ("/start", "/help"):
            reply = "Configure origem e destino na página web primeiro."
        else:
            log.info("Comando administrativo recebido: %s", text)
            reply = self.execute(cmd, args)
        try:
            await self.api.send_message(msg["chat"]["id"], reply)
        except Exception as e:
            log.warning("Falha ao responder comando: %s", e)

    def execute(self, cmd: str, args: list[str]) -> str:
        src = self.cfg.source_chat_id
        if cmd in ("/start", "/help"):
            return HELP

        if cmd == "/status":
            s = self.db.stats(src)
            c = s["counts"]
            paused = self.db.is_paused()
            conn = "?" if not (self.runtime and self.runtime.poller) else ("conectado" if self.runtime.poller.connected else "reconectando")
            lines = [
                f"Estado: {'⏸ PAUSADO' if paused else '▶️ ativo'}",
            ]
            if paused and self.db.pause_reason():
                lines.append(f"Motivo: {self.db.pause_reason()}")
            lines += [
                f"Bot API: {conn}",
                f"Origem: {src} → Destino: {self.cfg.dest_chat_id}",
                f"Fila: {c['pending']} pendente(s), {c['sending']} enviando",
                f"Falhas: {c['failed']} | Incertas: {c['uncertain']}",
                f"Última cópia: {_fmt_ts(s['last_copied_at'])}",
                f"Maior ID visto na origem: {s['max_seen'] or '—'}",
            ]
            if self.runtime:
                up = int(time.time() - self.runtime.started_at)
                lines.append(f"Em execução há {up // 3600}h{(up % 3600) // 60:02d}m")
            return "\n".join(lines)

        if cmd == "/stats":
            s = self.db.stats(src)
            c = s["counts"]
            kinds = ", ".join(f"{k}: {n}" for k, n in list(s["kinds"].items())[:10]) or "—"
            return (f"Total registrado: {s['total']}\n"
                    f"Copiadas: {c['done']} (últimas 24h: {s['copied_24h']})\n"
                    f"Ignoradas: {c['skipped']}\n"
                    f"Pendentes: {c['pending']}\nFalhas: {c['failed']}\nIncertas: {c['uncertain']}\n"
                    f"Por tipo: {kinds}\n"
                    f"Offset de updates: {s['offset']}")

        if cmd == "/pause":
            self.db.set_paused(True, "pausado pelo admin")
            log.info("Backup pausado pelo admin")
            return "⏸ Backup pausado. Novas mensagens continuam sendo enfileiradas."

        if cmd == "/resume":
            self.db.set_paused(False)
            self._wake()
            log.info("Backup retomado pelo admin")
            return "▶️ Backup retomado."

        if cmd in ("/failed", "/uncertain"):
            status = cmd[1:]
            rows = self.db.list_by_status(src, status, 20)
            if not rows:
                return f"Nenhuma mensagem com status '{status}'."
            body = "\n".join(f"#{r.source_message_id} [{r.kind}] {(r.last_error or '')[:120]}" for r in rows)
            return f"{len(rows)} mensagem(ns) '{status}' (máx. 20):\n{body}"

        if cmd in ("/retry", "/resend"):
            status = "failed" if cmd == "/retry" else "uncertain"
            if len(args) != 1:
                return f"Uso: {cmd} <id|all>"
            target = None if args[0].lower() == "all" else _parse_int(args[0])
            if args[0].lower() != "all" and target is None:
                return "ID inválido."
            n = self.db.requeue(src, status, target)
            self._wake()
            log.info("Admin recolocou %d mensagem(ns) '%s' na fila", n, status)
            return f"{n} mensagem(ns) recolocada(s) na fila."

        if cmd == "/skip":
            if len(args) != 1 or _parse_int(args[0]) is None:
                return "Uso: /skip <id>"
            n = self.db.skip_manual(src, int(args[0]))
            self._wake()
            return "Mensagem ignorada." if n else "Nada a ignorar (ID inexistente ou já processado)."

        if cmd == "/backfill":
            if not is_channel_or_supergroup_id(src):
                return "Backfill por ID só funciona quando a origem é canal ou supergrupo."
            if len(args) != 2 or _parse_int(args[0]) is None or _parse_int(args[1]) is None:
                return "Uso: /backfill <id_inicial> <id_final>"
            first, last = int(args[0]), int(args[1])
            if first < 1 or last < first:
                return "Intervalo inválido."
            if last - first + 1 > self.cfg.backfill_max:
                return f"Intervalo maior que BACKFILL_MAX ({self.cfg.backfill_max}). Divida em partes."
            n = self.db.enqueue_backfill(src, first, last)
            minutes = n / self.cfg.dest_messages_per_minute
            self._wake()
            log.info("Backfill enfileirado: IDs %d..%d (%d novos)", first, last, n)
            return (f"{n} ID(s) enfileirado(s) para backfill ({last - first + 1 - n} já registrados).\n"
                    f"Tempo estimado no limite configurado: ~{minutes:.0f} min (IDs apagados são pulados).")

        return "Comando desconhecido. Envie /help."


def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None
