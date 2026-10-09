#!/usr/bin/env python3
"""A real staging plan, reconciled with the mocked inventory, without its values.

    AWS_PROFILE=<sso profile> TERRAFORM=<path> python3 evidence/probes/staging_plan_check.py \\
        --var-file infra/aws-staging/staging.tfvars

infra/aws-staging/PLAN_INVENTORY.md lists what each configuration creates
under a mocked provider; a plan against the account must match it, and a
difference is explained before apply (docs/STAGING_PLAN.md). This runs
`terraform plan` -- read-only: it creates nothing and writes no state; the
plan file goes to a temporary directory that is removed -- reads the
addresses from `terraform show -json`, and accounts for every one:

* the inventory's foundation, and its deployable additions once
  image_digest is set (and the NAT additions with egress_mode = "nat");
* the load balancer's rules keyed by an allowed range: one per range in
  allowed_cidrs, where the mock has one example range;
* what the owner's variables add or remove: the two public rules
  (public_sign_in), the Bedrock budget (model_budget_usd > 0), the serving
  task's policy (enable_bedrock), the alias and certificate-validation
  records (route53_zone_id), no GitHub provider when one exists, no flow logs
  when they are off.

Anything else -- an address not accounted for, one accounted for but not
planned, or any action but create -- is unexplained. Prints one line of
counts and no value (the variable file holds the alert and the testers'
addresses). Exits 1 if anything is unexplained, 2 if the plan fails.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE = ROOT / "infra" / "aws-staging"
INVENTORY = MODULE / "PLAN_INVENTORY.md"

RANGE_KEYED = re.compile(r'^(aws_vpc_security_group_ingress_rule\.alb_https?)\["[^"]+/\d+"\]$')
FLOW_LOGS = ["aws_cloudwatch_log_group.flow[0]", "aws_flow_log.this[0]",
             "aws_iam_role.flow[0]", "aws_iam_role_policy.flow[0]"]


def section(inventory: str, heading: str) -> list[str]:
    """The addresses listed under the inventory heading that starts with `heading`."""
    parts = re.split(r"^## ", inventory, flags=re.M)
    body = next((p for p in parts if p.startswith(heading)), None)
    if body is None:
        raise ValueError(f"PLAN_INVENTORY.md has no section {heading!r}")
    return re.findall(r"^- `([^`]+)`$", body, re.M)


def normalise(address: str) -> str:
    m = RANGE_KEYED.match(address)
    return f"{m.group(1)}[<allowed range>]" if m else address


def expected(inventory: str, variables: dict) -> tuple[collections.Counter, list[str]]:
    """The addresses the variables should plan, and why beyond the foundation."""
    want = collections.Counter(normalise(a) for a in section(inventory, "Every address in the foundation"))
    why = []
    ranges = len(variables.get("allowed_cidrs") or [])
    for rule in [a for a in want if a.endswith("[<allowed range>]")]:
        want[rule] = ranges
    if variables.get("image_digest"):
        added = section(inventory, "Deployable (step 6)")
        want.update(added)
        why.append(f"+{len(added)} deployable")
    if variables.get("egress_mode") == "nat":
        nat = set(section(inventory, "Private tasks")) - set(section(inventory, "Deployable (step 6)"))
        want.update(nat)
        why.append(f"+{len(nat)} NAT")
    if variables.get("public_sign_in"):
        want.update(section(inventory, "Foundation with the public sign-in page"))
        why.append("+2 public sign-in")
    if (variables.get("model_budget_usd") or 0) > 0:
        want["aws_budgets_budget.bedrock[0]"] += 1
        why.append("+1 Bedrock budget")
    if variables.get("enable_bedrock") or variables.get("enable_observability"):
        want["aws_iam_role_policy.app_task[0]"] += 1
        why.append("+1 serving task policy")
    if variables.get("route53_zone_id"):
        want["aws_route53_record.app[0]"] += 1
        want[f'aws_route53_record.validation["{variables.get("hostname")}"]'] += 1
        why.append("+2 Route 53 records")
    if variables.get("create_github_oidc_provider") is False:
        del want["aws_iam_openid_connect_provider.github[0]"]
        why.append("-1 GitHub provider (one exists)")
    if variables.get("enable_flow_logs") is False:
        for a in FLOW_LOGS:
            del want[a]
        why.append(f"-{len(FLOW_LOGS)} flow logs")
    return +want, why


def reconcile(changes: list[tuple[str, list[str]]], variables: dict, inventory: str) -> tuple[str, list[str]]:
    """(one summary line, the unexplained items) for a plan's resource changes."""
    counts = collections.Counter()
    for _, actions in changes:
        for a in ("create", "update", "delete"):
            counts[a] += a in actions
    created = collections.Counter(normalise(a) for a, actions in changes if actions == ["create"])
    want, why = expected(inventory, variables)
    unexplained = [f"{a}: {actions}" for a, actions in changes if actions not in (["create"], ["no-op"], ["read"])]
    unexplained += [f"not in the inventory: {a}" for a in sorted((created - want).elements())]
    unexplained += [f"inventory, not planned: {a}" for a in sorted((want - created).elements())]
    base = len(section(inventory, "Every address in the foundation"))
    line = (f"plan: {counts['create']} to add, {counts['update']} to change, {counts['delete']} to destroy; "
            f"inventory foundation {base}" + "".join(f", {w}" for w in why)
            + f"; allowed ranges {len(variables.get('allowed_cidrs') or [])}; unexplained: "
            + (str(len(unexplained)) if unexplained else "none"))
    return line, unexplained


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--var-file", required=True, type=pathlib.Path)
    args = ap.parse_args()
    terraform = os.environ.get("TERRAFORM", "terraform")
    with tempfile.TemporaryDirectory() as tmp:
        planfile = pathlib.Path(tmp) / "check.tfplan"
        plan = subprocess.run([terraform, f"-chdir={MODULE}", "plan", "-input=false", "-no-color", "-lock=false",
                               f"-var-file={args.var_file.resolve()}", f"-out={planfile}"],
                              capture_output=True, text=True)
        if plan.returncode != 0:
            errors = [line for line in plan.stdout.splitlines() + plan.stderr.splitlines()
                      if line.startswith("Error:")]
            print("plan failed: " + ("; ".join(errors) or f"exit {plan.returncode}"))
            return 2
        shown = subprocess.run([terraform, f"-chdir={MODULE}", "show", "-json", str(planfile)],
                               capture_output=True, text=True, check=True)
    doc = json.loads(shown.stdout)
    variables = {k: v.get("value") for k, v in doc.get("variables", {}).items()}
    changes = [(rc["address"], rc["change"]["actions"]) for rc in doc.get("resource_changes", [])]
    line, unexplained = reconcile(changes, variables, INVENTORY.read_text())
    for item in unexplained:
        print(f"  {item}", file=sys.stderr)
    print(line)
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
