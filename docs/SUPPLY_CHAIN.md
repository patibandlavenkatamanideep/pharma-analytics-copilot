# Supply chain

What goes into the image, how it is pinned, how it is scanned, and what
happens when a scan finds something.

## Pinned and reproducible

| Input | Pinned by |
|---|---|
| Python packages, direct **and** transitive | `requirements.lock`: every package at an exact version with its SHA-256 hashes, generated from `requirements.txt` by `uv pip compile --universal --generate-hashes`. The image installs it with `pip install --require-hashes`, so a changed artifact on the index fails the build instead of entering it |
| JavaScript packages | `web/package-lock.json`, installed with `npm ci` |
| Base images | `python:3.13-slim`, `node:20-slim`, `postgres:16-alpine` by tag. **Not** by digest yet, so a rebuild picks up the tag's current patch release. That is deliberate (OS security fixes arrive that way), and the image scan below catches regressions |
| The build context | `scripts/image_smoke.sh` builds from `git archive` of the commit, so untracked files (`.env`, local dumps) cannot enter the image |
| The application | The image records the commit it was built from: `PAC_RELEASE` build argument, the OCI `org.opencontainers.image.revision` label, `/health`, and every telemetry span. CI checks that the running container reports the commit under test |

`requirements.txt` stays the human-edited list of direct dependencies. To
change a dependency, edit it, then regenerate:

```bash
uv pip compile requirements.txt --universal --generate-hashes \
    --python-version 3.13 -o requirements.lock
```

## Scanned

| Scan | Where | Fails on |
|---|---|---|
| Python dependencies (`pip-audit`, against the hashed lock) | CI `supply-chain` job | any known vulnerability |
| JavaScript dependencies (`npm audit --omit=dev`) | CI `supply-chain` job | high or critical |
| Secrets (`gitleaks`, every commit in history) | CI `supply-chain` job | any finding |
| Image: OS packages and the Python environment (`trivy`) | CI `image` job | high or critical **with a fixed version available** |

The image gate is the policy, and all a passing scan establishes. Locally,
`scripts/image_smoke.sh` also runs trivy unfiltered over the same image with
the same database (every severity, fixed or not; not a gate) and records the
database's own dates (`TRIVY_FULL_REPORT`, `TRIVY_DB_REPORT`), so a release
manifest can state complete counts and when the advisories were current. Every
HIGH finding is triaged in [VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md):
advisory, packages, reachability in this image, mitigation, owner and expiry.

**Reviewed exceptions.** `.gitleaksignore` lists exactly two findings, by
commit, file, rule and line: commit `58b3d3e`,
`evidence/runs/r3-final-trivy-amd64.json` lines 50 and 201, rule
`generic-api-key`. Both are the public GPG fingerprint of the Python release
key, quoted in trivy's metadata about the base image: not a secret. There is
no file-wide or rule-wide exemption ([REVIEW_2026_10_06.md](REVIEW_2026_10_06.md)).
pip-audit, npm audit and trivy have none.

What was run locally, and its result, is recorded:

- `evidence/runs/r2-supply-chain.json`: pip-audit, npm audit and gitleaks.
- `evidence/runs/r2-image-smoke.json`: the image, built from the committed
  tree with podman, tested end to end and scanned with trivy.

The hosted CI run of these jobs has not happened: nothing is pushed from
this branch.

### Two scanners, because they disagree

On 2026-10-01 pip-audit reported the Python dependencies clean. Trivy, run
on the built image, found 11 HIGH vulnerabilities with fixes available:

- **Five in the Debian base:** openssl (two CVEs across `libssl3t64`,
  `openssl` and `openssl-provider-legacy`) and libpcre2. The image now
  applies Debian security updates on top of `python:3.13-slim`.
- **Four in packages vendored inside the base image's own pip:** urllib3
  2.7.0 (two CVEs), msgpack 1.1.2, and `pkg_resources` (reported as
  setuptools 70.3.0). The application's own urllib3 was already 2.8.0.
  Nothing runs pip after the build, so the image uninstalls it.

After both changes, trivy reports none. pip-audit reads only the lock,
against PyPI and OSV advisories. Trivy reads the whole filesystem against
its own database. Each found what the other could not, so both run.

## When a scan finds something

| Finding | Action | By when |
|---|---|---|
| A credential in the repository or its history | Rotate the credential first, then remove it from the tree. Rewriting published history is a separate decision for the repository owner | Immediately; blocks release |
| Critical or high vulnerability, fix available | Upgrade, regenerate the lock, re-run the suites | Before the next release; it blocks CI |
| Critical or high, no fix available | Record an exception below: the package, the advisory, why the vulnerable path is or is not reachable here, and a review date at most 30 days out | Within 7 days |
| Medium or low | Upgrade with the next dependency update | Next release |

A finding is never silenced by lowering a threshold or by excluding the
package from the scan. Exceptions are listed here, with a date, where a
reviewer can see them.

### Current exceptions

None.
