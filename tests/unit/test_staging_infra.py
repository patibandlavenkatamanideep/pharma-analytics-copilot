"""The staging infrastructure's security properties, read from its source.

infra/aws-staging has never been planned against an account (that needs AWS
credentials), so these read the Terraform text: what a change would have to
remove to open the database, run a container with privileges, put a
password in plain environment or let CI deploy. `terraform validate` and the
static scan are recorded separately (docs/AWS_STAGING.md).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra" / "aws-staging"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-staging.yml"


def text(name: str) -> str:
    return (INFRA / name).read_text()


def blocks(source: str, kind: str, label: str | None = None) -> dict[str, str]:
    """`resource "<kind>" "<name>" { ... }` bodies by name (also data/variable)."""
    out = {}
    head = re.compile(r'^(?:resource|data|variable|output) "%s"(?: "([^"]+)")? \{' % re.escape(kind), re.M)
    for m in head.finditer(source):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(source[i], 0)
            i += 1
        name = m.group(1) or kind
        if label is None or name == label:
            out[name] = source[m.end():i - 1]
    return out


def all_tf() -> str:
    return "\n".join(p.read_text() for p in sorted(INFRA.glob("*.tf")))


def test_nothing_is_reachable_from_the_whole_internet():
    sg = text("security_groups.tf")
    ingress = blocks(sg, "aws_vpc_security_group_ingress_rule")
    assert ingress, "no ingress rules found"
    for name, body in ingress.items():
        assert "0.0.0.0/0" not in body and "::/0" not in body, name
        assert "var.allowed_cidrs" in body or "referenced_security_group_id" in body, name
    allowed = blocks(text("variables.tf"), "allowed_cidrs")["allowed_cidrs"]
    assert 'c != "0.0.0.0/0"' in allowed and "default" not in allowed


def test_database_is_private_encrypted_and_tls_only():
    rds = text("rds.tf")
    db = blocks(rds, "aws_db_instance")["this"]
    for setting in ("publicly_accessible         = false", "storage_encrypted           = true",
                    "manage_master_user_password = true", "skip_final_snapshot         = false",
                    "deletion_protection         = var.deletion_protection"):
        assert setting in db, setting
    # RDS generates the administrator's password; none passes through state.
    assert not re.search(r"^\s*password\s*=", db, re.M)
    params = blocks(rds, "aws_db_parameter_group")["this"]
    assert re.search(r'name\s*=\s*"rds.force_ssl"\s*value\s*=\s*"1"', params)
    # Role passwords are set by statements; no statement text is logged.
    assert re.search(r'name\s*=\s*"log_min_error_statement"\s*value\s*=\s*"panic"', params)
    assert re.search(r'name\s*=\s*"log_statement"\s*value\s*=\s*"none"', params)
    protection = blocks(text("variables.tf"), "deletion_protection")["deletion_protection"]
    assert "default     = true" in protection


def test_images_are_immutable_and_deployed_by_digest():
    ecr = blocks(text("ecr.tf"), "aws_ecr_repository")["app"]
    assert 'image_tag_mutability = "IMMUTABLE"' in ecr and "scan_on_push = true" in ecr
    ecs = text("ecs.tf")
    assert 'repository_url}@${var.image_digest}' in ecs
    assert ":latest" not in all_tf()
    digest = blocks(text("variables.tf"), "image_digest")["image_digest"]
    assert '^sha256:[0-9a-f]{64}$' in digest


def test_every_container_runs_hardened():
    ecs = text("ecs.tf")
    hardened = re.search(r"hardened = \{(.*?)\n  \}", ecs, re.S).group(1)
    for setting in ('user                   = "10001"', "readonlyRootFilesystem = true",
                    "privileged             = false", 'drop = ["ALL"], add = []'):
        assert setting in hardened, setting
    # app, collector and every one-shot task: each container definition
    # starts from the hardened settings.
    assert len(re.findall(r"merge\(local\.hardened, \{", ecs)) == 3
    assert ecs.count("container_definitions") == 2


def test_no_secret_is_in_a_plain_environment_variable():
    ecs = text("ecs.tf")
    plain = re.findall(r'\{ name = "([A-Z_]+)", value =', ecs)
    assert plain, "no environment entries found"
    sensitive = re.compile(r"PASSWORD|SECRET|TOKEN|PRIVATE|ACCESS_KEY|DSN_PASSWORD")
    assert [n for n in plain if sensitive.search(n)] == []
    # The bootstrap task's administrator DSN carries no password; libpq
    # takes it from PGPASSWORD, injected from the RDS-managed secret.
    admin_dsn = re.search(r'name = "PAC_ADMIN_DSN", value = join\(" ", \[(.*?)\]\)', ecs, re.S).group(1)
    assert "password" not in admin_dsn
    assert 'name = "PGPASSWORD", valueFrom' in ecs


def test_the_serving_task_never_holds_the_owner_credential():
    ecs = text("ecs.tf")
    serving = re.search(r"serving_secrets = concat\(\[for r in (\[[^\]]*\])", ecs).group(1)
    assert json.loads(serving) == ["auth", "exec", "scoped"]
    iam = text("iam.tf")
    execution = blocks(iam, "aws_iam_policy_document", "execution")["execution"]
    assert re.search(r'app\s*=\s*\[for r in \["auth", "exec", "scoped"\]', execution)


def test_every_database_connection_verifies_the_server_certificate():
    ecs = text("ecs.tf")
    assert '{ name = "PGSSLMODE", value = "verify-full" }' in ecs
    assert '{ name = "PGSSLROOTCERT", value = local.db_ca }' in ecs
    assert 'db_ca      = "/etc/ssl/certs/rds-global-bundle.pem"' in ecs
    assert "sslmode=verify-full" in ecs  # the bootstrap task's own DSN


def test_client_addresses_are_trusted_from_the_load_balancer_only():
    ecs = text("ecs.tf")
    assert '{ name = "FORWARDED_ALLOW_IPS", value = var.vpc_cidr }' in ecs
    assert '{ name = "PAC_COOKIE_SECURE", value = "true" }' in ecs
    alb = blocks(text("alb.tf"), "aws_lb")["this"]
    assert 'xff_header_processing_mode = "append"' in alb
    assert "drop_invalid_header_fields = true" in alb
    https = blocks(text("alb.tf"), "aws_lb_listener")["https"]
    assert '"ELBSecurityPolicy-TLS13-' in https
    app_ingress = blocks(text("security_groups.tf"), "aws_vpc_security_group_ingress_rule")["app_from_alb"]
    assert "referenced_security_group_id = aws_security_group.alb.id" in app_ingress


def test_ci_can_publish_images_and_nothing_else():
    iam = text("iam.tf")
    publish = blocks(iam, "aws_iam_policy_document", "publish")["publish"]
    actions = re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', publish)
    assert actions and all(a.startswith("ecr:") for a in actions), actions
    trust = blocks(iam, "aws_iam_policy_document", "publish_assume")["publish_assume"]
    assert 'variable = "token.actions.githubusercontent.com:sub"' in trust
    assert ':environment:${var.github_environment}"' in trust
    assert "StringLike" not in trust


def test_owner_decisions_have_no_defaults():
    variables = text("variables.tf")
    for name in ("region", "hostname", "allowed_cidrs", "monthly_budget_usd", "alert_emails"):
        assert "default" not in blocks(variables, name)[name], name
    assert 'default     = "offline"' in blocks(variables, "llm_provider")["llm_provider"]
    assert "default     = false" in blocks(variables, "enable_bedrock")["enable_bedrock"]


def test_seed_script_never_puts_a_value_in_argv_or_output():
    script = (INFRA / "seed-secrets.sh").read_text()
    assert "--secret-string file:///dev/stdin" in script
    assert not re.search(r'--secret-string\s+"?\$', script)
    assert "get-secret-value" not in script
    # Generated values go straight into the pipe; nothing echoes them.
    assert not re.search(r"echo .*\$\(python3", script)


def test_publish_workflow_is_manual_scoped_and_pinned():
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())
    assert list(wf[True]) == ["workflow_dispatch"]  # PyYAML reads `on:` as True
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["publish"]
    assert job["environment"] == "staging"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    for step in job["steps"]:
        uses = step.get("uses", "")
        if uses and not uses.startswith("actions/"):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), uses
    assert "--no-cache" in WORKFLOW.read_text()


@pytest.fixture(scope="module")
def estimate():
    spec = importlib.util.spec_from_file_location("estimate", INFRA / "cost" / "estimate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cost_estimate_is_generated_from_the_committed_prices(estimate):
    prices = json.loads(estimate.PRICES.read_text())
    assert estimate.ESTIMATE.read_text() == estimate.render(prices)
    for key, price in prices["prices"].items():
        offer = prices["offers"][price["offer"]]
        assert re.fullmatch(r"[0-9a-f]{64}", offer["sha256"]) and offer["version"], key
        assert price["sku"] and price["usagetype"], key
    assert set(prices["prices"]) == set(estimate.RULES)


def test_files_the_procedure_creates_are_never_committed():
    """README.md's procedure writes a variable file and saved plans into the
    module. A saved plan holds every variable's value and the provider's view
    of the account, and state holds its identifiers: none belongs in Git."""
    paths = [f"infra/aws-staging/{n}" for n in (
        "staging.tfvars", "foundation.plan", "deploy.tfplan",
        "terraform.tfstate", "terraform.tfstate.backup")]
    out = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", *paths],
                         capture_output=True, text=True)
    assert sorted(set(paths) - set(out.stdout.split())) == []
