# Running a fresh set against a live model

Defined before any result is seen. Changing it after seeing results makes the
run exploratory, and the report must say so.

**Fixed before the run, and recorded in the run's record:** commit and tree,
prompt version and fingerprint (currently 2.2.0, `ed8e49619d32de7b`), planner
contract (2.1.0), model id, provider and region, dataset id and fingerprint,
question set file and its frozen SHA-256, judge version (the commit of
`scripts/run_evals.py`), tolerance.

**Budget.** Spend caps (`--max-input-tokens`, `--max-output-tokens`) and the
contracted rates are agreed in writing first. A smoke run (`--smoke`) precedes
the full run. A violated bound stops the run; unrun questions are reported as
not run.

**One run, unless decided now.** The default is a single run. If repetition is
wanted to measure run-to-run variation, fix N before the first run (for
example N = 3), report every run, and report the per-question agreement. Do not
add runs after seeing a result.

**Report, each separately:**

- correct answers; correct refusals and clarifications; unsupported requests;
  wrong answers; failures (errors, timeouts, budget stops) — never merged into
  one accuracy figure;
- each proportion with a 95% Wilson interval and its n;
- the same by slice: family, principal, phrasing;
- latency p50 and p95, end to end and for planning;
- usage: reported input and output tokens, calls with unknown usage counted
  separately, and cost at the agreed rates;
- plan agreement scored apart from execution.

**What it does not establish:** behaviour on questions unlike the set, other
datasets, other models or prompts, or production load.
