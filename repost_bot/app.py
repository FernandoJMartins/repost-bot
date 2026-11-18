"""Ponto de entrada: sobe a página web e o motor do bot no mesmo processo."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiohttp import web
from dotenv import load_dotenv

from .config import Bootstrap, ConfigError, seed_from_env
from .db import Database
from .engine import Engine
from .logging_setup import setup_logging
from .web import create_app

log = logging.getLogger("repost_bot")


async def main_async(boot: Bootstrap, redact, mem) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C gera KeyboardInterrupt

    db = Database(boot.db_path)
    imported = seed_from_env(db)
    if imported:
        log.info("Configuração importada de variáveis de ambiente: %s", ", ".join(imported))

    engine = Engine(db, boot, redact)
    app = create_app(engine, db, mem)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, boot.web_host, boot.web_port)
    try:
        await site.start()
    except OSError as e:
        log.critical("Não foi possível abrir a página web em %s:%d: %s", boot.web_host, boot.web_port, e)
        await runner.cleanup()
        db.close()
        return 2
    shown_host = "localhost" if boot.web_host in ("127.0.0.1", "0.0.0.0", "::") else boot.web_host
    log.info("Página web disponível em http://%s:%d", shown_host, boot.web_port)
    if boot.web_host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("A página web está escutando em %s. Não exponha esta porta diretamente na "
                    "internet; use VPN, túnel SSH ou proxy com HTTPS.", boot.web_host)

    try:
        await engine.start()
        await stop.wait()
    finally:
        log.info("Encerrando...")
        await engine.shutdown()
        await runner.cleanup()
        db.close()
        log.info("Bot encerrado")
    return 0


def main() -> int:
    load_dotenv()
    try:
        boot = Bootstrap.from_env()
    except ConfigError as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        return 2
    redact, mem = setup_logging(boot.log_level, boot.log_file)
    log.info("Iniciando repost-bot (banco: %s)", boot.db_path)
    try:
        return asyncio.run(main_async(boot, redact, mem))
    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário")
        return 0
