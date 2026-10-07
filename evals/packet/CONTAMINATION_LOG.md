# Contamination log

Who has seen what, so a set's status is a record and not a hope. Add a row
before freezing a set; never edit a row afterwards.

| Date | Person or process | Has seen | Consequence |
|---|---|---|---|
| 2026-09-25 onward | Development sessions (Claude Code, OpenAI Codex) on this repository | every development question, `holdout.yaml`, `holdout2.yaml`, the system's answers and failures, the implementation | any set they write is a **development** set |
| 2026-09-24 | `holdout.yaml` | 4 of its 12 questions are identical to regression questions added the day before (h-05/gpo-01, h-07/src-02, h-08/acc-03, h-09/geo-01) and h-03 is a near-duplicate of hier-02; found 2026-10-07 by `scripts/check_question_set.py` | never fully held out: at most 7 unseen |
| 2026-09-25 | `holdout.yaml` | run once live; a miss (h-02) was fixed | `spent` |
| 2026-09-25 | `holdout2.yaml` | run once live; k-07 fixed on 2026-10-07 | `spent` |
| — | the next set's author | to be recorded before freezing | holdout only if this row shows no exposure |
