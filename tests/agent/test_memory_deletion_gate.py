import json

from agent.memory_deletion_gate import evaluate_memory_deletion_answer


def _messages():
    return [
        {"role": "user", "content": "Delete that wrong memory."},
        {
            "role": "tool",
            "name": "viking_forget",
            "content": json.dumps(
                {
                    "status": "deleted",
                    "active_projection_removed": True,
                    "evidence_uri": "viking://user/u/_observability/delete.json",
                }
            ),
        },
    ]


def test_truthful_projection_and_retention_answer_passes():
    assert (
        evaluate_memory_deletion_answer(
            messages=_messages(),
            current_turn_user_idx=0,
            final_response=(
                "Removed from active memory projection. The complete original "
                "content remains retained in isolated evidence."
            ),
            attempts=0,
        )
        is None
    )


def test_permanent_deletion_claim_is_nudged_even_if_retention_is_later_mentioned():
    decision = evaluate_memory_deletion_answer(
        messages=_messages(),
        current_turn_user_idx=0,
        final_response=(
            "Permanently removed from your memory store. The original content "
            "is retained in isolated evidence."
        ),
        attempts=0,
    )

    assert decision is not None
    assert decision.action == "nudge"
    assert any(
        "permanent physical deletion" in diagnostic
        for diagnostic in decision.diagnostics
    )


def test_repeated_dishonest_candidate_gets_deterministic_truthful_replacement():
    decision = evaluate_memory_deletion_answer(
        messages=_messages(),
        current_turn_user_idx=0,
        final_response="It is gone forever.",
        attempts=1,
    )

    assert decision is not None
    assert decision.action == "replace"
    assert "active memory projection" in decision.message
    assert "complete original content remains retained" in decision.message
    assert "hk_legal_authority" in decision.message


def test_legacy_delete_without_retention_receipt_does_not_invent_archive_claim():
    messages = _messages()
    messages[1]["content"] = json.dumps({"status": "deleted"})

    assert (
        evaluate_memory_deletion_answer(
            messages=messages,
            current_turn_user_idx=0,
            final_response="Deleted.",
            attempts=0,
        )
        is None
    )
