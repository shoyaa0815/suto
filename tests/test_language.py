import pytest

from core.language import choose_reply_language, detect_language_code


def test_explicit_language_request_has_highest_priority():
    choice = choose_reply_language("ตอบเป็นภาษาอังกฤษ สรุปไฟล์นี้")

    assert choice.code == "en"
    assert choice.source == "explicit"


def test_direct_thai_language_instruction_is_explicit():
    choice = choose_reply_language("ช่วยตอบภาษาอังกฤษ")

    assert choice.code == "en"
    assert choice.source == "explicit"


def test_explicit_language_survives_english_attachment_metadata():
    choice = choose_reply_language(
        "สรุปไฟล์นี้เป็นภาษาไทยแบบสั้น\n\n"
        "Attached files available:\n"
        "- attachment_id=1, filename=report.pdf"
    )

    assert choice.code == "th"
    assert choice.source == "explicit"


@pytest.mark.parametrize(
    ("prompt", "expected_code"),
    [
        ("สรุปเอกสารนี้ให้หน่อย", "th"),
        ("Please summarize this document", "en"),
        ("Résumez ce document", "fr"),
        ("この文書を要約してください", "ja"),
    ],
)
def test_detects_language_from_original_prompt(prompt, expected_code):
    choice = choose_reply_language(prompt)

    assert choice.code == expected_code
    assert choice.source == "detected"


def test_ambiguous_prompt_uses_previous_language():
    choice = choose_reply_language("OK", previous_code="en")

    assert choice.code == "en"
    assert choice.source == "previous"


def test_undetectable_prompt_defaults_to_thai():
    choice = choose_reply_language("👍")

    assert choice.code == "th"
    assert choice.source == "default"


def test_output_detection_does_not_apply_fallback():
    assert detect_language_code("This answer is in English.") == "en"
    assert detect_language_code("คำตอบนี้เป็นภาษาไทย") == "th"
    assert detect_language_code("👍") is None
