# Self-upgrade pipeline

The pipeline turns one scoped issue into a tested pull request on an `ai/*`
branch for human review. It never merges and never writes to `main`.
Code: `omnidesk_agent/self_upgrade/pipeline/`; workflow:
`.github/workflows/self-upgrade-pipeline.yml`.

```text
issue labelled self-upgrade (a maintainer applies the label)
  -> propose   model writes a unified diff            [model key; patch never runs]
  -> verify    isolated worktree: reproduce, apply,    [no secrets; read-only token]
               targeted tests, gates, full suite
  -> publish   re-check, apply (no run), create        [short-lived App token]
               ai/* branch + signed commit + draft PR
  -> review    CI (all required checks, plus the agent-repair-pr re-checks), code-owner approval, merge queue
```

## Steps

**propose** (`proposer.py`): the model gets the issue, the files under the
issue's `scope:` paths (60 000 characters at most) and must answer with one
diff between `BEGIN_DIFF` and `END_DIFF`. The answer is data. It is not run.

**diff policy** (`diff_policy.py`), checked in all three jobs: plain text edits
only (no deletions, renames, mode changes, binaries, path traversal; the
`---`/`+++` names must be the file the `diff --git` header names), at most
10 files and 400 changed lines, inside the issue's scope plus `tests/`, and
never in `.github/`, `scripts/`, `deploy/`, `infra/`, `release/`, `packaging/`,
`omnidesk_agent/security|sandbox|plugins|api|self_upgrade|self_healing/`,
`server.py`, `server_routes/`, `config.py`, `AGENTS.md`, `CODEOWNERS`,
dependency manifests and lock files, Dockerfiles. Those hold the policy,
approval, CI, signing and dependency surfaces AGENTS.md forbids AI changes to;
a change there needs a human. The pipeline cannot change itself.

**verify** (`workspace.py`, `verifier.py`): a fresh `git worktree` of the
base commit; commands get only `PATH`, `HOME`, locale and `PYTHONPATH`. It
passes only if the issue's named failing tests fail before the change, the
diff applies and changes exactly its declared files (otherwise it stops before running anything), at least one test covers
the change and the targeted tests pass with `-W error`, `git diff --check`,
ruff, bandit (medium and up) and compileall pass on the changed files, and
the full suite passes. The result is `evidence.json`
(`omnidesk-self-upgrade-evidence/v1`) with every command, exit code and
duration, and the patch SHA-256.

**publish** (`publisher.py`): refuses unless the evidence passed, includes a
passing full-suite run, and its patch hash matches. It applies the patch in a fresh worktree without running
anything, then: create `ai/self-upgrade-<issue>-<slug>-<yyyymmdd>-<run id>` (unique per attempt) at the base commit,
add one commit through GraphQL `createCommitOnBranch` (signed by GitHub,
attributed to the App), open a **draft** PR into `main` with the evidence
summary, risk and rollback notes and a machine-readable list of the targeted
tests. The client has no merge, delete or update-`main` call, rejects any
branch that is not a plain `ai/...` name (`ai/x:main`, `refs/heads/...`,
`+...` are refused) and only talks to `api.github.com`.

**review**: `agent-repair-pr.yml` independently re-checks every PR whose
branch has the pipeline's exact name format (`is_pipeline_branch`; a human
branch named `ai/<topic>-<yyyymmdd>` is not one, and the pipeline refuses to
publish a name the check would not recognise): the changed files against the
same restricted paths, and the targeted tests listed in the PR body. All required checks, a
code-owner approval and the merge queue still apply.

## Credential design (needs owner approval before any token is wired)

AGENTS.md: no token-scope, secret or network expansion without owner approval
and tests. This is the proposal; nothing is wired until the owner approves it.

**GitHub: a dedicated GitHub App**, installed on `Omniorganization/omni-desk-ai` only.

| permission | level | why |
|---|---|---|
| Contents | Read and write | create the `ai/*` branch and its commit |
| Pull requests | Read and write | open the draft PR and label it |
| Metadata | Read | mandatory for every App |
| everything else | none | no workflows, actions, administration, secrets, environments, members |

- The workflow mints a one-hour installation token per run with
  `actions/create-github-app-token` (pinned to v3.3.0's commit), narrowed again
  to this repository and these two permissions.
- What limits it: GitHub's permission model cannot restrict Contents write to
  `ai/*` names. The `main` ruleset (pull request required, signed commits,
  code-owner review, last-push approval, no bypass actors) blocks direct
  writes to `main`, and the publisher only writes `ai/*` branches. An optional
  extra, owner's choice and not done here: a branch ruleset that restricts
  creating branches outside `ai/**` for the App.
- The App cannot approve its own PR (GitHub forbids author approval) and is
  not a code owner, so its approval would not count elsewhere either.
- PRs created with an App token trigger CI, unlike `GITHUB_TOKEN` PRs.
- Why not a personal access token: a fine-grained PAT has the same
  branch-name limitation, lives up to a year, and acts as a person.
- Rollback: uninstall the App or revoke its private key; nothing else holds it.

**Model**: the user's model API key, as the environment secret
`OMNIDESK_UPGRADE_MODEL_API_KEY`. Only the `propose` job reads it; it is sent
only to the provider's API by the provider adapter and never logged.

**Where they live**: a GitHub environment `self-upgrade` (deployment branches:
`main` only; optionally required reviewers, which adds a manual approval per
run). `propose` and `publish` use it; `verify` does not, so code under test
can never read a secret.

| name | kind | set by |
|---|---|---|
| `OMNIDESK_UPGRADE_APP_ID` | environment variable | owner |
| `OMNIDESK_UPGRADE_APP_PRIVATE_KEY` | environment secret | owner: `gh secret set OMNIDESK_UPGRADE_APP_PRIVATE_KEY --env self-upgrade < key.pem` |
| `OMNIDESK_UPGRADE_MODEL_API_KEY` | environment secret | owner: `gh secret set OMNIDESK_UPGRADE_MODEL_API_KEY --env self-upgrade` |

## Threat model (short)

- The repository is public, so anyone can open an issue; only maintainers can
  label it `self-upgrade` or dispatch the workflow. Issue text and repository
  files reach the model as data; a prompt-injected model can at worst produce
  a bad diff, which the diff policy, the tests and human review stop.
- Model code runs only in `verify`, which has no secret and a read-only token
  on a public repository. It could tamper with its own `evidence.json`; that
  only changes what the draft PR claims, and CI re-runs everything.
- Self-healing repair requests (`<workspace>/repair_requests/*.json`) become
  issues through a maintainer, not automatically.

## Running it

1. Owner: approve the design above, create the App, the environment, the
   variable and the two secrets.
2. Open an issue with the `self-upgrade` label. Optional lines in the body:
   `scope: omnidesk_agent/some/package/` and
   `tests: tests/test_x.py::test_that_fails`.
3. Actions -> Self Upgrade Pipeline -> Run workflow (issue number, provider, model).

Locally, the same steps run with
`python -m omnidesk_agent.self_upgrade.pipeline propose|verify|publish`.

## Status

The workflow does not reference any secret yet. Its credential steps (the
model key in `propose`, the App token in `publish`) are added in a follow-up
change after the owner approves this design; until then `propose` stops with
an error and `publish` only re-checks the evidence (`--dry-run`). No real
pipeline run and no real self-upgrade PR exist yet.
