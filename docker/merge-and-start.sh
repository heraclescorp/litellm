#!/bin/sh
set -eu

exec python - "$@" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from typing import Final

import yaml

DEFAULT_CONFIG: Final = "/etc/litellm/config.yaml"
MCP_CONFIG: Final = "/app/mcp/mcp-servers.yml"


def read_mapping(path: str) -> dict[str, object]:
    with open(path, encoding="utf-8") as file:
        value = yaml.safe_load(file) or {}
    if not isinstance(value, dict):
        raise ValueError(f"LiteLLM config must contain a YAML mapping: {path}")
    return value


def merge_mappings(base: Mapping[str, object], overlay: Mapping[str, object], path: str = "") -> dict[str, object]:
    merged = dict(base)
    for key, overlay_value in overlay.items():
        key_path = f"{path}.{key}" if path else str(key)
        if key not in merged:
            merged[key] = overlay_value
            continue

        base_value = merged[key]
        if isinstance(base_value, Mapping) and isinstance(overlay_value, Mapping):
            merged[key] = merge_mappings(base_value, overlay_value, key_path)
            continue

        if base_value != overlay_value:
            raise ValueError(f"Conflicting values in LiteLLM config at {key_path}")

    return merged


def parse_arguments(arguments: list[str]) -> tuple[str, list[str]]:
    base_config = DEFAULT_CONFIG
    passthrough: list[str] = []
    index = 0

    while index < len(arguments):
        argument = arguments[index]
        if argument == "--config":
            if index + 1 >= len(arguments):
                raise ValueError("--config requires a path")
            base_config = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--config="):
            base_config = argument.removeprefix("--config=")
            index += 1
            continue
        passthrough.append(argument)
        index += 1

    return base_config, passthrough


base_config, passthrough_arguments = parse_arguments(sys.argv[1:])
merged_config = merge_mappings(read_mapping(base_config), read_mapping(MCP_CONFIG))

with tempfile.NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    prefix="litellm-merged-",
    suffix=".yaml",
    dir="/tmp",
    delete=False,
) as file:
    yaml.safe_dump(merged_config, file, sort_keys=False)
    merged_config_path = file.name

os.chmod(merged_config_path, 0o600)
os.execvpe(
    "litellm",
    ["litellm", "--config", merged_config_path, *passthrough_arguments],
    os.environ,
)
PY
