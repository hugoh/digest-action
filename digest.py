"""Builds an HTML digest of activity on an account's repos -- still-open PRs
and issues opened in the last N days, releases published in the last R days,
PRs and issues closed in the last M days, and a stars section (per-repo
totals plus stars gained in each --star-days window) -- in separate
sections, and emails it via an SMTP relay. Renovate's "Dependency Dashboard"
issues are filtered out as noise.

Usage: digest.py [repo ...] [--skip name1,name2] [--open-days 365]
    [--release-days 7] [--closed-days 7] [--star-days 7,30] [--star-top 10]
    [--out FILE] [--no-send]

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asyncgh import graphql
from jinja2 import Environment, FileSystemLoader, select_autoescape
from repokit import DEFAULT_JOBS, Repo, as_set, list_repos, run_cli
from rich.progress import Progress

from mailer import send_email_from_env

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


def _extract_stars(
    repo_name: str, stargazer_count: int, connection: dict, since_star: datetime
) -> dict:
    """GitHub has no record of *un*stars, so "recent" star activity can only
    ever be new stars -- the `starredAt` edge timestamps within since_star.
    """
    starred_at = sorted(
        (
            _parse_dt(edge["starredAt"])
            for edge in connection["edges"]
            if _parse_dt(edge["starredAt"]) >= since_star
        ),
        reverse=True,
    )
    return {"repo": repo_name, "total": stargazer_count, "starred_at": starred_at}


def _normalize_release(repo_name: str, node: dict) -> dict:
    return {
        "repo": repo_name,
        "tag_name": node["tagName"],
        "name": node["name"] or node["tagName"],
        "url": node["url"],
        "published_at": _parse_dt(node["publishedAt"]),
        "prerelease": node["isPrerelease"],
    }


_PR_FIELDS = """number title url state createdAt closedAt updatedAt
          author { login }
          mergeable
          commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"""
_ISSUE_FIELDS = "number title url state createdAt closedAt updatedAt author { login }"
_RELEASE_FIELDS = "tagName name url publishedAt createdAt isPrerelease isDraft"
_STARGAZER_FIELDS = "starredAt"

# Args shared between a connection's first page (in the batched multi-repo
# query) and its continuation pages (fetched one repo/connection at a time,
# below) -- `first: 100, after: $after` is added by whichever query builder
# is using them.
_CONNECTION_QUERY_ARGS = {
    "pullRequests": "orderBy: {field: UPDATED_AT, direction: DESC}, states: [OPEN, CLOSED, MERGED]",
    "issues": "orderBy: {field: UPDATED_AT, direction: DESC}, states: [OPEN, CLOSED]",
    # GitHub has no PUBLISHED_AT order option for releases, so CREATED_AT is
    # the only field pagination can treat as monotonic across pages.
    "releases": "orderBy: {field: CREATED_AT, direction: DESC}",
    "stargazers": "orderBy: {field: STARRED_AT, direction: DESC}",
}
_CONNECTION_FIELDS = {
    "pullRequests": _PR_FIELDS,
    "issues": _ISSUE_FIELDS,
    "releases": _RELEASE_FIELDS,
    "stargazers": _STARGAZER_FIELDS,
}
_CONNECTION_CUTOFF_FIELD = {
    "pullRequests": "updatedAt",
    "issues": "updatedAt",
    "releases": "createdAt",
    "stargazers": "starredAt",
}
# stargazers exposes `starredAt` on the edge, not the node.
_CONNECTION_ITEMS_KEY = {
    "pullRequests": "nodes",
    "issues": "nodes",
    "releases": "nodes",
    "stargazers": "edges",
}

_REPO_QUERY_FIELDS = "      stargazerCount\n" + "\n".join(
    f"""      {name}(first: 100, {args}) {{
        pageInfo {{ hasNextPage endCursor }}
        {_CONNECTION_ITEMS_KEY[name]} {{ {_CONNECTION_FIELDS[name]} }}
      }}"""
    for name, args in _CONNECTION_QUERY_ARGS.items()
)


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


@functools.lru_cache
def _build_connection_page_query(connection: str) -> str:
    """A single-repo, single-connection query for continuing pagination past
    a connection's first page -- the batched multi-repo query above can't
    express "page 2 of r3's PRs, nothing else" without a cursor variable per
    connection per repo, so a continuation just asks for that one connection.
    """
    args = _CONNECTION_QUERY_ARGS[connection]
    fields = _CONNECTION_FIELDS[connection]
    items_key = _CONNECTION_ITEMS_KEY[connection]
    return (
        "query DigestPage($owner: String!, $name: String!, $after: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"    {connection}(first: 100, after: $after, {args}) {{\n"
        "      pageInfo { hasNextPage endCursor }\n"
        f"      {items_key} {{ {fields} }}\n"
        "    }\n"
        "  }\n"
        "}"
    )


async def _fetch_connection_page(
    owner: str, repo_name: str, connection: str, after: str
) -> dict:
    data = await graphql(
        _build_connection_page_query(connection),
        {"owner": owner, "name": repo_name, "after": after},
    )
    return data["repository"][connection]


async def _paginate_connection(
    owner: str,
    repo_name: str,
    connection: str,
    data: dict,
    since_fetch: datetime,
    sem: asyncio.Semaphore,
) -> dict:
    """Follows a connection's pageInfo past its first page while there's more
    to fetch and the oldest node so far is still within since_fetch. Nodes
    are ordered by _CONNECTION_CUTOFF_FIELD DESC, so once a page's last node
    falls before since_fetch every later page is older still -- pagination
    can stop even if hasNextPage remains true.
    """
    cutoff_field = _CONNECTION_CUTOFF_FIELD[connection]
    items_key = _CONNECTION_ITEMS_KEY[connection]
    items = data[items_key]
    page_info = data["pageInfo"]
    while (
        page_info["hasNextPage"]
        and items
        and _parse_dt(items[-1][cutoff_field]) >= since_fetch
    ):
        async with sem:
            page = await _fetch_connection_page(
                owner, repo_name, connection, page_info["endCursor"]
            )
        items = items + page[items_key]
        page_info = page["pageInfo"]
    return {"pageInfo": page_info, items_key: items}


def _extract_prs(repo_name: str, connection: dict, since_fetch: datetime) -> list[dict]:
    return [
        _normalize_pr(repo_name, node)
        for node in connection["nodes"]
        if _parse_dt(node["updatedAt"]) >= since_fetch
    ]


def _extract_issues(
    repo_name: str, connection: dict, since_fetch: datetime
) -> list[dict]:
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
    star_since: datetime | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Fetches PRs, issues, releases, and stargazers for every repo via GraphQL, batching
    up to _BATCH_SIZE repos per query (bounded by `jobs` concurrent batches)
    -- CI status and mergeable state come back inline on each PR node, so
    unlike the old REST fetch there's no separate per-open-PR round-trip.
    """
    sem = asyncio.Semaphore(jobs)
    # Stars have a much shorter window than open PRs/issues; paginating them
    # against since_fetch would page a popular repo's whole star history.
    star_since = star_since or since_fetch
    connection_since = dict.fromkeys(_CONNECTION_QUERY_ARGS, since_fetch)
    connection_since["stargazers"] = star_since
    batches = [repos[i : i + _BATCH_SIZE] for i in range(0, len(repos), _BATCH_SIZE)]

    async def run_batch(batch: list[Repo], progress: Progress, task) -> dict:
        async with sem:
            result = await _fetch_batch(owner, [repo.name for repo in batch])
        progress.advance(task)
        return result

    def _needs_pagination(repo_data: dict) -> bool:
        return any(
            repo_data[name]["pageInfo"]["hasNextPage"]
            for name in _CONNECTION_QUERY_ARGS
        )

    async def paginate_repo(
        repo: Repo, repo_data: dict, progress: Progress, task
    ) -> dict:
        names = list(_CONNECTION_QUERY_ARGS)
        needs_pagination = _needs_pagination(repo_data)
        if needs_pagination:
            progress.update(task, description=f"Paginating {repo.name}...")
        pages = await asyncio.gather(
            *(
                _paginate_connection(
                    owner, repo.name, name, repo_data[name], connection_since[name], sem
                )
                for name in names
            )
        )
        if needs_pagination:
            progress.advance(task)
        return dict(zip(names, pages, strict=True))

    with Progress(disable=not sys.stdout.isatty()) as progress:
        fetch_task = progress.add_task("Fetching activity...", total=len(batches))
        results = await asyncio.gather(
            *(run_batch(batch, progress, fetch_task) for batch in batches)
        )

        repos_and_data = [
            (repo, batch_data[f"r{i}"])
            for batch, batch_data in zip(batches, results, strict=True)
            for i, repo in enumerate(batch)
        ]
        # Most repos have nothing to paginate -- sizing the bar to only the
        # repos that actually need extra pages keeps it from jumping straight
        # to 100% while doing real work for the few that do.
        paginate_total = sum(
            1 for _, repo_data in repos_and_data if _needs_pagination(repo_data)
        )
        paginate_task = progress.add_task(
            "Paginating repos with >100 items...", total=paginate_total
        )
        paginated = await asyncio.gather(
            *(
                paginate_repo(repo, repo_data, progress, paginate_task)
                for repo, repo_data in repos_and_data
            )
        )

    prs, issues, releases, stars = [], [], [], []
    for (repo, repo_data), connections in zip(repos_and_data, paginated, strict=True):
        prs.extend(_extract_prs(repo.name, connections["pullRequests"], since_fetch))
        issues.extend(_extract_issues(repo.name, connections["issues"], since_fetch))
        releases.extend(
            _extract_releases(repo.name, connections["releases"], since_fetch)
        )
        stars.append(
            _extract_stars(
                repo.name,
                repo_data["stargazerCount"],
                connections["stargazers"],
                star_since,
            )
        )
    return prs, issues, releases, stars


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


