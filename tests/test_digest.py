from datetime import UTC, datetime

import httpx
import pytest
import respx
from asyncgh import API_BASE
from repokit import Repo

from digest import build_parser, fetch_activity, render_html

# open PRs cover the last 14 days, closed PRs the last 7 -- both windows
# meet at UNTIL (2026-07-24).
SINCE_OPEN = datetime(2026, 7, 10, tzinfo=UTC)
SINCE_CLOSED = datetime(2026, 7, 17, tzinfo=UTC)
SINCE_RELEASE = datetime(2026, 7, 17, tzinfo=UTC)
UNTIL = datetime(2026, 7, 24, tzinfo=UTC)

REPO_A = Repo(name="repo-a", default_branch="main", is_private=False, is_fork=False)
REPO_B = Repo(name="repo-b", default_branch="main", is_private=False, is_fork=False)


@pytest.fixture(autouse=True)
def fake_auth_token(monkeypatch):
    monkeypatch.setattr("asyncgh.client._auth_token", lambda: "fake-token")


# ---------------------------------------------------------------------------
# fetch_activity
#
# fetch_activity fetches PRs, issues, and releases for every repo in one
# GraphQL query per batch of up to _BATCH_SIZE repos (aliased r0, r1, ...),
# filtering each connection's nodes by `since_fetch` (the caller passes the
# oldest of the open/closed/release windows -- render_html does the actual
# per-section windowing on top).
# ---------------------------------------------------------------------------


def _gql_pr(
    number=1,
    title="Add feature",
    created_at="2026-07-20T10:00:00Z",
    updated_at=None,
    closed_at=None,
    state="OPEN",
    login="octocat",
    mergeable="MERGEABLE",
    rollup_state="SUCCESS",
):
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/hugoh/repo-a/pull/{number}",
        "state": state,
        "createdAt": created_at,
        "updatedAt": updated_at or closed_at or created_at,
        "closedAt": closed_at,
        "author": {"login": login},
        "mergeable": mergeable,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": (
                            {"state": rollup_state} if rollup_state else None
                        )
                    }
                }
            ]
        },
    }


def _gql_issue(
    number=1,
    title="Something broke",
    created_at="2026-07-20T10:00:00Z",
    updated_at=None,
    closed_at=None,
    state="OPEN",
    login="octocat",
):
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/hugoh/repo-a/issues/{number}",
        "state": state,
        "createdAt": created_at,
        "updatedAt": updated_at or closed_at or created_at,
        "closedAt": closed_at,
        "author": {"login": login},
    }


def _gql_release(
    tag_name="v1.0.0",
    name="Version 1.0.0",
    published_at="2026-07-20T10:00:00Z",
    created_at=None,
    is_draft=False,
    is_prerelease=False,
):
    return {
        "tagName": tag_name,
        "name": name,
        "url": f"https://github.com/hugoh/repo-a/releases/tag/{tag_name}",
        "publishedAt": published_at,
        "createdAt": created_at or published_at,
        "isDraft": is_draft,
        "isPrerelease": is_prerelease,
    }


def _repo_data(
    prs=(),
    issues=(),
    releases=(),
    pr_has_next=False,
    issue_has_next=False,
    release_has_next=False,
):
    return {
        "pullRequests": {
            "pageInfo": {"hasNextPage": pr_has_next},
            "nodes": list(prs),
        },
        "issues": {
            "pageInfo": {"hasNextPage": issue_has_next},
            "nodes": list(issues),
        },
        "releases": {
            "pageInfo": {"hasNextPage": release_has_next},
            "nodes": list(releases),
        },
    }


def _mock_graphql(httpx2_mock: respx.Router, *repo_data: dict) -> respx.Route:
    return httpx2_mock.post(f"{API_BASE}/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {f"r{i}": data for i, data in enumerate(repo_data)}},
        )
    )


async def test_fetch_activity_normalizes_pr_fields(httpx2_mock: respx.Router):
    _mock_graphql(
        httpx2_mock, _repo_data(prs=[_gql_pr(number=5, title="Fix bug", login="hugoh")])
    )
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs == [
        {
            "repo": "repo-a",
            "number": 5,
            "title": "Fix bug",
            "url": "https://github.com/hugoh/repo-a/pull/5",
            "author": "hugoh",
            "created_at": datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC),
            "closed_at": None,
            "merged": False,
            "state": "open",
            "ci_status": "passing",
            "mergeable": "clean",
        }
    ]


