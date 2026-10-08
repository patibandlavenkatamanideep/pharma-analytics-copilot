# Release handoff — `codex/release-defects-oct06`

For an independent reviewer: what the candidate is, how CI would check it, how to push
it when that is authorized, and how to inspect it offline from the bundle. Nothing in
this document has been pushed, dispatched, published or deployed. The concrete external
steps, with the inputs each needs, are in [EXTERNAL_VERIFICATION_PLAN.md](EXTERNAL_VERIFICATION_PLAN.md);
the image's vulnerabilities are triaged in [VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md).

## Where things stand

| | |
|---|---|
| Public GitHub | `codex/release-defects-oct06` at `69c62de`; hosted CI run [37653591687](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37653591687) passed on it (`evidence/external.json`). `main` at `c8aab5b` |
| Local branch | unpushed beyond `69c62de`: 84 commits up to the executable candidate, then evidence and documentation commits (`git rev-list --count github/codex/release-defects-oct06..HEAD`). A push was attempted on 8 October 2026 at the owner's request and refused by the working session's permission controls; nothing was pushed |
| Executable candidate | **`e551650`**: the last commit that changes code, tests, configuration, dependencies, fixtures, workflow or build inputs. Every local gate, the image (`219ddd92…`) and the scans ran on it from a clean tree and fresh databases (`r5-rc-e551650-*.json`, `r5-manifest-e551650.json`), and the restore drill and upgrade compatibility on the same commit. It supersedes `a9de92e` with provisioning by a restricted (RDS-style) administrator, the image run hardened with the RDS CA bundle and one writable volume, sign-out's cookie attributes, the AWS staging module and the publishing workflow ([AWS_STAGING.md](AWS_STAGING.md)); `a9de92e`'s and `84dfc1e`'s records stay as history |
| Final HEAD | the candidate plus evidence, documentation and exact secret-scan exceptions in `.gitleaksignore` for the candidates' own trivy reports (the public CPython key). The release manifest lists the delta and classes each path; `.gitleaksignore` is the one input in it, it affects only the secret scan, and that scan is rerun over the whole history at HEAD |
| Hosted CI for the local commits | **Pending**: needs a push. `main` has **no branch protection** and no rulesets (read-only API, 8 October 2026), so nothing yet enforces the checks below before a merge |

## The CI path

`.github/workflows/ci.yml` runs on a push to `main`, `post-assessment/**`,
`production/nl2sql-readiness` or `codex/**` (so on this branch), on any pull request, and
on `workflow_dispatch`. Four jobs, none conditional on branch or path:

| Job | What fails it |
|---|---|
| `test` | the boundary check; the security suite under `--release-gate` with a floor of 436; ingestion under `--release-gate` (floor 176); **the full suite under `--release-gate`** (since `03316f3`: before it, a step that passed with its tests skipped, `evidence/probes/ci_full_suite_gate.py`); the regression question set; the release gate's own tests; the browser journeys. The two held-out sets are measurements (`continue-on-error`), by design |
| `frontend` | the component suite below its floor (32), any skipped or failed |
| `supply-chain` | `pip-audit` on the hashed lock, `npm audit --omit=dev --audit-level=high`, gitleaks over the history |
| `image` | the amd64 build (`push: false`: no registry), trivy at HIGH and CRITICAL with a fix available (`ignore-unfixed`, exit code 1; the complete counts are triaged in VULNERABILITY_TRIAGE.md), no setuid or setgid file (since `a9de92e`), and the image's journeys on a fresh database with a read-only root, every capability dropped and `no-new-privileges` (since `b4315bb`) |

The repository does not yet require these checks before a merge (no branch protection
on `main`); EXTERNAL_VERIFICATION_PLAN.md §2 gives the settings to apply.

## Pushing, when authorized (not done here)

```bash
git fetch github
git merge-base --is-ancestor github/codex/release-defects-oct06 HEAD   # fast-forward only
git push github codex/release-defects-oct06                             # never --force
gh run watch --repo patibandlavenkatamanideep/pharma-analytics-copilot
```

Then record the run in `evidence/external.json` (run id, head SHA, conclusion), and from
the image job's log its image id and the revision label. A CI-built image will not share
the local image's id (another builder, another host); compare the revision label with the
SHA. A pull request to `main`, and any merge, need their own authorization.

## Inspecting offline

The bundle, its SHA-256 and a short inventory are delivered side by side, outside the
repository: a file inside the bundle cannot carry the bundle's own checksum. The release
manifest inside it (`evidence/runs/r5-manifest-e551650.json`) identifies the candidate, its
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
| Hosted CI on the local commits | authorization to push | the procedure above |
| Branch protection requiring the four jobs | repository settings access | require `test`, `frontend`, `supply-chain`, `image` on `main` |
| Live model evaluation of prompt 2.3.0 | credentials, an approved spend cap, the contracted rates | smoke, regression sets, then an independently written holdout frozen before its first run (`evals/packet/AUTHORING.md`, `PROTOCOL.md`) |
| Single sign-on with a real provider | an IdP registration (issuer, client, redirect URI) | RUNBOOK.md, "Verifying with a real provider" |
| A real source feed | representative records, control totals, classification provenance, source keys, deletion semantics, schedule | REAL_FEED_ACCEPTANCE.md |
| Staging | an AWS account and region, a budget, a hostname, the testers' networks, and authorization to plan, apply and deploy | infra/aws-staging/README.md ("Create"), then AWS_STAGING.md's hosted checks and STAGING_VERIFICATION.md: hosted collector and backend, two replicas, forward-fix and restore drills, managed database recovery |
| Owner decisions | the product, data and compliance owners | TARGETS_DECISION.md (service, freshness, recovery), AUDIT_DECISION.md (audit mode), retention and residency |
| Publishing the release image | the staging foundation, the `staging` environment, and authorization to publish | `.github/workflows/publish-staging.yml` (EXTERNAL_VERIFICATION_PLAN.md §3): build from the pushed commit, scan, run the journeys, push, record the registry digest |
| A named release owner | the deploying team | owns the triage in VULNERABILITY_TRIAGE.md, which expires on 2026-11-07 or at the next rebuild |
