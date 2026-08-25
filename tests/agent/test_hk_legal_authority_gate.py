import json

from agent.hk_legal_authority_gate import (
    MAX_AUTHORITY_NUDGES,
    _will_artifact_errors,
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
EXACT_CROSS_SEARCH_PROMPT = (
    "apply Hong Kong Trade Mark Enquiry: please let me know if juice will be "
    "cross-class check with restaurant services"
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
                for provision in ("4", "11", "12", "44", "45", "52", "53")
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


def _cross_search_authority_message():
    return {
        "role": "tool",
        "name": "hk_legal_authority",
        "tool_call_id": "call-cross-search",
        "content": json.dumps({
            "success": True,
            "cannot_confirm": False,
            "chapter": "559",
            "version_date": "2025-02-14T00:00:00",
            "official_web_url": "https://www.elegislation.gov.hk/hk/cap559!en",
            "required_answer_citation": (
                "Hong Kong e-Legislation, Cap. 559, current version "
                "2025-02-14: https://www.elegislation.gov.hk/hk/cap559!en"
            ),
            "requested_provisions": [
                {"provision": "12", "found": True}
            ],
            "official_practice_guidance": [
                {
                    "success": True,
                    "cannot_confirm": False,
                    "matched_page_text_complete": True,
                    "title": "Cross search list",
                    "official_url": (
                        "https://www.ipd.gov.hk/filemanager/ipd/common/"
                        "trade-marks/registry-work-manual/current/eng/"
                        "Cross_search_list.pdf"
                    ),
                    "pdf_sha256": "c" * 64,
                    "requested_classes": [32, 43],
                    "classes": [
                        {
                            "class": 32,
                            "found": True,
                            "cross_search_classes": [30, 33],
                        },
                        {
                            "class": 43,
                            "found": True,
                            "cross_search_classes": [29, 30, 39, 42],
                        },
                    ],
                    "verified_extracts": [
                        {
                            "page": 20,
                            "text": "Class 32 Cross search classes : 30, 33",
                        },
                        {
                            "page": 27,
                            "text": (
                                "Class 43 Cross search classes : 29, 30, 39, 42"
                            ),
                        },
                    ],
                }
            ],
        }),
    }


def _estate_duty_authority_message(*provisions: str):
    texts = {
        "1": "1. Short title This Ordinance may be cited as the Estate Duty Ordinance.",
        "2": (
            "2. Application This Ordinance shall apply in the case of every "
            "deceased person who dies on or after 1 January 1916 and before "
            "11 February 2006."
        ),
    }
    return {
        "role": "tool",
        "name": "hk_legal_authority",
        "tool_call_id": "call-estate-duty",
        "content": json.dumps({
            "success": True,
            "cannot_confirm": False,
            "chapter": "111",
            "version_date": "2022-07-01T00:00:00",
            "official_web_url": "https://www.elegislation.gov.hk/hk/cap111!en",
            "required_answer_citation": (
                "Hong Kong e-Legislation, Cap. 111, current version "
                "2022-07-01: https://www.elegislation.gov.hk/hk/cap111!en"
            ),
            "requested_provisions": [
                {"provision": provision, "found": True, "text": texts[provision]}
                for provision in provisions
            ],
        }),
    }


def _wills_authority_message(*provisions: str):
    texts = {
        "4": "4. Wills of persons not of full age.",
        "5": "5. Signing and witnessing of a will.",
        "10": "10. A disposition to an attesting witness or spouse is void.",
        "14": "14. Will to be revoked by marriage, except in certain cases.",
        "15": "15. Effect of dissolution or annulment of marriage.",
        "16": "16. A will is construed to speak from the testator's death.",
    }
    return {
        "role": "tool",
        "name": "hk_legal_authority",
        "tool_call_id": "call-wills",
        "content": json.dumps({
            "success": True,
            "cannot_confirm": False,
            "chapter": "30",
            "version_date": "2024-08-18T00:00:00",
            "official_web_url": "https://www.elegislation.gov.hk/hk/cap30!en",
            "required_answer_citation": (
                "Hong Kong e-Legislation, Cap. 30, current version "
                "2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
            ),
            "requested_provisions": [
                {"provision": provision, "found": True, "text": texts[provision]}
                for provision in provisions
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
        "Kong. Fame in Korea alone is not enough to prove well-known status in Hong "
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


def test_cross_search_answer_requires_current_official_manual_rows():
    wrong_answer = (
        "Yes — juice (Class 32) and restaurant services (Class 43) will be "
        "cross-class checked. Current version 2025-02-14: "
        "https://www.elegislation.gov.hk/hk/cap559!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": EXACT_CROSS_SEARCH_PROMPT},
            _authority_message(),
        ],
        current_turn_user_idx=0,
        final_response=wrong_answer,
        attempts=0,
    )

    assert decision.action == "nudge"
    assert "current official IPD Cross search list" in decision.message
    assert "cross_search_classes=[32, 43]" in decision.message


def test_cross_search_answer_rejects_conclusion_contradicting_manual():
    wrong_answer = (
        "Yes — Class 32 and Class 43 will be cross-class checked. Current version "
        "2025-02-14: https://www.elegislation.gov.hk/hk/cap559!en. Manual: "
        "https://www.ipd.gov.hk/filemanager/ipd/common/trade-marks/"
        "registry-work-manual/current/eng/Cross_search_list.pdf"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": EXACT_CROSS_SEARCH_PROMPT},
            _cross_search_authority_message(),
        ],
        current_turn_user_idx=0,
        final_response=wrong_answer,
        attempts=1,
    )

    assert decision.action == "nudge"
    assert "direct answer 'No'" in decision.message
    assert "Class 32 Cross search classes : 30, 33" in decision.message
    assert "Class 43 Cross search classes : 29, 30, 39, 42" in decision.message


def test_cross_search_answer_passes_with_exact_manual_rows_and_citations():
    answer = (
        "No — Class 32 and Class 43 are not a cross-search pair in the current "
        "official list. Class 32 cross-searches Classes 30 and 33; Class 43 "
        "cross-searches Classes 29, 30, 39 and 42. Registry practice: "
        "https://www.ipd.gov.hk/filemanager/ipd/common/trade-marks/"
        "registry-work-manual/current/eng/Cross_search_list.pdf. Section 12 "
        "governs the separate legal consequence. Current version 2025-02-14: "
        "https://www.elegislation.gov.hk/hk/cap559!en"
    )

    assert (
        evaluate_hk_legal_answer(
            messages=[
                {"role": "user", "content": EXACT_CROSS_SEARCH_PROMPT},
                _cross_search_authority_message(),
            ],
            current_turn_user_idx=0,
            final_response=answer,
            attempts=1,
        ).action
        == "pass"
    )


def test_cross_search_completed_evidence_can_finalize_without_model_rewrite():
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": EXACT_CROSS_SEARCH_PROMPT},
            _cross_search_authority_message(),
        ],
        current_turn_user_idx=0,
        final_response="",
        attempts=5,
    )

    assert decision.action == "replace"
    assert decision.message.startswith("No — Classes 32 and 43 are not")
    assert "Class 32: cross-search classes 30, 33" in decision.message
    assert "Class 43: cross-search classes 29, 30, 39, 42" in decision.message
    assert "Cross_search_list.pdf" in decision.message
    assert "2025-02-14" in decision.message


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


def test_estate_duty_claim_requires_application_provision_not_short_title():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Estate duty was abolished with effect from 11 November 2018. "
        "Current version 2022-07-01: "
        "https://www.elegislation.gov.hk/hk/cap111!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
            _estate_duty_authority_message("1"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "chapter='111', provisions=['2']" in decision.message
    assert "Cap. 111 is the Estate Duty Ordinance" in decision.message
    assert "regenerate the affected artifact" in decision.message
    assert "trade-mark dispute" not in decision.message


def test_will_template_does_not_trigger_trade_mark_rule_13_practice_gate():
    prompt = (
        "Prepare a Hong Kong legal Will under the Wills Ordinance, following "
        "the attached template and best practices."
    )
    authority = _wills_authority_message("5")
    answer = (
        "The attached Will template follows section 5. Current version "
        "2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )

    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": prompt}, authority],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=3,
    )

    assert decision.action == "pass"


def test_divorced_will_requires_execution_and_divorce_provisions():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Signing and witnessing follow Cap. 30 s. 4. Divorce automatically "
        "revokes gifts to the former spouse under s. 4. Current version "
        "2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": prompt}, _wills_authority_message("4")],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "chapter='30', provisions=['5', '15']" in decision.message
    assert (
        "section 4, which concerns wills made by persons not of full age"
        in decision.message
    )
    assert "Regenerate any affected artifact" in decision.message