async def test_fetch_activity_excludes_prs_updated_before_since(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(
            prs=[
                _gql_pr(number=2, created_at="2026-07-20T10:00:00Z"),
                _gql_pr(number=1, created_at="2026-07-05T10:00:00Z"),
            ]
        ),
    )
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert [pr["number"] for pr in prs] == [2]


async def test_fetch_activity_includes_pr_opened_before_since_but_updated_after(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(
            prs=[
                _gql_pr(
                    number=1,
                    created_at="2026-06-01T00:00:00Z",
                    updated_at="2026-07-20T00:00:00Z",
                    closed_at="2026-07-20T00:00:00Z",
                    state="MERGED",
                )
            ]
        ),
    )
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert [pr["number"] for pr in prs] == [1]


async def test_fetch_activity_maps_merged_state_to_closed_and_merged_true(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(prs=[_gql_pr(state="MERGED", closed_at="2026-07-21T00:00:00Z")]),
    )
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs[0]["state"] == "closed"
    assert prs[0]["merged"] is True


async def test_fetch_activity_maps_closed_state_to_closed_and_merged_false(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(prs=[_gql_pr(state="CLOSED", closed_at="2026-07-21T00:00:00Z")]),
    )
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs[0]["state"] == "closed"
    assert prs[0]["merged"] is False


async def test_fetch_activity_maps_open_state(httpx2_mock: respx.Router):
    _mock_graphql(httpx2_mock, _repo_data(prs=[_gql_pr(state="OPEN")]))
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs[0]["state"] == "open"
    assert prs[0]["merged"] is False


async def test_fetch_activity_combines_multiple_repos(httpx2_mock: respx.Router):
    _mock_graphql(
        httpx2_mock,
        _repo_data(prs=[_gql_pr(number=1)]),
        _repo_data(prs=[_gql_pr(number=2)]),
    )
    prs, _issues, _releases = await fetch_activity(
        "hugoh", [REPO_A, REPO_B], SINCE_OPEN
    )
    assert sorted((pr["repo"], pr["number"]) for pr in prs) == [
        ("repo-a", 1),
        ("repo-b", 2),
    ]


@pytest.mark.parametrize(
    ("rollup_state", "expected_status"),
    [
        ("EXPECTED", "pending"),
        ("PENDING", "pending"),
        ("SUCCESS", "passing"),
        ("FAILURE", "failing"),
        ("ERROR", "failing"),
        (None, "no checks"),
    ],
)
async def test_fetch_activity_ci_status_mapping(
    httpx2_mock: respx.Router, rollup_state, expected_status
):
    _mock_graphql(httpx2_mock, _repo_data(prs=[_gql_pr(rollup_state=rollup_state)]))
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs[0]["ci_status"] == expected_status


async def test_fetch_activity_mergeable_conflict_when_conflicting(
    httpx2_mock: respx.Router,
):
    _mock_graphql(httpx2_mock, _repo_data(prs=[_gql_pr(mergeable="CONFLICTING")]))
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs[0]["mergeable"] == "conflict"


async def test_fetch_activity_mergeable_clean_when_unknown(httpx2_mock: respx.Router):
    # UNKNOWN is GraphQL's mergeable state right after a push, before GitHub
    # finishes computing it -- treated the same as clean, not flagged.
    _mock_graphql(httpx2_mock, _repo_data(prs=[_gql_pr(mergeable="UNKNOWN")]))
    prs, _issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert prs[0]["mergeable"] == "clean"


async def test_fetch_activity_normalizes_issue_fields(httpx2_mock: respx.Router):
    _mock_graphql(
        httpx2_mock,
        _repo_data(issues=[_gql_issue(number=5, title="Broken build", login="hugoh")]),
    )
    _prs, issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert issues == [
        {
            "repo": "repo-a",
            "number": 5,
            "title": "Broken build",
            "url": "https://github.com/hugoh/repo-a/issues/5",
            "author": "hugoh",
            "created_at": datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC),
            "closed_at": None,
            "state": "open",
        }
    ]


