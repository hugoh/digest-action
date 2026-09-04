"""Builds an HTML digest of activity on an account's repos -- still-open PRs
and issues opened in the last N days, releases published in the last R days,
and PRs and issues closed in the last M days -- in separate sections, and
emails it via an SMTP relay. Renovate's "Dependency Dashboard" issues are
filtered out as noise.

Usage: digest.py [repo ...] [--skip name1,name2] [--open-days 365]
    [--release-days 7] [--closed-days 7] [--out FILE] [--no-send]

Trailing repo names scope the digest to those repos (default: every
non-archived repo); --skip excludes instead, same convention as
repo_admin.py. Requires GH_OWNER (the account/org to report on).

Reads SMTP settings and the recipient from the environment: SMTP_HOST,
SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, DIGEST_FROM_EMAIL,
DIGEST_TO_EMAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asyncgh import graphql
from jinja2 import Environment, FileSystemLoader, select_autoescape
from repokit import DEFAULT_JOBS, Repo, as_set, list_repos, run_cli
from repokit.email import send_email_from_env
from rich.progress import Progress

_BATCH_SIZE = 10
_RENOVATE_LOGINS = {"renovate", "renovate[bot]"}
_RENOVATE_DASHBOARD_TITLE = "Dependency Dashboard"
_CI_STATUS_BY_ROLLUP_STATE = {
    None: "no checks",
    "EXPECTED": "pending",
    "PENDING": "pending",
    "SUCCESS": "passing",
    "FAILURE": "failing",
    "ERROR": "failing",
}


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_dt(value: str | None) -> datetime | None:
    return _parse_dt(value) if value is not None else None


_PR_STATE = {
    "OPEN": ("open", False),
    "CLOSED": ("closed", False),
    "MERGED": ("closed", True),
}


def _ci_status(node: dict) -> str:
    """Rolls a PR's `commits(last: 1)` status-check rollup up into one
    summary; None (no commits, or a commit with no checks) means "no checks".
    """
    commits = node["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    return _CI_STATUS_BY_ROLLUP_STATE[rollup["state"] if rollup else None]


def _mergeable(node: dict) -> str:
    """CONFLICTING is GraphQL's mergeable state for an actual merge conflict;
    everything else (including UNKNOWN right after a push, before GitHub
    finishes computing it) is treated as clean rather than flagged.
    """
    return "conflict" if node["mergeable"] == "CONFLICTING" else "clean"


def _normalize_pr(repo_name: str, node: dict) -> dict:
    state, merged = _PR_STATE[node["state"]]
    return {
        "repo": repo_name,
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "author": node["author"]["login"],
        "created_at": _parse_dt(node["createdAt"]),
        "closed_at": _parse_optional_dt(node["closedAt"]),
        "merged": merged,
        "state": state,
        "ci_status": _ci_status(node),
        "mergeable": _mergeable(node),
    }


def _is_renovate_dashboard(node: dict) -> bool:
    """Renovate opens one "Dependency Dashboard" issue per repo and keeps it
    open indefinitely, editing it in place -- it's not an actionable item,
    just noise that would otherwise dominate the open-issues section.
    """
    return (
        node["title"] == _RENOVATE_DASHBOARD_TITLE
        and node["author"]["login"] in _RENOVATE_LOGINS
    )


def _normalize_issue(repo_name: str, node: dict) -> dict:
    return {
        "repo": repo_name,
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "author": node["author"]["login"],
        "created_at": _parse_dt(node["createdAt"]),
        "closed_at": _parse_optional_dt(node["closedAt"]),
        "state": node["state"].lower(),
    }


def _normalize_release(repo_name: str, node: dict) -> dict:
    return {
        "repo": repo_name,
        "tag_name": node["tagName"],
        "name": node["name"] or node["tagName"],
        "url": node["url"],
        "published_at": _parse_dt(node["publishedAt"]),
        "prerelease": node["isPrerelease"],
    }


_REPO_QUERY_FIELDS = """
      pullRequests(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}, states: [OPEN, CLOSED, MERGED]) {
        pageInfo { hasNextPage }
        nodes {
          number title url state createdAt closedAt updatedAt
          author { login }
          mergeable
          commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        }
      }
      issues(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}, states: [OPEN, CLOSED]) {
        pageInfo { hasNextPage }
        nodes { number title url state createdAt closedAt updatedAt author { login } }
      }
      releases(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
        pageInfo { hasNextPage }
        nodes { tagName name url publishedAt createdAt isPrerelease isDraft }
      }
