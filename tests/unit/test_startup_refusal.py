import json
import pytest
from scripts.assert_startup_refusal import assert_refusal

LOG = json.dumps({'event': 'startup.refused', 'reason': 'database_unreachable'})

@pytest.mark.parametrize('code', [0, 1, 124, 125, 126, 127, 137, -15])
def test_timeout_or_unexpected_exit_never_passes(code):
    with pytest.raises(AssertionError):
        assert_refusal(LOG, code, 'database_unreachable')

def test_reason_and_failed_process_are_both_required():
    assert_refusal(LOG, 3, 'database_unreachable')
    for log in ['database_unreachable', '{}', LOG + '\n' + json.dumps({'event': 'server.started'})]:
        with pytest.raises(AssertionError):
            assert_refusal(log, 3, 'database_unreachable')
    with pytest.raises(AssertionError):
        assert_refusal(LOG, 3, 'owner_credential_present')