def test_will_rejects_wrong_sections_after_required_authority_was_read():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Execution requires two witnesses under Cap. 30 s. 4. A divorce "
        "automatically revokes a disposition to a former spouse under s. 4. "
        "Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "signing and witnessing requirements" in decision.message
    assert "effect of divorce" in decision.message
    assert "regenerate the artifact" in decision.message


def test_will_rejects_predeceased_fiction_and_blanket_marriage_revocation():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Under Cap. 30 section 15, divorce revokes any disposition or appointment "
        "for the former spouse, who is treated as having predeceased the testator. "
        "Remarriage automatically revokes the will under section 14. Signing and "
        "witnessing follow section 5. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "14", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "does not deem the former spouse to have predeceased" in decision.message
    assert "executor/trustee appointment is omitted" in decision.message
    assert "devise or bequest" in decision.message
    assert "statutory exceptions" in decision.message
    assert "regenerate the artifact" in decision.message
    assert "does not deem the former spouse" in decision.diagnostics[0]


def test_will_rejects_defective_latest_export_even_when_chat_answer_is_correct():
    prompt = (
        "Prepare a Hong Kong Last Will under the Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Signing and witnessing follow section 5. Section 14 revocation is subject "
        "to statutory exceptions. Under section 15, the former spouse's appointment "
        "as executor or trustee is omitted and any devise or bequest to that spouse "
        "lapses except where the will shows a contrary intention. Section 10 makes "
        "the disposition to an attesting witness or spouse void, not the will. "
        "Current version 2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    defective_artifact = (
        "Signing and witnessing follow section 5. Section 14 revokes a will on "
        "marriage except where the will says it should not be revoked. Under section "
        "15, the former spouse's executor appointment is omitted and a devise or "
        "bequest to that spouse lapses except where the will shows a contrary "
        "intention. Section 10 makes the disposition to an attesting witness void, "
        "not the will."
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "10", "14", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
        exported_artifact_contents=(defective_artifact,),
    )

    assert decision.action == "nudge"
    assert any(
        error.startswith("latest exported artifact:")
        for error in decision.diagnostics
    )


