import json

from agent.file_receipt_truth_gate import evaluate_file_receipt_truth


def _messages(*, truncated=False, end=50, total=50):
    return [
        {"role": "user", "content": "Package the sources into the skill."},
        {
            "role": "tool",
            "name": "read_file",
            "content": json.dumps(
                {
                    "truncated": truncated,
                    "readable": not truncated,
                    "report_as": "partial" if truncated else "read",
                    "name": "Security_for_costs.pdf",
                    "consumed": {"start": 1, "end": end, "total": total},
                    "source": {"request_handle": "F01"},
                }
            ),
        },
    ]


def test_complete_receipt_rejects_false_truncation_claim_and_routes_batch_import():
    decision = evaluate_file_receipt_truth(
        messages=_messages(),
        current_turn_user_idx=0,
        final_response="The read_file result for F01 was truncated.",
        attempts=0,
        import_files_available=True,
    )

    assert decision is not None
    assert decision.action == "nudge"
    assert "truncated=false" in decision.message
    assert "source_paths" in decision.message


def test_truthful_complete_claim_passes():
    assert (
        evaluate_file_receipt_truth(
            messages=_messages(),
            current_turn_user_idx=0,
            final_response="F01 was read completely; I am continuing the package build.",
            attempts=0,
            import_files_available=True,
        )
        is None
    )


def test_actual_partial_receipt_does_not_get_overridden():
    assert (
        evaluate_file_receipt_truth(
            messages=_messages(truncated=True, end=25),
            current_turn_user_idx=0,
            final_response="F01 was truncated after line 25.",
            attempts=0,
            import_files_available=True,
        )
        is None
    )


def test_repeated_false_claim_gets_deterministic_truthful_replacement():
    decision = evaluate_file_receipt_truth(
        messages=_messages(),
        current_turn_user_idx=0,
        final_response="Only the opening portion of Security_for_costs came through.",
        attempts=2,
        import_files_available=True,
    )

    assert decision is not None
    assert decision.action == "replace"
    assert "read completely" in decision.message
    assert "not completed the requested packaging" in decision.message
