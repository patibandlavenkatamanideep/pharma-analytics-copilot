# Release handoff — `codex/release-defects-oct06`

For an independent reviewer: what the candidate is, how CI would check it, how to push
it when that is authorized, and how to inspect it offline from the bundle. Nothing in
this document has been pushed, dispatched, published or deployed. The concrete external
steps, with the inputs each needs, are in [EXTERNAL_VERIFICATION_PLAN.md](EXTERNAL_VERIFICATION_PLAN.md);
the image's vulnerabilities are triaged in [VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md).

## Where things stand

| | |
|---|---|
| Public GitHub | `codex/release-defects-oct06` pushed (normally; never forced) to its head. Hosted CI run [37869897240](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37869897240) passed all four jobs on the executable candidate. `main` at `c8aab5b` |
| Pull request | [#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1), the release integration into `main` (a fast-forward), open for review; not merged |
| Executable candidate | **`e101db3`**: the last commit that changes code, tests, configuration, dependencies, fixtures, workflow or build inputs. Every local gate, the image (`68178125…`) and the scans ran on it from a clean tree and fresh databases (`r5-rc-e101db3-*.json`, `r5-manifest-e101db3.json`), as did the restore drill, upgrade compatibility and the mocked Terraform plans. It supersedes `e551650` with sign-out that waits for the server and never presents an unconfirmed sign-out as done, CI defects found by hosted runs, browser-failure evidence, a gated publishing workflow and the staging lifecycle design ([AWS_STAGING.md](AWS_STAGING.md)); earlier candidates' records stay as history |
| Final HEAD | the candidate plus evidence, documentation and exact secret-scan exceptions in `.gitleaksignore` for the candidates' own trivy reports (the public CPython key). The release manifest lists the delta and classes each path |
| Qualifications | The first AWS deployment ([STAGING_PLAN.md](STAGING_PLAN.md)) uses synthetic data and the offline planner: it verifies AWS infrastructure, not live NL2SQL and not a pilot; live-model and pilot qualification are separate. `keep-*` images are retained up to 9,999 of them, not forever, to be confirmed by an ECR lifecycle preview |
| Branch protection | `main` protected since 9 October: pull request, the four jobs below from GitHub Actions and up to date, administrators included, no force-push or deletion (read back, `evidence/external.json`) |

## The CI path

`.github/workflows/ci.yml` runs on a push to `main`, `post-assessment/**`,
`production/nl2sql-readiness` or `codex/**` (so on this branch), on any pull request, and
on `workflow_dispatch`. Four jobs, none conditional on branch or path:

| Job | What fails it |
|---|---|
| `test` | the boundary check; the security suite under `--release-gate` with a floor of 438; ingestion under `--release-gate` (floor 176); **the full suite under `--release-gate`** (since `03316f3`: before it, a step that passed with its tests skipped, `evidence/probes/ci_full_suite_gate.py`); the regression question set; the release gate's own tests; the browser journeys. The two held-out sets are measurements (`continue-on-error`), by design |
| `frontend` | the component suite below its floor (32), any skipped or failed |
| `supply-chain` | `pip-audit` on the hashed lock, `npm audit --omit=dev --audit-level=high`, gitleaks over the history |
| `image` | the amd64 build (`push: false`: no registry), trivy at HIGH and CRITICAL with a fix available (`ignore-unfixed`, exit code 1; the complete counts are triaged in VULNERABILITY_TRIAGE.md), no setuid or setgid file (since `a9de92e`), and the image's journeys on a fresh database with a read-only root, every capability dropped and `no-new-privileges` (since `b4315bb`) |

`main` requires all four before a merge (branch protection, 9 October).

## Pushing (done 9 October, as authorised)

```bash
git fetch github
git merge-base --is-ancestor github/codex/release-defects-oct06 HEAD   # fast-forward only
git push github codex/release-defects-oct06                             # never --force
gh run watch --repo patibandlavenkatamanideep/pharma-analytics-copilot
```

Then record the run in `evidence/external.json` (run id, head SHA, conclusion), and from
the image job's log its image id and the revision label. A CI-built image will not share
the local image's id (another builder, another host); compare the revision label with the
SHA. Any merge into `main` needs the owner's approval; pull request #1 is prepared for it.

## Inspecting offline

The bundle, its SHA-256 and a short inventory are delivered side by side, outside the
repository: a file inside the bundle cannot carry the bundle's own checksum. The release
manifest inside it (`evidence/runs/r5-manifest-e101db3.json`) identifies the candidate, its
records, the image and the scans.

```bash
shasum -a 256 -c pharma-analytics-copilot-codex-release-defects-oct06.bundle.sha256
git bundle verify pharma-analytics-copilot-codex-release-defects-oct06.bundle
git clone -b codex/release-defects-oct06 pharma-analytics-copilot-codex-release-defects-oct06.bundle pac
```

Targeted checks need only PostgreSQL 16 and Python 3.13 (RUNBOOK.md §1). The evidence
index (`docs/EVIDENCE_INDEX.md`, generated by `scripts/evidence_index.py`) names each
record, its command and the commit it measured.

## Deferred checklist

| | Needs | Then |
|---|---|---|
| Merging pull request #1 | the owner's review | makes `publish-staging` dispatchable; `main` then carries the release |
| Live model evaluation of prompt 2.3.0 | credentials, an approved spend cap, the contracted rates | smoke, regression sets, then an independently written holdout frozen before its first run (`evals/packet/AUTHORING.md`, `PROTOCOL.md`) |
| Single sign-on with a real provider | an IdP registration (issuer, client, redirect URI) | RUNBOOK.md, "Verifying with a real provider" |
| A real source feed | representative records, control totals, classification provenance, source keys, deletion semantics, schedule | REAL_FEED_ACCEPTANCE.md |
| Staging | the inputs in STAGING_PLAN.md and approval of that plan and its spend | infra/aws-staging/README.md ("Create"), then AWS_STAGING.md's hosted checks and STAGING_VERIFICATION.md: hosted collector and backend, two replicas, forward-fix and restore drills, managed database recovery |
| Owner decisions | the product, data and compliance owners | TARGETS_DECISION.md (service, freshness, recovery), AUDIT_DECISION.md (audit mode), retention and residency |
| Publishing the release image | #1 merged, the staging foundation, and the approved plan | `.github/workflows/publish-staging.yml` (EXTERNAL_VERIFICATION_PLAN.md §3): build from the pushed commit, scan, run the journeys, push, record the registry digest |
| A named release owner | the deploying team | owns the triage in VULNERABILITY_TRIAGE.md, which expires on 2026-11-07 or at the next rebuild |
