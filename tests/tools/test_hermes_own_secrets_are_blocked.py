#!/usr/bin/env python3
"""Hermes' own secrets must not reach code running inside a chat.

`API_SERVER_KEY` was already blocked from the local execution environment --
somebody decided the gateway key must not be handed to a chat's subprocess --
but four siblings were not, and the asymmetry read as an oversight rather than
a decision. Found 2026-08-11 while reviewing the ACL Increment 2 boundary,
after the reviewer proved a chat can reach the shared skill writer.

This is defence in depth and nothing more: the writer secret is bind-mounted
from the host, where the kernel does not enforce the file mode, so code that
hardcodes the path still reads it. What this removes is the handed-to-you
version.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# API_SERVER_KEY is deliberately NOT here: it is DERIVED from
# hermes_cli.config at import time, so it is absent whenever that import fails
# for environmental reasons -- which made this test fail on a host missing the
# dependency, for a reason that had nothing to do with the defect. A test that
# fails for the wrong reason teaches nothing. It is asserted against the
# running container instead.
MUST_BE_BLOCKED = (
    "HERMES_SKILL_WRITER_SECRET_FILE",
    "HERMES_SKILL_WRITER_SOCKET",
    "SKIP_RAG_HANDOFF_SIGNING_KEY",
    "OPENVIKING_API_KEY",
)


class HermesOwnSecretsAreBlockedTests(unittest.TestCase):
    def test_none_of_them_reach_a_chat_subprocess(self):
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST as blocked
        leaked = [v for v in MUST_BE_BLOCKED if v not in blocked]
        self.assertEqual(leaked, [], f"these would be handed to code running in a chat: {leaked}")

    def test_the_sanitiser_actually_drops_them(self):
        """A blocklist nothing consults protects nothing."""
        from tools.environments.local import _sanitize_subprocess_env
        env = {v: "SENTINEL-" + v for v in MUST_BE_BLOCKED}
        env["PATH"] = "/usr/bin"
        out = _sanitize_subprocess_env(env)
        survived = [v for v in MUST_BE_BLOCKED if v in out]
        self.assertEqual(survived, [], f"survived sanitisation: {survived}")
        self.assertEqual(out.get("PATH"), "/usr/bin", "an ordinary variable must still pass")


if __name__ == "__main__":
    unittest.main()
