from unittest.mock import Mock

from wave_service import WAVE_API, WAVE_PROGRAM_ID, WaveClient, WaveError, issue_repo, issue_url


def test_withdraw_uses_confirmed_application_and_issue_ids():
    client = WaveClient({"cookies": []})
    client.ensure_token = Mock(return_value=("token", False))
    response = Mock(status_code=204)
    client.http.post = Mock(return_value=response)

    ok, message, changed = client.withdraw({
        "id": "application-123",
        "issue": {"id": "issue-456"},
    })

    assert ok is True
    assert message == "Application withdrawn"
    assert changed is False
    url = client.http.post.call_args.args[0]
    assert url.endswith(
        f"/wave-programs/{WAVE_PROGRAM_ID}/issues/issue-456/applications/application-123/withdraw"
    )


def test_withdraw_fails_closed_without_required_ids():
    client = WaveClient({"cookies": []})
    client.ensure_token = Mock(return_value=("token", False))
    ok, message, _changed = client.withdraw({"issue": {"id": "issue-only"}})
    assert ok is False
    assert "missing" in message.lower()


def test_refresh_uses_current_cookie_endpoint_and_saves_rotated_cookie():
    state = {"cookies": [
        {"name": "wave_access_token", "value": "expired", "domain": ".drips.network"},
        {"name": "wave_refresh_token", "value": "old-refresh", "domain": ".drips.network"},
    ]}
    client = WaveClient(state)
    response = Mock(status_code=200)
    response.json.return_value = {"accessToken": "new-access"}
    response.cookies = []
    client.http.post = Mock(return_value=response)

    token, changed = client.ensure_token()

    assert token == "new-access"
    assert changed is True
    url = client.http.post.call_args.args[0]
    assert url == f"{WAVE_API}/auth/token/refresh"
    assert client.http.post.call_args.kwargs.get("json") is None
    assert "wave_refresh_token=old-refresh" in client.http.post.call_args.kwargs["headers"]["cookie"]


def test_refresh_does_not_send_the_expired_access_token_as_auth():
    state = {"cookies": [
        {"name": "wave_access_token", "value": "expired", "domain": ".drips.network"},
        {"name": "wave_refresh_token", "value": "old-refresh", "domain": ".drips.network"},
    ]}
    client = WaveClient(state)
    response = Mock(status_code=200)
    response.json.return_value = {"accessToken": "new-access"}
    response.cookies = []
    client.http.post = Mock(return_value=response)

    client.ensure_token()

    headers = client.http.post.call_args.kwargs["headers"]
    assert "authorization" not in headers


def test_refresh_captures_a_rotated_refresh_token_from_the_response_body():
    state = {"cookies": [
        {"name": "wave_access_token", "value": "expired", "domain": ".drips.network"},
        {"name": "wave_refresh_token", "value": "old-refresh", "domain": ".drips.network"},
    ]}
    client = WaveClient(state)
    response = Mock(status_code=200)
    response.json.return_value = {"accessToken": "new-access", "refreshToken": "new-refresh"}
    response.cookies = []
    client.http.post = Mock(return_value=response)

    client.ensure_token()

    assert client._cookie("wave_refresh_token") == "new-refresh"


def test_refresh_failure_message_includes_status_and_body_detail():
    state = {"cookies": [
        {"name": "wave_access_token", "value": "expired", "domain": ".drips.network"},
        {"name": "wave_refresh_token", "value": "old-refresh", "domain": ".drips.network"},
    ]}
    client = WaveClient(state)
    response = Mock(status_code=401)
    response.json.return_value = {"error": "invalid_grant"}
    client.http.post = Mock(return_value=response)

    try:
        client.ensure_token()
        assert False, "expected WaveError"
    except WaveError as exc:
        assert "401" in str(exc)
        assert "invalid_grant" in str(exc)


def test_fetch_applications_uses_quota_details_and_filters_status():
    client = WaveClient({"cookies": []})
    client.ensure_token = Mock(return_value=("token", False))
    response = Mock(status_code=200)
    response.json.return_value = {
        "applications": [
            {"id": "pending-app", "status": "pending", "issue": {"id": "one"}},
            {"id": "accepted-app", "status": "accepted", "issue": {"id": "two"}},
        ]
    }
    client.http.get = Mock(return_value=response)

    applications, changed = client.fetch_applications("pending")

    assert [app["id"] for app in applications] == ["pending-app"]
    assert changed is False
    assert client.http.get.call_args.args[0].endswith(
        f"/wave-programs/{WAVE_PROGRAM_ID}/quotas/applications/details"
    )


def test_quota_issue_repo_and_url_are_understood():
    issue = {
        "gitHubIssueNumber": 42,
        "repo": {"gitHubRepoFullName": "example/project"},
    }
    assert issue_repo(issue) == "example/project"
    assert issue_url(issue) == "https://github.com/example/project/issues/42"
