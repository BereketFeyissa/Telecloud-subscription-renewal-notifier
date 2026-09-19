"""Configuration must stay documented.

CLAUDE.md §9 requires `.env.example` to list every setting, and §17 makes it a Definition of
Done item. Both are prose, and prose is forgettable — this makes the requirement executable, so
adding a setting without documenting it fails CI rather than being noticed months later by
someone wondering why a knob they need is undiscoverable.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tele_scraper.config import Route, Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
CLAUDE_MD = ROOT / "CLAUDE.md"


def env_keys() -> set[str]:
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.M)}


def routing_example() -> str:
    line = next(
        row for row in ENV_EXAMPLE.read_text().splitlines() if row.startswith("NOTIFY_ROUTES_JSON=")
    )
    return line.partition("=")[2]


def test_every_setting_appears_in_env_example() -> None:
    missing = sorted({name.upper() for name in Settings.model_fields} - env_keys())
    assert not missing, (
        f"settings absent from .env.example: {missing}. CLAUDE.md §9 requires every variable "
        "to be listed there, with a placeholder and a one-line comment."
    )


def test_env_example_has_no_settings_that_no_longer_exist() -> None:
    """Catches the reverse drift: a removed setting still advertised to operators."""
    known = {name.upper() for name in Settings.model_fields}
    # Keys referenced indirectly by the routing table's address_env, not Settings fields.
    indirect = {k for k in env_keys() if k.endswith("_WEBHOOK_OPS")}
    stale = sorted(env_keys() - known - indirect)
    assert not stale, f".env.example documents settings that no longer exist: {stale}"


def test_every_route_field_is_shown_in_the_env_example() -> None:
    """Route fields live inside NOTIFY_ROUTES_JSON, so the §9 variable rule never covers them.

    That is exactly how `mode` and `summary_ack` shipped undocumented.
    """
    example = routing_example()
    missing = sorted(f for f in Route.model_fields if f not in example)
    assert not missing, (
        f"route fields missing from the .env.example routing example: {missing}. They are not "
        "environment variables, so nothing else makes them discoverable."
    )


def test_the_env_example_routing_table_actually_parses() -> None:
    table = json.loads(routing_example())
    assert Route.model_validate(table["routes"][0])


def test_every_route_field_is_documented_in_claude_md() -> None:
    text = CLAUDE_MD.read_text()
    missing = sorted(f for f in Route.model_fields if f'"{f}"' not in text)
    assert not missing, f"route fields absent from the CLAUDE.md §8 routing shape: {missing}"


@pytest.mark.parametrize(
    "setting",
    ["mode", "summary_ack", "require_acknowledgement", "detect_missing_components"],
)
def test_behaviour_changing_settings_are_explained_not_merely_listed(setting: str) -> None:
    """A knob that changes who gets notified needs a reason recorded, not just a name."""
    text = CLAUDE_MD.read_text().lower()
    assert setting.replace("_", " ") in text or setting in text, (
        f"{setting} changes notification behaviour but is not explained in CLAUDE.md"
    )
