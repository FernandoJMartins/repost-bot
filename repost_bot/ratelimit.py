"""Limitador de envio para o chat de destino.

Limites oficiais (core.telegram.org/bots/faq):
- evitar mais de 1 mensagem por segundo no mesmo chat;
- em grupos, no máximo 20 mensagens por minuto;
- ~30 mensagens por segundo no total do bot.

O bot envia para um único chat, então o limite efetivo é o por chat. Cada
mensagem de um álbum conta como uma mensagem enviada. Em caso de HTTP 429 o
limitador é bloqueado pelo tempo indicado em retry_after.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, messages_per_minute: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        # Nunca mais rápido que 1 msg/s, mesmo se configurado acima de 60/min.
        self.interval = max(60.0 / messages_per_minute, 1.0)
        self._clock = clock
        self._sleep = sleep
        self._next_free = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> None:
        """Espera até poder enviar `weight` mensagens e reserva o tempo correspondente."""
        async with self._lock:
            now = self._clock()
            wait = self._next_free - now
            if wait > 0:
                log.debug("Limitador: aguardando %.1fs antes do próximo envio", wait)
                await self._sleep(wait)
                now = self._clock()
            self._next_free = max(now, self._next_free) + self.interval * max(weight, 1)

    def penalize(self, retry_after: float) -> None:
        """Respeita o retry_after do HTTP 429 (com 1s de margem)."""
        self._next_free = max(self._next_free, self._clock() + retry_after + 1.0)
