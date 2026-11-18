import pytest
from conftest import SRC

from repost_bot.config import Config, ConfigError, coerce, save_settings, save_token, seed_from_env


def test_defaults_when_empty(db):
    cfg = Config.load(db)
    assert not cfg.ready
    assert cfg.missing() == ["token do bot", "chat de origem", "chat de destino"]
    assert cfg.dest_messages_per_minute == 20


def test_save_and_load_roundtrip(db):
    save_token(db, "123:abc")
    cfg = save_settings(db, {"source_chat_id": SRC, "dest_chat_id": "-1009", "gap_recovery": "false",
                             "album_settle_seconds": "2.5", "uncertain_policy": "resend"})
    assert cfg.ready and cfg.dest_chat_id == -1009 and cfg.gap_recovery is False
    assert cfg.album_settle_seconds == 2.5 and cfg.uncertain_policy == "resend"


def test_clear_field_with_null(db):
    save_settings(db, {"admin_user_id": 5})
    assert Config.load(db).admin_user_id == 5
    save_settings(db, {"admin_user_id": None})
    assert Config.load(db).admin_user_id is None


@pytest.mark.parametrize("name,value", [
    ("dest_messages_per_minute", 61), ("dest_messages_per_minute", "abc"), ("source_chat_id", 5),
    ("admin_user_id", -3), ("uncertain_policy", "x"), ("gap_recovery", "talvez"), ("bot_token", "semdoispontos"),
])
def test_invalid_values(name, value):
    with pytest.raises(ConfigError):
        coerce(name, value)


def test_same_source_and_dest_rejected(db):
    save_settings(db, {"source_chat_id": SRC})
    with pytest.raises(ConfigError):
        save_settings(db, {"dest_chat_id": SRC})


def test_non_editable_rejected(db):
    with pytest.raises(ConfigError):
        save_settings(db, {"bot_token": "1:x"})


def test_seed_from_env_only_fills_missing(db, monkeypatch):
    save_settings(db, {"source_chat_id": SRC})
    monkeypatch.setenv("SOURCE_CHAT_ID", "-1005")
    monkeypatch.setenv("BOT_TOKEN", "9:zz")
    imported = seed_from_env(db)
    cfg = Config.load(db)
    assert cfg.source_chat_id == SRC and cfg.bot_token == "9:zz"
    assert imported == ["BOT_TOKEN"]
