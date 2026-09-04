"""Per-user AI application writing with provider failover."""

from __future__ import annotations

from collections.abc import Iterable

import requests


PROVIDERS = ("gemini", "deepseek", "openai", "groq")
DEFAULT_MODELS = {
    "gemini": "gemini-3.7-flash",
    "deepseek": "deepseek-chat",
    "openai": "gpt-5.6",
    "groq": "openai/gpt-oss-20b",
}
# A curated starting point for the settings UI; users can type any other model id.
SUGGESTED_MODELS = {
    "gemini": ["gemini-3.7-flash", "gemini-3.7-pro", "gemini-3-flash-lite"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openai": ["gpt-5.6", "gpt-5.6-mini", "gpt-5.6-nano"],
    "groq": ["openai/gpt-oss-20b", "openai/gpt-oss-120b"],
}


def resolve_model(provider: str, requested: str | None) -> str:
    requested = (requested or "").strip()
    return requested or DEFAULT_MODELS.get(provider, "")


class AIWriterError(RuntimeError):
    pass


def provider_order(preferred: str) -> list[str]:
    normalized = preferred if preferred in PROVIDERS else "gemini"
    return [normalized, *(provider for provider in PROVIDERS if provider != normalized)]


def generate_application_message(
    issue: dict,
    repo: str,
    provider_keys: dict[str, str],
    preferred: str,
    fallback_message: str,
    provider_models: dict[str, str] | None = None,
    timeout: int = 25,
) -> tuple[str, str]:
    """Return an application message and the provider that produced it."""
    prompt = _prompt(issue, repo)
    errors = []
    for provider in provider_order(preferred):
        api_key = (provider_keys.get(provider) or "").strip()
        if not api_key:
            continue
        model = resolve_model(provider, (provider_models or {}).get(provider))
        try:
            message = _generate(provider, api_key, model, prompt, timeout)
            return _clean_message(message), provider
        except Exception as exc:
            errors.append(f"{provider}: {exc}")

    fallback = _clean_message(fallback_message or "Hi, I can fix this")
    return fallback, "fallback"


def test_provider(provider: str, api_key: str, model: str = "", timeout: int = 20) -> None:
    if provider not in PROVIDERS:
        raise AIWriterError("Unknown AI provider")
    if not api_key.strip():
        raise AIWriterError("Enter an API key first")
    _generate(
        provider,
        api_key.strip(),
        resolve_model(provider, model),
        "Reply with exactly: Connection successful",
        timeout,
    )


def _generate(provider: str, api_key: str, model: str, prompt: str, timeout: int) -> str:
    if provider == "gemini":
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": api_key, "content-type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.35, "maxOutputTokens": 140},
            },
            timeout=timeout,
        )
        _raise_for_provider(response, "Gemini")
        data = response.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(str(part.get("text") or "") for part in parts)

    if provider == "deepseek":
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.35,
                "max_tokens": 140,
            },
            timeout=timeout,
        )
        _raise_for_provider(response, "DeepSeek")
        return str(response.json().get("choices", [{}])[0].get("message", {}).get("content") or "")

    if provider == "openai":
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": model,
                "input": prompt,
                "max_output_tokens": 140,
                "store": False,
            },
            timeout=timeout,
        )
        _raise_for_provider(response, "OpenAI")
        data = response.json()
        if data.get("output_text"):
            return str(data["output_text"])
        return "".join(_openai_text_parts(data.get("output") or []))

    if provider == "groq":
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.35,
                "max_tokens": 140,
            },
            timeout=timeout,
        )
        _raise_for_provider(response, "Groq")
        return str(response.json().get("choices", [{}])[0].get("message", {}).get("content") or "")

    raise AIWriterError("Unknown AI provider")


def _openai_text_parts(output: Iterable[dict]) -> Iterable[str]:
    for item in output:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                yield str(content["text"])


def _raise_for_provider(response, provider: str) -> None:
    if 200 <= response.status_code < 300:
        return
    try:
        payload = response.json()
        detail = payload.get("error") or payload.get("message") or response.text
    except Exception:
        detail = response.text
    raise AIWriterError(f"{provider} returned HTTP {response.status_code}: {str(detail)[:180]}")


def _prompt(issue: dict, repo: str) -> str:
    title = str(issue.get("title") or "Untitled issue")[:500]
    body = str(issue.get("body") or issue.get("description") or "")[:4000]
    return (
        "Write a natural one- or two-sentence application to work on this GitHub issue. "
        "Mention one concrete implementation or testing detail from the issue when possible. "
        "Do not use markdown, greetings to a named person, exaggerated claims, or say that work "
        "is already complete. Keep it under 500 characters.\n\n"
        f"Repository: {repo or 'Unknown'}\nTitle: {title}\nDescription: {body}"
    )


def _clean_message(message: str) -> str:
    cleaned = " ".join(str(message or "").strip().strip('"').split())
    if not cleaned:
        raise AIWriterError("The provider returned an empty application message")
    return cleaned[:500]
