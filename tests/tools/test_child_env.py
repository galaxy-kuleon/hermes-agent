#!/usr/bin/env python3
"""The chat child gets what it was granted, and nothing else.

These tests exist because the *previous* three attempts at this boundary all
passed their tests and all leaked anyway:

* a frozen blocklist test built a fresh blocklist instead of exercising the one
  production consults, so the two disagreed and nobody noticed;
* two late channels (``base_env``, ``extra_env``) were never validated at all,
  because validation had been attached to one input rather than to the final
  authority;
* the vocabulary version was asserted against seven known secrets, and an
  eighth name -- one nobody had thought of -- went straight through.

So the rules here are: assert the **exact** output, not "the secret is absent";
use a canary **generated at run time**, whose spelling cannot be in any policy
list, production or test; and drive the **real** callable and the **real**
frozen objects, never a re-derived model of them.

Run with:  python -m pytest tests/tools/test_child_env.py -v
"""

import ast
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.child_env import (  # noqa: E402
    CHANNEL_BASE,
    CHANNEL_FIXED,
    CHANNEL_GENERATED,
    CHANNEL_GRANT,
    DEFAULT_BASE_NAMES,
    FORCE_PREFIX,
    REASON_DENIED,
    REASON_FORCE_PREFIX,
    REASON_INVALID_NAME,
    REASON_INVALID_VALUE,
    ChildEnvSpec,
    construct_chat_child_env,
)


def _canary() -> str:
    """A name that cannot be in any allow- or deny-list, here or in production.

    Generated per call. The whole failure of the vocabulary attempt was that
    the test knew which names to look for; this one does not.
    """
    return "CANARY_" + uuid.uuid4().hex.upper()


class PurityTests(unittest.TestCase):
    """No hidden inputs: same arguments, same result, any ambient environment."""

    def test_the_ambient_environment_is_not_an_input(self):
        secret = _canary()
        spec = ChildEnvSpec.create("chat", grants=["ALLOWED"])
        source = {"PATH": "/bin", "ALLOWED": "yes"}

        with patch.dict(os.environ, {secret: "leaked", "PATH": "/ambient"}, clear=True):
            first = construct_chat_child_env(source=source, spec=spec)
        with patch.dict(os.environ, {"TOTALLY": "different"}, clear=True):
            second = construct_chat_child_env(source=source, spec=spec)

        # The whole result, not just `.env`: a receipt that varied with the
        # ambient environment would mean the mechanism had read it somewhere.
        self.assertEqual(first, second)
        self.assertEqual(first.env, {"PATH": "/bin", "ALLOWED": "yes"})

    def test_a_noisy_source_contributes_exactly_the_named_keys(self):
        """`dict(source)` as the starting point is the mutation this kills."""
        source = {f"NOISE_{i}": str(i) for i in range(400)}
        source.update({"PATH": "/bin", "HOME": "/h", "GRANTED": "g", "SECRET": "s"})
        spec = ChildEnvSpec.create("chat", grants=["GRANTED"])

        result = construct_chat_child_env(source=source, spec=spec)

        self.assertEqual(result.env, {"PATH": "/bin", "HOME": "/h", "GRANTED": "g"})

    def test_mutating_the_spec_inputs_afterwards_changes_nothing(self):
        """frozen=True freezes the shell, not the set or dict inside it."""
        grants = {"ALLOWED"}
        fixed = {"FEATURE": "1"}
        spec = ChildEnvSpec.create("chat", grants=grants, fixed_env=fixed)

        grants.add("SNEAKY")
        fixed["SNEAKY2"] = "2"
        source = {"ALLOWED": "yes", "SNEAKY": "no"}

        result = construct_chat_child_env(source=source, spec=spec)
        self.assertEqual(result.env, {"ALLOWED": "yes", "FEATURE": "1"})

    def test_mutating_the_source_afterwards_changes_nothing(self):
        source = {"PATH": "/bin"}
        spec = ChildEnvSpec.create("chat")
        result = construct_chat_child_env(source=source, spec=spec)
        source["PATH"] = "/tampered"
        source["HOME"] = "/late"
        self.assertEqual(result.env, {"PATH": "/bin"})

    def test_the_returned_env_is_not_aliased_between_calls(self):
        spec = ChildEnvSpec.create("chat")
        a = construct_chat_child_env(source={"PATH": "/bin"}, spec=spec)
        a.env["INJECTED"] = "x"
        b = construct_chat_child_env(source={"PATH": "/bin"}, spec=spec)
        self.assertEqual(b.env, {"PATH": "/bin"})


