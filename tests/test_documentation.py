from __future__ import annotations

import io
import re
import shlex
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from oke_hpc_mgmt.cli import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = (PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md")))
LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")


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

    def test_documented_mgmt_commands_parse(self):
        failures: list[str] = []
        parsed_commands = 0
        parser = build_parser()
        for markdown_file in MARKDOWN_FILES:
            document = markdown_file.read_text(encoding="utf-8")
            for block_line, block in _bash_blocks(document):
                for offset, command in enumerate(_logical_shell_lines(block)):
                    argv = _mgmt_argv(command)
                    if argv is None:
                        continue
                    parsed_commands += 1
                    try:
                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                            parser.parse_args(argv)
                    except SystemExit as exc:
                        if exc.code != 0:
                            location = markdown_file.relative_to(PROJECT_ROOT)
                            failures.append(f"{location}:{block_line + offset}: {command}")

        self.assertGreater(parsed_commands, 40)
        self.assertEqual([], failures, "Invalid documented commands:\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
