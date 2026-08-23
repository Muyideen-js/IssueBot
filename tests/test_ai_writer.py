from unittest.mock import Mock, patch

from ai_writer import generate_application_message, provider_order


def test_provider_order_starts_with_user_preference():
    assert provider_order("deepseek") == ["deepseek", "gemini", "openai"]


def test_no_connected_provider_uses_custom_fallback():
    message, provider = generate_application_message(
        {"title": "Fix retries"},
        "owner/repo",
        {},
        "gemini",
        "Please assign this issue to me.",
    )
    assert message == "Please assign this issue to me."
    assert provider == "fallback"


def test_provider_failure_falls_through_to_next_connected_provider():
    failed = Mock(status_code=429, text="quota")
    failed.json.return_value = {"error": "quota"}
    succeeded = Mock(status_code=200)
    succeeded.json.return_value = {
        "choices": [{"message": {"content": "I can implement the retry guard and tests."}}]
    }
    with patch("ai_writer.requests.post", side_effect=[failed, succeeded]):
        message, provider = generate_application_message(
            {"title": "Fix retries", "body": "Add a bounded retry guard."},
            "owner/repo",
            {"gemini": "g-key", "deepseek": "d-key"},
            "gemini",
            "Fallback",
        )
    assert provider == "deepseek"
    assert "retry guard" in message