async def test_fetch_activity_excludes_renovate_dependency_dashboard(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(
            issues=[
                _gql_issue(
                    number=1, title="Dependency Dashboard", login="renovate[bot]"
                ),
                _gql_issue(number=2, title="Real bug"),
            ]
        ),
    )
    _prs, issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert [issue["number"] for issue in issues] == [2]


async def test_fetch_activity_keeps_dependency_dashboard_title_from_a_human(
    httpx2_mock: respx.Router,
):
    # only filter the renovate bot's own dashboard issue -- a human-authored
    # issue that happens to share its title is a real issue.
    _mock_graphql(
        httpx2_mock,
        _repo_data(
            issues=[_gql_issue(number=1, title="Dependency Dashboard", login="octocat")]
        ),
    )
    _prs, issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert [issue["number"] for issue in issues] == [1]


async def test_fetch_activity_excludes_issues_updated_before_since(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(
            issues=[
                _gql_issue(number=2, created_at="2026-07-20T10:00:00Z"),
                _gql_issue(number=1, created_at="2026-07-05T10:00:00Z"),
            ]
        ),
    )
    _prs, issues, _releases = await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert [issue["number"] for issue in issues] == [2]


async def test_fetch_activity_normalizes_release_fields(httpx2_mock: respx.Router):
    _mock_graphql(
        httpx2_mock,
        _repo_data(releases=[_gql_release(tag_name="v2.0.0", name="Version 2.0.0")]),
    )
    _prs, _issues, releases = await fetch_activity("hugoh", [REPO_A], SINCE_RELEASE)
    assert releases == [
        {
            "repo": "repo-a",
            "tag_name": "v2.0.0",
            "name": "Version 2.0.0",
            "url": "https://github.com/hugoh/repo-a/releases/tag/v2.0.0",
            "published_at": datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC),
            "prerelease": False,
        }
    ]


async def test_fetch_activity_falls_back_to_tag_name_when_name_blank(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock, _repo_data(releases=[_gql_release(tag_name="v2.0.0", name="")])
    )
    _prs, _issues, releases = await fetch_activity("hugoh", [REPO_A], SINCE_RELEASE)
    assert releases[0]["name"] == "v2.0.0"


async def test_fetch_activity_excludes_draft_releases(httpx2_mock: respx.Router):
    _mock_graphql(
        httpx2_mock,
        _repo_data(releases=[_gql_release(is_draft=True, published_at=None)]),
    )
    _prs, _issues, releases = await fetch_activity("hugoh", [REPO_A], SINCE_RELEASE)
    assert releases == []


async def test_fetch_activity_marks_prerelease(httpx2_mock: respx.Router):
    _mock_graphql(httpx2_mock, _repo_data(releases=[_gql_release(is_prerelease=True)]))
    _prs, _issues, releases = await fetch_activity("hugoh", [REPO_A], SINCE_RELEASE)
    assert releases[0]["prerelease"] is True


async def test_fetch_activity_excludes_releases_published_before_since(
    httpx2_mock: respx.Router,
):
    _mock_graphql(
        httpx2_mock,
        _repo_data(
            releases=[
                _gql_release(tag_name="v2.0.0", published_at="2026-07-20T10:00:00Z"),
                _gql_release(tag_name="v1.0.0", published_at="2026-07-01T10:00:00Z"),
            ]
        ),
    )
    _prs, _issues, releases = await fetch_activity("hugoh", [REPO_A], SINCE_RELEASE)
    assert [r["tag_name"] for r in releases] == ["v2.0.0"]


async def test_fetch_activity_warns_on_stderr_when_has_next_page(
    httpx2_mock: respx.Router, capsys
):
    _mock_graphql(httpx2_mock, _repo_data(prs=[_gql_pr()], pr_has_next=True))
    await fetch_activity("hugoh", [REPO_A], SINCE_OPEN)
    assert "repo-a" in capsys.readouterr().err


