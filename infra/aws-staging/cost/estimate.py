#!/usr/bin/env python3
"""Monthly cost of the staging stack, from AWS's public price list.

    # refresh the prices from downloaded offer files (README.md, "Cost")
    python3 infra/aws-staging/cost/estimate.py extract --offers DIR
    # write the estimate
    python3 infra/aws-staging/cost/estimate.py > infra/aws-staging/cost/ESTIMATE.md
    # is ESTIMATE.md what the committed prices and assumptions give?
    python3 infra/aws-staging/cost/estimate.py --check

Every rate comes from prices-us-east-1.json, which `extract` builds from the
offer files at https://pricing.us-east-1.amazonaws.com (no AWS account or
API is involved): each price names its offer, version, publication date,
SKU and usage type, and each offer file its SHA-256. A rule must match
exactly one product, so a renamed or duplicated usage type fails loudly
instead of pricing the wrong thing.

Quantities that are not prices -- hours, traffic, log volume, metric series,
tokens -- are assumptions, stated beside every line they feed. Measured ones
say where they were measured.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import sys
from decimal import ROUND_HALF_UP, Decimal

HERE = pathlib.Path(__file__).resolve().parent
PRICES = HERE / "prices-us-east-1.json"
ESTIMATE = HERE / "ESTIMATE.md"
REGION = "us-east-1"
EAST = "US East (N. Virginia)"
OPUS = "Claude Opus 4.5 (Amazon Bedrock Edition)"

# key: (offer file stem, attributes that must all match, tier beginRange)
RULES: dict[str, tuple[str, dict, str]] = {
    "fargate_vcpu_hour": ("AmazonECS", {"usagetype": "USE1-Fargate-vCPU-Hours:perCPU"}, "0"),
    "fargate_gb_hour": ("AmazonECS", {"usagetype": "USE1-Fargate-GB-Hours"}, "0"),
    "fargate_storage_gb_hour": ("AmazonECS", {"usagetype": "USE1-Fargate-EphemeralStorage-GB-Hours"}, "0"),
    "alb_hour": ("AWSELB", {"usagetype": "LoadBalancerUsage", "productFamily": "Load Balancer-Application"}, "0"),
    "alb_lcu_hour": ("AWSELB", {"usagetype": "LCUUsage", "productFamily": "Load Balancer-Application"}, "0"),
    "rds_t4g_micro_single_hour": ("AmazonRDS", {"usagetype": "InstanceUsage:db.t4g.micro", "databaseEngine": "PostgreSQL"}, "0"),
    "rds_t4g_small_single_hour": ("AmazonRDS", {"usagetype": "InstanceUsage:db.t4g.small", "databaseEngine": "PostgreSQL"}, "0"),
    "rds_t4g_small_multi_hour": ("AmazonRDS", {"usagetype": "Multi-AZUsage:db.t4g.small", "databaseEngine": "PostgreSQL"}, "0"),
    "rds_gp3_single_gb_month": ("AmazonRDS", {"usagetype": "RDS:GP3-Storage", "databaseEngine": "PostgreSQL"}, "0"),
    "rds_gp3_multi_gb_month": ("AmazonRDS", {"usagetype": "RDS:Multi-AZ-GP3-Storage", "databaseEngine": "PostgreSQL"}, "0"),
    "rds_backup_gb_month": ("AmazonRDS", {"usagetype": "RDS:ChargedBackupUsage", "databaseEngine": "PostgreSQL"}, "0"),
    "rds_t4g_cpu_credit_vcpu_hour": ("AmazonRDS", {"usagetype": "CPUCredits:db.t4g", "databaseEngine": "PostgreSQL"}, "0"),
    "public_ipv4_hour": ("AmazonVPC", {"usagetype": "USE1-PublicIPv4:InUseAddress"}, "0"),
    "vpc_endpoint_hour": ("AmazonVPC", {"usagetype": "USE1-VpcEndpoint-Hours"}, "0"),
    "vpc_endpoint_gb": ("AmazonVPC", {"usagetype": "USE1-VpcEndpoint-Bytes"}, "0"),
    "nat_hour": ("AmazonEC2-natgateway", {"usageType": "NatGateway-Hours"}, "0"),
    "nat_gb": ("AmazonEC2-natgateway", {"usageType": "NatGateway-Bytes"}, "0"),
    "secret_month": ("AWSSecretsManager", {"usagetype": "USE1-AWSSecretsManager-Secrets"}, "0"),
    "secret_api_request": ("AWSSecretsManager", {"usagetype": "USE1-AWSSecretsManagerAPIRequest"}, "0"),
    "kms_request": ("awskms", {"usagetype": "us-east-1-KMS-Requests"}, "0"),
    "ecr_gb_month": ("AmazonECR", {"usagetype": "TimedStorage-ByteHrs"}, "0"),
    "logs_ingest_gb": ("AmazonCloudWatch", {"usagetype": "DataProcessing-Bytes"}, "0"),
    "logs_vended_gb": ("AmazonCloudWatch", {"usagetype": "USE1-VendedLog-Bytes"}, "0"),
    "logs_storage_gb_month": ("AmazonCloudWatch", {"usagetype": "TimedStorage-ByteHrs"}, "0"),
    "amp_sample_free_tier": ("AmazonPrometheus", {"usagetype": "USE1-AMP:MetricSampleCount"}, "0"),
    "amp_sample": ("AmazonPrometheus", {"usagetype": "USE1-AMP:MetricSampleCount"}, "40000000"),
    "amp_query_sample": ("AmazonPrometheus", {"usagetype": "USE1-AMP:QuerySamplesProcessed"}, "0"),
    "amp_storage_free_tier": ("AmazonPrometheus", {"usagetype": "USE1-AMP:MetricStorageByteHrs"}, "0"),
    "data_out_gb": ("AWSDataTransfer", {"fromLocation": EAST, "toLocation": "External",
                                        "transferType": "AWS Outbound"}, "0"),
    "data_cross_az_gb": ("AWSDataTransfer", {"fromLocation": EAST, "toLocation": EAST,
                                             "transferType": "IntraRegion"}, "0"),
    "budget_day": ("AWSBudgets", {"usagetype": "BudgetsUsage"}, "0"),
    "opus45_in_geo_mtok": ("AmazonBedrockFoundationModels", {"servicename": OPUS, "usagetype": "USE1-MP:USE1_InputTokenCount-Units"}, "0"),
    "opus45_out_geo_mtok": ("AmazonBedrockFoundationModels", {"servicename": OPUS, "usagetype": "USE1-MP:USE1_OutputTokenCount-Units"}, "0"),
    "opus45_in_global_mtok": ("AmazonBedrockFoundationModels", {"servicename": OPUS, "usagetype": "USE1-MP:USE1_InputTokenCount_Global-Units"}, "0"),
    "opus45_out_global_mtok": ("AmazonBedrockFoundationModels", {"servicename": OPUS, "usagetype": "USE1-MP:USE1_OutputTokenCount_Global-Units"}, "0"),
}

OFFER_REGION = {"AWSBudgets": "aws-other"}
NOTES = {
    "AmazonEC2-natgateway": "the NAT gateway rows of the AmazonEC2 us-east-1 CSV (the full file is "
                            "several GB); its header lines, version included, are kept",
}


def _tier_matches(begin: str, want: str) -> bool:
    return Decimal(begin or "0") == Decimal(want)


def _json_offer(path: pathlib.Path, attrs: dict, tier: str) -> list[dict]:
    doc = json.loads(path.read_text())
    regions = {None, REGION, OFFER_REGION.get(path.stem)}
    found = []
    for sku, product in doc["products"].items():
        a = {**product["attributes"], "productFamily": product.get("productFamily")}
        if (a.get("regionCode") or None) not in regions or any(a.get(k) != v for k, v in attrs.items()):
            continue
        for term in doc["terms"].get("OnDemand", {}).get(sku, {}).values():
            for dim in term["priceDimensions"].values():
                if _tier_matches(dim.get("beginRange"), tier):
                    found.append({"usd": dim["pricePerUnit"]["USD"], "unit": dim["unit"], "sku": sku,
                                  "usagetype": a.get("usagetype"), "beginRange": dim.get("beginRange"),
                                  "endRange": dim.get("endRange"), "description": dim["description"]})
    return found


def _csv_offer(path: pathlib.Path, attrs: dict, tier: str) -> list[dict]:
    rows = list(csv.reader(path.open()))
    header = rows[5]
    found = []
    for row in rows[6:]:
        d = dict(zip(header, row))
        if d.get("TermType") == "OnDemand" and all(d.get(k) == v for k, v in attrs.items()) \
                and d.get("Location") == EAST and _tier_matches(d.get("StartingRange"), tier):
            found.append({"usd": d["PricePerUnit"], "unit": d["Unit"], "sku": d["SKU"],
                          "usagetype": d["usageType"], "beginRange": d["StartingRange"],
                          "endRange": d["EndingRange"], "description": d["PriceDescription"]})
    return found


def _offer_meta(path: pathlib.Path) -> dict:
    stem = path.stem
    if path.suffix == ".csv":
        head = dict(r[:2] for r in csv.reader(path.open()) if len(r) >= 2 and r[0] in
                    ("Version", "Publication Date", "OfferCode"))
        version, published, code = head["Version"], head["Publication Date"], head["OfferCode"]
        url = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{code}/{version}/{REGION}/index.csv"
    else:
        doc = json.loads(path.read_text())
        version, published, code = doc["version"], doc["publicationDate"], doc["offerCode"]
        url = (f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{code}/{version}/"
               f"{OFFER_REGION.get(stem, REGION)}/index.json")
    meta = {"offerCode": code, "version": version, "publicationDate": published, "url": url,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    if stem in NOTES:
        meta["note"] = NOTES[stem]
    return meta


def extract(offers: pathlib.Path) -> int:
    prices, used = {}, {}
    for key, (stem, attrs, tier) in RULES.items():
        path = offers / f"{stem}.csv" if (offers / f"{stem}.csv").exists() else offers / f"{stem}.json"
        found = (_csv_offer if path.suffix == ".csv" else _json_offer)(path, attrs, tier)
        if len(found) != 1:
            print(f"{key}: {len(found)} matches in {path.name} for {attrs} at {tier}", file=sys.stderr)
            return 1
        prices[key] = {"offer": stem, **found[0]}
        used[stem] = path
    out = {"region": REGION,
           "source": "AWS Price List bulk API, https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json",
           "offers": {stem: _offer_meta(path) for stem, path in sorted(used.items())},
           "prices": prices}
    PRICES.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {PRICES.name}: {len(prices)} prices from {len(used)} offers", file=sys.stderr)
    return 0


# --- the estimate --------------------------------------------------------------

HOURS = Decimal(730)                   # AWS's monthly-hours convention
DAYS = Decimal("30.4167")
SAMPLES_PER_SERIES = HOURS * 3600 / 15  # app/telemetry.py exports every 15 s

# Measured: evidence/probes/metric_series_count.py (588 series per process with
# every evaluation question asked as two roles). Planning figure adds the
# model counters a live provider emits (attempts by outcome, tokens by
# direction, cost, usage_unknown) and the response codes a month brings that
# the probe did not.
SERIES_MEASURED = 588
SERIES_PER_PROCESS = 800
WORKERS_PER_TASK = 2                   # Dockerfile: uvicorn --workers 2

# Measured: docs/EVALUATION.md, live runs on the us. Opus 4.5 profile.
TOKENS_IN_PER_QUESTION = 4670
TOKENS_OUT_PER_QUESTION = 160

SCENARIOS = {
    "low": {
        "title": "Low-cost staging (the module's defaults once data is loaded)",
        "shape": "1 serving task (1 vCPU, 2 GB) with a public address; RDS db.t4g.small Single-AZ, "
                 "20 GB gp3, 7-day backups; no Prometheus workspace; offline planner (no model calls)",
        "tasks": 1, "db": "rds_t4g_small_single_hour", "storage": "rds_gp3_single_gb_month",
        "job_task_hours": 10, "lcu": "0.2", "logs_gb": 1, "flow_gb": 1, "ecr_gb": 2,
        "data_out_gb": 10, "cross_az_gb": 5, "amp": False,
    },
    "failover": {
        "title": "Replica and failover tests",
        "shape": "2 serving tasks in two zones with public addresses (a zone loss leaves egress); "
                 "RDS db.t4g.small Multi-AZ, 20 GB gp3; Prometheus workspace with the alert rules; "
                 "offline planner",
        "tasks": 2, "db": "rds_t4g_small_multi_hour", "storage": "rds_gp3_multi_gb_month",
        "job_task_hours": 10, "lcu": "0.4", "logs_gb": 3, "flow_gb": 2, "ecr_gb": 2,
        "data_out_gb": 20, "cross_az_gb": 10, "amp": True,
    },
}


def usd(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01'), ROUND_HALF_UP):,.2f}"


def rate(x) -> str:
    """A unit price as the price list states it, without trailing zeros."""
    return format(Decimal(x).normalize(), "f")


def lines(s: dict, p: dict) -> list[tuple[str, str, Decimal]]:
    r = {k: Decimal(v["usd"]) for k, v in p.items()}
    tasks = s["tasks"]
    task_hour = r["fargate_vcpu_hour"] + 2 * r["fargate_gb_hour"]
    out = [
        ("Fargate, serving", f"{tasks} task{'s' if tasks > 1 else ''} x 730 h x (1 vCPU + 2 GB); 20 GB task storage included",
         tasks * HOURS * task_hour),
        ("Fargate, one-shot tasks", f"{s['job_task_hours']} task-hours (bootstrap, migrate, loads, "
         "boundary) x (1 vCPU + 2 GB + 1 GB storage over 20)",
         s["job_task_hours"] * (task_hour + r["fargate_storage_gb_hour"])),
        ("Load balancer", "730 h", HOURS * r["alb_hour"]),
        ("Load balancer capacity units", f"{s['lcu']} LCU average (testers only; assumption)",
         HOURS * Decimal(s["lcu"]) * r["alb_lcu_hour"]),
        ("RDS instance", f"db.t4g.small, {'Multi-AZ' if 'multi' in s['db'] else 'Single-AZ'}, 730 h",
         HOURS * r[s["db"]]),
        ("RDS storage", "20 GB gp3 (3,000 IOPS and 125 MiB/s included)", 20 * r[s["storage"]]),
        ("RDS backups", "7 days of a ~1.2 GB database: within the free allocation (equal to "
         f"provisioned storage); beyond it {rate(r['rds_backup_gb_month'])}/GB-month", Decimal(0)),
        ("RDS CPU credits", "assumed none: staging load stays under the t4g baseline "
         f"(else {rate(r['rds_t4g_cpu_credit_vcpu_hour'])}/vCPU-hour)", Decimal(0)),
        ("Public IPv4 addresses", f"{2 + tasks} in use (load balancer in 2 zones + {tasks} task{'s' if tasks > 1 else ''}) x 730 h",
         (2 + tasks) * HOURS * r["public_ipv4_hour"]),
        ("Secrets Manager", "7 secrets (4 database roles, test users, OIDC client, RDS-managed "
         "administrator) + 10,000 API calls", 7 * r["secret_month"] + 10000 * r["secret_api_request"]),
        ("KMS requests (AWS-managed keys)", "20,000 requests; AWS-managed keys have no monthly fee",
         20000 * r["kms_request"]),
        ("ECR storage", f"{s['ecr_gb']} GB of images", s["ecr_gb"] * r["ecr_gb_month"]),
        ("CloudWatch Logs", f"{s['logs_gb']} GB task and database logs + {s['flow_gb']} GB VPC flow "
         f"logs ingested; {s['logs_gb'] + s['flow_gb']} GB stored (30-day retention)",
         s["logs_gb"] * r["logs_ingest_gb"] + s["flow_gb"] * r["logs_vended_gb"]
         + (s["logs_gb"] + s["flow_gb"]) * r["logs_storage_gb_month"]),
        ("Data transfer out", f"{s['data_out_gb']} GB at the first paid tier (priced although the "
         "account's first 100 GB a month are free)", s["data_out_gb"] * r["data_out_gb"]),
        ("Data transfer between zones", f"{s['cross_az_gb']} GB (tasks <-> database), charged in "
         "and out", 2 * s["cross_az_gb"] * r["data_cross_az_gb"]),
        ("AWS Budgets", "2 budgets without actions", 2 * DAYS * r["budget_day"]),
        ("ACM certificate", "public certificate: the ACM offer prices only private CAs", Decimal(0)),
        ("Bedrock", "offline planner: no model calls (see the model table)", Decimal(0)),
    ]
    if s["amp"]:
        series = tasks * WORKERS_PER_TASK * SERIES_PER_PROCESS
        samples = series * SAMPLES_PER_SERIES
        free = Decimal(p["amp_sample_free_tier"]["endRange"])
        out += [
            ("Prometheus samples", f"{series:,} series ({tasks} tasks x {WORKERS_PER_TASK} workers x "
             f"{SERIES_PER_PROCESS}) every 15 s = {samples / 1_000_000:,.0f}M samples; first "
             f"{free / 1_000_000:,.0f}M free", max(samples - free, Decimal(0)) * r["amp_sample"]),
            ("Prometheus queries", "17 alert rules each minute, ~30,000 samples a round = 1.3B "
             "query samples", Decimal("1314000000") * r["amp_query_sample"]),
            ("Prometheus storage", "well under the first 10 GB (free)", Decimal(0)),
            ("Collector", "runs inside the serving task's CPU and memory", Decimal(0)),
        ]
    return out


def deltas(p: dict) -> list[tuple[str, str, Decimal]]:
    r = {k: Decimal(v["usd"]) for k, v in p.items()}
    nat = HOURS * r["nat_hour"] + 20 * r["nat_gb"] + HOURS * r["public_ipv4_hour"]
    one_task_ip = HOURS * r["public_ipv4_hour"]
    endpoints = lambda n: n * 2 * HOURS * r["vpc_endpoint_hour"] + 20 * r["vpc_endpoint_gb"]  # noqa: E731
    return [
        ("Private tasks behind one NAT gateway (egress_mode = nat), 1 task",
         "730 h + 20 GB processed + its address, less the task's own address", nat - one_task_ip),
        ("Private tasks with interface endpoints instead of NAT",
         "ecr.api, ecr.dkr, logs, secretsmanager in 2 zones (+2 endpoints with Bedrock and "
         "Prometheus); the module does not create these", endpoints(4)),
        ("db.t4g.micro instead of db.t4g.small (Single-AZ)",
         "too few connections for two serving tasks (variables.tf)",
         HOURS * (r["rds_t4g_micro_single_hour"] - r["rds_t4g_small_single_hour"])),
        ("Final snapshot kept after teardown", "~2 GB, per month until deleted",
         2 * r["rds_backup_gb_month"]),
        ("Prometheus for one task (low-cost shape)",
         f"1 x {WORKERS_PER_TASK} x {SERIES_PER_PROCESS} series every 15 s",
         max(WORKERS_PER_TASK * SERIES_PER_PROCESS * SAMPLES_PER_SERIES
             - Decimal(p["amp_sample_free_tier"]["endRange"]), Decimal(0)) * r["amp_sample"]),
    ]


def model_rows(p: dict) -> list[tuple[str, str, Decimal, Decimal]]:
    r = {k: Decimal(v["usd"]) for k, v in p.items()}

    def cost(tin: int, tout: int, route: str) -> Decimal:
        return (tin * r[f"opus45_in_{route}_mtok"] + tout * r[f"opus45_out_{route}_mtok"]) / 1_000_000

    q = (TOKENS_IN_PER_QUESTION, TOKENS_OUT_PER_QUESTION)
    return [
        ("1,000 staging questions", f"{q[0]:,} input / {q[1]:,} output tokens each (measured)",
         cost(1000 * q[0], 1000 * q[1], "geo"), cost(1000 * q[0], 1000 * q[1], "global")),
        ("Evaluation smoke run, at its caps", "150,000 input / 70,000 output tokens "
         "(--max-input-tokens / --max-output-tokens)", cost(150000, 70000, "geo"),
         cost(150000, 70000, "global")),
        ("Regression sets", "about 300,000 input / 15,000 output tokens for the three",
         cost(300000, 15000, "geo"), cost(300000, 15000, "global")),
    ]


def render(doc: dict) -> str:
    p, offers = doc["prices"], doc["offers"]
    out = ["# Staging cost estimate", "",
           "Generated by `infra/aws-staging/cost/estimate.py` from `prices-us-east-1.json`; do not "
           "edit by hand. Region **us-east-1**, on-demand rates, USD, 730 hours a month, before tax, "
           "credits and any free tier not shown in the price list. An estimate: what is billed "
           "depends on what runs and for how long, and nothing here has run on AWS.", ""]
    totals = {}
    for key, s in SCENARIOS.items():
        rows = lines(s, p)
        total = sum(x for _, _, x in rows)
        totals[key] = total
        out += [f"## {s['title']}", "", s["shape"] + ".", "",
                "| Component | Quantity | USD / month |", "|---|---|---:|"]
        out += [f"| {a} | {b} | {usd(c)} |" for a, b, c in rows]
        out += [f"| **Total** | | **{usd(total)}** |", "",
                f"Per day while it runs: **{usd(total / DAYS)}**.", ""]
    out += ["## Changes to either shape", "", "| Change | Basis | USD / month |", "|---|---|---:|"]
    out += [f"| {a} | {b} | {'+' if c >= 0 else '−'}{usd(abs(c))} |" for a, b, c in deltas(p)]
    out += ["", "## Model usage (only with `llm_provider = \"bedrock\"`)", "",
            "Claude Opus 4.5. The configured profile, `us.anthropic.claude-opus-4-5-20251101-v1:0`, "
            "is geographic (US): the price list gives it the regional rate. A `global.` profile is "
            "cheaper and routes outside the US, which is a data-residency decision.", "",
            "| Usage | Tokens | us. profile (USD) | global profile (USD) |", "|---|---|---:|---:|"]
    out += [f"| {a} | {b} | {usd(c)} | {usd(d)} |" for a, b, c, d in model_rows(p)]
    rates = [("Input, us. profile", "opus45_in_geo_mtok"), ("Output, us. profile", "opus45_out_geo_mtok"),
             ("Input, global", "opus45_in_global_mtok"), ("Output, global", "opus45_out_global_mtok")]
    out += ["", "Rates per million tokens: " + "; ".join(f"{a} {rate(p[k]['usd'])}"
                                                         for a, k in rates) + ".", "",
            "## Sources", "", "| Offer | Version | Published | SHA-256 of the file used |",
            "|---|---|---|---|"]
    out += [f"| [{k}]({m['url']}) | {m['version']} | {m['publicationDate'][:10]} | "
            f"`{m['sha256'][:16]}…`{' — ' + m['note'] if 'note' in m else ''} |"
            for k, m in offers.items()]
    out += ["", f"Metric series: {SERIES_MEASURED} per process measured by "
            "`evidence/probes/metric_series_count.py`; "
            f"{SERIES_PER_PROCESS} used for planning. Tokens per question: docs/EVALUATION.md.", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", choices=["extract"])
    ap.add_argument("--offers", type=pathlib.Path, help="directory of downloaded offer files")
    ap.add_argument("--check", action="store_true", help="fail unless ESTIMATE.md is current")
    args = ap.parse_args()
    if args.command == "extract":
        if not args.offers:
            ap.error("extract needs --offers")
        return extract(args.offers)
    text = render(json.loads(PRICES.read_text()))
    if args.check:
        if ESTIMATE.read_text() != text:
            print("ESTIMATE.md is stale: run estimate.py > ESTIMATE.md", file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