class AuthorityOrderTests(unittest.TestCase):
    """Four channels can write the same name. Exactly one must win."""

    def test_the_last_writer_is_the_mechanisms_own_channel(self):
        name = "COLLIDE"
        spec = ChildEnvSpec.create(
            "chat",
            grants=[name],
            fixed_env={name: "from_fixed"},
            base_names=DEFAULT_BASE_NAMES | {name},
        )
        result = construct_chat_child_env(
            source={name: "from_source"},
            spec=spec,
            generated={name: "from_generated"},
        )
        self.assertEqual(result.env[name], "from_generated")
        self.assertEqual(dict(result.provenance)[name], CHANNEL_GENERATED)

    def test_fixed_beats_grant_beats_base(self):
        name = "COLLIDE"
        base_spec = ChildEnvSpec.create("chat", base_names={name})
        self.assertEqual(
            dict(construct_chat_child_env(source={name: "b"}, spec=base_spec).provenance)[name],
            CHANNEL_BASE)

        grant_spec = ChildEnvSpec.create("chat", grants=[name], base_names={name})
        self.assertEqual(
            dict(construct_chat_child_env(source={name: "b"}, spec=grant_spec).provenance)[name],
            CHANNEL_GRANT)

        fixed_spec = ChildEnvSpec.create(
            "chat", grants=[name], fixed_env={name: "f"}, base_names={name})
        got = construct_chat_child_env(source={name: "b"}, spec=fixed_spec)
        self.assertEqual(got.env[name], "f")
        self.assertEqual(dict(got.provenance)[name], CHANNEL_FIXED)


class RejectionAtEveryChannelTests(unittest.TestCase):
    """A rejected name must not re-enter through a channel that skipped the check."""

    def test_a_denied_name_is_rejected_in_base_grant_and_fixed_alike(self):
        secret = _canary()
        spec = ChildEnvSpec.create(
            "chat",
            grants=[secret],
            fixed_env={secret: "smuggled"},
            base_names={secret, "PATH"},
        )
        result = construct_chat_child_env(
            source={secret: "real_value", "PATH": "/bin"},
            spec=spec,
            denied=frozenset({secret}),
        )
        self.assertEqual(result.env, {"PATH": "/bin"})
        self.assertEqual(
            sorted(result.rejected),
            sorted([(CHANNEL_BASE, secret, REASON_DENIED),
                    (CHANNEL_GRANT, secret, REASON_DENIED),
                    (CHANNEL_FIXED, secret, REASON_DENIED)]),
            "every caller channel must report its own rejection")

    def test_a_force_prefixed_name_is_never_decoded_into_the_bare_name(self):
        bare = _canary()
        forced = FORCE_PREFIX + bare
        spec = ChildEnvSpec.create(
            "chat",
            grants=[forced],
            fixed_env={forced: "smuggled"},
            base_names={forced},
        )
        result = construct_chat_child_env(
            source={forced: "v", bare: "the_real_secret"},
            spec=spec,
            denied=frozenset({bare}),
        )
        self.assertEqual(result.env, {}, "neither spelling may survive")
        self.assertNotIn(bare, result.env)
        self.assertNotIn(forced, result.env)
        for channel in (CHANNEL_BASE, CHANNEL_GRANT, CHANNEL_FIXED):
            self.assertIn((channel, forced, REASON_FORCE_PREFIX), result.rejected,
                          f"{channel} must record the name the caller actually used")

    def test_the_force_prefix_is_rejected_even_when_nothing_is_denied(self):
        forced = FORCE_PREFIX + "ANYTHING"
        spec = ChildEnvSpec.create("chat", grants=[forced])
        result = construct_chat_child_env(source={forced: "v"}, spec=spec)
        self.assertEqual(result.env, {})
        self.assertEqual(result.rejected, ((CHANNEL_GRANT, forced, REASON_FORCE_PREFIX),))

    def test_a_receipt_never_carries_a_value(self):
        secret = _canary()
        value = "sk-" + uuid.uuid4().hex
        spec = ChildEnvSpec.create("chat", grants=[secret], fixed_env={secret: value})
        result = construct_chat_child_env(
            source={secret: value}, spec=spec, denied=frozenset({secret}))
        self.assertNotIn(value, repr(result.rejected))
        self.assertNotIn(value, repr(result.provenance))
        self.assertNotIn(value, repr(result.missing))


