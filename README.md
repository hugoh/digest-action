# digest-action

Builds — and optionally emails — an HTML digest of a GitHub account's repo
activity: still-open PRs and issues, releases, recently closed PRs and
issues, and a stars section (per-repo totals plus stars gained in the last
N days), fetched via GraphQL in batches of 10 repos per query. Uses
[`repokit`](https://pypi.org/project/hugoh-repokit/) for repo listing/CLI
plumbing and [`asyncgh`](https://pypi.org/project/asyncgh/) for the GitHub
transport — both PyPI packages maintained in
[`hugoh/gh-workflows`](https://github.com/hugoh/gh-workflows).

Self-contained: installs its own pinned `uv` ([`astral-sh/setup-uv`](https://github.com/astral-sh/setup-uv)).

## Inputs

| Input | Required | Default | Purpose |
|---|---|---|---|
| `owner` | yes | — | GitHub account/org to report on |
| `github-token` | yes | — | A PAT, passed through as `GH_TOKEN`. `GITHUB_TOKEN` can't do account-wide repo listing, so it won't work. See [Token](#token) for the scopes. |
| `only` | no | *(all)* | Comma-separated repo names to limit to |
| `skip` | no | *(none)* | Comma-separated repo names to exclude |
| `open-days` | no | `365` | How far back to look for still-open PRs/issues |
| `closed-days` | no | `7` | How far back to look for closed PRs/issues |
| `release-days` | no | `7` | How far back to look for published releases |
| `star-days` | no | `7,30` | Comma-separated windows (days) for counting recently-gained stars — one column per value |
| `star-top` | no | `10` | Always show this many most-starred repos, even with no recent gain |
| `out` | no | *(none)* | Also write the rendered HTML to this path (relative to the runner's workspace) — sets the `html` output. Required if `send-email` is `false`, since otherwise the digest is built and immediately discarded. |
| `send-email` | no | `true` | Whether to email the digest — requires the `smtp-*`/`digest-*-email` inputs below |
| `smtp-host` / `smtp-port` / `smtp-username` / `smtp-password` | when `send-email` | — | SMTP relay settings. Connects over STARTTLS with cert + hostname verification — use a submission port (typically `587`), not an implicit-TLS port (`465`). |
| `digest-from-email` / `digest-to-email` | when `send-email` | — | Envelope From / recipient |
| `uv-version` | no | *(none)* | `uv` version to install (e.g. `0.5.0`, `latest`, `latest-known`) — defaults to the version in `pyproject.toml`, or `latest` |

SMTP settings are passed as **inputs**, not job-level `env:` — `ghalint`'s
`job_secrets` policy flags job-level env holding secrets as over-broad
exposure to every step in the job, since composite-action steps don't see
env set on the calling step, only on the job.

## Token

`github-token` needs to read PRs, issues, releases and stargazers across
every repo owned by `owner`, and to enumerate that account's repos —
`GITHUB_TOKEN` can't, so a PAT is required.

**Classic PAT** — scope `repo` (or `public_repo` if `owner` has no private
repos you want included). This is the simplest choice and supports every
part of the digest.

**Fine-grained PAT** — Resource owner `owner`, Repository access **All
repositories**, read-only on **Metadata**, **Contents**, **Issues**, and
**Pull requests**. Note: fine-grained PATs **cannot read the `stargazers`
connection**, so the star-activity section is dropped automatically (a
warning is logged) and `star-days` / `star-top` have no effect. Use a
classic PAT if you want the star section.

| Output | Set when | Value |
|---|---|---|
| `html` | `out` is given | the `out` path |

## Usage

```yaml
jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: hugoh/digest-action@<pinned-sha>
        with:
          owner: my-org
          github-token: ${{ secrets.DIGEST_PAT }}
          smtp-host: ${{ secrets.SMTP_HOST }}
          smtp-port: ${{ secrets.SMTP_PORT }}
          smtp-username: ${{ secrets.SMTP_USERNAME }}
          smtp-password: ${{ secrets.SMTP_PASSWORD }}
          digest-from-email: ${{ secrets.DIGEST_FROM_EMAIL }}
          digest-to-email: ${{ secrets.DIGEST_TO_EMAIL }}
```

Rendering only, no email (e.g. to upload as a workflow artifact instead):

```yaml
steps:
  - uses: hugoh/digest-action@<pinned-sha>
    with:
      owner: my-org
      github-token: ${{ secrets.DIGEST_PAT }}
      send-email: "false"
      out: digest.html
  - uses: actions/upload-artifact@<pinned-sha>
    with:
      name: digest
      path: digest.html
```

## History

Originally lived at `digest-action/` inside
[`hugoh/gh-workflows`](https://github.com/hugoh/gh-workflows), alongside
that repo's other composite actions. Split out into its own repo since
GitHub Marketplace only publishes an Action whose `action.yml` sits at a
repository root.
