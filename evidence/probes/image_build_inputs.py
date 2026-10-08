#!/usr/bin/env python3
"""What the image's mutable build inputs resolved to.

    CONTAINER_CLI=podman python3 evidence/probes/image_build_inputs.py <image> --packages <out.txt>

The Dockerfile names its bases by tag (`node:24-slim`, `python:3.13-slim`)
and runs `apt-get upgrade`: all three resolve when the image is built, so the
commit alone does not say what went in. This reads, from the local engine:

* the image's ID, manifest digest, platform and revision label;
* each base tag's ID, digest and repository digests as resolved locally, and
  whether the image's lower layers are exactly the Python base's layers (the
  Node stage leaves no layer in the image: only its build output is copied);
* the Debian packages installed in the image (`dpkg-query`), written to
  --packages, with their count and SHA-256: the result of `apt-get upgrade`.

Read-only: it inspects images and runs dpkg-query in a throwaway container.
Prints one JSON document; exits 1 if the image is missing or its layers do
not start with the Python base's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys

CLI = os.environ.get("CONTAINER_CLI", "docker")
BASES = {"web stage": "docker.io/library/node:24-slim",
         "runtime": "docker.io/library/python:3.13-slim"}


def inspect(ref: str) -> dict:
    out = subprocess.run([CLI, "image", "inspect", ref], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"{ref}: not in the local engine")
    return json.loads(out.stdout)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--packages", type=pathlib.Path, required=True)
    args = ap.parse_args()
    img = inspect(args.image)
    layers = img.get("RootFS", {}).get("Layers") or []
    result: dict = {
        "image": {"ref": args.image, "id": img.get("Id"), "digest": img.get("Digest"),
                  "platform": f"{img.get('Os')}/{img.get('Architecture')}",
                  "revision_label": (img.get("Labels") or {}).get("org.opencontainers.image.revision"),
                  "layers": len(layers)},
        "bases": {},
    }
    for role, ref in BASES.items():
        b = inspect(ref)
        base_layers = b.get("RootFS", {}).get("Layers") or []
        entry = {"tag": ref, "id": b.get("Id"), "digest": b.get("Digest"),
                 "repo_digests": b.get("RepoDigests"),
                 "platform": f"{b.get('Os')}/{b.get('Architecture')}", "created": b.get("Created")}
        if role == "runtime":
            entry["image_layers_start_with_it"] = layers[:len(base_layers)] == base_layers
        result["bases"][role] = entry
    q = subprocess.run([CLI, "run", "--rm", "--entrypoint", "dpkg-query", args.image,
                        "-W", "-f", "${Package} ${Version} ${Architecture}\n"],
                       capture_output=True, text=True)
    if q.returncode != 0:
        raise SystemExit(f"dpkg-query failed: {q.stderr[-500:]}")
    packages = "".join(sorted(q.stdout.splitlines(keepends=True)))
    args.packages.write_text(packages)
    result["debian_packages"] = {"count": len(packages.splitlines()),
                                 "sha256": hashlib.sha256(packages.encode()).hexdigest(),
                                 "list": str(args.packages)}
    print(json.dumps(result, indent=1))
    return 0 if result["bases"]["runtime"]["image_layers_start_with_it"] else 1


if __name__ == "__main__":
    sys.exit(main())