def test_will_rejects_invented_section_14_exceptions_in_export():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Signing and witnessing follow section 5. Section 14 revocation is subject "
        "to statutory exceptions. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    artifact = (
        "Section 14 revokes a will on marriage, subject to statutory exceptions, "
        "including a trust of a policy of assurance and becoming a parent."
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "14"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
        exported_artifact_contents=(artifact,),
    )

    assert decision.action == "nudge"
    assert any(
        "life-policy-trust or becoming-a-parent" in error
        for error in decision.diagnostics
    )


def test_will_rejects_false_female_only_section_14_scope():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Section 14 applies to wills made by women and is subject to both the "
        "power-of-appointment and contemplated-particular-marriage exceptions; "
        "for this male testator a subsequent marriage would not revoke the will. "
        "Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "14"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )

    assert decision.action == "nudge"
    assert any("female-only" in error for error in decision.diagnostics)


def test_will_rejects_incomplete_s14_and_inverted_s10_in_export():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Signing and witnessing follow section 5. Section 14 revocation is subject "
        "to statutory exceptions. Section 10 makes a disposition to an attesting "
        "witness or that witness's spouse void, not the will. Current version "
        "2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    artifact = (
        "Section 14 revokes the will on marriage, subject to statutory exceptions, "
        "where it was made in contemplation of that marriage. Gift to an attesting "
        "witness (section 10). A disposition by a will made by a beneficiary or the "
        "beneficiary's spouse is void; the will itself remains valid."
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "10", "14"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
        exported_artifact_contents=(artifact,),
    )

    assert decision.action == "nudge"
    assert any("incomplete list" in error for error in decision.diagnostics)
    assert any(
        "disposition to an attesting witness" in error
        for error in decision.diagnostics
    )


def test_operational_attestation_clause_does_not_require_citation_in_artifact():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Signing and witnessing requirements follow section 5. Current version "
        "2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    operative_artifact = (
        "SIGNED by the testator as his last will in the presence of us both present "
        "at the same time, who in his presence subscribe our names as witnesses."
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
        exported_artifact_contents=(operative_artifact,),
    )

    assert decision.action == "pass"


