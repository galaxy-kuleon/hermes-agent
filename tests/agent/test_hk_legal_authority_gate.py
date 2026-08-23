import json

from agent.hk_legal_authority_gate import (
    evaluate_hk_legal_answer,
    is_hk_statutory_query,
    successful_authorities,
)


PROMPT = (
    "apply Hong Kong Trade Mark enquiry skill. I found an identical mark "
    "registered in Hong Kong. What can I do under the law?"
)


def _authority_message():
    return {
        "role": "tool",
        "name": "hk_legal_authority",
        "tool_call_id": "call-1",
        "content": json.dumps(
            {
                "success": True,
                "cannot_confirm": False,
                "chapter": "559",
                "version_date": "2025-02-14",
                "official_web_url": "https://www.elegislation.gov.hk/hk/cap559!en",
                "required_answer_citation": (
                    "Hong Kong e-Legislation, Cap. 559, current version "
                    "2025-02-14: https://www.elegislation.gov.hk/hk/cap559!en"
                ),
            }
        ),
    }


def test_query_detection_is_bounded_to_hong_kong_legal_requests():
    assert is_hk_statutory_query({"content": PROMPT})
    assert is_hk_statutory_query({"content": "香港商標條例第53條是甚麼？"})
    assert not is_hk_statutory_query({"content": "Plan my Hong Kong holiday"})
    assert not is_hk_statutory_query({"content": "Explain US trademark law"})


def test_old_turn_authority_cannot_ground_the_current_turn():
    messages = [
        {"role": "user", "content": "Hong Kong trademark law"},
        _authority_message(),
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": PROMPT},
    ]
    assert successful_authorities(messages, current_turn_user_idx=3) == []
    decision = evaluate_hk_legal_answer(
        messages=messages,
        current_turn_user_idx=3,
        final_response="Section 53 applies.",
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "Call hk_legal_authority" in decision.message


def test_failed_or_non_official_tool_result_does_not_pass():
    messages = [
        {"role": "user", "content": PROMPT},
        {
            "role": "tool",
            "name": "hk_legal_authority",
            "content": json.dumps(
                {
                    "success": True,
                    "cannot_confirm": False,
                    "chapter": "559",
                    "version_date": "2025-02-14",
                    "official_web_url": "https://example.com/cap559",
                }
            ),
        },
    ]
    assert successful_authorities(messages, current_turn_user_idx=0) == []


def test_successful_tool_still_requires_visible_url_and_version():
    messages = [{"role": "user", "content": PROMPT}, _authority_message()]
    decision = evaluate_hk_legal_answer(
        messages=messages,
        current_turn_user_idx=0,
        final_response="Section 53 is the invalidity route.",
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "2025-02-14" in decision.message
    assert "https://www.elegislation.gov.hk/hk/cap559!en" in decision.message


def test_verified_and_cited_answer_passes():
    messages = [{"role": "user", "content": PROMPT}, _authority_message()]
    decision = evaluate_hk_legal_answer(
        messages=messages,
        current_turn_user_idx=0,
        final_response=(
            "Section 53 is the invalidity route. Current version 2025-02-14: "
            "https://www.elegislation.gov.hk/hk/cap559!en"
        ),
        attempts=1,
    )
    assert decision.action == "pass"


def test_retry_exhaustion_fails_closed_instead_of_returning_claims():
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": PROMPT}],
        current_turn_user_idx=0,
        final_response="Section 47 definitely applies.",
        attempts=2,
    )
    assert decision.action == "fail"
    assert "無法提供" in decision.message
    assert "第47條" not in decision.message