"""


@functools.lru_cache
def _build_digest_query(n: int) -> str:
    """One query, aliasing up to n repos (r0, r1, ...) so a batch fetches
    PRs/issues/releases for every repo it covers in a single round-trip.
    Cached since every full batch reuses the same n (and the trailing
    partial batch reuses its own n across runs within the process).
    """
    name_vars = ", ".join(f"$name{i}: String!" for i in range(n))
    repos = "\n".join(
        f"r{i}: repository(owner: $owner, name: $name{i}) {{{_REPO_QUERY_FIELDS}}}"
        for i in range(n)
    )
    return f"query Digest($owner: String!, {name_vars}) {{\n{repos}\n}}"


async def _fetch_batch(owner: str, names: list[str]) -> dict:
    variables = {"owner": owner, **{f"name{i}": name for i, name in enumerate(names)}}
    return await graphql(_build_digest_query(len(names)), variables)


def _warn_if_truncated(repo_name: str, connection: str, has_next_page: bool) -> None:
    if has_next_page:
        print(
            f"warning: {repo_name} has more than 100 {connection} in the fetch "
            "window -- results are truncated",
            file=sys.stderr,
        )


def _extract_prs(repo_name: str, connection: dict, since_fetch: datetime) -> list[dict]:
    _warn_if_truncated(repo_name, "PRs", connection["pageInfo"]["hasNextPage"])
    return [
        _normalize_pr(repo_name, node)
        for node in connection["nodes"]
        if _parse_dt(node["updatedAt"]) >= since_fetch
    ]


def _extract_issues(
    repo_name: str, connection: dict, since_fetch: datetime
) -> list[dict]:
    _warn_if_truncated(repo_name, "issues", connection["pageInfo"]["hasNextPage"])
    issues = []
    for node in connection["nodes"]:
        if _parse_dt(node["updatedAt"]) < since_fetch:
            continue
        if _is_renovate_dashboard(node):
            continue
        issues.append(_normalize_issue(repo_name, node))
    return issues


def _extract_releases(
    repo_name: str, connection: dict, since_fetch: datetime
) -> list[dict]:
    _warn_if_truncated(repo_name, "releases", connection["pageInfo"]["hasNextPage"])
    releases = []
    for node in connection["nodes"]:
        if node["isDraft"]:
            continue
        if _parse_dt(node["publishedAt"]) < since_fetch:
            continue
        releases.append(_normalize_release(repo_name, node))
    return releases


async def fetch_activity(
    owner: str,
    repos: list[Repo],
    since_fetch: datetime,
    jobs: int = DEFAULT_JOBS,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetches PRs, issues, and releases for every repo via GraphQL, batching
    up to _BATCH_SIZE repos per query (bounded by `jobs` concurrent batches)
    -- CI status and mergeable state come back inline on each PR node, so
    unlike the old REST fetch there's no separate per-open-PR round-trip.
    """
    sem = asyncio.Semaphore(jobs)
    batches = [repos[i : i + _BATCH_SIZE] for i in range(0, len(repos), _BATCH_SIZE)]

    async def run_batch(batch: list[Repo], progress: Progress, task) -> dict:
        async with sem:
            result = await _fetch_batch(owner, [repo.name for repo in batch])
        progress.advance(task)
        return result

    with Progress(disable=not sys.stdout.isatty()) as progress:
        task = progress.add_task("Fetching activity...", total=len(batches))
        results = await asyncio.gather(
            *(run_batch(batch, progress, task) for batch in batches)
        )

    prs, issues, releases = [], [], []
    for batch, batch_data in zip(batches, results, strict=True):
        for i, repo in enumerate(batch):
            repo_data = batch_data[f"r{i}"]
            prs.extend(_extract_prs(repo.name, repo_data["pullRequests"], since_fetch))
            issues.extend(_extract_issues(repo.name, repo_data["issues"], since_fetch))
            releases.extend(
                _extract_releases(repo.name, repo_data["releases"], since_fetch)
            )
    return prs, issues, releases


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "jinja"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_digest_template = _jinja_env.get_template("digest.html.jinja")


