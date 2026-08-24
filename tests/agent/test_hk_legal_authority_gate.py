import json

from agent.hk_legal_authority_gate import (
    evaluate_hk_legal_answer,
    is_hk_statutory_query,
    is_hk_statutory_turn,
    successful_authorities,
)


PROMPT = (
    "apply Hong Kong Trade Mark enquiry skill. I found an identical mark "
    "registered in Hong Kong. What can I do under the law?"
)
EXACT_REPORTED_PROMPT = (
    "apply Hong Kong Trade Mark enquiry skill.  I am a famous brand in Korea "
    "called the Bennett (I started in 2019), and the mark is moderately "
    "designed - Green colour, and circled. I suddenly find that a Korean "
    "company filed a mark virtually identical to mine in February 2026 in "
    "Hong Kong, and get registered on 2 July 2026.  Now is 23 August 2026, "
    "what can I do?"
)
EXACT_RULE_13_PROMPT = (
    "apply hong kong trade mark enquiry: If I received a Rule 13(1) Opinion on "
    "2 January 2026, and I forgot to reply the same until 5 July 2026, can I "
    "write a letter to seek an extension of time on 5 July 2026?"
)


def _authority_message():
    return {
        "role": "tool",
        "name": "hk_legal_authority",
        "tool_call_id": "call-1",
        "content": json.dumps({
            "success": True,
            "cannot_confirm": False,
            "chapter": "559",
            "version_date": "2025-02-14",
            "official_web_url": "https://www.elegislation.gov.hk/hk/cap559!en",
            "required_answer_citation": (
                "Hong Kong e-Legislation, Cap. 559, current version "
                "2025-02-14: https://www.elegislation.gov.hk/hk/cap559!en"
            ),
            "requested_provisions": [
                {"provision": provision, "found": True}
                for provision in ("11", "12", "44", "45", "52", "53")
            ],
        }),
    }


def _rule_13_authority_message():
    return {
        "role": "tool",
        "name": "hk_legal_authority",
        "tool_call_id": "call-rule-13",
        "content": json.dumps({
            "success": True,
            "cannot_confirm": False,
            "chapter": "559A",
            "version_date": "2025-10-01T00:00:00",
            "official_web_url": "https://www.elegislation.gov.hk/hk/cap559A!en",
            "required_answer_citation": (
                "Hong Kong e-Legislation, Cap. 559A, current version "
                "2025-10-01: https://www.elegislation.gov.hk/hk/cap559A!en"
            ),
            "requested_provisions": [
                {"provision": provision, "found": True}
                for provision in ("13", "95", "96")
            ],
            "official_practice_guidance": [
                {
                    "success": True,
                    "cannot_confirm": False,
                    "matched_page_text_complete": True,
                    "title": "Time limits in the examination process",
                    "official_url": (
                        "https://www.ipd.gov.hk/filemanager/ipd/common/"
                        "trade-marks/registry-work-manual/current/eng/"
                        "time_limits_in_exam_process.pdf"
                    ),
                    "pdf_sha256": "b" * 64,
                    "verified_extracts": [
                        {
                            "page": 2,
                            "text": "The prescribed period expires 6 months thereafter.",
                        },
                        {
                            "page": 2,
                            "text": "A timely request grants an extension of 3 months.",
                        },
                    ],
                }
            ],
        }),
    }


def _rule_13_answer():
    return (
        "Rule 13(2) gives 6 months. A request filed within that period may get "
        "one further 3 months under Rule 13(3). Rules 95 and 96 are distinct. "
        "Current version 2025-10-01: "
        "https://www.elegislation.gov.hk/hk/cap559A!en\n"
        "IPD practice manual: https://www.ipd.gov.hk/filemanager/ipd/common/"
        "trade-marks/registry-work-manual/current/eng/"
        "time_limits_in_exam_process.pdf"
    )


def _registered_famous_mark_answer():
    return (
        "Section 44 governs opposition while the application is pending; section "
        "45 is withdrawal by the applicant, not rectification. After registration, "
        "section 53 is the declaration-of-invalidity route to the Registrar or court. "
        "Bad faith under section 11(5)(b) supports invalidity through section 53(3). "
        "Section 52 is revocation, including continuous non-use in Hong Kong for at "
        "least 3 years. The section 12(4) well-known-mark and section 53(5)(b) earlier-"
        "right route requires establishing protection as a well-known mark in Hong "
        "Kong. Section 12(6) governs opposition but does not bar the separate section "
        "53(5)(b) invalidity route. Current version 2025-02-14: "
        "https://www.elegislation.gov.hk/hk/cap559!en"
    )


def test_query_detection_is_bounded_to_hong_kong_legal_requests():
    assert is_hk_statutory_query({"content": PROMPT})
    assert is_hk_statutory_query({"content": "香港商標條例第53條是甚麼？"})
    assert not is_hk_statutory_query({"content": "Plan my Hong Kong holiday"})
    assert not is_hk_statutory_query({"content": "Explain US trademark law"})


def test_declarative_project_memory_update_is_not_misclassified_as_legal_advice():
    prompt = """Kindly update memory about the Forever Trainee Project.

Legal framework supplied by the user:
- Governing Law: Hong Kong law
- HKIAC arbitration
- Data Protection: comply with Hong Kong legal requirements

Remember these project facts and raise any consistency question now.
"""
    assert not is_hk_statutory_query({"content": prompt})


