import pytest

from agents.language import language_instruction, response_language, validate_language
from agents.replies import workflow_reply


@pytest.mark.parametrize(
    "message,previous,expected",
    [
        ("如何配置 trolley slot pick？", "", "zh"),
        ("How do I configure trolley picking?", "zh", "en"),
        ("请用英文回答，如何配置收货？", "", "en"),
        ("Please reply in Chinese: how is receiving configured?", "", "zh"),
        ("请用英文回答，改为用中文回答", "", "zh"),
        ("2024.1", "zh", "zh"),
        ("DC01", "zh", "zh"),
        ("test", "zh", "zh"),
        ("2024.1 DC01 test", "zh", "zh"),
        ("测试环境", "en", "zh"),
        ("LPN Tracked", "zh", "zh"),
        ("How is LPN Tracked configured?", "", "en"),
        ("What is MOCA?", "zh", "en"),
        ("Can you help?", "zh", "en"),
    ],
)
def test_language_follows_user_not_technical_identifiers(message, previous, expected):
    assert response_language(message, previous) == expected


def test_language_contract_preserves_quotes_and_rejects_mismatch():
    assert "exact evidence quotes unchanged" in language_instruction("zh")
    validate_language("将 `LPN Tracked` 设为 No。", "zh")
    validate_language("Set `LPN Tracked` to No.", "en")
    validate_language("`Track Slot ID`", "zh")
    with pytest.raises(ValueError):
        validate_language("Set LPN Tracked to No.", "zh")
    with pytest.raises(ValueError):
        validate_language("请确认版本。", "en")


@pytest.mark.parametrize("language,marker", [("zh", "本轮处理已暂停"), ("en", "This turn paused")])
def test_failure_reply_never_replays_stale_clarification(language, marker):
    text, kind = workflow_reply(
        {
            "response_language": language,
            "status": "paused",
            "pause_reason": "turn_timeout",
            "open_questions": [{"text": "stale question"}],
        }
    )
    assert marker in text and "stale question" not in text
    assert kind == "workflow_paused"


def test_empty_clarification_has_visible_safe_reply():
    text, kind = workflow_reply({"pause_reason": "requirements_missing", "open_questions": []})
    assert text and kind == "workflow_paused"