@dataclass
class _StarRow:
    repo: str
    total: int
    gains: list[int]

    @property
    def widest_gain(self) -> int:
        return self.gains[-1] if self.gains else 0


def _star_rows(
    stars: list[dict], star_cutoffs: list[datetime], star_top: int
) -> list[_StarRow]:
    """One row per repo worth showing: those that gained a star inside the
    widest window, plus the star_top most-starred repos. A repo with no
    stars and no recent gain is dropped entirely. Each row's `gains` lines
    up with star_cutoffs (narrowest window first).
    """
    rows = [
        _StarRow(
            repo=s["repo"],
            total=s["total"],
            gains=[
                sum(1 for t in s["starred_at"] if t >= cutoff)
                for cutoff in star_cutoffs
            ],
        )
        for s in stars
    ]

    keep = {row.repo for row in rows if row.widest_gain > 0}
    keep |= {
        row.repo
        for row in sorted(
            (r for r in rows if r.total > 0),
            key=lambda r: r.total,
            reverse=True,
        )[:star_top]
    }
    shown = [row for row in rows if row.repo in keep]
    shown.sort(key=lambda r: (r.widest_gain, r.total), reverse=True)
    return shown


def render_html(
    prs: list[dict],
    releases: list[dict],
    issues: list[dict],
    since_open: datetime,
    since_closed: datetime,
    since_release: datetime,
    until: datetime,
    *,
    stars: list[dict] | None = None,
    star_days: list[int] | None = None,
    star_top: int = 10,
    owner: str = "",
) -> str:
    star_days = sorted(star_days or [])
    star_cutoffs = [until - timedelta(days=d) for d in star_days]
    star_rows = _star_rows(stars or [], star_cutoffs, star_top)
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
        star_rows=star_rows,
        star_days=star_days,
        owner=owner,
        since_open=since_open,
        since_closed=since_closed,
        since_release=since_release,
        until=until,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_day_list(value: str) -> list[int]:
    return sorted({int(part) for part in value.split(",") if part.strip()})


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
    parser.add_argument(
        "--star-days",
        type=_parse_day_list,
        default=[7, 30],
        help="comma-separated windows for counting recently-gained stars (default 7,30)",
    )
    parser.add_argument(
        "--star-top",
        type=int,
        default=10,
        help="always show this many most-starred repos, even with no recent gain (default 10)",
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
    star_since = min(
        (until - timedelta(days=d) for d in args.star_days), default=since_fetch
    )

    repos = await list_repos(
        owner, only=set(args.repos) or None, skip=as_set(args.skip)
    )
    prs, issues, releases, stars = await fetch_activity(
        owner, repos, since_fetch, star_since=star_since
    )
    rendered = render_html(
        prs,
        releases,
        issues,
        since_open,
        since_closed,
        since_release,
        until,
        stars=stars,
        star_days=args.star_days,
        star_top=args.star_top,
        owner=owner,
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
