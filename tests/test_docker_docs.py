"""Docker docs Quick start contract (Issue #1049).

One copy-pasted `docker run` from `docs/docker.mdx` against the published
image must reach a real delivery with no `docker exec` into the container:
the Quick start pulls the published image (no `docker build` before the
first `docker run`) and the first `docker run` block names `GH_TOKEN`,
`ORBI_SOURCE_REPO` and the required `ORBI_PI_*` provider variables #1048
introduced. The docs and the entrypoint must also agree on the `ORBI_*`
variable surface — every variable the docs name is read by
`3rd/docker/docker-entrypoint.sh`, every one the entrypoint reads is
documented in both files — and one table per file explains the three
wirings (the repository mapping, the GitHub token path, the model
key/provider path) plus the two volumes and what lives in each; an
Orbi quick guide section carries the delivery vocabulary on the page.
The Docker Hub overview (`README.dockerhub.md`, synced by the image
workflow) mirrors the Quick start `docker run` with only the image
reference swapped to Docker Hub (Issue #1054) — the pre-#1054 block
predates #1048 and started a runner with no model provider.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "3rd" / "docker" / "docker-entrypoint.sh"
DOC_FILES = (
    REPO_ROOT / "docs" / "docker.mdx",
    REPO_ROOT / "3rd" / "docker" / "README.md",
)
DOCS_PAGE = DOC_FILES[0]
ZH_DOC = REPO_ROOT / "docs" / "zh" / "docker.mdx"
SETUP_ENTRYPOINT = REPO_ROOT / "3rd" / "docker" / "docker-entrypoint.sh"
HUB_README = REPO_ROOT / "3rd" / "docker" / "README.dockerhub.md"

# The registries the image publish workflow pushes to; both carry `latest`
# and the release number without the `v` prefix (e.g. `0.5.17`).
REGISTRIES = ("ghcr.io/orbi-build/orbi", "docker.io/orbibuild/orbi")

# A standalone image-reference line inside a bash block, e.g. the
# `  ghcr.io/orbi-build/orbi:latest` a `docker run` ends with.
IMAGE_REF_LINE = re.compile(
    r"^[ \t]*(?:%s):[\w.\-]+[ \t]*$" % "|".join(re.escape(r) for r in REGISTRIES),
    re.MULTILINE,
)

# The ORBI_PI_* variables the first `docker run` must name: the required
# four from #1048 (all four must be set together) plus the three optional
# limits, which the block names in its leading comments (comments stay
# bash-safe; the pasted command is unchanged).
QUICK_START_PI_VARS = (
    "ORBI_PI_PROVIDER",
    "ORBI_PI_MODEL",
    "ORBI_PI_BASE_URL",
    "ORBI_PI_API_KEY",
)
QUICK_START_OPTIONAL_PI_VARS = (
    "ORBI_PI_API",
    "ORBI_PI_CONTEXT_WINDOW",
    "ORBI_PI_MAX_TOKENS",
)

# Entrypoint-only subprocess-test knobs — the entrypoint's own header
# comment: "The overrides keep this script's subprocess tests isolated
# without changing the production path." Not user-facing, never documented.
ENTRYPOINT_TEST_OVERRIDES = frozenset({
    "ORBI_DEPLOY_HOME",
    "ORBI_WORKSPACE",
    "ORBI_SYSTEMD_BIN",
    "ORBI_UV_BIN",
})

VAR_PATTERN = re.compile(r"ORBI_[A-Z_]+")


def section(text: str, title: str) -> str:
    """The body of a `## <title>` section."""
    match = re.search(
        rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text,
        re.DOTALL | re.MULTILINE,
    )
    assert match, f"missing '## {title}' section"
    return match.group(1)


def bash_blocks(text: str) -> list[str]:
    """The fenced bash code blocks of a markdown document."""
    return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)


def first_run_block(text: str) -> str:
    """The first fenced bash block that runs `docker run`."""
    for block in bash_blocks(text):
        if "docker run " in block:
            return block
    raise AssertionError("no docker run block found")


def test_first_run_block_fails_fast_without_a_run():
    """A page whose bash blocks never run an image fails the helper
    loudly instead of letting the contract tests pass vacuously."""
    with pytest.raises(AssertionError, match="no docker run block found"):
        first_run_block("```bash\ndocker pull ghcr.io/orbi-build/orbi:latest\n```\n")


def doc_variables(text: str) -> set[str]:
    """The ORBI_* variable families a document names. The repeatable
    ORBI_ENV_<NAME> family collapses to its ORBI_ENV_ prefix — the prefix
    is what the entrypoint reads via ${!ORBI_ENV_@}."""
    normalized = re.sub(r"ORBI_ENV_[A-Za-z0-9_]*", "ORBI_ENV_", text)
    return set(VAR_PATTERN.findall(normalized))


def entrypoint_variables() -> set[str]:
    normalized = re.sub(r"ORBI_ENV_[A-Za-z0-9_]*", "ORBI_ENV_",
                        ENTRYPOINT.read_text(encoding="utf-8"))
    return set(VAR_PATTERN.findall(normalized)) - ENTRYPOINT_TEST_OVERRIDES


def markdown_tables(text: str) -> list[str]:
    """The markdown tables of a document, each as its joined lines."""
    tables: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            current.append(line)
        elif current:
            tables.append("\n".join(current))
            current = []
    if current:
        tables.append("\n".join(current))
    return tables


def test_markdown_tables_captures_a_table_at_end_of_file():
    """A table running to end-of-file is captured by the post-loop
    flush (the current docs pages end with prose, so only a table at
    EOF reaches that branch)."""
    assert markdown_tables("prose\n\n| a | b |\n|---|---|\n| 1 | 2 |\n") == [
        "| a | b |\n|---|---|\n| 1 | 2 |",
    ]


def test_quick_start_pulls_the_published_image():
    """The Quick start pulls a published image on both registries and
    states the tags: `latest` and the release number without the `v`
    prefix (Issue #1049)."""
    quick_start = section(DOCS_PAGE.read_text(encoding="utf-8"), "Quick start")
    pulls = [line.strip() for line in quick_start.splitlines()
             if line.strip().startswith("docker pull ")]
    for registry in REGISTRIES:
        assert any(line.startswith(f"docker pull {registry}:") for line in pulls), \
            f"no docker pull for {registry}"
    run_block = first_run_block(quick_start)
    image_ref = run_block.strip().splitlines()[-1].strip()
    assert f"docker pull {image_ref}" in pulls, "the run image must be pulled first"
    assert "latest" in quick_start
    # the release number without the `v` prefix, e.g. 0.5.17
    assert re.search(r"\d+\.\d+\.\d+", quick_start)


def test_no_docker_build_before_the_first_run():
    """Nothing before the first `docker run` line builds an image — the
    pre-#1049 page ran `docker build` in the same block, immediately
    before the run (Issue #1049)."""
    text = DOCS_PAGE.read_text(encoding="utf-8")
    match = re.search(r"^docker run ", text, re.MULTILINE)
    assert match, "no docker run in the page"
    assert "docker build" not in text[: match.start()]


def test_first_run_names_the_delivery_contract():
    """The first `docker run` block names GH_TOKEN, ORBI_SOURCE_REPO and
    every ORBI_PI_* variable #1048 introduces — the required four plus
    the three optional limits (Issue #1049)."""
    block = first_run_block(DOCS_PAGE.read_text(encoding="utf-8"))
    for name in ("GH_TOKEN", "ORBI_SOURCE_REPO",
                 *QUICK_START_PI_VARS, *QUICK_START_OPTIONAL_PI_VARS):
        assert name in block, name


def with_placeholder_image(block: str) -> str:
    """The block with its image-reference line replaced by a fixed
    placeholder, so two blocks compare equal when only the registry
    differs (Issue #1054)."""
    replaced, count = IMAGE_REF_LINE.subn("IMAGE", block)
    assert count == 1, f"expected one image reference line, found {count}"
    return replaced


def test_image_placeholder_fails_fast_without_an_image_line():
    """A block without a standalone image reference line fails the
    helper loudly instead of comparing vacuously."""
    with pytest.raises(AssertionError, match="image reference line"):
        with_placeholder_image("```bash\ndocker run -d --name orbi\n```")


def test_hub_readme_run_block_mirrors_the_quick_start():
    """The Docker Hub overview's `docker run` is the docs Quick start
    block with only the image reference swapped to Docker Hub
    (Issue #1054): the pre-#1054 block predates #1048, named no
    ORBI_PI_* variable and pointed Docker Hub readers at GHCR. A later
    Quick start edit fails here until the Hub README follows."""
    docs = with_placeholder_image(first_run_block(DOCS_PAGE.read_text(encoding="utf-8")))
    hub_block = first_run_block(HUB_README.read_text(encoding="utf-8"))
    assert "docker.io/orbibuild/orbi:latest" in hub_block, "the one allowed difference"
    assert with_placeholder_image(hub_block) == docs


def test_entrypoint_and_docs_agree_on_the_variable_surface():
    """Every ORBI_* variable named in the docs is read by the entrypoint,
    and every ORBI_* variable the entrypoint reads is documented in both
    files (Issue #1049; ORBI_ENV_<NAME> collapses to its prefix)."""
    entrypoint = entrypoint_variables()
    assert entrypoint, "the entrypoint must read ORBI_* variables"
    for doc in DOC_FILES:
        documented = doc_variables(doc.read_text(encoding="utf-8"))
        assert documented == entrypoint, (
            f"{doc.name}: docs-only {sorted(documented - entrypoint)}, "
            f"entrypoint-only {sorted(entrypoint - documented)}"
        )


def test_one_wiring_table_explains_the_three_paths():
    """One table per docs file explains how the repository, the GitHub
    token and the model key/provider reach the runner — plus the two
    volumes and what lives in each, and the token's scope requirement,
    all in the SAME table (Issue #1049; maintainer acceptance restated
    2026-09-17, check 4)."""
    for doc in DOC_FILES:
        text = doc.read_text(encoding="utf-8")
        wiring = [
            table for table in markdown_tables(text)
            if all(name in table
                   for name in ("ORBI_SOURCE_REPO", "GH_TOKEN", "ORBI_PI_PROVIDER"))
        ]
        assert len(wiring) == 1, f"{doc.name}: the three wirings live in one table"
        table = wiring[0]
        # repository: cloned into the /work volume by the entrypoint;
        # bind-mount alternative with the uid 1000 rule
        assert "/work" in table and "uid 1000" in table, "repository wiring"
        # GitHub token: env file -> gh credential helper over HTTPS;
        # the scope statement lives in the same table
        assert "/orbi/.orbi/env" in table and "gh credential helper" in table, \
            "GitHub token wiring"
        assert "write access" in table, "token scope statement"
        # model key and provider: env file + generated pi-providers.json
        assert "PI_API_KEY" in table and "pi-providers.json" in table, \
            "model provider wiring"
        # the two volumes and what lives in each
        assert "orbi-deploy" in table and "orbi-work" in table, "volume rows"
        assert "orbi.toml" in table, "deploy-home volume contents"
        assert "worktrees" in table, "delivery-checkout volume contents"


def table_row(text: str, first_cell: str) -> str:
    """The table row whose first cell starts with `first_cell`."""
    for line in text.splitlines():
        if line.lstrip().startswith(f"| {first_cell}"):
            return line
    raise AssertionError(f"no table row starting with '| {first_cell}'")


# Issue #1052: the ORBI_PI_* row must describe the env file the way the
# GH_TOKEN row already does — regenerated from the injected variables on
# every start (entrypoint step 5); only pi-providers.json and orbi.toml
# are first-start-only. One tuple per docs file: page, the stale
# first-start sentence in that page's language, the required statement.
PI_ROW_FILES = (
    (REPO_ROOT / "docs" / "docker.mdx",
     "On first start the entrypoint writes `PI_API_KEY`", "every start"),
    (REPO_ROOT / "3rd" / "docker" / "README.md",
     "On first start the entrypoint writes `PI_API_KEY`", "every start"),
    (REPO_ROOT / "docs" / "zh" / "docker.mdx",
     "首次启动时 entrypoint 把 `PI_API_KEY` 写进", "每次启动"),
)


def test_pi_row_states_the_every_start_env_regeneration():
    """The ORBI_PI_* row says the env file (with `PI_API_KEY`) is
    regenerated on every start — a changed key takes effect on the next
    start — while `pi-providers.json` and `orbi.toml` are first-start-
    only (Issue #1052): not the stale sentence that froze the key at
    first start, contradicting the GH_TOKEN row on the same table."""
    for doc, stale, every_start in PI_ROW_FILES:
        row = table_row(doc.read_text(encoding="utf-8"), "`ORBI_PI_PROVIDER`")
        assert stale not in row, f"{doc.name}: stale first-start sentence"
        assert every_start in row, f"{doc.name}: no every-start statement"


def test_table_row_fails_fast_without_a_match():
    """A page without the wanted row fails the helper loudly instead of
    returning None (same helper contract as first_run_block)."""
    with pytest.raises(AssertionError, match="no table row starting"):
        table_row("prose only, no tables\n", "`ORBI_PI_PROVIDER`")


GUIDE_LABELS = (
    "ai-ready",
    "ai-in-progress",
    "ai-pr-opened",
    "ai-merged",
    "ai-blocked",
    "ai-release",
)


def test_setup_result_command_is_shared_by_docs_and_entrypoint():
    """The no-TTY setup result contract stays aligned across the English,
    Chinese, and community Docker documentation and the handover message."""
    command = "docker logs -f orbi"
    for path in (*DOC_FILES, ZH_DOC):
        assert command in path.read_text(encoding="utf-8"), path.name
    assert command in SETUP_ENTRYPOINT.read_text(encoding="utf-8")
    for path in (*DOC_FILES, ZH_DOC):
        text = path.read_text(encoding="utf-8")
        assert "setup=ok" in text and "setup_failed" in text, path.name


def test_orbi_quick_guide_is_selfcontained():
    """The Orbi quick guide section gives a Docker reader the delivery
    vocabulary without leaving the page (maintainer acceptance restated
    2026-09-17, check 4): the Issue shape with an acceptance list, which
    label starts work and what the other ai-* labels mean, how to watch
    progress, what to expect, and how a release is triggered."""
    for doc in DOC_FILES:
        guide = section(doc.read_text(encoding="utf-8"), "Orbi quick guide")
        assert "when X, should Y, actually Z" in guide, "Issue shape"
        assert "acceptance" in guide.lower(), "acceptance list"
        for label in GUIDE_LABELS:
            assert label in guide, f"{doc.name}: {label}"
        assert "milestone" in guide.lower(), "release scoping via milestone"
        assert "journalctl" in guide, "watching inside the container"
        assert "two ticks" in guide, "what to expect"
