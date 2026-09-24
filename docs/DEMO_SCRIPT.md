# Demo recording script

Separate from [DEMO.md](DEMO.md) on purpose. **DEMO.md is generated** by
`scripts/make_demo.py` from a real run against the loaded data; anything
hand-written in it is destroyed the next time that script runs. This guide was
living there, so regenerating the transcript would have silently deleted it.

The transcript in DEMO.md is the script. What follows is the running order and
the narration.

Run these exact questions in the same order and you will hit every point in
under five minutes.

**Before you start**

```bash
# Nothing to start: the app is already live at
#   https://44-217-117-172.sslip.io
# Use the evaluator logins provisioned on the instance.
```

Have the three logins on a sticky note. Keep the browser at ~1400px wide so the
tables do not wrap.

**Running order** (target times are cumulative)

| Time | Do | Say |
|---|---|---|
| 0:00 | Sign in as the RAM | "Access is decided by who you are, not by a dropdown. There is no role selector — the server reads it from the session." |
| 0:25 | "What are my top 5 accounts by pack units this quarter?" | Point at the scope line. "New York Metro only. That filter is applied by PostgreSQL row-level security before anything is aggregated." |
| 1:00 | "Now break that down by quarter" | "The follow-up keeps the same five accounts and adds a grain — it patches a structured plan, it does not re-read the old text." |
| 1:25 | "Exclude 340B accounts" | "One filter changes. The ranking and the window survive, and the answer says what is still applied." |
| 1:50 | "What is my total revenue in dollars this quarter?" | "No pricing — and note it says so, and answers in packs instead. The database role this connection uses was never granted the WAC column, so even a bug in my code could not return a price." *(Say "no pricing", not "refused": the system answers with a labelled volume substitute. An earlier version of this script said "refused", which overstated it.)* |
| 2:20 | "Show me sales in the Texas territory" | "Refused by name. Earlier this silently answered with their own territory, which is worse than refusing — it answers a question they did not ask." |
| 2:45 | Sign out, sign in as the Director | "Same questions, different scope." |
| 3:00 | "How are the territories in my region performing this quarter?" | "Two territories. The RAM's total is exactly this territory's number — a Director's region equals the sum of the RAMs inside it." |
| 3:30 | Sign out, sign in as the Exec | |
| 3:40 | "What is our total revenue this quarter?" | "Only an Exec with can_view_wac sees pricing." |
| 4:00 | "What is our market share for Zenovax?" | **The important one.** "113.78%. That is not a real share, and the system says so. Every market-data row in the supplied dataset is a competitor, so the documented denominator is incomplete. I did not clamp it to 100% or quietly change the formula — the number is faithful to the spec and the caveat is what makes it honest." |
| 4:40 | Tick "Show the SQL" and re-ask as the RAM | "Users never see SQL unless they ask. When they do, they get only the statement their own role was authorised to run — no WAC column anywhere in it." |

**What to make sure lands**

1. The same question gives different, correct answers to different people.
2. Pricing is refused by the *database*, not hidden by the UI.
3. Follow-ups keep the population instead of starting over.
4. The system reports bad data rather than papering over it.

**What not to claim.** If the planner is still running offline, say so — call it
"a deterministic fallback planner" and note that the language-model path is
configured but gated on account approval. Do not present offline behaviour as
natural-language understanding.
