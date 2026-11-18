"""Classificação de mensagens: tipo de conteúdo, mídia e se pode ser copiada.

Segundo a documentação do copyMessage, não podem ser copiadas: mensagens de
serviço, paid media, giveaway, giveaway winners e invoice. Enquetes do tipo
quiz só podem ser copiadas se o bot conhecer a(s) resposta(s) correta(s).
"""
from __future__ import annotations

from dataclasses import dataclass

# A ordem importa: 'animation' também vem acompanhado de 'document'.
MEDIA_KINDS = ("animation", "photo", "video", "video_note", "voice", "audio", "document", "sticker")
OTHER_KINDS = ("text", "poll", "location", "venue", "contact", "dice", "story", "checklist", "game")
NON_COPYABLE = ("paid_media", "giveaway", "giveaway_winners", "invoice")

SERVICE_KEYS = (
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created",
    "message_auto_delete_timer_changed", "migrate_to_chat_id", "migrate_from_chat_id",
    "pinned_message", "successful_payment", "refunded_payment", "users_shared", "chat_shared",
    "connected_website", "write_access_allowed", "passport_data", "proximity_alert_triggered",
    "boost_added", "chat_background_set", "forum_topic_created", "forum_topic_edited",
    "forum_topic_closed", "forum_topic_reopened", "general_forum_topic_hidden",
    "general_forum_topic_unhidden", "giveaway_created", "giveaway_completed",
    "video_chat_scheduled", "video_chat_started", "video_chat_ended",
    "video_chat_participants_invited", "web_app_data", "gift", "unique_gift",
    "paid_message_price_changed", "direct_message_price_changed", "checklist_tasks_done",
    "checklist_tasks_added",
)


@dataclass(frozen=True)
class Classified:
    kind: str
    copyable: bool
    file_unique_id: str | None = None
    reason: str | None = None


def classify(msg: dict) -> Classified:
    for key in NON_COPYABLE:
        if key in msg:
            return Classified(key, False, reason=f"tipo '{key}' não pode ser copiado pela Bot API")

    for key in MEDIA_KINDS:
        if key in msg:
            media = msg[key]
            if key == "photo" and isinstance(media, list) and media:
                media = media[-1]  # maior resolução
            fuid = media.get("file_unique_id") if isinstance(media, dict) else None
            return Classified(key, True, fuid)

    for key in OTHER_KINDS:
        if key in msg:
            if key == "poll":
                poll = msg["poll"]
                if poll.get("type") == "quiz" and "correct_option_id" not in poll \
                        and "correct_option_ids" not in poll:
                    return Classified("poll", False,
                                      reason="quiz sem resposta correta conhecida pelo bot (não copiável)")
            return Classified(key, True)

    for key in SERVICE_KEYS:
        if key in msg:
            return Classified(f"service:{key}", False, reason="mensagem de serviço (não copiável)")

    # Tipo novo/desconhecido: tentamos copiar; a API recusa se não for permitido.
    return Classified("other", True)
