from __future__ import annotations

import base64
import gzip
import re
from copy import deepcopy
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from importlib import resources
from typing import Any

import yaml

from oke_hpc_mgmt.models import PoolCreateSpec

ASSET_PACKAGE = "oke_hpc_mgmt.assets.oci_hpc_oke"
ASSET_DIRECTORY = "/var/lib/mgmt-oke/bootstrap"
DEFAULT_MERGE_TYPE = "list(append)+dict(no_replace,recurse_list)+str(append)"
STORAGE_SCRIPT_NAMES = (
    "oke-nvme-raid.sh",
    "oke-fss-mount.sh",
    "oke-lustre-mount.sh",
)
_OKE_VERSION_PATTERNS = (
    re.compile(
        r"(oke-ubuntu-cloud-init\.sh(?:\s+|[\"']\s+)[\"']?)"
        r"v\d+\.\d+\.\d+"
    ),
    re.compile(r"(oci-oke-node-all-)\d+\.\d+\.\d+"),
    re.compile(r"(\bkubernetes-)\d+\.\d+(\b)"),
)


class BootstrapCompositionError(ValueError):
    """Raised when inherited worker cloud-init cannot be modified safely."""


def compose_worker_user_data(
    source_user_data: str,
    spec: PoolCreateSpec,
) -> str:
    if not source_user_data:
        raise BootstrapCompositionError(
            "The source pool does not contain OKE worker cloud-init."
        )

    raw = decode_user_data(source_user_data)
    message = _ensure_multipart(raw)
    if spec.storage_mode == "replace":
        _remove_official_storage_bootstrap(message)
    if spec.nvme_raid or spec.fss_mounts or spec.lustre_mounts:
        _add_storage_bootstrap(message, spec)
    if spec.cloud_init is not None:
        _append_cloud_init_payload(message, spec.cloud_init)
    if spec.kubernetes_version:
        _update_kubernetes_version(message, spec.kubernetes_version)
    if spec.ssh_public_key:
        _update_ssh_authorized_key(message, spec.ssh_public_key)
    return encode_user_data(message.as_bytes(policy=policy.default))


def decode_user_data(value: str) -> bytes:
    encoded = "".join(value.split())
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return value.encode("utf-8")
    if decoded.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(decoded)
        except OSError as exc:
            raise BootstrapCompositionError(
                "Source worker cloud-init is invalid gzip data."
            ) from exc
    if _looks_like_cloud_init(decoded):
        return decoded
    return value.encode("utf-8")


def encode_user_data(value: bytes) -> str:
    compressed = gzip.compress(value, mtime=0)
    return base64.b64encode(compressed).decode("ascii")


def load_upstream_asset(name: str) -> bytes:
    if name not in (*STORAGE_SCRIPT_NAMES, "oke-ubuntu-cloud-init.sh"):
        raise BootstrapCompositionError(f"Unknown bundled bootstrap asset: {name}")
    return resources.files(ASSET_PACKAGE).joinpath(name).read_bytes()


def _looks_like_cloud_init(value: bytes) -> bool:
    stripped = value.lstrip()
    return bool(
        stripped.startswith((b"Content-Type:", b"MIME-Version:", b"#cloud-config", b"#!"))
    )


def _ensure_multipart(raw: bytes) -> EmailMessage:
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    if parsed.is_multipart():
        return _as_email_message(parsed)

    root = EmailMessage(policy=policy.default)
    root.make_mixed()
    if parsed.get("Content-Type"):
        root.attach(_as_email_message(parsed))
    else:
        root.attach(_new_text_part(raw, _infer_content_type(raw), "00-inherited"))
    return root


def _as_email_message(message: Message) -> EmailMessage:
    if isinstance(message, EmailMessage):
        return message
    return BytesParser(policy=policy.default).parsebytes(
        message.as_bytes(policy=policy.default)
    )


def _infer_content_type(payload: bytes) -> str:
    stripped = payload.lstrip()
    if stripped.startswith(b"#cloud-config"):
        return "text/cloud-config"
    if stripped.startswith(b"#!"):
        return "text/x-shellscript"
    return "text/plain"


def _new_text_part(
    payload: bytes,
    content_type: str,
    filename: str,
) -> EmailMessage:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapCompositionError(
            f"Cloud-init part {filename!r} is not UTF-8 text."
        ) from exc
    maintype, subtype = content_type.split("/", 1)
    if maintype != "text":
        raise BootstrapCompositionError(
            f"Only text cloud-init parts are supported: {content_type}"
        )
    part = EmailMessage(policy=policy.default)
    part.set_content(text, subtype=subtype, charset="utf-8")
    part.add_header("Content-Disposition", "attachment", filename=filename)
    part["X-Merge-Type"] = DEFAULT_MERGE_TYPE
    return part


