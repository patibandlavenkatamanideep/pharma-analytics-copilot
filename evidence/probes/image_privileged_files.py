#!/usr/bin/env python3
"""Does the image carry setuid or setgid files?

    CONTAINER_CLI=podman python3 evidence/probes/image_privileged_files.py <image>

The application runs as an unprivileged user and never mounts, switches user
or changes a password. A setuid-root `mount`, `umount`, `su` or `newgrp` --
util-linux binaries with open HIGH advisories in the image scan -- is a way
from a compromised application process to root that the application does not
need. Lists every setuid or setgid regular file on the image's filesystem.

Read-only: runs `find` in a throwaway container. Exit 0 when there is none.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

CLI = os.environ.get("CONTAINER_CLI", "docker")


def main() -> int:
    image = sys.argv[1]
    r = subprocess.run([CLI, "run", "--rm", "--entrypoint", "find", image, "/", "-xdev",
                        "-type", "f", "(", "-perm", "-4000", "-o", "-perm", "-2000", ")"],
                       capture_output=True, text=True)
    files = sorted(line for line in r.stdout.splitlines() if line.startswith("/"))
    print(json.dumps({"image": image, "setuid_or_setgid_files": files, "count": len(files)}))
    return 0 if not files else 1


if __name__ == "__main__":
    sys.exit(main())
