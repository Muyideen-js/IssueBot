from unittest.mock import Mock, patch

from ai_writer import generate_application_message, provider_order


def test_provider_order_starts_with_user_preference():
    assert provider_order("deepseek") == ["deepseek", "gemini", "openai", "groq"]


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


def test_selected_gemini_model_is_sent_to_gemini():
    succeeded = Mock(status_code=200)
    succeeded.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "I can add the requested tests."}]}}]
    }
    with patch("ai_writer.requests.post", return_value=succeeded) as post:
        message, provider = generate_application_message(
            {"title": "Add tests"},
            "owner/repo",
            {"gemini": "g-key"},
            "gemini",
            "Fallback",
            {"gemini": "gemini-custom-model"},
        )
    assert provider == "gemini"
    assert "requested tests" in message
    assert "/models/gemini-custom-model:generateContent" in post.call_args.args[0]


def test_selected_deepseek_and_openai_models_are_sent_in_payloads():
    deepseek_response = Mock(status_code=200)
    deepseek_response.json.return_value = {
        "choices": [{"message": {"content": "I can implement this safely."}}]
    }
    with patch("ai_writer.requests.post", return_value=deepseek_response) as post:
        _, provider = generate_application_message(
            {"title": "Fix bug"}, "owner/repo", {"deepseek": "d-key"},
            "deepseek", "Fallback", {"deepseek": "deepseek-custom-model"},
        )
    assert provider == "deepseek"
    assert post.call_args.kwargs["json"]["model"] == "deepseek-custom-model"

    openai_response = Mock(status_code=200)
    openai_response.json.return_value = {"output_text": "I can implement this safely."}
    with patch("ai_writer.requests.post", return_value=openai_response) as post:
        _, provider = generate_application_message(
            {"title": "Fix bug"}, "owner/repo", {"openai": "o-key"},
            "openai", "Fallback", {"openai": "openai-custom-model"},
        )
    assert provider == "openai"
    assert post.call_args.kwargs["json"]["model"] == "openai-custom-model"


def test_selected_groq_model_is_sent_to_openai_compatible_endpoint():
    response = Mock(status_code=200)
    response.json.return_value = {
        "choices": [{"message": {"content": "I can add the validation and tests."}}]
    }
    with patch("ai_writer.requests.post", return_value=response) as post:
        message, provider = generate_application_message(
            {"title": "Validate payment amounts"},
            "owner/repo",
            {"groq": "gsk-key"},
            "groq",
            "Fallback",
            {"groq": "openai/gpt-oss-120b"},
        )

    assert provider == "groq"
    assert "validation and tests" in message
    assert post.call_args.args[0] == "https://api.groq.com/openai/v1/chat/completions"
    assert post.call_args.kwargs["headers"]["authorization"] == "Bearer gsk-key"
    assert post.call_args.kwargs["json"]["model"] == "openai/gpt-oss-120b"
