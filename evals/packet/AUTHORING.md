# Writing an independent evaluation set

This packet is for someone writing NEW questions and expected answers for the
Pharma Analytics Copilot. It is a specification only. It contains nothing the
system produced.

## Who may write it

A set measures generalisation only if its author has not seen how the system
behaves. **Do not read** any of these before your set is frozen:

- `evals/questions.yaml`, `evals/holdout.yaml`, `evals/holdout2.yaml` and
  `evals/runs/`, the questions used to develop and test the system;
- `docs/EVALUATION.md`, `docs/RELEASE_EVIDENCE.md`, `docs/REVIEW_*.md`,
  `docs/QUALIFICATION_*.md`, `evidence/`, `tests/` and `app/`, which describe
  or embody the system's behaviour and its failures;
- any answer the system has given, including in a demo.

If you have seen any of them, your set is a **development** set. Say so in the
contamination log; it is still useful, but it is not a holdout. Freezing a file
makes it immutable, not independent.

## What you may read

The supplied specification, which defines what a correct answer is:

- `schema/create_tables.sql` (the tables) and `schema/seed_data.sql` (users);
- `docs/metric_definitions.md`, `docs/product_analytics.md`,
  `docs/account_analytics.md`, `docs/market_classification.md`,
  `docs/org_hierarchy.md`, `docs/period_offsets.md`,
  `docs/data_source_guide.md`, `docs/security_model.md`.

## What to write

One YAML file with `version`, `status: "holdout"` and a list of `questions`;
each entry follows `oracle_schema.json`. Aim for at least 40 questions so that a
proportion has a usable interval (with 40 and a true rate of 80%, a 95% Wilson
interval is about 65%–90%), spread across:

| Slice | Aim |
|---|---|
| Families | volume and revenue; share (brand, segment, 340B, PAP); counts; geography; hierarchy and accounts; time windows and series; growth (two-window and period over period); thresholds and rankings; follow-ups; ambiguous questions; refusals |
| Principals | `exec` (with pricing), `director`, `ram`; the runner uses one user per role. An Exec without pricing cannot be named in a set yet: test that case with the security suite |
| Phrasing | plain, terse, synonym-heavy, misspelt, multi-part |

**Oracles.** For a number, write reference SQL from the schema and the
definitions (`type: sql`, with `compare: scalar | rows | top_ids`). Write it
before anyone runs the system, and do not run it against the system's answers to
"fix" it. For an expected refusal or clarification, say which (`denied`,
`plan` with `status: clarify`) and why in `note`. A question whose correct answer
you cannot state is a flaw in the question: rewrite it or drop it.

## Freezing

    python3 scripts/check_question_set.py evals/<set>.yaml   # schema, overlap with development sets
    python3 scripts/freeze_holdout.py evals/<set>.yaml       # records its SHA-256 in evals/frozen.json

Commit the set and `evals/frozen.json` together **before** the first run. Run
it once, under `PROTOCOL.md`. Anything learned from it spends it.