def _append_cloud_init_payload(message: EmailMessage, payload: bytes) -> None:
    parsed = BytesParser(policy=policy.default).parsebytes(payload)
    if parsed.is_multipart():
        for part in parsed.walk():
            if not part.is_multipart():
                message.attach(deepcopy(_as_email_message(part)))
        return
    if parsed.get("Content-Type"):
        message.attach(_as_email_message(parsed))
        return
    message.attach(_new_text_part(payload, _infer_content_type(payload), "90-user"))


def _remove_official_storage_bootstrap(message: EmailMessage) -> None:
    for part in _cloud_config_parts(message):
        data = _load_cloud_config(part)
        changed = False
        commands = data.get("runcmd")
        if isinstance(commands, list):
            filtered = [
                command
                for command in commands
                if not _contains_storage_script(command)
            ]
            if filtered != commands:
                changed = True
                if filtered:
                    data["runcmd"] = filtered
                else:
                    data.pop("runcmd", None)
        write_files = data.get("write_files")
        if isinstance(write_files, list):
            filtered_files = [
                entry
                for entry in write_files
                if not _contains_storage_script(
                    entry.get("path", "") if isinstance(entry, dict) else entry
                )
            ]
            if filtered_files != write_files:
                changed = True
                if filtered_files:
                    data["write_files"] = filtered_files
                else:
                    data.pop("write_files", None)
        if changed:
            _replace_cloud_config(part, data)


def _add_storage_bootstrap(message: EmailMessage, spec: PoolCreateSpec) -> None:
    target = _find_oke_bootstrap_cloud_config(message)
    if target is None and spec.nvme_raid:
        raise BootstrapCompositionError(
            "NVMe RAID must run before OKE bootstrap, but the inherited cloud-init "
            "does not expose the official OKE bootstrap command."
        )
    if target is None:
        target = _new_text_part(
            b"#cloud-config\n{}\n",
            "text/cloud-config",
            "80-storage.yml",
        )
        message.attach(target)

    data = _load_cloud_config(target)
    write_files = list(data.get("write_files") or [])
    commands = list(data.get("runcmd") or [])
    generated_files, before_bootstrap, after_bootstrap = _storage_entries(spec)
    write_files.extend(generated_files)
    bootstrap_index = _oke_bootstrap_index(commands)
    if before_bootstrap and bootstrap_index is None:
        raise BootstrapCompositionError(
            "NVMe RAID must run before OKE bootstrap, but no bootstrap command was found."
        )
    insert_at = bootstrap_index if bootstrap_index is not None else 0
    commands[insert_at:insert_at] = before_bootstrap
    if bootstrap_index is not None:
        insert_after = bootstrap_index + len(before_bootstrap) + 1
    else:
        insert_after = len(commands)
    commands[insert_after:insert_after] = after_bootstrap
    data["write_files"] = write_files
    data["runcmd"] = commands
    _replace_cloud_config(target, data)


def _storage_entries(
    spec: PoolCreateSpec,
) -> tuple[list[dict[str, str]], list[list[str]], list[list[str]]]:
    names: list[str] = []
    before: list[list[str]] = []
    after: list[list[str]] = []
    if spec.nvme_raid:
        names.append("oke-nvme-raid.sh")
        before.append(
            [
                "bash",
                f"{ASSET_DIRECTORY}/oke-nvme-raid.sh",
                str(spec.nvme_raid.level),
                spec.nvme_raid.device_pattern,
                spec.nvme_raid.mount_path,
            ]
        )
    if spec.fss_mounts:
        names.append("oke-fss-mount.sh")
        after.extend(
            [
                "bash",
                f"{ASSET_DIRECTORY}/oke-fss-mount.sh",
                mount.export_path,
                mount.mount_path,
                mount.mount_target_ip,
            ]
            for mount in spec.fss_mounts
        )
    if spec.lustre_mounts:
        names.append("oke-lustre-mount.sh")
        after.extend(
            [
                "bash",
                f"{ASSET_DIRECTORY}/oke-lustre-mount.sh",
                mount.management_address,
                mount.filesystem_name,
                mount.mount_path,
            ]
            for mount in spec.lustre_mounts
        )

    files = [
        {
            "path": f"{ASSET_DIRECTORY}/{name}",
            "owner": "root:root",
            "permissions": "0755",
            "encoding": "b64",
            "content": base64.b64encode(load_upstream_asset(name)).decode("ascii"),
        }
        for name in dict.fromkeys(names)
    ]
    return files, before, after


