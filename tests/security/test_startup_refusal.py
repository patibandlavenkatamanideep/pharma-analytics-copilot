"""Run real uvicorn startup; never accept a timeout as refusal."""
import os
import subprocess
import sys
import pytest
from scripts.assert_startup_refusal import assert_refusal
from tests.security.conftest import AUTHTEST_DB

pytestmark = pytest.mark.security

@pytest.mark.parametrize('reason', ['database_unreachable', 'owner_credential_present'])
def test_actual_startup_refuses_with_structured_reason(authtest_db, reason):
    env = {**os.environ, 'PAC_DB_NAME': AUTHTEST_DB, 'PAC_LLM_PROVIDER': 'offline',
           'PAC_ENVIRONMENT': 'cloud', 'PAC_OTEL_ENDPOINT': ''}
    if reason == 'database_unreachable':
        env['PAC_DB_PORT'] = '1'
    result = subprocess.run([sys.executable, '-m', 'uvicorn', 'app.api.main:app',
        '--log-config', 'app/log_config.json', '--host', '127.0.0.1', '--port', '0'],
        env=env, capture_output=True, text=True, timeout=25)
    assert_refusal(result.stdout + result.stderr, result.returncode, reason)
