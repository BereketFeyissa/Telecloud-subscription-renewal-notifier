"""Floating tags may be published, but never consumed.

§3.6 and §13.7 forbid `:latest` **in a manifest** — the hazard is deploying or building from a
tag that can move underneath you, not the tag existing in a registry. Publishing `:latest` as a
pointer to the newest release is a convenience for people pulling by hand.

So these tests guard the consuming side: the Dockerfile must build from a digest, manifests must
not name a floating tag, and the release must still publish the immutable git SHA that
deployments actually use.

Parsed as text rather than YAML on purpose: pyyaml is not a declared dependency and §4 forbids
adding one to satisfy a test.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = ROOT / "Dockerfile"
OVERLAYS = ROOT / "deploy" / "overlays"

#: An image reference, not prose: attached to the colon with no space, so an error message
#: reading "a manifest uses :latest" is not a match.
FLOATING = re.compile(r"[A-Za-z0-9._/-]+:latest")


def metadata_block() -> str:
    text = RELEASE.read_text()
    rest = text[text.index("docker/metadata-action") :]
    end = rest.find("\n      - ")
    return rest if end == -1 else rest[:end]


def test_the_release_publishes_the_immutable_git_sha() -> None:
    """The SHA is what manifests deploy, so it must always be published."""
    assert "type=sha" in metadata_block(), "the git SHA must be one of the published tags (§13.7)"


def test_the_dockerfile_builds_from_a_digest() -> None:
    """A base image pinned by tag alone can change underneath a rebuild (§13.2)."""
    froms = [ln for ln in DOCKERFILE.read_text().splitlines() if ln.startswith("FROM ")]
    assert froms, "no FROM lines found"
    for line in froms:
        assert "@sha256:" in line, f"base image is not digest-pinned: {line}"


def test_no_overlay_deploys_a_floating_tag() -> None:
    """Deploying :latest is the actual hazard §3.6 and §13.7 are about."""
    for path in sorted(OVERLAYS.rglob("*.yaml")):
        offending = [
            line
            for line in path.read_text().splitlines()
            if FLOATING.search(line) and not line.strip().startswith("#")
        ]
        assert not offending, f"{path.relative_to(ROOT)} deploys a floating tag: {offending}"


def test_ci_checks_rendered_manifests_for_floating_tags() -> None:
    """The overlay check above only sees source; CI must check the rendered output too."""
    assert ":latest" in CI.read_text(), (
        "ci.yml no longer guards rendered manifests against floating tags"
    )