def test_same_matter_followup_without_repeating_hong_kong_is_still_gated():
    messages = [
        {"role": "user", "content": "Hong Kong trade mark Rule 13 advice?"},
        {"role": "assistant", "content": "Earlier answer"},
        {
            "role": "user",
            "content": "Did you read the working manual? The time limit is 6 months.",
        },
        _rule_13_authority_message(),
    ]
    assert is_hk_statutory_turn(messages, 2)
    assert (
        evaluate_hk_legal_answer(
            messages=messages,
            current_turn_user_idx=2,
            final_response=_rule_13_answer(),
            attempts=0,
        ).action
        == "pass"
    )


def test_same_matter_skill_edit_is_not_misclassified_as_legal_answer():
    messages = [
        {"role": "user", "content": "Hong Kong trade mark Rule 13 advice?"},
        {"role": "assistant", "content": "Earlier answer"},
        {
            "role": "user",
            "content": "Please add rules to this skill: search laws, then manuals.",
        },
    ]
    assert not is_hk_statutory_turn(messages, 2)


def test_rule_13_write_a_letter_prompt_is_not_misclassified_as_skill_edit():
    messages = [{"role": "user", "content": EXACT_RULE_13_PROMPT}]
    assert is_hk_statutory_turn(messages, 0)


def test_verified_manual_cannot_be_described_as_unread_or_truncated():
    prompt = "Hong Kong trade mark Rule 13 extension of time?"
    answer = (
        _rule_13_answer()
        + " However, I cannot confirm I read the IPD manual because it was truncated."
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": prompt}, _rule_13_authority_message()],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "delivered the official IPD manual evidence completely" in decision.message
    assert "Exact verified extract" in decision.message


def test_rule_13_time_answer_requires_manual_url_and_both_time_mechanisms():
    prompt = "Hong Kong trade mark Rule 13 extension of time?"
    incomplete = (
        "Rule 13(3) may help. Current version 2025-10-01: "
        "https://www.elegislation.gov.hk/hk/cap559A!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": prompt}, _rule_13_authority_message()],
        current_turn_user_idx=0,
        final_response=incomplete,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "Rule 13(2)" in decision.message
    assert "Rule 13(3)" in decision.message

    assert (
        evaluate_hk_legal_answer(
            messages=[
                {"role": "user", "content": prompt},
                _rule_13_authority_message(),
            ],
            current_turn_user_idx=0,
            final_response=_rule_13_answer(),
            attempts=1,
        ).action
        == "pass"
    )


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
            "content": json.dumps({
                "success": True,
                "cannot_confirm": False,
                "chapter": "559",
                "version_date": "2025-02-14",
                "official_web_url": "https://example.com/cap559",
            }),
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


def test_registered_mark_dispute_requires_complete_minimum_provision_set():
    partial = _authority_message()
    payload = json.loads(partial["content"])
    payload["requested_provisions"] = [
        {"provision": provision, "found": True}
        for provision in ("44", "45", "52", "53")
    ]
    partial["content"] = json.dumps(payload)
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": PROMPT}, partial],
        current_turn_user_idx=0,
        final_response=("2025-02-14 https://www.elegislation.gov.hk/hk/cap559!en"),
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "['11', '12']" in decision.message


def test_exact_long_reported_prompt_is_still_a_registered_mark_dispute():
    partial = _authority_message()
    payload = json.loads(partial["content"])
    payload["requested_provisions"] = [
        {"provision": provision, "found": True}
        for provision in ("44", "45", "52", "53")
    ]
    partial["content"] = json.dumps(payload)
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, partial],
        current_turn_user_idx=0,
        final_response=("2025-02-14 https://www.elegislation.gov.hk/hk/cap559!en"),
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "['11', '12']" in decision.message


def test_hkel_timestamp_is_cited_by_its_public_calendar_date():
    authority = _authority_message()
    payload = json.loads(authority["content"])
    payload["version_date"] = "2025-02-14T00:00:00"
    authority["content"] = json.dumps(payload)
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": PROMPT}, authority],
        current_turn_user_idx=0,
        final_response=(
            "Current version 2025-02-14: https://www.elegislation.gov.hk/hk/cap559!en"
        ),
        attempts=1,
    )
    assert decision.action == "pass"


def test_section_12_6_must_not_be_said_to_bar_section_53_5_b_invalidity():
    authority = _authority_message()
    misleading = (
        "Section 53(5)(b) concerns well-known marks. Section 12(6) says those "
        "grounds normally must be raised in opposition, and you missed that window. "
        "Current version 2025-02-14: "
        "https://www.elegislation.gov.hk/hk/cap559!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
        current_turn_user_idx=0,
        final_response=misleading,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "must not be presented as barring" in decision.message

    corrected = _registered_famous_mark_answer()
    assert (
        evaluate_hk_legal_answer(
            messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
            current_turn_user_idx=0,
            final_response=corrected,
            attempts=1,
        ).action
        == "pass"
    )


def test_registered_famous_mark_answer_rejects_wrong_remedy_section_titles():
    authority = _authority_message()
    wrong = (
        "Opposition is under section 21. Use rectification for bad faith under "
        "sections 45 and 52. The 5-year non-use route also uses section 52. "
        "Section 53 is a court rectification alternative. Current version "
        "2025-02-14: https://www.elegislation.gov.hk/hk/cap559!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
        current_turn_user_idx=0,
        final_response=wrong,
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "section 44 is opposition" in decision.message
    assert "section 45 is withdrawal" in decision.message
    assert "section 52 is revocation" in decision.message
    assert "section 53 is declaration of invalidity" in decision.message

    assert (
        evaluate_hk_legal_answer(
            messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
            current_turn_user_idx=0,
            final_response=_registered_famous_mark_answer(),
            attempts=1,
        ).action
        == "pass"
    )


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
        attempts=3,
    )
    assert decision.action == "fail"
    assert "無法提供" in decision.message
    assert "第47條" not in decision.message
