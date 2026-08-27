"""Mermaid diagram contract (Issue #116).

The docs must ship two diagrams, in BOTH languages (English at the
`docs/` root, Chinese under `docs/zh/`):

1. the system architecture overview (index page): GitHub Issues/PRs,
   the systemd timer/service, the Runner, the Pi sessions, the task
   worktree, the core llama-server path and the OPTIONAL
   `local-llm-kv-cache` proxy clearly distinguished from it;
2. the task lifecycle / state machine (workflow page): all six delivery
   states plus the Epic / Release task / P0 boundary.

Mintlify renders Mermaid from fenced `mermaid` code blocks (verified
against the official component docs); the fenced source stays readable
in GitHub Markdown. These tests fail when a diagram is missing in either
language, when a required node/state disappears, when the optional proxy
is not marked optional, or when the Mermaid syntax is broken (unbalanced
subgraphs/brackets, a diagram that does not start with a supported
type keyword).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# The diagram types Mermaid (and therefore Mintlify) supports for these
# diagrams; a block that starts with anything else is a broken diagram.
DIAGRAM_START_PATTERN = re.compile(
    r"^\s*(flowchart|graph|sequenceDiagram|stateDiagram-v2|classDiagram|erDiagram|gantt|pie|journey|gitGraph|mindmap|timeline|quadrantChart|sankey-beta|xychart-beta|block)\b"
)


def mermaid_blocks(text: str) -> list[str]:
    """Every fenced ```mermaid block of a page (the Mintlify contract)."""
    return re.findall(r"```mermaid\n(.*?)```", text, flags=re.DOTALL)


def page_text(rel_path: str) -> str:
    path = DOCS_DIR / rel_path
    assert path.is_file(), f"missing docs page: {path}"
    return path.read_text(encoding="utf-8")


def check_syntax(blocks: list[str], where: str) -> None:
    """Fail fast on a broken diagram: every block must start with a
    supported diagram type, keep balanced subgraph/end and
    brackets/parens, and keep ASCII parens out of `|...|` edge labels
    (verified against the real mermaid v11 parser: an unquoted `(` in an
    edge label breaks the flowchart grammar, while quoted node labels
    may contain parens)."""
    assert blocks, f"{where}: no ```mermaid block found"
    for block in blocks:
        assert DIAGRAM_START_PATTERN.search(block), (
            f"{where}: mermaid block does not start with a supported "
            f"diagram type: {block.splitlines()[0] if block.strip() else '<empty>'!r}"
        )
        subgraphs = len(re.findall(r"^\s*subgraph\b", block, flags=re.MULTILINE))
        ends = len(re.findall(r"^\s*end\b", block, flags=re.MULTILINE))
        assert subgraphs == ends, (
            f"{where}: unbalanced subgraph/end ({subgraphs} subgraphs, {ends} ends)"
        )
        for open_ch, close_ch in (("[", "]"), ("(", ")")):
            assert block.count(open_ch) == block.count(close_ch), (
                f"{where}: unbalanced {open_ch}{close_ch} in the diagram"
            )
        for line in block.splitlines():
            for label in re.findall(r"\|([^|]*)\|", line):
                assert label.count("(") == label.count(")"), (
                    f"{where}: unbalanced parens inside an edge label "
                    f"break the mermaid flowchart grammar: {line.strip()!r}"
                )


def test_state_machine_diagram_exists_in_both_languages():
    for rel in ("workflow.mdx", "zh/workflow.mdx"):
        blocks = mermaid_blocks(page_text(rel))
        check_syntax(blocks, f"{rel}")
        diagram = "\n".join(blocks)
        for state in (
            "ai-ready", "ai-in-progress", "ai-pr-opened",
            "ai-fix-needed", "ai-merged", "ai-blocked",
        ):
            assert state in diagram, (
                f"{rel}: state machine diagram misses the {state} state"
            )
        # The diagram must mark the Epic / Release task / P0 boundary
        # (Issue #116: 标出 Epic/Release task/P0 的边界).
        assert "Epic" in diagram, f"{rel}: diagram misses the Epic boundary"
        assert "Release" in diagram, f"{rel}: diagram misses the Release task boundary"
        assert "P0" in diagram, f"{rel}: diagram misses the P0 boundary"


def test_architecture_diagram_exists_in_both_languages():
    for rel in ("index.mdx", "zh/index.mdx"):
        blocks = mermaid_blocks(page_text(rel))
        check_syntax(blocks, f"{rel}")
        diagram = "\n".join(blocks)
        # The required actors (Issue #116 requirement list).
        assert "GitHub" in diagram, f"{rel}: diagram misses GitHub (Issues/PRs)"
        assert "timer" in diagram.lower(), f"{rel}: diagram misses the systemd timer"
        assert "Runner" in diagram, f"{rel}: diagram misses the Runner"
        assert "Pi" in diagram, f"{rel}: diagram misses the Pi sessions"
        assert "worktree" in diagram.lower(), f"{rel}: diagram misses the task worktree"
        assert "llama" in diagram.lower(), (
            f"{rel}: diagram misses the core llama-server path"
        )
        assert "local-llm-kv-cache" in diagram, (
            f"{rel}: diagram misses the optional KV proxy"
        )
        assert "PR" in diagram, f"{rel}: diagram misses the PR/review/merge chain"


def test_architecture_diagram_marks_the_proxy_as_optional():
    """Acceptance: the diagrams must clearly distinguish the core
    llama-server path from the OPTIONAL `local-llm-kv-cache` proxy — the
    proxy node/label must carry an optional marker in both languages."""
    for rel in ("index.mdx", "zh/index.mdx"):
        diagram = "\n".join(mermaid_blocks(page_text(rel)))
        proxy_lines = [
            line for line in diagram.splitlines()
            if "local-llm-kv-cache" in line
        ]
        assert proxy_lines, f"{rel}: no proxy line in the architecture diagram"
        marked = any(
            "optional" in line.lower() or "可选" in line
            for line in proxy_lines
        )
        assert marked, (
            f"{rel}: the KV proxy node must be marked optional/可选 in the diagram: "
            f"{proxy_lines!r}"
        )
        # The core path must not be marked optional.
        core_lines = [
            line for line in diagram.splitlines()
            if "llama" in line.lower() and "local-llm-kv-cache" not in line
        ]
        assert core_lines, f"{rel}: no core llama-server line in the diagram"
        assert not any(
            "optional" in line.lower() or "可选" in line for line in core_lines
        ), (
            f"{rel}: the core llama-server path must not be marked optional: "
            f"{core_lines!r}"
        )
