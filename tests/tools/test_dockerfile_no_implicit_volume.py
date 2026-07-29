"""The image must not declare VOLUME for the Hermes state paths.

A `VOLUME` instruction makes Docker create an anonymous read-write volume for
any container that does not mount something at that path itself. On the 8083
deployment that had three effects, all measured on 2026-07-29:

* `read_only: true` stopped applying. `hermes-skill-admin` declares
  `read_only`, `cap_drop: ALL`, `no-new-privileges` and `network_mode: none`,
  and still received two writable volumes.
* Volumes were stranded: `docker create` added two, `docker rm` without `-v`
  left both, and four orphans had accumulated — one holding 8,246 entries and
  276 MB of abandoned Hermes state.
* Each one first copied 7,916 files out of the image layer.

Every service that needs persistence mounts it explicitly in
docker-compose.yml, so removing the instruction costs nothing and makes the
absence of a mount mean what it says.
"""

import re
import unittest
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
STATE_PATHS = ("/home/hermes", "/opt/data")


class DockerfileVolumeTests(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text(encoding="utf-8")
        self.directives = [
            line
            for line in self.text.splitlines()
            if re.match(r"^\s*VOLUME\b", line, re.IGNORECASE)
        ]

    def test_no_volume_directive_at_all(self):
        self.assertEqual(
            self.directives,
            [],
            "a VOLUME directive silently grants writable anonymous volumes; "
            "mount explicitly in docker-compose.yml instead",
        )

    def test_state_paths_are_not_declared_as_volumes(self):
        for path in STATE_PATHS:
            for directive in self.directives:
                self.assertNotIn(path, directive)

    def test_the_reason_is_recorded_next_to_the_absence(self):
        # An absent line cannot explain itself. Without this, the next person
        # to add a VOLUME has nothing telling them why it was taken out.
        self.assertIn("No VOLUME instruction", self.text)
        self.assertIn("read_only", self.text)

    def test_env_home_still_declared(self):
        # Removing VOLUME must not disturb the path configuration around it.
        self.assertIn("ENV HERMES_HOME=/home/hermes", self.text)
        self.assertRegex(self.text, r"(?m)^ENTRYPOINT ")
        self.assertRegex(self.text, r"(?m)^CMD ")


if __name__ == "__main__":
    unittest.main()
