"""A cohort must not claim to be the previous result when it is a slice of it.

"What about those?" freezes the previous answer's population into the next
plan. If that population is a truncated slice presented as the whole, the
system answers a question about a subset while looking like it answered one
about everything -- and nothing in the answer says so. That is worse than
refusing, because it is confident.

Three limits can cut a cohort down, and they are not the same limit:

* **The ranking limit.** "Top 5 accounts" returns 5 rows. Those 5 *are* the
  population the question asked about, so the cohort is complete.
* **The response row cap** (``max_result_rows``). The compiler asks for one
  row more than the cap so the renderer can tell a result that exactly
  fills it from one that was cut short. Rows beyond the cap are never
  shown.
* **The storage cap** (200 ids per turn). A conversation row keeps at most
  200.

The defect these were written against: completeness was derived from the
storage cap alone. With the default configuration the two caps happen to
mask it -- 5,000 is larger than 200, so any response truncation also
exceeded the storage cap -- but ``max_result_rows`` is configuration, and
at any value below 200 a truncated answer was recorded as a complete
cohort. The pipeline also built the cohort from the raw rows, including
the extra probe row the renderer discards, so "those" could include an
entity the user was never shown.
"""

from __future__ import annotations

import pytest

from app.conversation.continuity import (
    COHORT_STORAGE_CAP,
    Cohort,
    summarise_cohort,
)


def rows(n: int, *, start: int = 0) -> list[dict]:
    return [{"dim0_id": f"ACC{i:05d}", "dim0_label": f"Account {i}"}
            for i in range(start, start + n)]


# ---------------------------------------------------------------------------
# The response cap
# ---------------------------------------------------------------------------

def test_a_result_cut_off_by_the_response_cap_is_not_complete():
    """The regression. Below 200 the storage cap cannot notice this."""
    summary = summarise_cohort(rows(51), dimension="account", max_rows=50)

    assert summary.complete is False, (
        "a truncated response was recorded as a complete cohort")


def test_the_probe_row_is_never_part_of_the_cohort():
    """The compiler asks for max_rows + 1 so the renderer can detect
    truncation. That extra row is not shown to anyone, so "those" must not
    include it."""
    summary = summarise_cohort(rows(51), dimension="account", max_rows=50)

    assert len(summary.ids) == 50
    assert "ACC00050" not in summary.ids


def test_a_result_that_exactly_fills_the_cap_is_complete():
    """Exactly at the cap is not truncation. This is the distinction the
    extra row exists to make, and getting it wrong in the other direction
    would ask for clarification on a complete answer."""
    summary = summarise_cohort(rows(50), dimension="account", max_rows=50)

    assert summary.complete is True
    assert len(summary.ids) == 50
    assert summary.total_available == 50


def test_a_truncated_total_is_not_reported_as_an_exact_figure():
    """The rows returned are a floor, not a population count: the query
    stopped counting. Saying "50 of 51" would be a measurement nobody
    took."""
    summary = summarise_cohort(rows(51), dimension="account", max_rows=50)

    assert summary.total_available is None
    assert "more" in summary.describe()
    assert "51" not in summary.describe()


# ---------------------------------------------------------------------------
# The storage cap
# ---------------------------------------------------------------------------

def test_more_ids_than_the_storage_cap_is_not_complete():
    summary = summarise_cohort(
        rows(COHORT_STORAGE_CAP + 1), dimension="account", max_rows=5000)

    assert summary.complete is False
    assert len(summary.ids) == COHORT_STORAGE_CAP


def test_exactly_the_storage_cap_is_complete():
    summary = summarise_cohort(
        rows(COHORT_STORAGE_CAP), dimension="account", max_rows=5000)

    assert summary.complete is True
    assert summary.total_available == COHORT_STORAGE_CAP


def test_the_stored_total_is_exact_when_nothing_was_truncated():
    """Within the response cap the query counted every group, so the total
    is a measurement and can be stated."""
    summary = summarise_cohort(rows(350), dimension="account", max_rows=5000)

    assert summary.total_available == 350
    assert summary.describe() == "200 of 350 accounts (truncated)"


def test_both_caps_at_once_still_reports_the_weaker_claim():
    """Truncated by the response cap AND over the storage cap. The total is
    unknown, so it must not be stated."""
    summary = summarise_cohort(rows(301), dimension="account", max_rows=300)

    assert summary.complete is False
    assert summary.total_available is None


# ---------------------------------------------------------------------------
# Top-N
# ---------------------------------------------------------------------------

def test_a_top_n_result_is_a_complete_cohort():
    """"Top 5 accounts" asked about those five. They are the population,
    not a sample of one, so a follow-up may freeze them."""
    summary = summarise_cohort(rows(5), dimension="account", max_rows=5000)

    assert summary.complete is True
    assert summary.total_available == 5
    assert summary.describe() == "5 accounts"


def test_a_top_n_larger_than_the_storage_cap_is_not_complete():
    """"Top 500" is still more than a turn can keep."""
    summary = summarise_cohort(rows(500), dimension="account", max_rows=5000)

    assert summary.complete is False
    assert len(summary.ids) == COHORT_STORAGE_CAP


# ---------------------------------------------------------------------------
# Degenerate results
# ---------------------------------------------------------------------------

def test_a_result_with_no_dimension_has_no_cohort():
    """A single total is not a population."""
    summary = summarise_cohort([{"value": 1}], dimension=None, max_rows=5000)

    assert summary is None


def test_an_empty_result_is_a_complete_empty_cohort():
    """Nothing matched. That is a complete answer, and carrying it forward
    is correct: the follow-up is about nothing, and says so."""
    summary = summarise_cohort([], dimension="account", max_rows=5000)

    assert summary is not None
    assert summary.ids == ()
    assert summary.complete is True


def test_rows_with_a_null_id_do_not_become_cohort_members():
    """A NULL group key is a real row in the table but not an entity that
    can be referred to."""
    mixed = rows(3) + [{"dim0_id": None, "dim0_label": "Unassigned"}]

    summary = summarise_cohort(mixed, dimension="account", max_rows=5000)

    assert summary.ids == ("ACC00000", "ACC00001", "ACC00002")


def test_null_ids_still_count_toward_truncation():
    """They occupy a row in the response, so they consume the cap even
    though they cannot be carried forward."""
    mixed = rows(50) + [{"dim0_id": None, "dim0_label": "Unassigned"}]

    summary = summarise_cohort(mixed, dimension="account", max_rows=50)

    assert summary.complete is False


# ---------------------------------------------------------------------------
# Legacy records
# ---------------------------------------------------------------------------

def test_a_legacy_cohort_of_unknown_completeness_is_treated_as_incomplete():
    """Turns written before migration 009 have NULL completeness. Unknown
    completeness is not completeness, and the safe reading is the one that
    asks rather than assumes."""
    legacy = Cohort(dimension="account", ids=("A", "B"), complete=False,
                    total_available=None)

    assert legacy.complete is False
    assert "more" in legacy.describe()


def test_an_incomplete_cohort_is_never_carried_forward_silently():
    """The property the whole field exists for, asserted at the boundary
    that consumes it."""
    from app.conversation.continuity import TurnKind, resolve

    truncated = Cohort(dimension="account", ids=("A", "B"), complete=False,
                       total_available=None)

    decision = resolve("break that down by territory for those accounts",
                       previous_plan={"filters": {}}, cohort=truncated)

    assert decision.kind is TurnKind.FOLLOW_UP, (
        "chosen so the cohort rule is what is under test, not the "
        "bare-reference rule that would also have asked")
    assert decision.carries_cohort is False
    assert decision.clarification is not None
