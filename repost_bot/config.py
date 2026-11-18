"""Configuração.

Apenas o necessário para *iniciar o processo* vem de variáveis de ambiente
(caminho do banco, logs, endereço da página web). Todo o resto — token, chats
de origem/destino, admin e ajustes finos — é definido pela página web e
guardado no SQLite (tabela kv, chaves 'cfg:<campo>').
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import Database


class ConfigError(Exception):
    pass


def is_channel_or_supergroup_id(chat_id: int | None) -> bool:
    """Canais e supergrupos têm IDs no formato -100XXXXXXXXXX na Bot API.

    Nesses chats o message_id é sequencial por chat, o que permite detectar
    lacunas. Em grupos básicos o message_id não é sequencial por chat.
    """
    return chat_id is not None and str(chat_id).startswith("-100")


# --------------------------------------------------------------- bootstrap
@dataclass(frozen=True)
class Bootstrap:
    db_path: Path
    log_level: str
    log_file: Path | None
    web_host: str
    web_port: int
    api_base: str

    @classmethod
    def from_env(cls) -> "Bootstrap":
        log_file = os.environ.get("LOG_FILE", "logs/repost-bot.log").strip()
        port_raw = os.environ.get("WEB_PORT", "8080").strip() or "8080"
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"WEB_PORT inválido: {port_raw!r}") from exc
        return cls(
            db_path=Path(os.environ.get("DB_PATH", "data/repost.db").strip() or "data/repost.db"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            log_file=Path(log_file) if log_file else None,
            web_host=os.environ.get("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
            web_port=port,
            api_base=os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").strip().rstrip("/"),
        )


# ----------------------------------------------------------------- runtime
@dataclass(frozen=True)
class Config:
    bot_token: str = ""
    source_chat_id: int | None = None
    dest_chat_id: int | None = None
    admin_user_id: int | None = None
    db_path: Path = Path("data/repost.db")
    api_base: str = "https://api.telegram.org"
    # Limite de envio no destino (FAQ oficial: <= 20 msg/min em grupos, ~1 msg/s por chat).
    dest_messages_per_minute: int = 20
    poll_timeout: int = 50
    request_timeout: float = 30.0
    album_settle_seconds: float = 3.0
    gap_settle_seconds: float = 10.0
    gap_recovery: bool = True
    gap_auto_max: int = 300
    backfill_max: int = 5000
    max_attempts: int = 8
    # hold   = mensagens com entrega incerta ficam retidas até decisão do admin (nunca duplica)
    # resend = reenvia automaticamente (pode duplicar em casos raros)
    uncertain_policy: str = "hold"
    heartbeat_minutes: int = 15
    # Cria no destino um tópico com o mesmo nome quando um tópico da origem não está mapeado.
    auto_create_topics: bool = True

    @property
    def ready(self) -> bool:
        """Tudo o que é necessário para copiar mensagens está definido."""
        return bool(self.bot_token and self.source_chat_id and self.dest_chat_id)

    @property
    def gap_recovery_supported(self) -> bool:
        return self.gap_recovery and is_channel_or_supergroup_id(self.source_chat_id)

    def missing(self) -> list[str]:
        out = []
        if not self.bot_token:
            out.append("token do bot")
        if not self.source_chat_id:
            out.append("chat de origem")
        if not self.dest_chat_id:
            out.append("chat de destino")
        return out

    # ------------------------------------------------------------ store
    @classmethod
    def load(cls, db: "Database", boot: Bootstrap | None = None) -> "Config":
        values: dict[str, Any] = {}
        for name in EDITABLE + ("bot_token",):
            raw = db.get_kv(f"cfg:{name}")
            if raw is None or raw == "":
                continue
            values[name] = coerce(name, raw)
        cfg = cls(**values)
        if boot:
            cfg = replace(cfg, db_path=boot.db_path, api_base=boot.api_base)
        return cfg


# Campos que a página web pode alterar (o token tem fluxo próprio).
EDITABLE = (
    "source_chat_id", "dest_chat_id", "admin_user_id", "dest_messages_per_minute",
    "poll_timeout", "request_timeout", "album_settle_seconds", "gap_settle_seconds",
    "gap_recovery", "gap_auto_max", "backfill_max", "max_attempts", "uncertain_policy",
    "heartbeat_minutes", "auto_create_topics",
)

_RANGES: dict[str, tuple[float, float]] = {
    "dest_messages_per_minute": (1, 60),
    "poll_timeout": (1, 120),
    "request_timeout": (5, 300),
    "album_settle_seconds": (0, 60),
    "gap_settle_seconds": (0, 600),
    "gap_auto_max": (0, 100_000),
    "backfill_max": (1, 1_000_000),
    "max_attempts": (1, 100),
    "heartbeat_minutes": (1, 1440),
}

_TYPES = {f.name: f.type for f in fields(Config)}


def coerce(name: str, raw: Any) -> Any:
    """Converte e valida um valor vindo do banco ou da página web."""
    if name not in _TYPES:
        raise ConfigError(f"Campo desconhecido: {name}")
    typ = str(_TYPES[name])
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        if name in ("source_chat_id", "dest_chat_id", "admin_user_id", "bot_token"):
            return None if name != "bot_token" else ""
        raise ConfigError(f"{name} não pode ficar vazio")

    if name == "bot_token":
        token = str(raw).strip()
        if ":" not in token or " " in token:
            raise ConfigError("Token com formato inválido (esperado <id>:<hash> do @BotFather)")
        return token
    if name == "uncertain_policy":
        v = str(raw).strip().lower()
        if v not in ("hold", "resend"):
            raise ConfigError("Política de entrega incerta deve ser 'hold' ou 'resend'")
        return v
    if "bool" in typ:
        if isinstance(raw, bool):
            return raw
        v = str(raw).strip().lower()
        if v in ("1", "true", "yes", "sim", "on"):
            return True
        if v in ("0", "false", "no", "nao", "não", "off"):
            return False
        raise ConfigError(f"{name} deve ser verdadeiro/falso")
    try:
        value: float | int = float(raw) if "float" in typ else int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} deve ser numérico (recebido: {raw!r})") from exc
    if name == "admin_user_id" and value <= 0:
        raise ConfigError("O ID do admin deve ser o ID numérico (positivo) do seu usuário")
    if name in ("source_chat_id", "dest_chat_id") and value >= 0:
        raise ConfigError("IDs de grupos e canais são negativos")
    if name in _RANGES:
        lo, hi = _RANGES[name]
        if not lo <= value <= hi:
            raise ConfigError(f"{name} deve estar entre {lo:g} e {hi:g}")
    return value


def save_settings(db: "Database", updates: dict[str, Any]) -> Config:
    """Valida e grava alterações vindas da página web. Tudo ou nada."""
    clean: dict[str, Any] = {}
    for name, raw in updates.items():
        if name not in EDITABLE:
            raise ConfigError(f"Campo não editável: {name}")
        clean[name] = coerce(name, raw)
    src = clean.get("source_chat_id", Config.load(db).source_chat_id)
    dst = clean.get("dest_chat_id", Config.load(db).dest_chat_id)
    if src is not None and src == dst:
        raise ConfigError("Origem e destino não podem ser o mesmo chat")
    with db.transaction():
        for name, value in clean.items():
            db.set_kv(f"cfg:{name}", "" if value is None else str(value))
    return Config.load(db)


def save_token(db: "Database", token: str) -> None:
    db.set_kv("cfg:bot_token", coerce("bot_token", token))


_ENV_SEED = {
    "BOT_TOKEN": "bot_token", "SOURCE_CHAT_ID": "source_chat_id", "DEST_CHAT_ID": "dest_chat_id",
    "ADMIN_USER_ID": "admin_user_id", "DEST_MESSAGES_PER_MINUTE": "dest_messages_per_minute",
    "POLL_TIMEOUT": "poll_timeout", "REQUEST_TIMEOUT": "request_timeout",
    "ALBUM_SETTLE_SECONDS": "album_settle_seconds", "GAP_SETTLE_SECONDS": "gap_settle_seconds",
    "GAP_RECOVERY": "gap_recovery", "GAP_AUTO_MAX": "gap_auto_max", "BACKFILL_MAX": "backfill_max",
    "MAX_ATTEMPTS": "max_attempts", "UNCERTAIN_POLICY": "uncertain_policy",
    "HEARTBEAT_MINUTES": "heartbeat_minutes",
}


def seed_from_env(db: "Database") -> list[str]:
    """Compatibilidade: importa valores de variáveis de ambiente antigas
    apenas se ainda não existirem no banco. Depois disso, vale a página web."""
    imported = []
    for env, name in _ENV_SEED.items():
        raw = os.environ.get(env, "").strip()
        if raw and db.get_kv(f"cfg:{name}") in (None, ""):
            try:
                db.set_kv(f"cfg:{name}", str(coerce(name, raw)))
                imported.append(env)
            except ConfigError:
                pass
    return imported
