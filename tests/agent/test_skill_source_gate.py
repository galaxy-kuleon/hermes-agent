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


def _answer_contract():
    return _tool(
        {
            "success": True,
            "name": "tw-tmcc",
            "source_contract": {
                "required_before_answer": True,
                "declared_skill_view_examples": [
                    {"name": "tw-tmc", "file_path": "references/class-N.md"}
                ],
                "answer_contract": {
                    "class_1_34_max_items": 20,
                    "class_35_wholesale_retail_max_items": 5,
                    "class_1_34_relevant_max_items": 17,
                    "class_1_34_coverage_items": 3,
                    "class_1_34_coverage_distinct_subgroups": 3,
                    "list_every_chosen_item": True,
                    "require_total": True,
                    "require_relevant_and_coverage_sections": True,
                    "require_authoritative_item_wording": True,
                    "coverage_must_add_new_subgroups": True,
                },
            },
        }
    )


def _loaded_sources():
    return [
        _tool(
            {
                "success": True,
                "name": "tw-tmc",
                "file": "references/class-14.md",
                "content": (
                    "1401 A B jewelry Jewelry retail 珠寶 貴重金屬 寶石 項鍊 戒指 "
                    "耳環 手鏈 墜子 胸針 手環 銀 黃金 Ｋ金 珍珠 鑽石 翡翠 紅寶石\n"
                    "1402 C\n1403 D 珠寶盒\n1404 紀念章\n1406 E 手錶"
                ),
                "content_complete": True,
            }
        ),
        _tool(
            {
                "success": True,
                "name": "tw-tmc",
                "file": "references/class-35.md",
                "content": (
                    "351914 Jewelry retail Watch retail 首飾零售批發 "
                    "貴重金屬零售批發 珠寶零售 鐘錶零售"
                ),
                "content_complete": True,
            }
        ),
    ]


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


def test_referential_confirmation_inherits_recent_skill_mutation_context():
    history = [
        {
            "role": "assistant",
            "content": (
                "Do you want me to patch the hong-kong-trade-mark-enquiry "
                "skill and make this its governing rule?"
            ),
        }
    ]

    assert has_explicit_skill_mutation_intent(
        "yes, make it governing rule, permanent across sessions",
        conversation_history=history,
    )
    assert not has_explicit_skill_mutation_intent(
        "yes",
        conversation_history=[{"role": "assistant", "content": "Is that clear?"}],
    )
    assert not has_explicit_skill_mutation_intent("yes")


def test_declared_answer_contract_rejects_candidate_dump():
    decision = evaluate_skill_source_contract(
        messages=[
            {"role": "user", "content": "apply tm-twcc: 珠寶直銷 classes 14 and 35."},
            _answer_contract(),
            *_loaded_sources(),
        ],
        current_turn_user_idx=0,
        attempts=0,
        final_response=(
            "Class 14 — jewelry\n\n"
            + "\n".join(f"{index}. candidate" for index in range(1, 22))
            + "\nTotal: 21 items"
        ),
    )

    assert decision is not None
    assert decision.action == "nudge"
    assert any("Class 14 lists 21 items" in item for item in decision.diagnostics)
    assert "Class 35 section is missing" in decision.diagnostics
    assert "Rewrite the answer only" in decision.message
    assert "full-fidelity copies" in decision.message
    assert "--- authoritative source: tw-tmc / references/class-14.md ---" in decision.message
    assert "351914 Jewelry retail" in decision.message
    assert '"class_1_34_max_items": 20' in decision.message
    assert "Return only this literal outer shape" in decision.message
    assert "Class [N] — [name]" in decision.message
    assert "Total: [exact listed-item count] items" in decision.message