def test_will_rejects_mutual_witness_presence_as_section_5_minimum():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Section 5 of Cap. 30 governs execution: the testator must sign with "
        "two witnesses present at the same time, and each witness must sign in "
        "the testator's presence and in the presence of each other. Current "
        "version 2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )

    assert decision.action == "nudge"
    assert "does not require a witness" in decision.message
    assert "not an encyclopedic legal memo" in decision.message


def test_will_allows_mutual_witness_presence_as_cautious_practice_only():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Under section 5 of Cap. 30, both witnesses must be present when the "
        "testator signs or acknowledges the signature. A witness may attest and "
        "sign, or acknowledge that witness's signature, in the testator's presence "
        "but not necessarily in the presence of the other witness. As cautious "
        "execution practice, I recommend that everyone remain together and sign in "
        "each other's presence. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )

    assert decision.action == "pass", decision


def test_will_allows_explicit_correction_of_mutual_presence_fiction():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Section 5 does NOT require each witness to sign in the presence of the "
        "other witness. The 'in the presence of each other' wording in the "
        "attestation is cautious practice, not the statutory minimum. Current "
        "version 2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )

    assert decision.action == "pass", decision


def test_will_uses_deterministic_handoff_when_artifact_is_clean():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Under section 5, each witness must sign in the presence of each other. "
        "Current version 2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    export_markdown = (
        "[Download Will.docx](/api/hermes/v1/artifacts/a/Will.docx/download/t/s)\n"
        "[Download Will.pdf](/api/hermes/v1/artifacts/a/Will.pdf/download/t/s)"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
            _wills_authority_message("5"),
            {
                "role": "tool",
                "name": "local_document_export",
                "content": json.dumps(
                    {"success": True, "markdown": export_markdown}
                ),
            },
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=2,
        exported_artifact_contents=(
            "THIS IS THE LAST WILL. SIGNED by the testator in the presence of "
            "two witnesses present at the same time.",
        ),
    )

    assert decision.action == "replace"
    assert export_markdown in decision.message
    assert "current version 2024-08-18" in decision.message
    assert decision.message.count("current version 2024-08-18") == 1
    assert "each witness must sign" not in decision.message


def test_will_does_not_handoff_artifact_containing_lawyer_notes():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    answer = (
        "Under section 5, each witness must sign in the presence of each other. "
        "Current version 2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
            {
                "role": "tool",
                "name": "local_document_export",
                "content": json.dumps(
                    {
                        "success": True,
                        "markdown": "[Download Will.docx](/api/hermes/will.docx)",
                    }
                ),
            },
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=2,
        exported_artifact_contents=(
            "THIS IS THE LAST WILL. Drafting and Practice Notes "
            "(to be removed before execution).",
        ),
    )

    assert decision.action == "nudge"
    assert any("client-signature artifact" in row for row in decision.diagnostics)


def test_will_does_not_invent_alternate_executor_or_gift_fallback():
    prompt = (
        "Prepare a Hong Kong Last Will under the Wills Ordinance. Specific "
        "Bequest: my home to David. Sole Executor: David."
    )
    artifact = (
        "I APPOINT David to be the sole Executor and Trustee. I APPOINT [full "
        "name of default appointee] of [address] to be Executor and Trustee in "
        "default of David. I GIVE my home to David absolutely; should he fail "
        "to survive me, I GIVE the home to Jackson and Eric."
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
            {
                "role": "tool",
                "name": "local_document_export",
                "content": json.dumps(
                    {
                        "success": True,
                        "markdown": "[Download Will.docx](/api/hermes/will.docx)",
                    }
                ),
            },
        ],
        current_turn_user_idx=0,
        final_response=(
            "Under section 5, each witness must sign in the presence of each other. "
            "Current version 2024-08-18: "
            "https://www.elegislation.gov.hk/hk/cap30!en"
        ),
        attempts=2,
        exported_artifact_contents=(artifact,),
    )

    assert decision.action == "nudge"
    assert any("alternate or default executor" in row for row in decision.diagnostics)
    assert any("unresolved name" in row for row in decision.diagnostics)
    assert any("dispositive fallback" in row for row in decision.diagnostics)


