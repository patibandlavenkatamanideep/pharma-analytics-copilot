#!/usr/bin/env python3
"""Freeze a holdout set before it is ever evaluated.

    python3 scripts/freeze_holdout.py evals/holdout3.yaml

Records the file's SHA-256 -- questions AND expected answers -- with the date
in evals/frozen.json. Commit that file before the first run: the commit is
the evidence that the expectations predate the answers. scripts/run_evals.py
refuses to run a set with `status: "holdout"` unless it matches its frozen
hash, so a holdout edited after freezing cannot be passed off as the same
set. A set is frozen once; a changed set is a new set with a new name.

Once a holdout has been run and anything is learned from it, change its
status to "spent". It then measures regression, not generalisation.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
FROZEN = ROOT / "evals" / "frozen.json"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    spec = yaml.safe_load(path.read_text())
    if spec.get("status") != "holdout":
        print(f"{path.name} has status {spec.get('status')!r}; only a holdout is frozen",
              file=sys.stderr)
        return 2
    frozen = json.loads(FROZEN.read_text()) if FROZEN.exists() else {}
    if path.name in frozen:
        print(f"{path.name} was frozen on {frozen[path.name]['frozen_at']}; a changed set "
              "needs a new name", file=sys.stderr)
        return 2
    import hashlib
    frozen[path.name] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "version": spec.get("version"),
        "questions": len(spec.get("questions") or []),
    }
    FROZEN.write_text(json.dumps(frozen, indent=2) + "\n")
    print(f"frozen {path.name}: {frozen[path.name]['sha256'][:16]}; commit evals/frozen.json "
          "before the first run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