def _format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def render_html(
    prs: list[dict],
    releases: list[dict],
    issues: list[dict],
    since_open: datetime,
    since_closed: datetime,
    since_release: datetime,
    until: datetime,
) -> str:
    open_prs = sorted(
        (pr for pr in prs if pr["state"] == "open" and pr["created_at"] >= since_open),
        key=lambda pr: pr["created_at"],
        reverse=True,
    )
    closed_prs = sorted(
        (
            pr
            for pr in prs
            if pr["state"] == "closed"
            and pr["closed_at"] is not None
            and pr["closed_at"] >= since_closed
        ),
        key=lambda pr: pr["closed_at"],
        reverse=True,
    )
    recent_releases = sorted(
        (r for r in releases if r["published_at"] >= since_release),
        key=lambda r: r["published_at"],
        reverse=True,
    )
    open_issues = sorted(
        (
            issue
            for issue in issues
            if issue["state"] == "open" and issue["created_at"] >= since_open
        ),
        key=lambda issue: issue["created_at"],
        reverse=True,
    )
    closed_issues = sorted(
        (
            issue
            for issue in issues
            if issue["state"] == "closed"
            and issue["closed_at"] is not None
            and issue["closed_at"] >= since_closed
        ),
        key=lambda issue: issue["closed_at"],
        reverse=True,
    )
    return _digest_template.render(
        open_prs=open_prs,
        releases=recent_releases,
        closed_prs=closed_prs,
        open_issues=open_issues,
        closed_issues=closed_issues,
        since_open=since_open,
        since_closed=since_closed,
        since_release=since_release,
        until=until,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "repos", nargs="*", metavar="REPO", help="repo names to target (default: all)"
    )
    parser.add_argument("--skip", help="comma-separated repo names to exclude")
    parser.add_argument(
        "--open-days",
        type=int,
        default=365,
        help="how many days back to look for still-open PRs and issues (default 365)",
    )
    parser.add_argument(
        "--closed-days",
        type=int,
        default=7,
        help="how many days back to look for closed PRs (default 7)",
    )
    parser.add_argument(
        "--release-days",
        type=int,
        default=7,
        help="how many days back to look for published releases (default 7)",
    )
    parser.add_argument("--out", help="write the rendered HTML to this file")
    parser.add_argument(
        "--no-send", action="store_true", help="skip sending the email (for dry runs)"
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    owner = os.environ.get("GH_OWNER")
    if not owner:
        print("error: GH_OWNER must be set", file=sys.stderr)
        return 1

    until = datetime.now(UTC)
    since_open = until - timedelta(days=args.open_days)
    since_closed = until - timedelta(days=args.closed_days)
    since_release = until - timedelta(days=args.release_days)
    since_fetch = min(since_open, since_closed, since_release)

    repos = await list_repos(
        owner, only=set(args.repos) or None, skip=as_set(args.skip)
    )
    prs, issues, releases = await fetch_activity(owner, repos, since_fetch)
    rendered = render_html(
        prs, releases, issues, since_open, since_closed, since_release, until
    )

    if args.out:
        await asyncio.to_thread(Path(args.out).write_text, rendered)

    if not args.no_send:
        send_email_from_env(rendered, subject=f"GitHub digest: {_format_date(until)}")
    return 0


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return run_cli(_main_async, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
