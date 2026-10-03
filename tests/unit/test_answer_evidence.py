from observability.dashboard.services.answer_evidence import split_answer_evidence


def test_old_inline_evidence_is_separated_and_environment_note_preserved():
    message = (
        "结论\n修改配置 [1]\n\n引用依据\n[1] manual.pdf\n"
        "原文：Scan Slot. [IMAGE: hash_1_1]\n\n以上基于文档，尚未核验你的实际环境。"
    )
    body, evidence = split_answer_evidence(message)
    assert "manual.pdf" not in body and "引用依据" not in body
    assert "修改配置 [1]" in body and "尚未核验" in body
    assert "manual.pdf" in evidence and "Scan Slot." in evidence
    assert "IMAGE:" not in evidence


def test_plain_answer_and_english_evidence_are_handled():
    assert split_answer_evidence("Normal answer [1]") == ("Normal answer [1]", "")
    body, evidence = split_answer_evidence(
        "Conclusion\nResult [1]\n\nSupporting evidence\n[1] source.pdf\nQuote: support\n\n"
        "Based on documents; your actual environment is not verified."
    )
    assert "source.pdf" not in body and "not verified" in body
    assert "source.pdf" in evidence
