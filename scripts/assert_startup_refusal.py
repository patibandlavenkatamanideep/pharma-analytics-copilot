"""Shared image/CI refusal gate: a reason alone cannot prove failed startup."""
import json
import pathlib
import sys


def assert_refusal(log: str, returncode: int, reason: str) -> None:
    # Uvicorn's lifespan failure exits 3. Timeout, kill, shell/engine failure,
    # successful exit and unexpected serving all fail this check.
    if returncode != 3:
        raise AssertionError(f"expected uvicorn exit 3, got {returncode}")
    records = []
    for line in log.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    if any(r.get('event') == 'server.started' or
           r.get('event') == 'Application startup complete.' for r in records):
        raise AssertionError('application started unexpectedly')
    if not any(r.get('event') == 'startup.refused' and r.get('reason') == reason
               for r in records):
        raise AssertionError('expected structured startup refusal missing')


if __name__ == '__main__':
    assert_refusal(pathlib.Path(sys.argv[1]).read_text(), int(sys.argv[2]), sys.argv[3])
