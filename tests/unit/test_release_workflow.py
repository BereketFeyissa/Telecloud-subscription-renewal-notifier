"""The release workflow must not publish a floating tag.

§3.6 and §13.7 forbid `:latest`. The release workflow published it anyway on v0.1.0, v0.2.0 and
v0.3.0, because docker/metadata-action defaults to `flavor: latest=auto` and adds it for any
semver tag regardless of the `tags:` list — while a comment in the workflow claimed it was
absent. A comment is not a control; this is.

Parsed as text rather than YAML on purpose: pyyaml is not a declared dependency and §4 forbids
adding one to satisfy a test.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def metadata_block() -> str:
    """The metadata-action step, up to the start of the next step."""
    text = RELEASE.read_text()
    start = text.index("docker/metadata-action")
    rest = text[start:]
    end = rest.find("\n      - ")
    return rest if end == -1 else rest[:end]


def test_the_floating_latest_tag_is_disabled() -> None:
    assert re.search(r"latest\s*=\s*false", metadata_block()), (
        "metadata-action defaults to latest=auto and publishes ':latest' for every semver tag, "
        "which §3.6 and §13.7 forbid. Set `flavor: latest=false`."
    )


def test_the_git_sha_is_published_as_the_real_identity() -> None:
    assert "type=sha" in metadata_block(), (
        "the immutable git SHA must be one of the published tags (§13.7)"
    )


def test_no_workflow_hardcodes_a_latest_tag() -> None:
    """No workflow may name a :latest image.

    Matches an image *reference* - something attached to the colon with no space - so ci.yml's
    own guard, which greps for :latest and prints "a manifest uses :latest", is not flagged.
    Failing the check that enforces the rule would make the rule unenforceable.
    """
    reference = re.compile(r"[A-Za-z0-9._/-]+:latest")
    for workflow in (RELEASE, CI):
        offending = [
            line
            for line in workflow.read_text().splitlines()
            if reference.search(line) and not line.strip().startswith("#") and "grep" not in line
        ]
        assert not offending, f"{workflow.name} names a :latest image: {offending}"
