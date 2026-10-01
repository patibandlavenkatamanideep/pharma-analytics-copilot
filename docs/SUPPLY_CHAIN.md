# Supply chain

What goes into the image, how it is pinned, how it is scanned, and what
happens when a scan finds something.

## Pinned and reproducible

| Input | Pinned by |
|---|---|
| Python packages, direct **and** transitive | `requirements.lock`: every package at an exact version with its SHA-256 hashes, generated from `requirements.txt` by `uv pip compile --universal --generate-hashes`. The image installs it with `pip install --require-hashes`, so a changed artifact on the index fails the build instead of entering it |
| JavaScript packages | `web/package-lock.json`, installed with `npm ci` |
| Base images | `python:3.13-slim`, `node:20-slim`, `postgres:16-alpine` by tag. **Not** by digest yet, so a rebuild picks up the tag's current patch release. That is deliberate (OS security fixes arrive that way), and the image scan below catches regressions |
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

What was run locally, and its result, is recorded in
`evidence/runs/r2-supply-chain.json`. The hosted CI run of these jobs has
not happened: nothing is pushed from this branch.

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
