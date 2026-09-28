"""Runtime helpers for integration commands."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._invocation_style import get_invocation_prefix
from ._agent_config import SCRIPT_TYPE_CHOICES
from ._init_options import load_init_options
from .integration_state import integration_setting, integration_settings


ParseOptions = Callable[[Any, str], dict[str, Any] | None]


def parse_integration_options(integration: Any, raw_options: str) -> dict[str, Any] | None:
    """Parse declared integration options or raise ValueError."""
    try:
        tokens = shlex.split(raw_options)
    except ValueError as exc:
        raise ValueError(f"Could not parse integration options: {exc}.") from exc
    declared = {opt.name.lstrip("-"): opt for opt in integration.options()}
    allowed = ", ".join(sorted(opt.name for opt in declared.values()))
    parsed: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise ValueError(f"Unexpected integration option value '{token}'.\nAllowed options: {allowed}")
        name, separator, value = token.lstrip("-").partition("=")
        option = declared.get(name)
        if option is None:
            raise ValueError(f"Unknown integration option '{token}'.\nAllowed options: {allowed}")
        key = name.replace("-", "_")
        if option.is_flag:
            if separator:
                raise ValueError(f"Option '{option.name}' is a flag and does not accept a value.")
            parsed[key] = True
        elif separator:
            parsed[key] = value
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            index += 1
            parsed[key] = tokens[index]
        else:
            raise ValueError(f"Option '{option.name}' requires a value.")
        index += 1
    return parsed or None


def resolve_integration_script_type(
    project_root: Path, state: dict[str, Any], key: str, script_type: str | None = None,
) -> str:
    """Resolve explicit, per-integration, saved, then platform script choices."""
    value, source = script_type, "--script"
    if not value:
        value = integration_setting(state, key).get("script")
        source = f".specify/integration.json integration_settings.{key}.script"
        if not isinstance(value, str) or not value.strip():
            value = load_init_options(project_root).get("script")
            source = ".specify/init-options.json"
        if not isinstance(value, str) or not value.strip():
            return "ps" if os.name == "nt" else "sh"
    normalized = value.strip().lower()
    if normalized not in SCRIPT_TYPE_CHOICES:
        raise ValueError(
            f"Invalid script type {value!r} from {source}. "
            f"Expected one of: {', '.join(sorted(SCRIPT_TYPE_CHOICES))}."
        )
    return normalized


def resolve_integration_options(
    integration: Any,
    state: dict[str, Any],
    key: str,
    raw_options: str | None,
    *,
    parse_options: ParseOptions = parse_integration_options,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve raw and parsed options for an integration operation."""
    if raw_options is not None:
        return raw_options, parse_options(integration, raw_options)

    setting = integration_setting(state, key)
    stored_raw = setting.get("raw_options")
    if not isinstance(stored_raw, str):
        stored_raw = None

    stored_parsed = setting.get("parsed_options")
    if isinstance(stored_parsed, dict):
        return stored_raw, stored_parsed or None

    if stored_raw:
        return stored_raw, parse_options(integration, stored_raw)

    return None, None


def with_integration_setting(
    state: dict[str, Any],
    key: str,
    integration: Any,
    *,
    script_type: str | None = None,
    raw_options: str | None = None,
    parsed_options: dict[str, Any] | None = None,
    project_root: Any = None,
) -> dict[str, dict[str, Any]]:
    """Return integration settings with *key* updated."""
    settings = integration_settings(state)
    current = dict(settings.get(key, {}))

    if script_type:
        current["script"] = script_type
    if raw_options is not None:
        current["raw_options"] = raw_options
    elif "raw_options" in current and not current.get("raw_options"):
        current.pop("raw_options", None)

    if parsed_options is not None:
        current["parsed_options"] = parsed_options
    elif raw_options is not None:
        current.pop("parsed_options", None)

    # Recompute the separator from the options actually STORED on ``current``
    # after the update, not the raw ``parsed_options`` argument. When only
    # ``script_type`` changes (``parsed_options`` and ``raw_options`` both
    # None), the previously-stored ``parsed_options`` are retained above, so
    # deriving the separator from the argument (None) would drop an
    # options-dependent separator (e.g. Copilot ``--commands`` -> ".") back to
    # the default "-".
    current["invoke_separator"] = integration.effective_invoke_separator(
        current.get("parsed_options"), project_root
    )
    settings[key] = current
    return settings


def invoke_separator_for_integration(
    integration: Any,
    state: dict[str, Any],
    key: str,
    parsed_options: dict[str, Any] | None = None,
    project_root: Any = None,
) -> str:
    """Resolve the invocation separator for stored/default integration state."""
    if parsed_options is not None:
        return integration.effective_invoke_separator(parsed_options, project_root)

    setting = integration_setting(state, key)
    stored_separator = setting.get("invoke_separator")
    if isinstance(stored_separator, str) and stored_separator:
        return stored_separator

    stored_parsed = setting.get("parsed_options")
    if isinstance(stored_parsed, dict):
        return integration.effective_invoke_separator(stored_parsed, project_root)

    return integration.effective_invoke_separator(None, project_root)


def invoke_prefix_for_integration(
    integration: Any,
    key: str,
    parsed_options: dict[str, Any] | None = None,
    project_root: Any = None,
) -> str:
    """Resolve the native invocation prefix for an integration's output mode."""
    skills_mode = integration.is_skills_mode(parsed_options, project_root)
    return get_invocation_prefix(key, skills_mode)
