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


# The owner's decision of 9 October 2026 opens the sign-in page to everyone:
# these two rules, and only when public_sign_in is set.
PUBLIC_SIGN_IN = {"alb_https_public": 443, "alb_http_public": 80}


def test_nothing_is_reachable_from_the_whole_internet():
    sg = text("security_groups.tf")
    ingress = blocks(sg, "aws_vpc_security_group_ingress_rule")
    assert ingress, "no ingress rules found"
    assert set(PUBLIC_SIGN_IN) <= set(ingress)
    for name, body in ingress.items():
        assert "::/0" not in body, name
        if name in PUBLIC_SIGN_IN:
            continue
        assert "0.0.0.0/0" not in body, name
        assert "var.allowed_cidrs" in body or "referenced_security_group_id" in body, name
    allowed = blocks(text("variables.tf"), "allowed_cidrs")["allowed_cidrs"]
    assert 'c != "0.0.0.0/0"' in allowed and "default" not in allowed


def test_only_the_load_balancer_listeners_open_to_everyone_and_only_by_decision():
    ingress = blocks(text("security_groups.tf"), "aws_vpc_security_group_ingress_rule")
    for name, port in PUBLIC_SIGN_IN.items():
        body = ingress[name]
        assert re.search(r"^\s*count\s*=\s*var\.public_sign_in \? 1 : 0$", body, re.M), name
        assert re.search(r"^\s*security_group_id\s*=\s*aws_security_group\.alb\.id$", body, re.M), name
        assert re.search(r'^\s*ip_protocol\s*=\s*"tcp"$', body, re.M), name
        assert re.search(r"^\s*from_port\s*=\s*%d$" % port, body, re.M), name
        assert re.search(r"^\s*to_port\s*=\s*%d$" % port, body, re.M), name
    decision = blocks(text("variables.tf"), "public_sign_in")["public_sign_in"]
    assert re.search(r"^\s*default\s*=\s*false$", decision, re.M)


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
    # One value is read: the reviewers' list, piped straight into the merge
    # that keeps the passwords already handed out (and never anywhere else).
    reads = [line for line in script.splitlines() if "get-secret-value" in line]
    assert len(reads) == 1 and reads[0].strip().startswith("merged=$(aws secretsmanager")
    assert script.count("| python3 ../../scripts/provision_reviewers.py --merge-csv") == 2
    # Generated values go straight into the pipe; nothing echoes them.
    assert not re.search(r"echo .*\$\(python3", script)
    assert not re.search(r"echo .*\$merged", script)


STUB_AWS = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/argv"
case "$2" in
  list-secret-version-ids) case "$4" in *reviewers*) echo "$STUB_REVIEWERS_SET" ;; *) echo 1 ;; esac ;;
  get-secret-value) cat "$STUB/current.json" ;;
  put-secret-value) cat > "$STUB/put.json"; echo "$4" ;;