def test_will_rejects_duplicate_specific_property_in_residue():
    prompt = (
        "Prepare a Hong Kong Last Will under the Wills Ordinance. Give the said "
        "Premises to David and divide the residuary estate equally among my three "
        "sons. Sole Executor: David."
    )
    artifact = (
        "I APPOINT David to be the sole Executor. I GIVE DEVISE and BEQUEATH "
        "the said Premises to David absolutely. I GIVE DEVISE and BEQUEATH all "
        "my estate, including the said Premises, equally among my three sons."
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5"),
            {
                "role": "tool",
                "name": "local_document_export",
                "content": json.dumps(
                    {
                        "success": True,
                        "markdown": "[Download Will.docx](/api/hermes/will.docx)",
                    }
                ),
            },
        ],
        current_turn_user_idx=0,
        final_response=(
            "Under section 5, each witness must sign in the presence of each other. "
            "Current version 2024-08-18: "
            "https://www.elegislation.gov.hk/hk/cap30!en"
        ),
        attempts=2,
        exported_artifact_contents=(artifact,),
    )

    assert decision.action == "nudge"
    assert any("specifically bequeathed" in row for row in decision.diagnostics)


def test_will_does_not_treat_no_alternate_executor_note_as_an_appointment():
    prompt = "Prepare a Hong Kong Last Will. Sole Executor: David."
    artifact = (
        "THIS IS THE LAST WILL. I APPOINT David to be the sole Executor. "
        "No alternate executor has been nominated."
    )

    assert not any(
        "alternate or default executor" in row
        for row in _will_artifact_errors({"content": prompt}, artifact)
    )


def test_will_does_not_confuse_alternate_executor_survival_with_divorce_effect():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "I declare that I am divorced and make no appointment or gift to my "
        "former spouse. If my executor predeceases me, I appoint an alternate. "
        "Signing and witnessing follow Cap. 30 section 5. Current version "
        "2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "pass"


def test_will_rejects_operates_as_if_former_spouse_were_dead_fiction():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Signing and witnessing follow Cap. 30 section 5. Under section 15, the "
        "will operates as if the former spouse were dead. An appointment of that "
        "spouse as executor or trustee is omitted and a disposition to that spouse "
        "lapses unless a contrary intention appears. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "does not deem the former spouse" in decision.message


def test_will_accepts_correct_execution_divorce_and_remarriage_sections():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Signing and witnessing are governed by Cap. 30 section 5. Under "
        "section 15, dissolution causes an appointment of the former spouse to "
        "be omitted and a devise or bequest to lapse unless contrary intention "
        "appears. Remarriage generally revokes the will under section 14, subject "
        "to its exceptions. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "14", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "pass"


def test_will_accepts_explicit_corrections_of_section_4_and_predeceased_fiction():
    prompt = (
        "Prepare a Last Will under the Hong Kong Wills Ordinance. "
        "Spouse status: divorced."
    )
    answer = (
        "Signing and witnessing are governed by Cap. 30 section 5. "
        "Section 4 concerns wills by persons not of full age and is not relevant "
        "to signing. Section 15 causes an appointment of that spouse as executor "
        "or trustee to be omitted and a disposition to that spouse to lapse unless a contrary "
        "intention appears. It does not deem the former spouse to have predeceased "
        "the testator. Marriage generally revokes a will under section 14, subject "
        "to its statutory exceptions. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "14", "15"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "pass", decision


def test_will_rejects_section_16_for_interested_witness_rule():
    prompt = "Prepare a Last Will under the Hong Kong Wills Ordinance."
    answer = (
        "Signing and witnessing follow Cap. 30 section 5. Beneficiaries must not "
        "witness because section 16 voids any legacy to an interested witness. "
        "Current version 2024-08-18: https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "10", "16"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "section 10" in decision.message
    assert "section 16" in decision.message


def test_will_accepts_correct_interested_witness_rule():
    prompt = "Prepare a Last Will under the Hong Kong Wills Ordinance."
    answer = (
        "Signing and witnessing follow Cap. 30 section 5. Under section 10, a "
        "disposition to an attesting witness or that witness's spouse is void, "
        "without invalidating the will itself. Current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": prompt},
            _wills_authority_message("5", "10"),
        ],
        current_turn_user_idx=0,
        final_response=answer,
        attempts=1,
    )
    assert decision.action == "pass", decision


