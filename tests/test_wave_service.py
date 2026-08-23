from unittest.mock import Mock

from wave_service import WAVE_PROGRAM_ID, WaveClient


def test_withdraw_uses_confirmed_application_and_issue_ids():
    client = WaveClient({"cookies": []})
    client.ensure_token = Mock(return_value=("token", False))
    response = Mock(status_code=204)
    client.http.delete = Mock(return_value=response)

    ok, message, changed = client.withdraw({
        "id": "application-123",
        "issue": {"id": "issue-456"},
    })

    assert ok is True
    assert message == "Application withdrawn"
    assert changed is False
    url = client.http.delete.call_args.args[0]
    assert url.endswith(
        f"/wave-programs/{WAVE_PROGRAM_ID}/issues/issue-456/applications/application-123"
    )


def test_withdraw_fails_closed_without_required_ids():
    client = WaveClient({"cookies": []})
    client.ensure_token = Mock(return_value=("token", False))
    ok, message, _changed = client.withdraw({"issue": {"id": "issue-only"}})
    assert ok is False
    assert "missing" in message.lower()