esac
"""


def seed_reviewers(tmp_path, csv_text: str, current: list | None):
    """seed-secrets.sh --reviewers against stub aws and terraform commands."""
    import json
    import os
    import shutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(STUB_AWS)
    arns = {k: f"arn:{k}" for k in ("db_owner", "db_auth", "db_exec", "db_scoped",
                                    "test_users", "reviewers")}
    (bin_dir / "terraform").write_text(f"#!/bin/sh\necho '{json.dumps(arns)}'\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    (tmp_path / "current.json").write_text(json.dumps(current or []))
    csv_path = tmp_path / "reviewers.csv"
    csv_path.write_text(csv_text)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB": str(tmp_path),
           "STUB_REVIEWERS_SET": "1" if current is not None else "0", "HOME": str(tmp_path)}
    result = subprocess.run([shutil.which("bash"), str(INFRA / "seed-secrets.sh"), "--reviewers",
                             "reviewers.csv"], cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=60)
    put = tmp_path / "put.json"
    return result, (tmp_path / "argv").read_text(), json.loads(put.read_text()) if put.exists() else None


def test_seed_script_keeps_reviewer_passwords_out_of_argv_and_output(tmp_path):
    held = "h" * 24
    result, argv, put = seed_reviewers(
        tmp_path, "email,name,like\nheld@test.invalid,Held,U1\nnew@test.invalid,New,U1\n",
        [{"email": "held@test.invalid", "name": "Held", "like": "U1", "password": held}])
    assert result.returncode == 0, result.stderr
    passwords = {r["email"]: r["password"] for r in put}
    assert passwords["held@test.invalid"] == held and len(passwords["new@test.invalid"]) >= 12
    for password in passwords.values():
        assert password not in result.stdout + result.stderr + argv
    assert "reviewers: set" in result.stdout and "2 active (1 new)" in result.stderr


def test_seed_script_writes_nothing_for_an_invalid_reviewer_list(tmp_path):
    result, argv, put = seed_reviewers(tmp_path, "email,name\nx@test.invalid,X\n", None)
    assert result.returncode != 0 and put is None and "put-secret-value" not in argv


def test_publish_workflow_is_manual_scoped_and_pinned():
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())
    assert list(wf[True]) == ["workflow_dispatch"]  # PyYAML reads `on:` as True
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["publish"]
    assert job["environment"] == "staging"
    assert job["permissions"] == {"contents": "read", "checks": "read", "id-token": "write"}
    for step in job["steps"]:
        uses = step.get("uses", "")
        if uses and not uses.startswith("actions/"):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), uses
    source = WORKFLOW.read_text()
    assert "--no-cache" in source and "--provenance=false" in source
    names = [step.get("name", step.get("uses", "")) for step in job["steps"]]
    # Nothing is built before CI is known to have passed on this commit, and
    # nothing is pushed before the image's own scan and journeys.
    gate = names.index("CI passed on this commit")
    assert gate < names.index("Build the image")
    assert names.index("Image journeys on a freshly provisioned database") < names.index(
        "Push, then check the registry holds the tested image")
    ci_gate = job["steps"][gate]["run"]
    assert all(c in ci_gate for c in ("test", "frontend", "supply-chain", "image", "15368"))
    assert "is not the tested image" in source


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


def test_a_forged_forwarded_for_cannot_choose_the_client_address():
    """Staging trusts X-Forwarded-For from the VPC range (the load balancer),
    which appends the address it received the connection from. uvicorn must
    take the rightmost address outside that range -- the appended one -- or a
    client could choose the address its failed sign-ins count against, and
    either escape the lockout or lock out someone else. Read with the range
    the module actually configures."""
    import asyncio

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    vpc = re.search(r'variable "vpc_cidr" \{.*?default\s*=\s*"([^"]+)"',
                    text("variables.tf"), re.S).group(1)
    assert '{ name = "FORWARDED_ALLOW_IPS", value = var.vpc_cidr }' in text("ecs.tf")
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=vpc)

    def client(peer: str, forwarded: str) -> str:
        scope = {"type": "http", "scheme": "http", "client": (peer, 40000),
                 "headers": [(b"x-forwarded-for", forwarded.encode())]}
        asyncio.run(middleware(scope, None, None))
        return seen["client"]

    load_balancer = "10.40.0.17"
    assert client(load_balancer, "198.51.100.9") == "198.51.100.9"
    assert client(load_balancer, "203.0.113.5, 198.51.100.9") == "198.51.100.9"
    assert client(load_balancer, "10.40.3.3, 198.51.100.9") == "198.51.100.9"
    # Not through the load balancer: the header is not believed at all.
    assert client("198.51.100.9", "203.0.113.5") == "198.51.100.9"


def plan_check():
    spec = importlib.util.spec_from_file_location(
        "staging_plan_check", ROOT / "evidence" / "probes" / "staging_plan_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OWNER = {"allowed_cidrs": ["203.0.113.7/32"], "public_sign_in": True, "model_budget_usd": 5,
         "route53_zone_id": "Z1", "hostname": "staging.example.org", "image_digest": None,
         "create_github_oidc_provider": True, "enable_flow_logs": True}


def owner_plan(check, inventory: str) -> list[tuple[str, list[str]]]:
    """What a real plan with OWNER's variables creates, per the inventory."""
    planned = [a.replace("198.51.100.0/24", "203.0.113.7/32")
               for a in check.section(inventory, "Every address in the foundation")]
    planned += check.section(inventory, "Foundation with the public sign-in page")
    planned += ["aws_budgets_budget.bedrock[0]", "aws_route53_record.app[0]",
                'aws_route53_record.validation["staging.example.org"]']
    return [(a, ["create"]) for a in planned]


