from repost_bot.classify import classify


def test_photo_uses_largest_size():
    c = classify({"photo": [{"file_unique_id": "small"}, {"file_unique_id": "big"}], "caption": "x"})
    assert c.kind == "photo" and c.copyable and c.file_unique_id == "big"


def test_animation_wins_over_document():
    c = classify({"animation": {"file_unique_id": "a"}, "document": {"file_unique_id": "d"}})
    assert c.kind == "animation" and c.file_unique_id == "a"


def test_text():
    assert classify({"text": "oi"}).kind == "text"


def test_service_message_not_copyable():
    c = classify({"pinned_message": {"message_id": 1}})
    assert not c.copyable and c.kind.startswith("service:")


def test_paid_media_not_copyable():
    assert not classify({"paid_media": {}}).copyable


def test_quiz_without_answer_not_copyable():
    assert not classify({"poll": {"type": "quiz", "options": []}}).copyable
    assert classify({"poll": {"type": "quiz", "correct_option_id": 0}}).copyable
    assert classify({"poll": {"type": "regular"}}).copyable


def test_unknown_type_is_attempted():
    c = classify({"some_future_type": {}})
    assert c.copyable and c.kind == "other"
