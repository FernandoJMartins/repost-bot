"""Cliente mínimo e explícito da Telegram Bot API (HTTPS, JSON).

Escrito diretamente sobre httpx para termos controle total sobre:
- o offset do getUpdates (persistido no SQLite junto com a fila);
- a distinção entre erros em que a requisição *não* chegou ao Telegram
  (seguro repetir) e erros em que ela *pode* ter sido processada
  (repetir poderia duplicar a mensagem no destino).
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

import httpx

log = logging.getLogger(__name__)


class TelegramError(Exception):
    """Resposta da Bot API com ok=false."""

    def __init__(self, method: str, code: int, description: str,
                 retry_after: float | None = None, migrate_to_chat_id: int | None = None):
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after
        self.migrate_to_chat_id = migrate_to_chat_id

    @property
    def desc(self) -> str:
        return self.description.lower()


class RetryAfter(TelegramError):
    """HTTP 429: o Telegram recusou a requisição e informou quanto esperar."""


class NetworkError(Exception):
    """Falha de transporte.

    maybe_delivered=False -> a conexão nem foi estabelecida; repetir é seguro.
    maybe_delivered=True  -> a requisição pode ter sido processada (ex.: timeout
                             de leitura); repetir pode duplicar.
    """

    def __init__(self, method: str, message: str, maybe_delivered: bool):
        super().__init__(f"{method}: {message}")
        self.method = method
        self.maybe_delivered = maybe_delivered


_SAFE_TO_RETRY = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


class BotApi:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org",
                 timeout: float = 30.0, client: httpx.AsyncClient | None = None):
        self._token = token
        self._base = f"{base_url}/bot{token}"
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "repost-bot/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def redact(self, text: str) -> str:
        return text.replace(self._token, "<BOT_TOKEN>")

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   timeout: float | None = None) -> Any:
        url = f"{self._base}/{method}"
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        req_timeout = httpx.Timeout(timeout or self._timeout, connect=10.0)
        try:
            resp = await self._client.post(url, json=payload, timeout=req_timeout)
        except _SAFE_TO_RETRY as exc:
            raise NetworkError(method, self.redact(f"{type(exc).__name__}: {exc}"), False) from None
        except httpx.TransportError as exc:
            raise NetworkError(method, self.redact(f"{type(exc).__name__}: {exc}"), True) from None

        try:
            data = resp.json()
        except ValueError:
            # Corpo não-JSON (ex.: 502 de proxy). Não houve processamento confiável.
            if resp.status_code >= 500:
                raise TelegramError(method, resp.status_code, f"HTTP {resp.status_code} sem corpo JSON") from None
            raise TelegramError(method, resp.status_code, "resposta inválida da API") from None

        if data.get("ok"):
            return data.get("result")

        params_resp = data.get("parameters") or {}
        code = int(data.get("error_code") or resp.status_code)
        desc = self.redact(str(data.get("description", "")))
        retry_after = params_resp.get("retry_after")
        migrate = params_resp.get("migrate_to_chat_id")
        if code == 429:
            raise RetryAfter(method, code, desc, float(retry_after or 5), migrate)
        raise TelegramError(method, code, desc,
                            float(retry_after) if retry_after is not None else None, migrate)

    # ---------------------------------------------------------------- métodos
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_chat(self, chat_id: int | str) -> dict:
        return await self.call("getChat", {"chat_id": chat_id})

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> dict:
        return await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def delete_webhook(self) -> bool:
        return await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def get_webhook_info(self) -> dict:
        return await self.call("getWebhookInfo")

    async def get_updates(self, offset: int | None, timeout: int,
                          allowed_updates: Sequence[str]) -> list[dict]:
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": 100, "allowed_updates": list(allowed_updates)},
            # O timeout HTTP precisa ser maior que o long polling.
            timeout=timeout + 15,
        )

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int,
                           message_thread_id: int | None = None) -> int:
        res = await self.call("copyMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                                              "message_id": message_id, "message_thread_id": message_thread_id})
        return int(res["message_id"])

    async def copy_messages(self, chat_id: int, from_chat_id: int, message_ids: Sequence[int],
                            message_thread_id: int | None = None) -> list[int]:
        ids = sorted(message_ids)  # a API exige ordem estritamente crescente, 1..100 IDs
        if not 1 <= len(ids) <= 100:
            raise ValueError("copyMessages aceita de 1 a 100 IDs")
        res = await self.call("copyMessages", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                                               "message_ids": ids, "message_thread_id": message_thread_id})
        return [int(m["message_id"]) for m in res]

    async def create_forum_topic(self, chat_id: int, name: str, icon_color: int | None = None,
                                 icon_custom_emoji_id: str | None = None) -> dict:
        return await self.call("createForumTopic", {"chat_id": chat_id, "name": name[:128],
                                                    "icon_color": icon_color,
                                                    "icon_custom_emoji_id": icon_custom_emoji_id})

    async def send_message(self, chat_id: int, text: str) -> dict:
        return await self.call("sendMessage", {
            "chat_id": chat_id,
            "text": text[:4096],
            "link_preview_options": {"is_disabled": True},
        })
