from types import SimpleNamespace

from agent.conversation_loop import _require_referential_skill_manage


def _agent(*, required=True, api_mode="chat_completions"):
    return SimpleNamespace(
        api_mode=api_mode,
        valid_tool_names={"memory", "skill_manage"},
        _tool_guardrails=SimpleNamespace(
            referential_skill_mutation_required=required
        ),
    )


def test_first_referential_confirmation_forces_skill_manage():
    kwargs = {"model": "local"}
    _require_referential_skill_manage(_agent(), kwargs, api_call_count=1)
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "skill_manage"},
    }


def test_direct_or_later_calls_keep_normal_tool_choice():
    for agent, call_count in ((_agent(required=False), 1), (_agent(), 2)):
        kwargs = {"model": "local"}
        _require_referential_skill_manage(agent, kwargs, api_call_count=call_count)
        assert "tool_choice" not in kwargs