def test_declared_answer_contract_accepts_structured_selection():
    response = """## Class 14 — jewelry

**Relevant items:**
- 1401 jewelry:
  1. A
  2. B

**Coverage items:**
- **1402 cufflinks:**
  1. C
- **1403 jewelry boxes:**
  2. D
- **1406 watches:**
  3. E

**Total: 5 items**

Class 35 — retail

Relevant items:
- 351914 jewelry retail:
  1. Jewelry retail
- 351909 watch retail:
  2. Watch retail

**Total: 2 items**"""
    assert (
        evaluate_skill_source_contract(
            messages=[
                {"role": "user", "content": "apply tm-twcc: 珠寶直銷 classes 14 and 35."},
                _answer_contract(),
                *_loaded_sources(),
            ],
            current_turn_user_idx=0,
            attempts=0,
            final_response=response,
        )
        is None
    )


def test_explicit_smaller_item_count_overrides_default_coverage_shape():
    assert (
        evaluate_skill_source_contract(
            messages=[
                {"role": "user", "content": "apply tm-twcc class 14, only 2 items"},
                _answer_contract(),
                _loaded_sources()[0],
            ],
            current_turn_user_idx=0,
            attempts=0,
            final_response="Class 14\n1. A\n2. B\nTotal: 2 items",
        )
        is None
    )


def test_declared_answer_contract_accepts_localized_markdown_labels():
    response = """## 第 14 類 — 珠寶

### 相關項目（2）
- **1401 珠寶：**
  1. A
  2. B

### 補充涵蓋項目（3）
- **1402 袖扣：**
  1. C
- **1403 珠寶盒：**
  2. D
- **1406 手錶：**
  3. E

**小計：5 項**

## 第 35 類 — 零售

### 相關服務
- **351914 珠寶零售：**
  1. 珠寶零售
- **351909 鐘錶零售：**
  2. 鐘錶零售

**總計：2 項**"""
    assert (
        evaluate_skill_source_contract(
            messages=[
                {"role": "user", "content": "apply tm-twcc: 珠寶直銷 classes 14 and 35."},
                _answer_contract(),
                *_loaded_sources(),
            ],
            current_turn_user_idx=0,
            attempts=0,
            final_response=response,
        )
        is None
    )


def test_exhausted_answer_contract_fails_closed():
    decision = evaluate_skill_source_contract(
        messages=[
            {"role": "user", "content": "apply tm-twcc class 14"},
            _answer_contract(),
            _loaded_sources()[0],
        ],
        current_turn_user_idx=0,
        attempts=MAX_SKILL_SOURCE_NUDGES,
        final_response="Class 14\n1. A",
    )
    assert decision is not None
    assert decision.action == "fail"
    assert decision.diagnostics


def test_declared_answer_contract_rejects_non_authoritative_wording():
    response = """Class 14

Relevant items:
- 1401 jewelry:
  1. 手鏈

Coverage items:
- 1402 cufflinks:
  1. C
- 1403 jewelry boxes:
  2. D
- 1406 watches:
  3. E

Total: 4 items"""
    sources = _loaded_sources()
    class_14 = json.loads(sources[0]["content"])
    class_14["content"] = class_14["content"].replace("手鏈", "手鍊")
    sources[0]["content"] = json.dumps(class_14, ensure_ascii=False)

    decision = evaluate_skill_source_contract(
        messages=[
            {"role": "user", "content": "apply tm-twcc class 14"},
            _answer_contract(),
            sources[0],
        ],
        current_turn_user_idx=0,
        attempts=0,
        final_response=response,
    )

    assert decision is not None
    assert decision.action == "nudge"
    assert any("'手鏈'" in item for item in decision.diagnostics)


def test_declared_answer_contract_rejects_coverage_from_relevant_subgroup():
    response = """Class 14

Relevant items:
- 1401 jewelry:
  1. A

Coverage items:
- 1401 jewelry:
  1. B
- 1403 jewelry boxes:
  2. D
- 1406 watches:
  3. E

Total: 4 items"""
    decision = evaluate_skill_source_contract(
        messages=[
            {"role": "user", "content": "apply tm-twcc class 14"},
            _answer_contract(),
            _loaded_sources()[0],
        ],
        current_turn_user_idx=0,
        attempts=0,
        final_response=response,
    )

    assert decision is not None
    assert decision.action == "nudge"
    assert any("reuses relevant subgroup codes" in item for item in decision.diagnostics)
    assert any("1401" in item for item in decision.diagnostics)
