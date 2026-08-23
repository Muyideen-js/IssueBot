import base64
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests


WAVE_API = "https://wave-api.drips.network/api"
WAVE_PROGRAM_ID = "fdc01c95-806f-4b6a-998b-a6ed37e0d81b"


class WaveError(RuntimeError):
    pass


class WaveClient:
    def __init__(self, session_state: dict[str, Any], timeout: int = 20):
        self.session_state = session_state
        self.timeout = timeout
        self.http = requests.Session()

    def _cookie(self, name: str) -> str:
        for cookie in self.session_state.get("cookies", []):
            if cookie.get("name") == name:
                return cookie.get("value", "")
        return ""

    def _set_cookie(self, name: str, value: str, expires: float) -> None:
        for cookie in self.session_state.setdefault("cookies", []):
            if cookie.get("name") == name:
                cookie.update(value=value, expires=expires)
                return
        self.session_state["cookies"].append({
            "name": name,
            "value": value,
            "domain": ".drips.network",
            "path": "/",
            "expires": expires,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        })

    @staticmethod
    def _token_is_valid(token: str) -> bool:
        try:
            payload_part = token.split(".")[1]
            padding = "=" * (-len(payload_part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_part + padding))
            return float(payload.get("exp", 0)) - datetime.now(timezone.utc).timestamp() > 60
        except Exception:
            return False

    def ensure_token(self) -> tuple[str, bool]:
        access = self._cookie("wave_access_token")
        if self._token_is_valid(access):
            return access, False

        refresh = self._cookie("wave_refresh_token")
        if not refresh:
            raise WaveError("Drips session has expired; reconnect the account")

        response = self.http.post(
            f"{WAVE_API}/auth/refresh",
            json={"refreshToken": refresh},
            headers={"content-type": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise WaveError("Drips session refresh failed; reconnect the account")

        data = response.json()
        access = data.get("accessToken") or data.get("access_token") or data.get("token") or ""
        if not access:
            raise WaveError("Drips returned no access token")
        self._set_cookie("wave_access_token", access, datetime.now(timezone.utc).timestamp() + 900)
        return access, True

    def _headers(self, token: str) -> dict[str, str]:
        cookies = "; ".join(
            f"{cookie.get('name')}={cookie.get('value')}"
            for cookie in self.session_state.get("cookies", [])
            if cookie.get("name") and cookie.get("value")
        )
        return {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "cookie": cookies,
            "referer": "https://www.drips.network/",
            "user-agent": "IssueBot/1.0",
            "x-timezone": "Africa/Lagos",
        }

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            value = data.get("data") or data.get("applications") or []
            return value if isinstance(value, list) else []
        return []

    def fetch_applications(self, status: str) -> tuple[list[dict[str, Any]], bool]:
        token, changed = self.ensure_token()
        urls = [f"{WAVE_API}/user/applications?waveProgramId={WAVE_PROGRAM_ID}&status={status}&limit=50"]
        errors = []
        for url in urls:
            try:
                response = self.http.get(url, headers=self._headers(token), timeout=self.timeout)
                if response.status_code == 200:
                    return self._items(response.json()), changed
                errors.append(str(response.status_code))
            except requests.RequestException as exc:
                errors.append(type(exc).__name__)
        raise WaveError(f"Could not verify {status} applications ({', '.join(errors)})")

    def fetch_open_issues(self) -> tuple[list[dict[str, Any]], bool]:
        token, changed = self.ensure_token()
        response = self.http.get(
            f"{WAVE_API}/issues?limit=100&offset=0&waveProgramId={WAVE_PROGRAM_ID}&state=open&sortBy=updatedAt",
            headers=self._headers(token),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise WaveError(f"Could not load Wave issues (HTTP {response.status_code})")
        issues = self._items(response.json())
        available = [issue for issue in issues if not issue.get("acceptedApplicationsCount")]
        return available, changed

    def apply(self, issue_id: str, application_text: str) -> tuple[bool, str, bool]:
        token, changed = self.ensure_token()
        response = self.http.post(
            f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{issue_id}/applications",
            headers=self._headers(token),
            json={"applicationText": application_text},
            timeout=self.timeout,
        )
        if response.status_code == 201:
            return True, "Application submitted", changed
        try:
            payload = response.json()
            error = payload.get("error") or payload.get("message") or response.text
        except Exception:
            error = response.text
        return False, f"Drips rejected the application: {str(error)[:180]}", changed


def issue_repo(issue: dict[str, Any]) -> str:
    repo = issue.get("repo") or issue.get("repository") or ""
    if isinstance(repo, dict):
        return repo.get("fullName") or repo.get("full_name") or ""
    for field in ("gitHubIssueUrl", "htmlUrl", "html_url"):
        url = issue.get(field) or ""
        if "github.com/" in url:
            parts = url.split("github.com/", 1)[1].split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
    return str(repo)


def issue_url(issue: dict[str, Any]) -> str:
    raw = issue.get("gitHubIssueUrl") or issue.get("htmlUrl") or issue.get("html_url") or ""
    try:
        parsed = urlparse(str(raw))
        if parsed.scheme == "https" and parsed.hostname in {"github.com", "www.drips.network", "drips.network"}:
            return str(raw)
    except ValueError:
        pass
    return ""


def issue_points(issue: dict[str, Any]) -> int | None:
    raw = issue.get("points") or issue.get("pointValue")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
