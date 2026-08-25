import json

from agent.skill_source_gate import (
    MAX_SKILL_SOURCE_NUDGES,
    evaluate_skill_source_contract,
    has_explicit_skill_mutation_intent,
)


def _tool(payload):
    return {"role": "tool", "name": "skill_view", "content": json.dumps(payload)}


def _contract():
    return _tool(
        {
            "success": True,
            "name": "tw-tmcc",
            "source_contract": {
                "required_before_answer": True,
                "declared_skill_view_examples": [
                    {"name": "tw-tmc", "file_path": "references/class-N.md"}
                ],
            },
        }
    )


def test_missing_requested_class_sources_nudges_exact_calls():
    decision = evaluate_skill_source_contract(
        messages=[
            {"role": "user", "content": "apply tm-twcc: jewelry classes 14 and 35."},
            _contract(),
        ],
        current_turn_user_idx=0,
        attempts=0,
    )
    assert decision is not None
    assert decision.action == "nudge"
    assert decision.missing == (
        ("tw-tmc", "references/class-14.md"),
        ("tw-tmc", "references/class-35.md"),
    )
    assert "skill_view(name='tw-tmc', file_path='references/class-14.md')" in decision.message
    assert "skill_view(name='tw-tmc', file_path='references/class-35.md')" in decision.message


def test_all_requested_sources_pass_even_when_routed_by_read_file():
    messages = [
        {"role": "user", "content": "apply tm-twcc: jewelry classes 14 and 35."},
        _contract(),
        _tool({"success": True, "name": "tw-tmc", "file": "references/class-14.md"}),
        {
            "role": "tool",
            "name": "read_file",
            "content": json.dumps(
                {
                    "success": True,
                    "name": "tw-tmc",
                    "routing": {
                        "to_tool": "skill_view",
                        "file_path": "references/class-35.md",
                    },
                }
            ),
        },
    ]
    assert (
        evaluate_skill_source_contract(
            messages=messages,
            current_turn_user_idx=0,
            attempts=0,
        )
        is None
    )


def test_exhausted_source_gate_fails_closed():
    decision = evaluate_skill_source_contract(
        messages=[
            {"role": "user", "content": "apply tm-twcc: jewelry class 14."},
            _contract(),
        ],
        current_turn_user_idx=0,
        attempts=MAX_SKILL_SOURCE_NUDGES,
    )
    assert decision is not None
    assert decision.action == "fail"
    assert "Cannot confirm" in decision.message


def test_no_contract_does_not_gate_ordinary_turn():
    assert (
        evaluate_skill_source_contract(
            messages=[{"role": "user", "content": "hello"}],
            current_turn_user_idx=0,
            attempts=0,
        )
        is None
    )


def test_skill_application_is_read_only_without_explicit_mutation_intent():
    assert not has_explicit_skill_mutation_intent(
        "apply tm-twcc: jewelry classes 14 and 35."
    )
    assert has_explicit_skill_mutation_intent(
        "Please patch the tw-tmcc skill to add this missing rule."
    )
    assert has_explicit_skill_mutation_intent(
        "請更新 tw-tmcc 技能並加入這條規則。"
    )
