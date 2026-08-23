from automation import _add_priority_repo, _is_priority_repo, _priority_names, _rank_issues


def test_priority_owner_matches_every_repository_owned_by_it():
    priorities = _priority_names("Fluxora-Org\nother/specific")
    assert _is_priority_repo("Fluxora-Org/contracts", priorities)
    assert _is_priority_repo("other/specific", priorities)
    assert not _is_priority_repo("other/unlisted", priorities)


def test_priority_issues_are_ranked_before_normal_issues():
    issues = [
        {"id": "normal", "repository": {"fullName": "normal/repo"}},
        {"id": "priority", "repository": {"fullName": "Fluxora-Org/contracts"}},
    ]
    ranked = _rank_issues(issues, {"fluxora-org"})
    assert ranked[0]["id"] == "priority"


def test_newest_issue_wins_within_same_priority_tier():
    issues = [
        {"id": "old", "updatedAt": "2026-08-20T10:00:00Z", "repository": {"fullName": "priority/one"}},
        {"id": "new", "updatedAt": "2026-08-23T10:00:00Z", "repository": {"fullName": "priority/two"}},
    ]
    ranked = _rank_issues(issues, {"priority"})
    assert [issue["id"] for issue in ranked] == ["new", "old"]


def test_accepted_repository_can_be_promoted_to_priority_list():
    settings = type("Settings", (), {"priority_repos": "Fluxora-Org"})()
    assert _add_priority_repo(settings, "new-owner/new-repo") is True
    assert "new-owner/new-repo" in settings.priority_repos
    assert _add_priority_repo(settings, "new-owner/new-repo") is False
