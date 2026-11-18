import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repost_bot.config import Config  # noqa: E402
from repost_bot.db import Database  # noqa: E402
from repost_bot.telegram_api import NetworkError, RetryAfter, TelegramError  # noqa: E402

SRC = -1001111111111
DST = -1002222222222
ADMIN = 4242


def make_cfg(tmp_path, **kw) -> Config:
    base = dict(bot_token="123:ABC", source_chat_id=SRC, dest_chat_id=DST, admin_user_id=ADMIN,
                db_path=tmp_path / "t.db", album_settle_seconds=0, gap_settle_seconds=0)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path)
    yield d
    d.close()


class FakeLimiter:
    def __init__(self):
        self.acquired = []
        self.penalties = []

    async def acquire(self, weight=1):
        self.acquired.append(weight)

    def penalize(self, s):
        self.penalties.append(s)


class FakeApi:
    """Simula a Bot API. `script` é uma lista de respostas/exceções consumidas em ordem."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self.sent = []
        self.threads = []
        self.topics_created = []
        self._next_id = 1000

    async def create_forum_topic(self, chat_id, name, icon_color=None, icon_custom_emoji_id=None):
        self.topics_created.append((name, icon_color, icon_custom_emoji_id))
        tid = 500 + len(self.topics_created)
        return {"message_thread_id": tid, "name": name, "icon_color": icon_color or 7322096}

    def _next(self, default):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return default

    async def copy_message(self, chat_id, from_chat_id, message_id, message_thread_id=None):
        self.calls.append(("copyMessage", [message_id]))
        self.threads.append(message_thread_id)
        self._next_id += 1
        return self._next(self._next_id)

    async def copy_messages(self, chat_id, from_chat_id, message_ids, message_thread_id=None):
        self.calls.append(("copyMessages", list(message_ids)))
        self.threads.append(message_thread_id)
        default = list(range(self._next_id + 1, self._next_id + 1 + len(message_ids)))
        self._next_id += len(message_ids)
        return self._next(default)

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {}


__all__ = ["SRC", "DST", "ADMIN", "make_cfg", "FakeApi", "FakeLimiter",
           "NetworkError", "RetryAfter", "TelegramError"]
