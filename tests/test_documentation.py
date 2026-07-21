from __future__ import annotations

import json
import re
import shlex
import unittest
from pathlib import Path

from click.testing import CliRunner

from oke_hpc_mgmt.commands import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = (PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md")))
LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")
OUTPUT_HEADING_RE = re.compile(r"Example[^\n]*output", re.IGNORECASE)
OUTPUT_FENCE_RE = re.compile(r"```(?:text|json|csv)\n")
JSON_FENCE_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _bash_blocks(document: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    lines = document.splitlines()
    in_bash = False
    start_line = 0
    content: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if not in_bash and line.strip() in {"```bash", "```sh", "```shell"}:
            in_bash = True
            start_line = line_number + 1
            content = []
            continue
        if in_bash and line.strip() == "```":
            blocks.append((start_line, "\n".join(content)))
            in_bash = False
            continue
        if in_bash:
            content.append(line)
    return blocks


def _logical_shell_lines(block: str) -> list[str]:
    logical_lines: list[str] = []
    pending = ""
    for raw_line in block.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        logical_lines.append(pending)
    return logical_lines


def _mgmt_argv(command: str) -> list[str] | None:
    tokens = shlex.split(command)
    try:
        marker_index = tokens.index("mgmt-oke")
    except ValueError:
        return None

    argv = tokens[marker_index + 1 :]
    # Synopsis lines describe grammar rather than an executable example.
    if "..." in argv or any(token.startswith(("[", "(")) for token in argv):
        return None

    # Everything after a shell operator belongs to the output consumer, not the
    # mgmt-oke argument parser. Operators inside placeholders are not surrounded
    # by whitespace and therefore remain intact.
    shell_operators = {"|", ">", ">>", "2>", "2>>", "&&", "||", ";"}
    for index, token in enumerate(argv):
        if token in shell_operators:
            return argv[:index]
    return argv


class DocumentationTests(unittest.TestCase):
    def test_docs_index_links_every_guide(self):
        index = (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        missing = [
            path.name
            for path in MARKDOWN_FILES
            if path.parent == PROJECT_ROOT / "docs"
            and path.name != "README.md"
            and f"(./{path.name})" not in index
        ]

        self.assertEqual([], missing, "Guides missing from docs/README.md")

    def test_relative_markdown_links_exist(self):
        missing: list[str] = []
        for markdown_file in MARKDOWN_FILES:
            document = markdown_file.read_text(encoding="utf-8")
            for target in LINK_RE.findall(document):
                path_text = target.split("#", maxsplit=1)[0]
                if not path_text or "://" in path_text or path_text.startswith("mailto:"):
                    continue
                target_path = (markdown_file.parent / path_text).resolve()
                if not target_path.exists():
                    missing.append(f"{markdown_file.relative_to(PROJECT_ROOT)} -> {target}")

        self.assertEqual([], missing, "Missing documentation links:\n" + "\n".join(missing))

    def test_markdown_code_fences_are_balanced(self):
        unbalanced = [
            str(path.relative_to(PROJECT_ROOT))
            for path in MARKDOWN_FILES
            if path.read_text(encoding="utf-8").count("```") % 2
        ]

        self.assertEqual([], unbalanced)

    def test_documented_json_output_is_valid(self):
        failures: list[str] = []
        parsed_blocks = 0
        for markdown_file in MARKDOWN_FILES:
            document = markdown_file.read_text(encoding="utf-8")
            for block_number, block in enumerate(JSON_FENCE_RE.findall(document), start=1):
                parsed_blocks += 1
                try:
                    json.loads(block)
                except json.JSONDecodeError as error:
                    failures.append(
                        f"{markdown_file.relative_to(PROJECT_ROOT)} "
                        f"JSON block {block_number}: {error}"
                    )

        self.assertGreaterEqual(parsed_blocks, 13)
        self.assertEqual([], failures, "Invalid JSON examples:\n" + "\n".join(failures))

    def test_documented_mgmt_commands_parse(self):
        failures: list[str] = []
        parsed_commands = 0
        runner = CliRunner()
        for markdown_file in MARKDOWN_FILES:
            document = markdown_file.read_text(encoding="utf-8")
            for block_line, block in _bash_blocks(document):
                for offset, command in enumerate(_logical_shell_lines(block)):
                    argv = _mgmt_argv(command)
                    if argv is None:
                        continue
                    parsed_commands += 1
                    result = runner.invoke(cli, [*argv, "--help"])
                    if result.exit_code != 0:
                        location = markdown_file.relative_to(PROJECT_ROOT)
                        failures.append(
                            f"{location}:{block_line + offset}: {command}: {result.output.strip()}"
                        )

        self.assertGreater(parsed_commands, 40)
        self.assertEqual([], failures, "Invalid documented commands:\n" + "\n".join(failures))

    def test_operational_docs_include_example_output(self):
        missing: list[str] = []
        operational_docs = 0
        for markdown_file in MARKDOWN_FILES:
            document = markdown_file.read_text(encoding="utf-8")
            has_executable_command = any(
                _mgmt_argv(command) is not None
                for _, block in _bash_blocks(document)
                for command in _logical_shell_lines(block)
            )
            if not has_executable_command:
                continue

            operational_docs += 1
            if not OUTPUT_HEADING_RE.search(document) or not OUTPUT_FENCE_RE.search(
                document
            ):
                missing.append(str(markdown_file.relative_to(PROJECT_ROOT)))

        self.assertGreaterEqual(operational_docs, 13)
        self.assertEqual(
            [],
            missing,
            "Operational guides without example command output:\n" + "\n".join(missing),
        )

    def test_public_docs_exclude_live_cluster_identifiers(self):
        findings: list[str] = []
        full_oci_identifier = re.compile(r"ocid1\.[a-z0-9.-]+\.[a-z0-9-]+\.[a-z0-9]{20,}")
        for markdown_file in MARKDOWN_FILES:
            document = markdown_file.read_text(encoding="utf-8")
            for forbidden in ("10.140.", "84.8.152.", "ssh-key-a10.key"):
                if forbidden in document:
                    findings.append(
                        f"{markdown_file.relative_to(PROJECT_ROOT)} contains {forbidden}"
                    )
            if full_oci_identifier.search(document):
                findings.append(
                    f"{markdown_file.relative_to(PROJECT_ROOT)} contains a full OCI identifier"
                )

        self.assertEqual([], findings, "Live identifiers in public docs:\n" + "\n".join(findings))

    def test_pool_inventory_docs_describe_fast_path_limits(self):
        discovery_guide = (
            PROJECT_ROOT / "docs" / "discovering-worker-pools-and-nodes.md"
        ).read_text(encoding="utf-8")

        self.assertIn("do not scan workload pod counts", discovery_guide)
        self.assertIn("Kueue resources", discovery_guide)
        self.assertIn("full `reconcile`", discovery_guide)

    def test_lifecycle_docs_distinguish_capacity_from_node_selection(self):
        resize_guide = (PROJECT_ROOT / "docs" / "resizing-worker-pools.md").read_text(
            encoding="utf-8"
        )
        removal_guide = (
            PROJECT_ROOT / "docs" / "removing-and-replacing-worker-nodes.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Pool-level scale-down does not select a worker", resize_guide)
        self.assertIn("use `nodes terminate`", resize_guide)
        self.assertIn("always targets selected Kubernetes workers", removal_guide)

    def test_wait_docs_cover_layered_resource_convergence(self):
        readiness_guide = (
            PROJECT_ROOT / "docs" / "verifying-gpu-and-rdma-readiness.md"
        ).read_text(encoding="utf-8")

        for field in ("oci_active", "k8s_ready", "gpu_ready", "rdma_ready"):
            self.assertIn(field, readiness_guide)
        self.assertIn("node_present=False", readiness_guide)


if __name__ == "__main__":
    unittest.main()