async def test_fetch_activity_batches_repos_across_multiple_queries(
    httpx2_mock: respx.Router,
):
    repos = [
        Repo(name=f"repo-{i}", default_branch="main", is_private=False, is_fork=False)
        for i in range(11)
    ]
    route = httpx2_mock.post(f"{API_BASE}/graphql").mock(
        return_value=httpx.Response(
            200, json={"data": {f"r{i}": _repo_data() for i in range(10)}}
        )
    )
    await fetch_activity("hugoh", repos, SINCE_OPEN)
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------


def _normalized_pr(**overrides):
    base = {
        "repo": "repo-a",
        "number": 1,
        "title": "Add feature",
        "url": "https://github.com/hugoh/repo-a/pull/1",
        "author": "octocat",
        "created_at": datetime(2026, 7, 20, tzinfo=UTC),
        "closed_at": None,
        "merged": False,
        "state": "open",
        "ci_status": "passing",
        "mergeable": "clean",
    }
    base.update(overrides)
    return base


def test_render_html_lists_open_pr_with_relevant_info():
    html = render_html(
        [_normalized_pr()], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "Add feature" in html
    assert "repo-a" in html
    assert "#1" in html
    assert "octocat" in html
    assert "2026-07-20" in html
    assert "https://github.com/hugoh/repo-a/pull/1" in html


def test_render_html_splits_open_and_closed_sections():
    open_pr = _normalized_pr(number=1, title="Open one", state="open")
    closed_pr = _normalized_pr(
        number=2,
        title="Closed one",
        state="closed",
        merged=True,
        closed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    html = render_html(
        [open_pr, closed_pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    open_idx = html.index("Open one")
    closed_idx = html.index("Closed one")
    open_section_idx = html.index("Open (")
    closed_section_idx = html.index("Closed (", open_section_idx)
    assert open_section_idx < open_idx < closed_section_idx < closed_idx


def test_render_html_orders_sections_open_releases_closed():
    open_pr = _normalized_pr(state="open")
    closed_pr = _normalized_pr(
        state="closed", merged=True, closed_at=datetime(2026, 7, 21, tzinfo=UTC)
    )
    release = _normalized_release()
    html = render_html(
        [open_pr, closed_pr],
        [release],
        [],
        SINCE_OPEN,
        SINCE_CLOSED,
        SINCE_RELEASE,
        UNTIL,
    )
    open_idx = html.index("Open (")
    releases_idx = html.index("Releases (")
    closed_idx = html.index("Closed (")
    assert open_idx < releases_idx < closed_idx


def test_render_html_open_pr_outside_open_window_is_excluded():
    # created 20 days before UNTIL -- inside the (implied) 30-day closed
    # window used here isn't relevant since it's still open; it's outside
    # the 14-day open window (SINCE_OPEN is 2026-07-10).
    pr = _normalized_pr(created_at=datetime(2026, 7, 1, tzinfo=UTC), state="open")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "no open" in html.lower()


def test_render_html_closed_pr_outside_closed_window_is_excluded():
    # opened well within the open window, but closed before the (shorter)
    # closed window started -- must not show up in either section.
    pr = _normalized_pr(
        state="closed",
        merged=True,
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
        closed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "no closed" in html.lower()
    assert "Add feature" not in html


def test_render_html_closed_pr_opened_before_open_window_still_shown():
    # opened well before the open window even started, but closed within
    # the closed window -- should still appear in Closed.
    pr = _normalized_pr(
        state="closed",
        merged=True,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        closed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "Add feature" in html


def test_render_html_shows_merged_status_for_merged_pr():
    pr = _normalized_pr(
        state="closed", merged=True, closed_at=datetime(2026, 7, 21, tzinfo=UTC)
    )
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "merged" in html.lower()


def test_render_html_shows_closed_without_merge():
    pr = _normalized_pr(
        state="closed",
        merged=False,
        closed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "closed" in html.lower()
    assert "merged" not in html.lower()


def test_render_html_empty_state_for_no_open_prs():
    closed_pr = _normalized_pr(
        state="closed", merged=True, closed_at=datetime(2026, 7, 21, tzinfo=UTC)
    )
    html = render_html(
        [closed_pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "no open" in html.lower()


def test_render_html_empty_state_for_no_closed_prs():
    html = render_html(
        [_normalized_pr()], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "no closed" in html.lower()


def test_render_html_section_headers_show_each_windows_own_date_range():
    html = render_html([], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "2026-07-10" in html  # since_open
    assert "2026-07-17" in html  # since_closed
    assert "2026-07-24" in html  # until, shared


def test_render_html_shows_cutoff_summary_with_day_counts_and_dates():
    # top-of-email summary so the windows are legible without reading every
    # section header's own "(since to until)" range.
    html = render_html([], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "14 days" in html  # until - since_open
    assert "7 days" in html  # until - since_closed / since_release
    assert "2026-07-10" in html
    assert "2026-07-17" in html


def test_render_html_shows_ci_status_and_mergeable_for_open_prs():
    pr = _normalized_pr(ci_status="failing", mergeable="conflict")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "failing" in html
    assert "conflict" in html


def test_render_html_color_codes_passing_ci_status():
    pr = _normalized_pr(ci_status="passing")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "status-passing" in html


def test_render_html_color_codes_failing_ci_status():
    pr = _normalized_pr(ci_status="failing")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "status-failing" in html


def test_render_html_color_codes_pending_ci_status():
    pr = _normalized_pr(ci_status="pending")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "status-pending" in html


def test_render_html_color_codes_no_checks_ci_status():
    pr = _normalized_pr(ci_status="no checks")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "status-no-checks" in html


def test_render_html_color_codes_clean_mergeable():
    pr = _normalized_pr(mergeable="clean")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "mergeable-clean" in html


def test_render_html_color_codes_conflict_mergeable():
    pr = _normalized_pr(mergeable="conflict")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "mergeable-conflict" in html


def test_render_html_closed_section_has_no_ci_or_mergeable_columns():
    pr = _normalized_pr(
        state="closed", merged=True, closed_at=datetime(2026, 7, 21, tzinfo=UTC)
    )
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    closed_section = html[html.index("Closed (") :]
    assert "status-" not in closed_section
    assert "mergeable-" not in closed_section


def test_render_html_escapes_title():
    pr = _normalized_pr(title="<script>alert(1)</script>")
    html = render_html([pr], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_shows_counts_in_section_headers():
    open_pr = _normalized_pr(number=1, state="open")
    closed_pr = _normalized_pr(
        number=2,
        state="closed",
        merged=True,
        closed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    releases = [_normalized_release(), _normalized_release(tag_name="v2.0.0")]
    html = render_html(
        [open_pr, closed_pr],
        releases,
        [],
        SINCE_OPEN,
        SINCE_CLOSED,
        SINCE_RELEASE,
        UNTIL,
    )
    assert "Open (1)" in html
    assert "Releases (2)" in html
    assert "Closed (1)" in html


# ---------------------------------------------------------------------------
# releases (render_html)
# ---------------------------------------------------------------------------


def _normalized_release(**overrides):
    base = {
        "repo": "repo-a",
        "tag_name": "v1.0.0",
        "name": "Version 1.0.0",
        "url": "https://github.com/hugoh/repo-a/releases/tag/v1.0.0",
        "published_at": datetime(2026, 7, 20, tzinfo=UTC),
        "prerelease": False,
    }
    base.update(overrides)
    return base


def test_render_html_lists_release_with_relevant_info():
    html = render_html(
        [], [_normalized_release()], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "repo-a v1.0.0" in html
    assert "2026-07-20" in html
    assert "https://github.com/hugoh/repo-a/releases/tag/v1.0.0" in html


def test_render_html_release_outside_window_is_excluded():
    release = _normalized_release(published_at=datetime(2026, 7, 1, tzinfo=UTC))
    html = render_html(
        [], [release], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "no releases" in html.lower()
    assert "repo-a v1.0.0" not in html


def test_render_html_empty_state_for_no_releases():
    html = render_html([], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "no releases" in html.lower()


def test_render_html_marks_prerelease():
    release = _normalized_release(prerelease=True)
    html = render_html(
        [], [release], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "prerelease" in html.lower()


def test_render_html_releases_sorted_newest_first():
    older = _normalized_release(
        tag_name="v1.0.0",
        url="https://github.com/hugoh/repo-a/releases/tag/v1.0.0",
        published_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    newer = _normalized_release(
        tag_name="v2.0.0",
        url="https://github.com/hugoh/repo-a/releases/tag/v2.0.0",
        published_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    html = render_html(
        [], [older, newer], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert html.index("repo-a v2.0.0") < html.index("repo-a v1.0.0")


def test_render_html_release_section_header_shows_its_own_window():
    html = render_html([], [], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "2026-07-17" in html  # since_release


def test_render_html_escapes_release_tag_name():
    release = _normalized_release(tag_name="<script>alert(1)</script>")
    html = render_html(
        [], [release], [], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# issues (render_html)
# ---------------------------------------------------------------------------


def _normalized_issue(**overrides):
    base = {
        "repo": "repo-a",
        "number": 1,
        "title": "Something broke",
        "url": "https://github.com/hugoh/repo-a/issues/1",
        "author": "octocat",
        "created_at": datetime(2026, 7, 20, tzinfo=UTC),
        "closed_at": None,
        "state": "open",
    }
    base.update(overrides)
    return base


def test_render_html_lists_open_issue_with_relevant_info():
    html = render_html(
        [], [], [_normalized_issue()], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "Something broke" in html
    assert "repo-a" in html
    assert "#1" in html
    assert "octocat" in html
    assert "https://github.com/hugoh/repo-a/issues/1" in html


def test_render_html_splits_open_and_closed_issue_sections():
    open_issue = _normalized_issue(number=1, title="Open one", state="open")
    closed_issue = _normalized_issue(
        number=2,
        title="Closed one",
        state="closed",
        closed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    html = render_html(
        [],
        [],
        [open_issue, closed_issue],
        SINCE_OPEN,
        SINCE_CLOSED,
        SINCE_RELEASE,
        UNTIL,
    )
    open_idx = html.index("Open one")
    closed_idx = html.index("Closed one")
    open_section_idx = html.index("Open issues")
    closed_section_idx = html.index("Closed issues", open_section_idx)
    assert open_section_idx < open_idx < closed_section_idx < closed_idx


def test_render_html_open_issue_outside_open_window_is_excluded():
    issue = _normalized_issue(created_at=datetime(2026, 7, 1, tzinfo=UTC), state="open")
    html = render_html([], [], [issue], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "no open issues" in html.lower()


def test_render_html_closed_issue_outside_closed_window_is_excluded():
    issue = _normalized_issue(
        state="closed",
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
        closed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    html = render_html([], [], [issue], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "no closed issues" in html.lower()
    assert "Something broke" not in html


def test_render_html_empty_state_for_no_open_issues():
    closed_issue = _normalized_issue(
        state="closed", closed_at=datetime(2026, 7, 21, tzinfo=UTC)
    )
    html = render_html(
        [], [], [closed_issue], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "no open issues" in html.lower()


def test_render_html_empty_state_for_no_closed_issues():
    html = render_html(
        [], [], [_normalized_issue()], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL
    )
    assert "no closed issues" in html.lower()


def test_render_html_shows_counts_in_issue_section_headers():
    open_issue = _normalized_issue(number=1, state="open")
    closed_issue = _normalized_issue(
        number=2, state="closed", closed_at=datetime(2026, 7, 21, tzinfo=UTC)
    )
    html = render_html(
        [],
        [],
        [open_issue, closed_issue],
        SINCE_OPEN,
        SINCE_CLOSED,
        SINCE_RELEASE,
        UNTIL,
    )
    assert "Open issues (1)" in html
    assert "Closed issues (1)" in html


def test_render_html_escapes_issue_title():
    issue = _normalized_issue(title="<script>alert(1)</script>")
    html = render_html([], [], [issue], SINCE_OPEN, SINCE_CLOSED, SINCE_RELEASE, UNTIL)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_parser_accepts_repo_scope():
    args = build_parser().parse_args(["repo-a", "repo-b", "--skip", "repo-c,repo-d"])
    assert args.repos == ["repo-a", "repo-b"]
    assert args.skip == "repo-c,repo-d"


def test_parser_repo_scope_defaults_to_everything():
    args = build_parser().parse_args([])
    assert args.repos == []
    assert args.skip is None
