"""A turn that happened must be recorded.

record_turn() inserted with ON CONFLICT (conversation_id, seq) DO NOTHING. Two
requests on the same conversation compute the same next_seq, both answer the
user, and one of them vanishes: the person saw a reply, and the conversation
has no record of ever being asked. For a system whose audit story is "every
question is recorded", a silently dropped turn is the worst possible shape of
bug -- it is invisible from both ends.
"""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor

from app.conversation.state import load_history, open_conversation, record_turn


def _state_for(principal, conversation_id):
    """A state object pinned to a specific next_seq, as a racing request has."""
    return open_conversation(principal, conversation_id)


def test_two_turns_racing_for_one_sequence_number_are_both_kept(
    authtest_db, make_identity
):
    from app.auth.policy import principal_for_user_id

    user = make_identity("exec", can_view_wac=1)
    principal = principal_for_user_id(user.user_id)

    state = open_conversation(principal, None)
    conversation_id = state.conversation_id

    # Two independent states, both believing they are turn 1 -- exactly what
    # two concurrent requests produce.
    first = _state_for(principal, conversation_id)
    second = _state_for(principal, conversation_id)
    assert first.next_seq == second.next_seq == 1

    record_turn(principal, first, question="first question", plan=None,
                cohort=[], answer_text="first answer", status="answered")
    record_turn(principal, second, question="second question", plan=None,
                cohort=[], answer_text="second answer", status="answered")

    history = load_history(principal, conversation_id)
    questions = [t["question"] for t in history]
    assert len(history) == 2, f"a turn was silently discarded: {questions}"
    assert set(questions) == {"first question", "second question"}
    assert len({t["seq"] for t in history}) == 2, "two turns share a sequence number"


def test_concurrent_writes_from_threads_all_survive(authtest_db, make_identity):
    from app.auth.policy import principal_for_user_id

    user = make_identity("exec", can_view_wac=1)
    principal = principal_for_user_id(user.user_id)
    conversation_id = open_conversation(principal, None).conversation_id

    def write(n: int):
        state = _state_for(principal, conversation_id)
        record_turn(principal, state, question=f"q{n}", plan=None, cohort=[],
                    answer_text=f"a{n}", status="answered")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(6)))

    history = load_history(principal, conversation_id)
    assert len(history) == 6, (
        f"{6 - len(history)} of 6 concurrent turns were dropped: "
        f"{[t['question'] for t in history]}"
    )
    assert len({t["seq"] for t in history}) == 6


def test_the_cohort_grain_is_stored_with_the_cohort(authtest_db, make_identity):
    from app.auth.policy import principal_for_user_id

    user = make_identity("exec", can_view_wac=1)
    principal = principal_for_user_id(user.user_id)
    state = open_conversation(principal, None)

    record_turn(principal, state, question="top products", plan={"metric": "x"},
                cohort=["ZENOVAX", "GEMTARA"], answer_text="ok",
                status="answered", cohort_dimension="product")

    reopened = open_conversation(principal, state.conversation_id)
    assert reopened.previous_cohort == ["ZENOVAX", "GEMTARA"]
    assert reopened.previous_cohort_dimension == "product", (
        "the cohort came back without the grain it was collected at"
    )


def test_an_unrecorded_grain_comes_back_as_none(authtest_db, make_identity):
    from app.auth.policy import principal_for_user_id

    user = make_identity("exec", can_view_wac=1)
    principal = principal_for_user_id(user.user_id)
    state = open_conversation(principal, None)
    record_turn(principal, state, question="totals", plan={"metric": "x"},
                cohort=[], answer_text="ok", status="answered")

    reopened = open_conversation(principal, state.conversation_id)
    assert reopened.previous_cohort_dimension is None


def test_unique_ids_are_not_reused_across_conversations(authtest_db, make_identity):
    from app.auth.policy import principal_for_user_id

    user = make_identity("exec", can_view_wac=1)
    principal = principal_for_user_id(user.user_id)
    ids = {open_conversation(principal, None).conversation_id for _ in range(10)}
    assert len(ids) == 10
    assert all(len(i) > 8 for i in ids)
    assert secrets  # imported for the id scheme this asserts about
