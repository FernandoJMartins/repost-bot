"""Persistência em SQLite: fila de cópia, mapeamento origem->destino e estado do bot.

Todas as transições de estado são gravadas antes/depois de cada chamada à API
para garantir idempotência:

    pending  -> sending -> done | skipped
                        -> pending (nova tentativa, com backoff)
                        -> failed  (tentativas esgotadas)
                        -> uncertain (a requisição pode ter sido entregue; não reenviar sozinho)

Uma linha em 'sending' encontrada na inicialização significa que o processo
caiu no meio de uma chamada: ela vira 'uncertain' (política 'hold').
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id    INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    media_group_id    TEXT,
    kind              TEXT    NOT NULL,          -- photo, video, text, ..., 'unknown' (lacuna/backfill)
    file_unique_id    TEXT,
    origin            TEXT    NOT NULL,          -- update | gap | backfill
    status            TEXT    NOT NULL,          -- pending|sending|done|skipped|failed|uncertain
    split             INTEGER NOT NULL DEFAULT 0, -- 1 = processar individualmente
    dest_chat_id      INTEGER,
    dest_message_id   INTEGER,
    dest_message_ids  TEXT,                      -- JSON quando copiado em lote sem mapeamento 1:1
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   REAL    NOT NULL DEFAULT 0,
    last_error        TEXT,
    source_date       INTEGER,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL,
    copied_at         REAL,
    UNIQUE (source_chat_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_queue
    ON messages (source_chat_id, status, source_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_group
    ON messages (source_chat_id, media_group_id);

-- Chats que o bot conhece (a Bot API não tem método para listar os chats do bot;
-- eles são descobertos pelos updates: my_chat_member, posts, encaminhamentos).
CREATE TABLE IF NOT EXISTS chats (
    bot_id      INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    type        TEXT,
    title       TEXT,
    username    TEXT,
    bot_status  TEXT,       -- creator|administrator|member|restricted|left|kicked|unknown
    can_post    INTEGER,    -- o bot pode enviar mensagens (destino)
    protected   INTEGER,    -- has_protected_content
    found_via   TEXT,
    last_seen   REAL NOT NULL,
    PRIMARY KEY (bot_id, chat_id)
);

-- Usuários que falaram com o bot no privado (para escolher o admin com um clique).
CREATE TABLE IF NOT EXISTS private_users (
    bot_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    first_name TEXT,
    last_name  TEXT,
    username   TEXT,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);

-- Tópicos conhecidos (origem e destino). thread_id = message_id da criação do tópico.
CREATE TABLE IF NOT EXISTS topics (
    chat_id      INTEGER NOT NULL,
    thread_id    INTEGER NOT NULL,
    title        TEXT,
    icon_color   INTEGER,
    icon_emoji_id TEXT,
    closed       INTEGER NOT NULL DEFAULT 0,
    found_via    TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

-- Tópico da origem -> tópico do destino. dest_thread_id NULL = tópico Geral.
CREATE TABLE IF NOT EXISTS topic_map (
    source_chat_id   INTEGER NOT NULL,
    source_thread_id INTEGER NOT NULL,
    dest_chat_id     INTEGER NOT NULL,
    dest_thread_id   INTEGER,
    created_by_bot   INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL,
    PRIMARY KEY (source_chat_id, source_thread_id, dest_chat_id)
);
"""

# Colunas adicionadas depois da primeira versão (migração simples).
MIGRATIONS = (
    ("messages", "source_thread_id", "INTEGER"),
)

STATUSES = ("pending", "sending", "done", "skipped", "failed", "uncertain")


@dataclass
class Row:
    id: int
    source_chat_id: int
    source_message_id: int
    media_group_id: str | None
    kind: str
    file_unique_id: str | None
    origin: str
    status: str
    split: int
    attempts: int
    next_attempt_at: float
    created_at: float
    last_error: str | None = None
    dest_message_id: int | None = None
    source_thread_id: int | None = None


