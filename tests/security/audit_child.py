"""One request in a process that dies at a chosen point around the commit.

    python -m tests.security.audit_child <user_id> <idempotency_key> <mode> <point>

Used by tests/security/test_audit_modes.py. `mode` is the audit mode
(best_effort | strict); `point` is where the process exits, with no cleanup,
as a killed worker would:

* before_commit -- the answer is computed and the commit has not started;
* inside_commit -- the turn and (strict) the audit row are written and the
                   transaction is open;
* after_commit  -- the transaction committed and the response was never sent.

Exit codes 70, 71, 72 name the point reached. The run's lease is one second,
so the parent can retry the same key right after.
"""

from __future__ import annotations

import os
import sys

QUESTION = "What is our total volume this quarter?"
EXIT = {"before_commit": 70, "inside_commit": 71, "after_commit": 72}


def main() -> None:
    user_id, key, mode, point = sys.argv[1:5]
    os.environ["PAC_AUDIT_MODE"] = mode
    os.environ["PAC_RUN_LEASE_SECONDS"] = "1"
    from app.config import get_settings
    get_settings.cache_clear()

    from app import pipeline as P
    from app.auth.policy import principal_for_user_id
    from app.llm.planner import OfflinePlanner

    real = P.finalise

    def finalise(*args, **kwargs):
        if point == "before_commit":
            os._exit(EXIT[point])
        if point == "inside_commit":
            inner = kwargs.get("audit")

            def dying(cur):
                if inner is not None:
                    inner(cur)
                os._exit(EXIT[point])
            kwargs["audit"] = dying
        out = real(*args, **kwargs)
        if point == "after_commit":
            os._exit(EXIT[point])
        return out

    P.finalise = finalise
    P.Pipeline(OfflinePlanner()).ask(principal_for_user_id(user_id), QUESTION,
                                     idempotency_key=key)
    os._exit(0)          # the death point was never reached


if __name__ == "__main__":
    main()