class NameAndValueContractTests(unittest.TestCase):
    """Contracts decided here, not inherited from os.environ by accident."""

    def test_an_authorised_name_absent_from_source_is_reported_not_invented(self):
        spec = ChildEnvSpec.create("chat", grants=["NOT_SET"])
        result = construct_chat_child_env(source={"PATH": "/bin"}, spec=spec)
        self.assertEqual(result.env, {"PATH": "/bin"})
        self.assertEqual(result.missing, ("NOT_SET",))

    def test_an_empty_string_is_a_real_value_and_is_kept(self):
        spec = ChildEnvSpec.create("chat", grants=["EMPTY"], fixed_env={"ALSO": ""})
        result = construct_chat_child_env(source={"EMPTY": ""}, spec=spec)
        self.assertEqual(result.env, {"EMPTY": "", "ALSO": ""})

    def test_names_that_execve_cannot_carry_are_rejected_with_a_receipt(self):
        for bad in ("", "HAS=EQUALS", "HAS\x00NUL", 7, None):
            with self.subTest(bad=bad):
                spec = ChildEnvSpec(kind="chat", grants=frozenset({bad}),
                                    base_names=frozenset())
                result = construct_chat_child_env(source={bad: "v"}, spec=spec)
                self.assertEqual(result.env, {})
                self.assertEqual(
                    result.rejected, ((CHANNEL_GRANT, str(bad), REASON_INVALID_NAME),))

    def test_a_non_string_value_is_rejected_rather_than_passed_to_subprocess(self):
        spec = ChildEnvSpec(kind="chat", grants=frozenset({"NUMERIC"}),
                            base_names=frozenset())
        result = construct_chat_child_env(source={"NUMERIC": 7}, spec=spec)
        self.assertEqual(result.env, {})
        self.assertEqual(result.rejected, ((CHANNEL_GRANT, "NUMERIC", REASON_INVALID_VALUE),))

    def test_case_is_significant_and_a_lowercase_twin_is_not_smuggled_through(self):
        """POSIX scope, stated: the lowercase twin is a DIFFERENT name.

        It is therefore not covered by denying the uppercase one -- which is
        exactly why it must be granted explicitly to appear at all.
        """
        spec = ChildEnvSpec.create("chat", base_names=set())
        result = construct_chat_child_env(
            source={"API_KEY": "u", "api_key": "l"},
            spec=spec,
            denied=frozenset({"API_KEY"}),
        )
        self.assertEqual(result.env, {}, "an ungranted lowercase twin is still not copied")

    def test_the_generated_channel_is_name_validated_too(self):
        spec = ChildEnvSpec.create("chat", base_names=set())
        result = construct_chat_child_env(
            source={}, spec=spec, generated={"BAD=NAME": "v", "GOOD": "ok"})
        self.assertEqual(result.env, {"GOOD": "ok"})
        self.assertEqual(result.rejected,
                         ((CHANNEL_GENERATED, "BAD=NAME", REASON_INVALID_NAME),))


class UnwiredTests(unittest.TestCase):
    """UNWIRED is a claim about the tree, so the tree is what gets parsed."""

    def test_no_production_module_imports_or_calls_the_constructor(self):
        root = Path(__file__).resolve().parents[2]
        offenders = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root)
            parts = rel.parts
            if parts[0] in {"tests", ".git", "venv", ".venv"}:
                continue
            if rel == Path("tools/child_env.py"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("child_env"):
                    offenders.append(f"{rel}:{node.lineno} imports child_env")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.endswith("child_env"):
                            offenders.append(f"{rel}:{node.lineno} imports child_env")
                elif isinstance(node, ast.Name) and node.id == "construct_chat_child_env":
                    offenders.append(f"{rel}:{node.lineno} references the constructor")
        self.assertEqual(
            offenders, [],
            "this commit is the mechanism only; wiring is a separate, visible change:\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