@dataclass
class Unit:
    """Conjunto de mensagens copiadas em uma única chamada à API."""
    rows: list[Row]
    kind: str  # single | album | gap
    ready_at: float = 0.0

    @property
    def message_ids(self) -> list[int]:
        return [r.source_message_id for r in self.rows]

    @property
    def row_ids(self) -> list[int]:
        return [r.id for r in self.rows]

    @property
    def thread_id(self) -> int | None:
        return self.rows[0].source_thread_id


@dataclass
class IngestItem:
    source_message_id: int
    kind: str
    status: str  # pending ou skipped
    media_group_id: str | None = None
    file_unique_id: str | None = None
    reason: str | None = None
    source_date: int | None = None
    source_thread_id: int | None = None  # tópico da origem (None = Geral / sem tópicos)


@dataclass
class IngestResult:
    new_rows: int = 0
    gap_rows: int = 0
    skipped: int = 0
    extra: dict = field(default_factory=dict)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ util
    def _tx(self):
        return _Transaction(self.conn)

    def transaction(self):
        return _Transaction(self.conn)

    @staticmethod
    def _row(r: sqlite3.Row) -> Row:
        return Row(
            id=r["id"],
            source_chat_id=r["source_chat_id"],
            source_message_id=r["source_message_id"],
            media_group_id=r["media_group_id"],
            kind=r["kind"],
            file_unique_id=r["file_unique_id"],
            origin=r["origin"],
            status=r["status"],
            split=r["split"],
            attempts=r["attempts"],
            next_attempt_at=r["next_attempt_at"],
            created_at=r["created_at"],
            last_error=r["last_error"],
            dest_message_id=r["dest_message_id"],
            source_thread_id=r["source_thread_id"],
        )

    # -------------------------------------------------------------------- kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_offset(self) -> int | None:
        v = self.get_kv("update_offset")
        return int(v) if v is not None else None

    def reset_offset(self) -> None:
        """Usado ao trocar de bot: o offset de updates pertence a cada bot."""
        self.conn.execute("DELETE FROM kv WHERE key='update_offset'")

    def is_paused(self) -> bool:
        return self.get_kv("paused", "0") == "1"

    def pause_reason(self) -> str | None:
        return self.get_kv("pause_reason")

    def set_paused(self, paused: bool, reason: str = "") -> None:
        with self._tx():
            self.set_kv("paused", "1" if paused else "0")
            self.set_kv("pause_reason", reason if paused else "")

    def max_seen(self, source_chat_id: int) -> int | None:
        v = self.get_kv(f"max_seen:{source_chat_id}")
        return int(v) if v is not None else None

    # ---------------------------------------------------------------- ingest
    def ingest(
        self,
        source_chat_id: int,
        items: Sequence[IngestItem],
        gap_ids: Iterable[int] = (),
        new_offset: int | None = None,
        max_seen: int | None = None,
        now: float | None = None,
        origin: str = "update",
    ) -> IngestResult:
        """Grava mensagens recebidas, lacunas e o novo offset numa única transação.

        Se o processo cair antes do COMMIT, o Telegram reentrega os updates
        (offset não confirmado) e o UNIQUE impede duplicidade.
        """
        now = time.time() if now is None else now
        res = IngestResult()
        with self._tx():
            for it in items:
                before = self.conn.total_changes
                self.conn.execute(
                    """
                    INSERT INTO messages (source_chat_id, source_message_id, media_group_id, kind,
                        file_unique_id, origin, status, last_error, source_date, created_at, updated_at,
                        source_thread_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_chat_id, source_message_id) DO UPDATE SET
                        media_group_id   = excluded.media_group_id,
                        kind             = excluded.kind,
                        file_unique_id   = excluded.file_unique_id,
                        origin           = excluded.origin,
                        status           = excluded.status,
                        last_error       = excluded.last_error,
                        source_date      = excluded.source_date,
                        source_thread_id = excluded.source_thread_id,
                        updated_at       = excluded.updated_at
                    WHERE messages.status = 'pending' AND messages.kind = 'unknown'
                    """,
                    (source_chat_id, it.source_message_id, it.media_group_id, it.kind,
                     it.file_unique_id, origin, it.status, it.reason, it.source_date, now, now,
                     it.source_thread_id),
                )
                if self.conn.total_changes > before:
                    res.new_rows += 1
                    if it.status == "skipped":
                        res.skipped += 1
            for mid in gap_ids:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO messages (source_chat_id, source_message_id, kind, origin,
                        status, created_at, updated_at)
                    VALUES (?, ?, 'unknown', 'gap', 'pending', ?, ?)
                    """,
                    (source_chat_id, mid, now, now),
                )
                res.gap_rows += cur.rowcount
            if new_offset is not None:
                self.set_kv("update_offset", str(new_offset))
            if max_seen is not None:
                self.set_kv(f"max_seen:{source_chat_id}", str(max_seen))
        return res

    def enqueue_backfill(self, source_chat_id: int, first: int, last: int, now: float | None = None) -> int:
        now = time.time() if now is None else now
        inserted = 0
        with self._tx():
            for mid in range(first, last + 1):
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO messages (source_chat_id, source_message_id, kind, origin,
                        status, created_at, updated_at)
                    VALUES (?, ?, 'unknown', 'backfill', 'pending', ?, ?)
                    """,
                    (source_chat_id, mid, now, now),
                )
                inserted += cur.rowcount
        return inserted

    # ----------------------------------------------------------------- fila
    def next_unit(
        self,
        source_chat_id: int,
        album_settle: float,
        gap_settle: float,
        max_album: int = 10,
        max_gap_chunk: int = 100,
    ) -> Unit | None:
        """Retorna a próxima unidade a copiar, respeitando a ordem do message_id de origem."""
        head_r = self.conn.execute(
            """SELECT * FROM messages WHERE source_chat_id=? AND status='pending'
               ORDER BY source_message_id LIMIT 1""",
            (source_chat_id,),
        ).fetchone()
        if head_r is None:
            return None
        head = self._row(head_r)

        if head.split:
            settle = gap_settle if head.kind == "unknown" else 0.0
            return Unit([head], "single", max(head.next_attempt_at, head.created_at + settle))

        if head.kind == "unknown":
            cand = [self._row(r) for r in self.conn.execute(
                """SELECT * FROM messages WHERE source_chat_id=? AND status='pending' AND kind='unknown'
                   AND split=0 AND source_message_id>=? ORDER BY source_message_id LIMIT ?""",
                (source_chat_id, head.source_message_id, max_gap_chunk),
            )]
            rows = [cand[0]]
            for r in cand[1:]:
                if r.source_message_id != rows[-1].source_message_id + 1:
                    break
                rows.append(r)
            ready = max(max(r.created_at for r in rows) + gap_settle, max(r.next_attempt_at for r in rows))
            return Unit(rows, "gap", ready)

        if head.media_group_id:
            rows = [self._row(r) for r in self.conn.execute(
                """SELECT * FROM messages WHERE source_chat_id=? AND status='pending' AND split=0
                   AND media_group_id=? ORDER BY source_message_id LIMIT ?""",
                (source_chat_id, head.media_group_id, max_album),
            )]
            ready = max(max(r.created_at for r in rows) + album_settle, max(r.next_attempt_at for r in rows))
            return Unit(rows, "album" if len(rows) > 1 else "single", ready)

        return Unit([head], "single", head.next_attempt_at)

    def _set_status(self, row_ids: Sequence[int], sql_set: str, params: tuple) -> None:
        if not row_ids:
            return
        marks = ",".join("?" * len(row_ids))
        self.conn.execute(f"UPDATE messages SET {sql_set} WHERE id IN ({marks})", (*params, *row_ids))

    def mark_sending(self, row_ids: Sequence[int], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._tx():
            self._set_status(row_ids, "status='sending', attempts=attempts+1, updated_at=?", (now,))

    def mark_done(self, row_id: int, dest_chat_id: int, dest_message_id: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._tx():
            self._set_status([row_id],
                             "status='done', dest_chat_id=?, dest_message_id=?, last_error=NULL, "
                             "updated_at=?, copied_at=?",
                             (dest_chat_id, dest_message_id, now, now))

    def mark_done_batch(self, rows: Sequence[Row], dest_chat_id: int, dest_ids: Sequence[int],
                        now: float | None = None) -> None:
        """Lote copiado com copyMessages. Mapeia 1:1 se as quantidades baterem;
        caso contrário (algumas mensagens não existiam), guarda a lista de destino."""
        now = time.time() if now is None else now
        with self._tx():
            if len(dest_ids) == len(rows):
                for r, d in zip(rows, dest_ids):
                    self._set_status([r.id],
                                     "status='done', dest_chat_id=?, dest_message_id=?, last_error=NULL, "
                                     "updated_at=?, copied_at=?",
                                     (dest_chat_id, d, now, now))
            else:
                blob = json.dumps(list(dest_ids))
                note = (f"lote copiado parcialmente: {len(dest_ids)} de {len(rows)} "
                        f"(as demais não existiam ou não eram copiáveis)")
                self._set_status([r.id for r in rows],
                                 "status='done', dest_chat_id=?, dest_message_ids=?, last_error=?, "
                                 "updated_at=?, copied_at=?",
                                 (dest_chat_id, blob, note, now, now))

    def mark_skipped(self, row_ids: Sequence[int], reason: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._tx():
            self._set_status(row_ids, "status='skipped', last_error=?, updated_at=?", (reason, now))

    def mark_retry(self, row_ids: Sequence[int], error: str, next_attempt_at: float,
                   refund_attempt: bool = False, now: float | None = None) -> None:
        now = time.time() if now is None else now
        refund = ", attempts=MAX(attempts-1, 0)" if refund_attempt else ""
        with self._tx():
            self._set_status(row_ids,
                             f"status='pending', last_error=?, next_attempt_at=?, updated_at=?{refund}",
                             (error, next_attempt_at, now))

    def mark_split(self, row_ids: Sequence[int], error: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._tx():
            self._set_status(row_ids,
                             "status='pending', split=1, attempts=MAX(attempts-1, 0), last_error=?, updated_at=?",
                             (error, now))

    def mark_failed(self, row_ids: Sequence[int], error: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._tx():
            self._set_status(row_ids, "status='failed', last_error=?, updated_at=?", (error, now))

    def mark_uncertain(self, row_ids: Sequence[int], error: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._tx():
            self._set_status(row_ids, "status='uncertain', last_error=?, updated_at=?", (error, now))

    def recover_on_startup(self, policy: str = "hold") -> int:
        """Trata linhas que ficaram em 'sending' por queda do processo."""
        now = time.time()
        with self._tx():
            if policy == "resend":
                cur = self.conn.execute(
                    "UPDATE messages SET status='pending', updated_at=?, "
                    "last_error='interrompido durante envio; reenviando (UNCERTAIN_POLICY=resend)' "
                    "WHERE status='sending'", (now,))
            else:
                cur = self.conn.execute(
                    "UPDATE messages SET status='uncertain', updated_at=?, "
                    "last_error='processo interrompido durante o envio; entrega não confirmada' "
                    "WHERE status='sending'", (now,))
        return cur.rowcount

    # ------------------------------------------------------- administração
    def counts(self, source_chat_id: int) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM messages WHERE source_chat_id=? GROUP BY status",
            (source_chat_id,),
        ):
            out[r["status"]] = r["n"]
        return out

    def stats(self, source_chat_id: int) -> dict:
        c = self.counts(source_chat_id)
        last = self.conn.execute(
            "SELECT MAX(copied_at) AS t, MAX(source_message_id) AS m FROM messages "
            "WHERE source_chat_id=? AND status='done'", (source_chat_id,)).fetchone()
        kinds = {r["kind"]: r["n"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM messages WHERE source_chat_id=? AND status='done' "
            "GROUP BY kind ORDER BY n DESC", (source_chat_id,))}
        since = time.time() - 86400
        last24 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE source_chat_id=? AND status='done' AND copied_at>=?",
            (source_chat_id, since)).fetchone()["n"]
        return {
            "counts": c,
            "total": sum(c.values()),
            "last_copied_at": last["t"],
            "last_copied_source_id": last["m"],
            "max_seen": self.max_seen(source_chat_id),
            "offset": self.get_offset(),
            "kinds": kinds,
            "copied_24h": last24,
        }

    def list_by_status(self, source_chat_id: int, status: str, limit: int = 20) -> list[Row]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM messages WHERE source_chat_id=? AND status=? ORDER BY source_message_id LIMIT ?",
            (source_chat_id, status, limit))]

    def get(self, source_chat_id: int, source_message_id: int) -> Row | None:
        r = self.conn.execute(
            "SELECT * FROM messages WHERE source_chat_id=? AND source_message_id=?",
            (source_chat_id, source_message_id)).fetchone()
        return self._row(r) if r else None

    def requeue(self, source_chat_id: int, from_status: str, source_message_id: int | None = None) -> int:
        """Volta mensagens 'failed' ou 'uncertain' para a fila (decisão explícita do admin)."""
        if from_status not in ("failed", "uncertain"):
            raise ValueError(from_status)
        now = time.time()
        sql = ("UPDATE messages SET status='pending', attempts=0, next_attempt_at=0, updated_at=? "
               "WHERE source_chat_id=? AND status=?")
        params: list = [now, source_chat_id, from_status]
        if source_message_id is not None:
            sql += " AND source_message_id=?"
            params.append(source_message_id)
        with self._tx():
            cur = self.conn.execute(sql, params)
        return cur.rowcount

    def skip_manual(self, source_chat_id: int, source_message_id: int) -> int:
        now = time.time()
        with self._tx():
            cur = self.conn.execute(
                "UPDATE messages SET status='skipped', last_error='ignorada manualmente pelo admin', "
                "updated_at=? WHERE source_chat_id=? AND source_message_id=? "
                "AND status IN ('pending','failed','uncertain')",
                (now, source_chat_id, source_message_id))
        return cur.rowcount


    # ------------------------------------------------- chats e usuários
    def upsert_chat(self, bot_id: int, chat: dict, found_via: str, bot_status: str | None = None,
                    can_post: bool | None = None, protected: bool | None = None,
                    now: float | None = None) -> None:
        """Registra/atualiza um chat conhecido. Campos None não sobrescrevem valores existentes."""
        now = time.time() if now is None else now
        title = chat.get("title") or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x) or None
        self.conn.execute(
            """
            INSERT INTO chats (bot_id, chat_id, type, title, username, bot_status, can_post, protected,
                               found_via, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (bot_id, chat_id) DO UPDATE SET
                type       = COALESCE(excluded.type, chats.type),
                title      = COALESCE(excluded.title, chats.title),
                username   = COALESCE(excluded.username, chats.username),
                bot_status = COALESCE(excluded.bot_status, chats.bot_status),
                can_post   = COALESCE(excluded.can_post, chats.can_post),
                protected  = COALESCE(excluded.protected, chats.protected),
                last_seen  = excluded.last_seen
            """,
            (bot_id, chat["id"], chat.get("type"), title, chat.get("username"), bot_status,
             None if can_post is None else int(can_post), None if protected is None else int(protected),
             found_via, now),
        )

    def list_chats(self, bot_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM chats WHERE bot_id=? AND COALESCE(type, '')!='private' "
            "ORDER BY CASE WHEN bot_status IN ('left','kicked') THEN 1 ELSE 0 END, last_seen DESC",
            (bot_id,))]

    def forget_chat(self, bot_id: int, chat_id: int) -> None:
        self.conn.execute("DELETE FROM chats WHERE bot_id=? AND chat_id=?", (bot_id, chat_id))

    def upsert_user(self, bot_id: int, user: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.conn.execute(
            """
            INSERT INTO private_users (bot_id, user_id, first_name, last_name, username, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (bot_id, user_id) DO UPDATE SET
                first_name=excluded.first_name, last_name=excluded.last_name,
                username=excluded.username, last_seen=excluded.last_seen
            """,
            (bot_id, user["id"], user.get("first_name"), user.get("last_name"), user.get("username"), now),
        )

    def list_users(self, bot_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM private_users WHERE bot_id=? ORDER BY last_seen DESC LIMIT 20", (bot_id,))]

    # ---------------------------------------------------------------- tópicos
    def upsert_topic(self, chat_id: int, thread_id: int, title: str | None = None,
                     icon_color: int | None = None, icon_emoji_id: str | int | None = None,
                     closed: bool | None = None, found_via: str = "update") -> None:
        self.conn.execute(
            """
            INSERT INTO topics (chat_id, thread_id, title, icon_color, icon_emoji_id, closed, found_via, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?)
            ON CONFLICT (chat_id, thread_id) DO UPDATE SET
                title         = COALESCE(excluded.title, topics.title),
                icon_color    = COALESCE(excluded.icon_color, topics.icon_color),
                icon_emoji_id = COALESCE(excluded.icon_emoji_id, topics.icon_emoji_id),
                closed        = COALESCE(?, topics.closed),
                updated_at    = excluded.updated_at
            """,
            (chat_id, thread_id, title, icon_color, None if icon_emoji_id is None else str(icon_emoji_id),
             None if closed is None else int(closed), found_via, time.time(),
             None if closed is None else int(closed)),
        )

    def delete_topic(self, chat_id: int, thread_id: int) -> None:
        self.conn.execute("DELETE FROM topics WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))

    def get_topic(self, chat_id: int, thread_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM topics WHERE chat_id=? AND thread_id=?", (chat_id, thread_id)).fetchone()
        return dict(r) if r else None

    def list_topics(self, chat_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM topics WHERE chat_id=? ORDER BY thread_id", (chat_id,))]

    def source_threads_in_queue(self, source_chat_id: int) -> list[int]:
        """Tópicos que aparecem em mensagens registradas mas talvez não tenham nome conhecido."""
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT source_thread_id FROM messages WHERE source_chat_id=? AND source_thread_id IS NOT NULL",
            (source_chat_id,))]

    def get_topic_map(self, source_chat_id: int, source_thread_id: int, dest_chat_id: int) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM topic_map WHERE source_chat_id=? AND source_thread_id=? AND dest_chat_id=?",
            (source_chat_id, source_thread_id, dest_chat_id)).fetchone()
        return dict(r) if r else None

    def set_topic_map(self, source_chat_id: int, source_thread_id: int, dest_chat_id: int,
                      dest_thread_id: int | None, created_by_bot: bool = False) -> None:
        self.conn.execute(
            """
            INSERT INTO topic_map (source_chat_id, source_thread_id, dest_chat_id, dest_thread_id,
                                   created_by_bot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_chat_id, source_thread_id, dest_chat_id) DO UPDATE SET
                dest_thread_id = excluded.dest_thread_id,
                created_by_bot = excluded.created_by_bot,
                updated_at     = excluded.updated_at
            """,
            (source_chat_id, source_thread_id, dest_chat_id, dest_thread_id, int(created_by_bot), time.time()),
        )

    def delete_topic_map(self, source_chat_id: int, source_thread_id: int, dest_chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM topic_map WHERE source_chat_id=? AND source_thread_id=? AND dest_chat_id=?",
            (source_chat_id, source_thread_id, dest_chat_id))

    def list_topic_map(self, source_chat_id: int, dest_chat_id: int) -> dict[int, dict]:
        return {r["source_thread_id"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM topic_map WHERE source_chat_id=? AND dest_chat_id=?", (source_chat_id, dest_chat_id))}

    # ------------------------------------------------------------ sessões web
    def add_session(self, token_hash: str, expires_at: float) -> None:
        self.conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (time.time(),))
        self.conn.execute("INSERT INTO web_sessions(token_hash, expires_at) VALUES(?, ?)",
                          (token_hash, expires_at))

    def session_valid(self, token_hash: str) -> bool:
        r = self.conn.execute("SELECT expires_at FROM web_sessions WHERE token_hash=?", (token_hash,)).fetchone()
        return bool(r and r["expires_at"] > time.time())

    def delete_session(self, token_hash: str) -> None:
        self.conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash,))

    def delete_all_sessions(self) -> None:
        self.conn.execute("DELETE FROM web_sessions")


class _Transaction:
    """BEGIN IMMEDIATE / COMMIT com suporte a aninhamento simples."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.owner = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self.owner = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.owner:
            return False
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