def test_estate_duty_abolition_date_must_match_verified_section_2_cutoff():
    prompt = "Prepare a Hong Kong Last Will under the Wills Ordinance."
    wrong = (
        "Estate duty was abolished with effect from 11 November 2018. "
        "Current version 2022-07-01: "
        "https://www.elegislation.gov.hk/hk/cap111!en"
    )
    messages = [
        {"role": "user", "content": prompt},
        _wills_authority_message("5"),
        _estate_duty_authority_message("2"),
    ]
    decision = evaluate_hk_legal_answer(
        messages=messages,
        current_turn_user_idx=0,
        final_response=wrong,
        attempts=1,
    )
    assert decision.action == "nudge"
    assert "11 February 2006" in decision.message
    assert "not the legislation version date" in decision.message

    corrected = (
        "Estate duty was abolished for persons dying on or after "
        "11 February 2006. Current version 2022-07-01: "
        "https://www.elegislation.gov.hk/hk/cap111!en. "
        "Cap. 30 current version 2024-08-18: "
        "https://www.elegislation.gov.hk/hk/cap30!en"
    )
    assert (
        evaluate_hk_legal_answer(
            messages=messages,
            current_turn_user_idx=0,
            final_response=corrected,
            attempts=1,
        ).action
        == "pass"
    )


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
    assert "['4', '11', '12']" in decision.message


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
    assert "['4', '11', '12']" in decision.message


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
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "must not be presented as barring" in decision.message

    exhausted = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
        current_turn_user_idx=0,
        final_response=misleading,
        attempts=1,
    )
    assert exhausted.action == "replace"

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


def test_registered_mark_answer_rejects_open_opposition_after_registration():
    authority = _authority_message()
    wrong = _registered_famous_mark_answer().replace(
        "Section 44 governs opposition while the application is pending;",
        "Check whether the opposition window is still open. If it is open, file "
        "a notice of opposition under section 44;",
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
        current_turn_user_idx=0,
        final_response=wrong,
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "opposition is no longer a current remedy" in decision.message


def test_registered_mark_answer_rejects_korea_only_fame_as_strong_hk_ground():
    authority = _authority_message()
    wrong = _registered_famous_mark_answer().replace(
        "Fame in Korea alone is not enough to prove well-known status in Hong Kong.",
        "Paris Convention well-known status is the strong case because it is famous "
        "in Korea.",
    )
    decision = evaluate_hk_legal_answer(
        messages=[{"role": "user", "content": EXACT_REPORTED_PROMPT}, authority],
        current_turn_user_idx=0,
        final_response=wrong,
        attempts=0,
    )
    assert decision.action == "nudge"
    assert "Korean fame alone is not enough" in decision.message


def test_registered_mark_rewrite_exhaustion_returns_verified_safe_answer():
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": EXACT_REPORTED_PROMPT},
            _authority_message(),
        ],
        current_turn_user_idx=0,
        final_response="Opposition may still be open because the brand is famous in Korea.",
        attempts=3,
    )

    assert decision.action == "replace"
    assert "opposition is no longer the current route" in decision.message
    assert "section 53" in decision.message
    assert "section 11(5)(b)" in decision.message
    assert "section 53(5)(b)" in decision.message
    assert "at least 3 years" in decision.message
    assert "Korean-market fame, alone does not prove" in decision.message
    assert "https://www.elegislation.gov.hk/hk/cap559!en" in decision.message


def test_registered_mark_second_bad_candidate_uses_verified_safe_answer():
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": EXACT_REPORTED_PROMPT},
            _authority_message(),
        ],
        current_turn_user_idx=0,
        final_response="Opposition may still be open because the brand is famous in Korea.",
        attempts=1,
    )

    assert decision.action == "replace"
    assert "Korean-market fame, alone does not prove" in decision.message


def test_registered_mark_safe_answer_omits_unrelied_subsidiary_legislation():
    rule_authority = _rule_13_authority_message()
    decision = evaluate_hk_legal_answer(
        messages=[
            {"role": "user", "content": EXACT_REPORTED_PROMPT},
            _authority_message(),
            rule_authority,
        ],
        current_turn_user_idx=0,
        final_response="Opposition may still be open because the brand is famous in Korea.",
        attempts=3,
    )

    assert decision.action == "replace"
    assert "cap559!en" in decision.message
    assert "cap559A!en" not in decision.message


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
        attempts=MAX_AUTHORITY_NUDGES,
    )
    assert decision.action == "fail"
    assert "無法提供" in decision.message
    assert "第47條" not in decision.message