def _find_oke_bootstrap_cloud_config(
    message: EmailMessage,
) -> EmailMessage | None:
    for part in _cloud_config_parts(message):
        data = _load_cloud_config(part)
        commands = data.get("runcmd")
        if isinstance(commands, list) and _oke_bootstrap_index(commands) is not None:
            return part
    return None


def _oke_bootstrap_index(commands: list[Any]) -> int | None:
    for index, command in enumerate(commands):
        if "oke-ubuntu-cloud-init.sh" in _command_text(command):
            return index
    return None


def _contains_storage_script(value: Any) -> bool:
    text = _command_text(value)
    return any(name in text for name in STORAGE_SCRIPT_NAMES)


def _command_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value)


def _update_kubernetes_version(
    message: EmailMessage,
    kubernetes_version: str,
) -> None:
    for part in _text_parts(message):
        text = _part_text(part)
        updated = text
        updated = _OKE_VERSION_PATTERNS[0].sub(
            rf"\g<1>{kubernetes_version}",
            updated,
        )
        version_without_prefix = kubernetes_version.removeprefix("v")
        updated = _OKE_VERSION_PATTERNS[1].sub(
            rf"\g<1>{version_without_prefix}",
            updated,
        )
        major_minor = ".".join(version_without_prefix.split(".")[:2])
        updated = _OKE_VERSION_PATTERNS[2].sub(
            rf"\g<1>{major_minor}\g<2>",
            updated,
        )
        if updated != text:
            _replace_text_part(part, updated)


def _update_ssh_authorized_key(
    message: EmailMessage,
    ssh_public_key: str,
) -> None:
    updated = False
    for part in _cloud_config_parts(message):
        data = _load_cloud_config(part)
        if "ssh_authorized_keys" not in data:
            continue
        data["ssh_authorized_keys"] = [ssh_public_key]
        _replace_cloud_config(part, data)
        updated = True
    if not updated:
        part = _new_text_part(
            yaml.safe_dump(
                {"ssh_authorized_keys": [ssh_public_key]},
                sort_keys=False,
            ).encode("utf-8"),
            "text/cloud-config",
            "85-ssh-key.yml",
        )
        message.attach(part)


def _cloud_config_parts(message: EmailMessage) -> list[EmailMessage]:
    return [
        part
        for part in _text_parts(message)
        if part.get_content_type() == "text/cloud-config"
        or _part_text(part).lstrip().startswith("#cloud-config")
    ]


def _text_parts(message: EmailMessage) -> list[EmailMessage]:
    return [
        _as_email_message(part)
        for part in message.walk()
        if not part.is_multipart() and part.get_content_maintype() == "text"
    ]


def _load_cloud_config(part: EmailMessage) -> dict[str, Any]:
    text = _part_text(part)
    if text.lstrip().startswith("#cloud-config"):
        text = text[text.index("#cloud-config") + len("#cloud-config") :]
    try:
        loaded = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise BootstrapCompositionError(
            f"Cannot parse inherited cloud-config part {part.get_filename() or '<unnamed>'}."
        ) from exc
    if not isinstance(loaded, dict):
        raise BootstrapCompositionError(
            f"Inherited cloud-config part {part.get_filename() or '<unnamed>'} "
            "must contain a mapping."
        )
    return loaded


def _replace_cloud_config(part: EmailMessage, data: dict[str, Any]) -> None:
    content = "#cloud-config\n" + yaml.safe_dump(data, sort_keys=False)
    _replace_text_part(part, content, content_type="text/cloud-config")


def _part_text(part: EmailMessage) -> str:
    content = part.get_content()
    if isinstance(content, bytes):
        try:
            return content.decode(part.get_content_charset() or "utf-8")
        except UnicodeDecodeError as exc:
            raise BootstrapCompositionError(
                f"Cloud-init part {part.get_filename() or '<unnamed>'} is not UTF-8 text."
            ) from exc
    return str(content)


def _replace_text_part(
    part: EmailMessage,
    content: str,
    *,
    content_type: str | None = None,
) -> None:
    target_type = content_type or part.get_content_type()
    _maintype, subtype = target_type.split("/", 1)
    filename = part.get_filename()
    merge_type = part.get("X-Merge-Type")
    part.clear_content()
    part.set_content(content, subtype=subtype, charset="utf-8")
    if filename:
        part.add_header("Content-Disposition", "attachment", filename=filename)
    if merge_type:
        part["X-Merge-Type"] = merge_type