def test_a_real_plan_is_reconciled_with_the_inventory_address_by_address():
    check = plan_check()
    inventory = (INFRA / "PLAN_INVENTORY.md").read_text()
    line, unexplained = check.reconcile(owner_plan(check, inventory), OWNER, inventory)
    assert unexplained == []
    assert line.startswith("plan: 68 to add, 0 to change, 0 to destroy; inventory foundation 63")
    assert "+2 public sign-in, +1 Bedrock budget, +2 Route 53 records; allowed ranges 1" in line
    assert "203.0.113.7" not in line and "staging.example.org" not in line


@pytest.mark.parametrize("mutate", [
    lambda p: p + [("aws_instance.surprise", ["create"])],
    lambda p: p[1:],
    lambda p: p + [("aws_vpc.this", ["delete", "create"])],
    lambda p: [(a.replace("203.0.113.7/32", "0.0.0.0/0"), x) for a, x in p] + [
        ('aws_vpc_security_group_ingress_rule.alb_https["10.0.0.0/8"]', ["create"])],
], ids=["unplanned-extra", "missing", "replacement", "extra-range"])
def test_anything_the_inventory_does_not_account_for_is_unexplained(mutate):
    check = plan_check()
    inventory = (INFRA / "PLAN_INVENTORY.md").read_text()
    line, unexplained = check.reconcile(mutate(owner_plan(check, inventory)), OWNER, inventory)
    assert unexplained and not line.endswith("unexplained: none")


# A plan straight after the apply must be empty, or real drift hides among
# changes that are not changes (r5-staging-plan-drift-reproduced.json). A
# mocked provider cannot show AWS's normalisation, so these read the source.

def test_rds_force_ssl_is_declared_the_way_aws_stores_it():
    params = blocks(text("rds.tf"), "aws_db_parameter_group")["this"]
    ssl = re.search(r'parameter \{\s*name\s*=\s*"rds.force_ssl"(.*?)\}', params, re.S)
    assert ssl and re.search(r'apply_method\s*=\s*"pending-reboot"', ssl.group(1))
    assert re.search(r'value\s*=\s*"1"', ssl.group(1))


def test_the_github_provider_thumbprint_is_left_to_iam():
    provider = blocks(text("iam.tf"), "aws_iam_openid_connect_provider")["github"]
    assert re.search(r"ignore_changes\s*=\s*\[thumbprint_list\]", provider)
    assert 'url             = "https://token.actions.githubusercontent.com"' in provider


# GitHub sends the immutable subject, repo:<owner>@<owner id>/<name>@<repository id>,
# for repositories that use it; a role trusting only repo:<owner>/<name> refused
# every token (hosted run 37939037102).
def test_the_publish_role_trusts_the_subject_github_sends():
    trust = blocks(text("iam.tf"), "aws_iam_policy_document")["publish_assume"]
    sub = re.search(r'variable\s*=\s*"token.actions.githubusercontent.com:sub"\s*values\s*=\s*\[(.*?)\]', trust, re.S)
    assert sub and 'coalesce(var.github_subject_prefix, "repo:${var.github_repository}")' in sub.group(1)
    assert ':environment:${var.github_environment}' in sub.group(1)
    prefix = blocks(text("variables.tf"), "github_subject_prefix")["github_subject_prefix"]
    assert "use_immutable_subject" in prefix and re.search(r"^\s*default\s*=\s*null$", prefix, re.M)


# The sign-in page is reachable from anywhere (public_sign_in), so the load
# balancer adds the browser protections the application does not send
# (r5-staging-headers-reproduced.json: none of the three on the live site).
def test_the_https_listener_adds_browser_security_headers():
    https = blocks(text("alb.tf"), "aws_lb_listener")["https"]
    assert re.search(r'routing_http_response_strict_transport_security_header_value\s*=\s*"max-age=\d{7,}"', https)
    assert re.search(r'routing_http_response_x_content_type_options_header_value\s*=\s*"nosniff"', https)
    assert re.search(r'routing_http_response_x_frame_options_header_value\s*=\s*"DENY"', https)
