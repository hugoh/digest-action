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

<!-- AUTO-DOC-INPUT:START - Do not remove or modify this section -->

|       INPUT       | REQUIRED |              DEFAULT               |                                                                                                                                         DESCRIPTION                                                                                                                                         |
|-------------------|----------|------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|    closed-days    |  false   |               `"7"`                |                                                                                                                    How many days back to look for closed PRs and issues                                                                                                                     |
| digest-from-email |  false   |                                    |                                                                                                                  Envelope From address. Required when send-email is true.                                                                                                                   |
|  digest-to-email  |  false   |                                    |                                                                                                                    Recipient address. Required when send-email is true.                                                                                                                     |
|   github-token    |   true   |                                    | Token gh (and so `gh auth token`) should use -- needs read access to every targeted repo, and to list repos for `owner` account-wide, which GITHUB_TOKEN cannot do outside its own repo. Passed through as GH_TOKEN. A PAT is required; see the Token section of the README for the scopes. |
|       only        |  false   |                                    |                                                                                                         Comma-separated repo names to limit to (default -- every non-archived repo)                                                                                                         |
|     open-days     |  false   |              `"365"`               |                                                                                                                  How many days back to look for still-open PRs and issues                                                                                                                   |
|        out        |  false   |                                    |                                          Path (relative to the runner's workspace) to also write the rendered HTML to. Sets the html output. Required if send-email is false, since otherwise the digest would be built and immediately discarded.                                          |
|       owner       |  false   | `"${{ github.repository_owner }}"` |                                                                                                            GitHub account/org to report on (default -- whoever runs the action)                                                                                                             |
|   release-days    |  false   |               `"7"`                |                                                                                                                      How many days back to look for published releases                                                                                                                      |
|    send-email     |  false   |              `"true"`              |                                                                                                         Whether to email the digest -- requires the smtp-* / digest-*-email inputs                                                                                                          |
|       skip        |  false   |                                    |                                                                                                                            Comma-separated repo names to exclude                                                                                                                            |
|     smtp-host     |  false   |                                    |                                                                                                                     SMTP relay host. Required when send-email is true.                                                                                                                      |
|   smtp-password   |  false   |                                    |                                                                                                            SMTP password -- pass as a secret. Required when send-email is true.                                                                                                             |
|     smtp-port     |  false   |                                    |                                            SMTP relay port. Required when send-email is true. The relay is reached over STARTTLS with cert and hostname verification, so use a submission port (typically 587), not an implicit-TLS port (465).                                             |
|   smtp-username   |  false   |                                    |                                                                                                                      SMTP username. Required when send-email is true.                                                                                                                       |
|     star-days     |  false   |              `"7,30"`              |                                                                                     Comma-separated windows (in days) for counting recently-gained stars -- one column per value in the Stars section.                                                                                      |
|     star-top      |  false   |               `"10"`               |                                                                                                  Always show this many most-starred repos in the Stars section, even with no recent gain.                                                                                                   |
|    uv-version     |  false   |                                    |                                                                                       uv version to install (e.g. 0.5.0, latest, latest-known). Defaults to the version in pyproject.toml, or latest.                                                                                       |

<!-- AUTO-DOC-INPUT:END -->

The `smtp-*` and `digest-*-email` inputs are required when `send-email` is
`true` (the default).

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

## Outputs

<!-- AUTO-DOC-OUTPUT:START - Do not remove or modify this section -->

| OUTPUT |                          DESCRIPTION                          |
|--------|---------------------------------------------------------------|
|  html  | Path to the rendered HTML file (set only when `out` is given) |

<!-- AUTO-DOC-OUTPUT:END -->

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
