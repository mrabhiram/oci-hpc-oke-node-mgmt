from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_KUBECONFIG = "~/.kube/config"
OKE_TOKEN_COMMAND = ("ce", "cluster", "generate-token")


class KubeconfigDiscoveryError(RuntimeError):
    """Raised when an OKE cluster reference cannot be read from kubeconfig."""


@dataclass(frozen=True)
class OkeKubeconfigContext:
    context_name: str
    cluster_name: str
    cluster_id: str
    region: str | None


def load_oke_kubeconfig_context(
    kubeconfig: str | None = None,
    context: str | None = None,
) -> OkeKubeconfigContext:
    """Load the selected OKE cluster OCID and region from kubeconfig."""

    raw_paths = kubeconfig or os.getenv("KUBECONFIG") or DEFAULT_KUBECONFIG
    paths = [Path(value).expanduser() for value in raw_paths.split(os.pathsep) if value]
    if not paths:
        raise KubeconfigDiscoveryError("No kubeconfig path is configured.")

    merged: dict[str, Any] = {
        "clusters": [],
        "contexts": [],
        "users": [],
    }
    named_entries: dict[str, dict[str, dict[str, Any]]] = {
        "clusters": {},
        "contexts": {},
        "users": {},
    }
    loaded_paths: list[Path] = []
    current_context: str | None = None

    for path in paths:
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise KubeconfigDiscoveryError(f"Cannot read kubeconfig {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise KubeconfigDiscoveryError(f"Cannot parse kubeconfig {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise KubeconfigDiscoveryError(f"Kubeconfig {path} does not contain a YAML mapping.")

        loaded_paths.append(path)
        if current_context is None:
            value = document.get("current-context")
            if isinstance(value, str) and value:
                current_context = value

        for section in named_entries:
            entries = document.get(section, [])
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise KubeconfigDiscoveryError(
                    f"Kubeconfig {path} field '{section}' must be a list."
                )
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise KubeconfigDiscoveryError(
                        f"Kubeconfig {path} contains an invalid '{section}' entry."
                    )
                named_entries[section].setdefault(entry["name"], entry)

    if not loaded_paths:
        locations = os.pathsep.join(str(path) for path in paths)
        raise KubeconfigDiscoveryError(f"Kubeconfig file not found: {locations}")

    for section, entries in named_entries.items():
        merged[section] = list(entries.values())
    if current_context:
        merged["current-context"] = current_context
    return parse_oke_kubeconfig_context(merged, context=context)


def parse_oke_kubeconfig_context(
    config: dict[str, Any],
    context: str | None = None,
) -> OkeKubeconfigContext:
    """Extract an OCI CLI OKE token target from parsed kubeconfig data."""

    contexts = _index_named_entries(config, "contexts")
    clusters = _index_named_entries(config, "clusters")
    users = _index_named_entries(config, "users")

    selected_name = context
    if selected_name is None:
        current = config.get("current-context")
        if isinstance(current, str) and current:
            selected_name = current
    if selected_name is None:
        selected_name = _single_cluster_context_name(contexts, clusters)
    if selected_name not in contexts:
        raise KubeconfigDiscoveryError(f"Kubeconfig context not found: {selected_name}")

    selected = _required_mapping(contexts[selected_name], "context", "context")
    cluster_name = selected.get("cluster")
    user_name = selected.get("user")
    if not isinstance(cluster_name, str) or cluster_name not in clusters:
        raise KubeconfigDiscoveryError(
            f"Kubeconfig context '{selected_name}' does not reference a defined cluster."
        )
    if not isinstance(user_name, str) or user_name not in users:
        raise KubeconfigDiscoveryError(
            f"Kubeconfig context '{selected_name}' does not reference a defined user."
        )

    user = _required_mapping(users[user_name], "user", "user")
    exec_config = _required_mapping(user, "exec", "user exec configuration")
    command = exec_config.get("command")
    args = exec_config.get("args")
    if not isinstance(command, str) or Path(command).name != "oci":
        raise KubeconfigDiscoveryError(
            f"Kubeconfig context '{selected_name}' does not use the OCI CLI exec plugin."
        )
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        raise KubeconfigDiscoveryError(
            f"Kubeconfig context '{selected_name}' has invalid OCI CLI exec arguments."
        )
    if not _contains_sequence(args, OKE_TOKEN_COMMAND):
        raise KubeconfigDiscoveryError(
            f"Kubeconfig context '{selected_name}' does not generate an OKE cluster token."
        )

    cluster_id = _option_value(args, "--cluster-id")
    if not cluster_id or not cluster_id.startswith("ocid1.cluster."):
        raise KubeconfigDiscoveryError(
            f"Kubeconfig context '{selected_name}' does not contain a valid OKE cluster OCID."
        )
    region = _option_value(args, "--region")
    return OkeKubeconfigContext(
        context_name=selected_name,
        cluster_name=cluster_name,
        cluster_id=cluster_id,
        region=region,
    )


def _index_named_entries(config: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    entries = config.get(section, [])
    if not isinstance(entries, list):
        raise KubeconfigDiscoveryError(f"Kubeconfig field '{section}' must be a list.")
    indexed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise KubeconfigDiscoveryError(f"Kubeconfig contains an invalid '{section}' entry.")
        indexed.setdefault(entry["name"], entry)
    return indexed


def _single_cluster_context_name(
    contexts: dict[str, dict[str, Any]],
    clusters: dict[str, dict[str, Any]],
) -> str:
    if len(contexts) == 1:
        return next(iter(contexts))
    if len(clusters) == 1:
        cluster_name = next(iter(clusters))
        matching = [
            name
            for name, entry in contexts.items()
            if isinstance(entry.get("context"), dict)
            and entry["context"].get("cluster") == cluster_name
        ]
        users = {
            entry["context"].get("user")
            for name, entry in contexts.items()
            if name in matching and isinstance(entry.get("context"), dict)
        }
        if matching and len(users) == 1:
            return sorted(matching)[0]
    raise KubeconfigDiscoveryError(
        "Kubeconfig has no current context and does not identify a single cluster. "
        "Select one with --context."
    )


def _required_mapping(parent: dict[str, Any], key: str, description: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise KubeconfigDiscoveryError(f"Kubeconfig {description} is missing or invalid.")
    return value


def _contains_sequence(values: list[str], expected: tuple[str, ...]) -> bool:
    width = len(expected)
    return any(tuple(values[index : index + width]) == expected for index in range(len(values)))


def _option_value(args: list[str], option: str) -> str | None:
    prefix = f"{option}="
    for index, value in enumerate(args):
        if value.startswith(prefix):
            return value[len(prefix) :] or None
        if value == option:
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                return None
            return args[index + 1]
    return None
