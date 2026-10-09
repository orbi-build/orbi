"""orbi suggest (Issue #1576): the command, the Suggestions document,
the setup offer and the skill contract.

Pi and gh are stubbed through tests/seam.py: the one subprocess seam is a
recorder that routes gh to FakeGh and git to the real CLI; stream_pi is
replaced by a canned answer. The run directory, context.json and the
recorded argv are the observable evidence.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import orbi.cli as cli
import orbi.config as config_domain
import orbi.pilot_setup as pilot_setup
import orbi.suggest as suggest
from seam import seam
from tests.fakes.github import FakeGh
from tests.test_pilot_setup import (
    VALID_DEFS,
    fake_run_factory,
    make_config,
    make_repo,
)

pytestmark = pytest.mark.usefixtures("systemd_scheduler")

RUN_ID_RE = re.compile(r"^[0-9a-f]{8}$")
SKILL_REL = Path(
    "integrations/claude-plugin/skills/suggest-ai-ready-issues/SKILL.md"
)
SUGGEST_DIR = Path(".orbi") / "suggest"

EXISTING = {
    "kind": "existing", "issue": 5, "title": "Fix the existing thing",
    "why": "src/orbi/claim.py does X", "body": None,
}
NEW_TWO = {
    "kind": "new", "issue": None, "title": "Second thing",
    "why": "README gap", "body": "## Request\nSecond.",
}
NEW_THREE = {
    "kind": "new", "issue": None, "title": "Third thing",
    "why": "tests gap", "body": "## Request\nThird.",
}


class GhRecorder:
    """The seam fake: gh to FakeGh, everything else to the real CLI."""

    def __init__(self, gh: FakeGh) -> None:
        self.gh = gh
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        argv = list(command)
        if argv and argv[0] == "gh":
            return self.gh(command, **kwargs)
        completed = subprocess.run(
            command, capture_output=True, text=True, check=True,
            cwd=kwargs.get("cwd"),
        )
        return completed.stdout.strip()

    def rendered(self) -> list[str]:
        return [" ".join(command) for command in self.commands]


class PiStub:
    """The stream_pi seam: record the argv, return one canned answer."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    @property
    def command(self) -> list[str]:
        return self.calls[0][0]

    @property
    def kwargs(self) -> dict:
        return self.calls[0][1]


class Tty:
    """A stdin/stdout double with an explicit isatty()."""

    def __init__(self, tty: bool = True) -> None:
        self._tty = tty
        self.text = ""

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> None:
        self.text += text


def _answer_json(*suggestions) -> str:
    return json.dumps({"suggestions": list(suggestions)})


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(argv, cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def _world(tmp_path: Path, monkeypatch, *, provider: bool = False):
    deploy = tmp_path / "deploy"
    prompts = deploy / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "prompt.md").write_text("prompt", encoding="utf-8")
    (prompts / "prompt_review.md").write_text("prompt", encoding="utf-8")
    repo = _git_repo(tmp_path / "target")
    extra = ""
    if provider:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        (deploy / "providers.json").write_text(
            json.dumps({"providers": {"openai": {
                "baseUrl": "http://localhost/v1", "api": "openai-completions",
                "apiKey": "$OPENAI_API_KEY",
                "models": [{"id": "m"}],
            }}}),
            encoding="utf-8",
        )
        extra = (
            'pi_provider = "openai"\npi_model = "m"\n'
            'pi_providers = "' + str(deploy / "providers.json") + '"\n'
        )
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["o/r"]\n'
        f'repo_dir = "{repo}"\ndeploy_home = "{deploy}"\n' + extra,
        encoding="utf-8",
    )
    gh = FakeGh("o/r")
    recorder = GhRecorder(gh)
    monkeypatch.setattr(seam, "run_command", recorder)
    return SimpleNamespace(
        tmp_path=tmp_path, deploy=deploy, repo=repo,
        config_path=config_path, gh=gh, recorder=recorder,
    )


def _loaded(world):
    return config_domain.load_config(world.config_path)


def _run_dirs(world) -> list[Path]:
    root = world.deploy / SUGGEST_DIR
    return sorted(path for path in root.iterdir()) if root.is_dir() else []


def _no_github_writes(command: str) -> bool:
    return not (
        command.startswith("gh issue create")
        or command.startswith("gh issue edit")
        or " -X POST" in command
        or " --method POST" in command
        or " --method PATCH" in command
        or " --method PUT" in command
    )


def _repo_files(root: Path) -> list[Path]:
    """Every path under `root` except git's own bookkeeping.

    A raw `rglob("*")` snapshot is not stable: git runs its
    auto-maintenance detached after `git commit` and creates and removes
    `.git/objects/maintenance.lock` (repacking loose objects too) while
    the test runs — the two snapshots differed by exactly that lock on
    the macOS CI runner. The contract under test is that `orbi suggest`
    writes nothing under `repo_dir`, so git's own bookkeeping is not part
    of the snapshot; every other path still is, and the recorded
    commands below cover git's internals (a fetch or a checkout would
    run `git` through the one subprocess seam).
    """
    return sorted(
        path.relative_to(root) for path in root.rglob("*")
        if path.relative_to(root).parts[0] != ".git"
    )


# --- the command -------------------------------------------------------------


def test_orbi_suggest_json_prints_the_document(
    tmp_path, monkeypatch, capsys, caplog,
):
    world = _world(tmp_path, monkeypatch)
    world.gh.add_issue(5, title="Existing", body="body", labels=())
    world.gh.add_pr(1, head="feature", body="pr body")
    stub = PiStub(_answer_json(EXISTING, NEW_TWO, NEW_THREE))
    monkeypatch.setattr(seam, "stream_pi", stub)
    before = _repo_files(world.repo)
    # The snapshot must actually see the checkout, not an empty set.
    assert Path("README.md") in before

    with caplog.at_level("INFO", logger="orbi.bootstrap"):
        assert cli.main([
            "suggest", "--json", "--config", str(world.config_path),
        ]) == 0
    document = json.loads(capsys.readouterr().out)

    assert set(document) == {"repo", "base_sha", "session_dir", "suggestions"}
    assert document["repo"] == "o/r"
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=world.repo, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    assert document["base_sha"] == expected_sha
    assert [item["title"] for item in document["suggestions"]] == [
        "Fix the existing thing", "Second thing", "Third thing",
    ]

    run_dirs = _run_dirs(world)
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    assert RUN_ID_RE.match(run_id)
    assert document["session_dir"] == str(run_dirs[0] / ".pi-session")
    assert (run_dirs[0] / ".pi-session").is_dir()

    context = json.loads(
        (run_dirs[0] / "context.json").read_text(encoding="utf-8"),
    )
    assert [issue["number"] for issue in context["issues"]] == [5]
    assert [pr["number"] for pr in context["prs"]] == [1]

    lines = [
        record.message for record in caplog.records
        if record.name == "orbi.bootstrap"
    ]
    assert lines
    assert all(line.startswith(f"[{run_id}] ") for line in lines)

    assert all(_no_github_writes(c) for c in world.recorder.rendered())
    # The only git command is the read behind `base_sha`: no fetch, no
    # checkout and no worktree (their writes would sit in `.git/`,
    # which the file snapshot below excludes as git's own churn).
    assert [
        line for line in world.recorder.rendered()
        if line.startswith("git ")
    ] == ["git rev-parse HEAD"]

    command = stub.command
    assert command[command.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-skills" in command
    assert "--no-context-files" in command
    assert "--no-extensions" in command
    assert command[command.index("--skill") + 1] == str(
        world.deploy / SKILL_REL
    )
    rendered = " ".join(command)
    for forbidden in ("bash", "edit", "write"):
        assert forbidden not in rendered
    assert stub.kwargs["cwd"] == run_dirs[0]
    assert str(run_dirs[0] / "context.json") in command[-1]

    after = _repo_files(world.repo)
    assert after == before


def test_suggest_accepts_a_fenced_json_block(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path, monkeypatch)
    world.gh.add_issue(5, labels=())
    fenced = "\n".join(
        ["\u0060\u0060\u0060json", _answer_json(EXISTING), "\u0060\u0060\u0060"]
    )
    monkeypatch.setattr(seam, "stream_pi", PiStub(fenced))
    assert cli.main([
        "suggest", "--json", "--config", str(world.config_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["suggestions"][0]["issue"] == 5


BAD_RESULTS = {
    "not_json": "this is not JSON",
    "too_many": json.dumps({"suggestions": [
        EXISTING, NEW_TWO, NEW_THREE,
        {"kind": "new", "issue": None, "title": "Fourth",
         "why": "more", "body": "## Request\nFourth."},
    ]}),
    "extra_key": json.dumps({
        "suggestions": [EXISTING], "extra": True,
    }),
    "body_on_existing": json.dumps({"suggestions": [
        {**EXISTING, "body": "## Request\nx"},
    ]}),
    "entries_not_a_list": json.dumps({"suggestions": {}}),
    "entry_not_an_object": json.dumps({"suggestions": ["nope"]}),
    "entry_missing_keys": json.dumps({"suggestions": [{"kind": "new"}]}),
    "empty_title": json.dumps({"suggestions": [{**NEW_TWO, "title": "  "}]}),
    "empty_why": json.dumps({"suggestions": [{**NEW_TWO, "why": ""}]}),
    "existing_issue_not_a_number": json.dumps(
        {"suggestions": [{**EXISTING, "issue": "5"}]},
    ),
    "new_with_an_issue_number": json.dumps(
        {"suggestions": [{**NEW_TWO, "issue": 5}]},
    ),
    "new_with_an_empty_body": json.dumps(
        {"suggestions": [{**NEW_TWO, "body": "  "}]},
    ),
    "unknown_kind": json.dumps(
        {"suggestions": [{**NEW_TWO, "kind": "maybe"}]},
    ),
}


@pytest.mark.parametrize("answer", sorted(BAD_RESULTS.values()))
def test_suggest_fails_fast_on_an_invalid_result(
    answer, tmp_path, monkeypatch, caplog, capsys,
):
    world = _world(tmp_path, monkeypatch)
    world.gh.add_issue(5, labels=())
    monkeypatch.setattr(seam, "stream_pi", PiStub(answer))
    with caplog.at_level("INFO", logger="orbi.bootstrap"):
        assert cli.main([
            "suggest", "--json", "--config", str(world.config_path),
        ]) == 1
    assert "suggest_failed" in capsys.readouterr().err
    assert "suggest_failed" in caplog.text
    assert "returncode=0" in caplog.text
    assert "stdout=" in caplog.text
    # The run directory is kept after the command exits, on failure too.
    run_dirs = _run_dirs(world)
    assert len(run_dirs) == 1
    assert RUN_ID_RE.match(run_dirs[0].name)


def test_suggest_rejects_an_existing_issue_with_a_delivery_label(
    tmp_path, monkeypatch, capsys,
):
    world = _world(tmp_path, monkeypatch)
    world.gh.add_issue(5, title="Labelled", labels=("ai-merged",))
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json(EXISTING)))
    assert cli.main([
        "suggest", "--json", "--config", str(world.config_path),
    ]) == 1
    assert "suggest_failed" in capsys.readouterr().err


def test_suggest_rejects_an_existing_issue_outside_the_open_context(
    tmp_path, monkeypatch, capsys,
):
    world = _world(tmp_path, monkeypatch)
    world.gh.add_issue(5, title="Closed", state="closed")
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json(EXISTING)))
    assert cli.main([
        "suggest", "--json", "--config", str(world.config_path),
    ]) == 1
    assert "suggest_failed" in capsys.readouterr().err


def test_suggest_interactive_requires_a_tty(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path, monkeypatch)
    stub = PiStub(_answer_json(EXISTING))
    monkeypatch.setattr(seam, "stream_pi", stub)
    assert suggest.run_suggest(
        _loaded(world), "o/r", json_output=False,
        dispatch_issue=cli.dispatch_issue,
        stdin=Tty(False), stdout=Tty(True),
    ) == 2
    assert "--json" in capsys.readouterr().err
    assert stub.calls == []
    assert world.recorder.commands == []


# --- the interactive flow ----------------------------------------------------


def _interactive_world(tmp_path, monkeypatch, *, milestone_state="open"):
    world = _world(tmp_path, monkeypatch)
    world.gh.set_repo_config(
        'active_milestone = "v9"\ndispatch_label = "custom-label"\n'
    )
    world.gh.add_issue(5, title="Existing", labels=())
    world.gh.add_milestone(1, title="v9", state=milestone_state)
    stub = PiStub(_answer_json(EXISTING, NEW_TWO, NEW_THREE))
    monkeypatch.setattr(seam, "stream_pi", stub)
    return world, stub


def test_suggest_interactive_labels_files_and_skips(tmp_path, monkeypatch):
    world, stub = _interactive_world(tmp_path, monkeypatch)
    answers = iter(["y", "n", "y"])
    asked: list[str] = []
    out = Tty(True)
    assert suggest.run_suggest(
        _loaded(world), "o/r", json_output=False,
        dispatch_issue=cli.dispatch_issue,
        stdin=Tty(True), stdout=out,
        ask=lambda prompt: asked.append(prompt) or next(answers),
    ) == 0

    commands = world.recorder.commands
    assert [
        "gh", "issue", "edit", "5", "--repo", "o/r",
        "--add-label", "custom-label", "--milestone", "v9",
    ] in commands
    assert [
        "gh", "issue", "create", "--repo", "o/r", "--title", "Third thing",
        "--body", "## Request\nThird.", "--milestone", "v9",
    ] in commands
    assert [
        "gh", "issue", "edit", "6", "--repo", "o/r",
        "--add-label", "custom-label",
    ] in commands
    rendered = world.recorder.rendered()
    assert not any("Second thing" in command for command in rendered)
    labels = [
        command[command.index("--add-label") + 1]
        for command in commands if "--add-label" in command
    ]
    assert labels == ["custom-label", "custom-label"]
    assert len(asked) == 3
    assert asked[0].startswith("Label #5 custom-label?")
    assert asked[1].startswith("File this Issue as custom-label?")
    assert "https://github.com/o/r/issues/5" in out.text
    assert "https://github.com/o/r/issues/6" in out.text
    assert stub.calls


def test_suggest_interactive_omits_a_closed_milestone(tmp_path, monkeypatch):
    world, _stub = _interactive_world(
        tmp_path, monkeypatch, milestone_state="closed",
    )
    answers = iter(["y", "n", "n"])
    assert suggest.run_suggest(
        _loaded(world), "o/r", json_output=False,
        dispatch_issue=cli.dispatch_issue,
        stdin=Tty(True), stdout=Tty(True),
        ask=lambda prompt: next(answers),
    ) == 0
    assert all(
        "--milestone" not in command for command in world.recorder.commands
    )
    assert [
        "gh", "issue", "edit", "5", "--repo", "o/r",
        "--add-label", "custom-label",
    ] in world.recorder.commands


def test_suggest_interactive_enter_skips_every_suggestion(
    tmp_path, monkeypatch,
):
    """The prompt says `[y/N]`: an empty answer is the shown default and
    must skip the suggestion (Issue #1576: "any other answer skips")."""
    world, _stub = _interactive_world(tmp_path, monkeypatch)
    out = Tty(True)
    assert suggest.run_suggest(
        _loaded(world), "o/r", json_output=False,
        dispatch_issue=cli.dispatch_issue,
        stdin=Tty(True), stdout=out,
        ask=lambda prompt: "",
    ) == 0
    rendered = world.recorder.rendered()
    assert not any(
        line.startswith("gh issue edit") or line.startswith("gh issue create")
        for line in rendered
    )
    assert not any(
        "https://github.com/o/r/issues/" in line
        for line in out.text.splitlines()
    )


def test_suggest_interactive_with_no_suggestions(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json()))
    out = Tty(True)
    assert suggest.run_suggest(
        _loaded(world), "o/r", json_output=False,
        dispatch_issue=cli.dispatch_issue,
        stdin=Tty(True), stdout=out,
        ask=lambda prompt: "y",
    ) == 0
    assert "No suggestions for o/r." in out.text
    assert not any(
        "gh issue create" in command or "gh issue edit" in command
        for command in world.recorder.rendered()
    )


# --- the setup offer ---------------------------------------------------------


def _offer(world, config, *, tty, answers=()):
    asked: list[str] = []
    out = Tty(tty)
    answers_iter = iter(answers)
    result = {"setup": "ok", "repos": [{"repo": "o/r"}]}
    code = suggest.offer_setup_suggest(
        config, result, dispatch_issue=cli.dispatch_issue,
        stdin=Tty(tty), stdout=out,
        ask=lambda prompt: asked.append(prompt) or next(answers_iter),
    )
    return code, asked, out


def test_setup_offer_asks_on_a_fresh_repository(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch, provider=True)
    stub = PiStub(_answer_json())
    monkeypatch.setattr(seam, "stream_pi", stub)
    code, asked, _out = _offer(
        world, _loaded(world), tty=True, answers=["n"],
    )
    assert code == 0
    assert asked == ["Scan o/r and suggest three Issues to deliver? [Y/n] "]
    assert stub.calls == []


def test_setup_offer_is_silent_when_a_delivery_issue_exists(
    tmp_path, monkeypatch,
):
    world = _world(tmp_path, monkeypatch, provider=True)
    world.gh.add_issue(9, title="Merged work", labels=("ai-merged",))
    stub = PiStub(_answer_json())
    monkeypatch.setattr(seam, "stream_pi", stub)
    code, asked, out = _offer(world, _loaded(world), tty=True, answers=[])
    assert code == 0
    assert asked == []
    assert out.text == ""
    assert world.recorder.commands  # the one gh search ran
    assert stub.calls == []


def test_setup_offer_without_a_terminal_prints_only_the_hint(
    tmp_path, monkeypatch,
):
    world = _world(tmp_path, monkeypatch)  # provider NOT configured
    code, asked, out = _offer(world, _loaded(world), tty=False, answers=[])
    assert code == 0
    assert asked == []
    assert out.text == (
        "Run 'orbi suggest --repo o/r' to get three suggested Issues.\n"
    )
    assert world.recorder.commands == []


def test_setup_offer_runs_the_interactive_flow_on_enter(
    tmp_path, monkeypatch,
):
    world = _world(tmp_path, monkeypatch, provider=True)
    stub = PiStub(_answer_json())
    monkeypatch.setattr(seam, "stream_pi", stub)
    code, asked, out = _offer(world, _loaded(world), tty=True, answers=[""])
    assert code == 0
    assert asked[0].startswith("Scan o/r and suggest three Issues")
    assert stub.calls
    assert "No suggestions for o/r." in out.text


# --- cli wiring --------------------------------------------------------------


def test_suggest_repo_must_be_one_of_the_configured_sources(
    tmp_path, monkeypatch, capsys,
):
    world = _world(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as suggest_exit:
        cli.main([
            "suggest", "--repo", "x/y", "--json",
            "--config", str(world.config_path),
        ])
    suggest_error = capsys.readouterr().err
    with pytest.raises(SystemExit) as add_exit:
        cli.main([
            "add", "title", "--repo", "x/y", "--config", str(world.config_path),
        ])
    add_error = capsys.readouterr().err
    assert suggest_exit.value.code == add_exit.value.code == 2
    assert "--repo must be one of: o/r" in suggest_error
    assert "--repo must be one of: o/r" in add_error


@pytest.fixture
def setup_world(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("prompt", encoding="utf-8")
    (prompts / "prompt_review.md").write_text("prompt", encoding="utf-8")
    config_path = make_config(tmp_path, repo)
    state = {"existing_labels": [dict(entry) for entry in VALID_DEFS]}
    fake_run, calls = fake_run_factory(state)
    monkeypatch.setattr(
        pilot_setup.shutil, "which", lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(seam, "run_command", fake_run)
    stub = PiStub(_answer_json())
    monkeypatch.setattr(seam, "stream_pi", stub)
    return SimpleNamespace(
        config_path=config_path, calls=calls, stub=stub,
        installed=tmp_path / "units",
    )


def test_setup_runs_the_real_run_setup_and_prints_the_hint(
    setup_world, capsys,
):
    assert cli.main([
        "setup", "--config", str(setup_world.config_path),
        "--installed-dir", str(setup_world.installed),
    ]) == 0
    out = capsys.readouterr().out
    assert "setup=ok" in out
    assert (
        "Run 'orbi suggest --repo xqliu/orbi' to get three suggested Issues."
        in out
    )
    assert setup_world.stub.calls == []


def test_setup_json_prints_neither_the_question_nor_the_hint(
    setup_world, capsys,
):
    assert cli.main([
        "setup", "--json", "--config", str(setup_world.config_path),
        "--installed-dir", str(setup_world.installed),
    ]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["setup"] == "ok"
    assert "orbi suggest" not in out
    assert "Scan " not in out
    assert setup_world.stub.calls == []


# --- defensive paths ---------------------------------------------------------


def test_label_names_reads_dicts_and_strings():
    assert suggest._label_names([
        {"name": "ai-ready"}, "plain", {"nope": 1}, 7,
    ]) == ["ai-ready", "plain"]


def test_setup_repo_falls_back_to_the_first_source_repo(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    config = _loaded(world)
    assert suggest._setup_repo(config, None) == "o/r"
    assert suggest._setup_repo(config, {"repos": "nope"}) == "o/r"


def test_suggest_rejects_a_non_array_pr_list(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path, monkeypatch)
    recorder = world.recorder

    def fake(command, **kwargs):
        argv = list(command)
        if argv[0:3] == ["gh", "pr", "list"]:
            return "{}"
        return recorder(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json(EXISTING)))
    assert cli.main([
        "suggest", "--json", "--config", str(world.config_path),
    ]) == 1
    assert "suggest_failed" in capsys.readouterr().err


def test_suggest_fails_fast_when_a_gh_command_fails(
    tmp_path, monkeypatch, capsys, caplog,
):
    world = _world(tmp_path, monkeypatch)

    def failing(command, **kwargs):
        argv = list(command)
        if argv[0:3] == ["gh", "pr", "list"]:
            raise subprocess.CalledProcessError(
                1, command, output="", stderr="gh boom",
            )
        return world.recorder(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", failing)
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json(EXISTING)))
    with caplog.at_level("INFO", logger="orbi.bootstrap"):
        assert cli.main([
            "suggest", "--json", "--config", str(world.config_path),
        ]) == 1
    assert "suggest_failed" in capsys.readouterr().err
    assert "returncode=1" in caplog.text
    assert chr(34) + "gh boom" + chr(34) in caplog.text
    # The failure line carries the COMMAND that failed, not a
    # placeholder: the stdlib CalledProcessError exposes it as cmd.
    failure = caplog.text.split("suggest_failed", 1)[-1]
    assert "gh pr list" in failure
    assert "command=-" not in failure


def test_setup_offer_is_silent_without_a_provider(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)  # provider NOT configured
    stub = PiStub(_answer_json())
    monkeypatch.setattr(seam, "stream_pi", stub)
    code, asked, out = _offer(world, _loaded(world), tty=True, answers=[])
    assert code == 0
    assert asked == []
    assert out.text == ""
    assert world.recorder.commands == []
    assert stub.calls == []


def test_setup_offer_fails_fast_when_the_search_fails(
    tmp_path, monkeypatch, capsys, caplog,
):
    world = _world(tmp_path, monkeypatch, provider=True)

    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, output="", stderr="gh search boom",
        )

    monkeypatch.setattr(seam, "run_command", failing)
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json()))
    with caplog.at_level("INFO", logger="orbi.bootstrap"):
        code, asked, _out = _offer(
            world, _loaded(world), tty=True, answers=[],
        )
    assert code == 1
    assert asked == []
    assert "suggest_failed" in capsys.readouterr().err
    assert "returncode=1" in caplog.text
    assert chr(34) + "gh search boom" + chr(34) in caplog.text


def test_suggest_interactive_without_a_resolved_milestone(
    tmp_path, monkeypatch,
):
    world = _world(tmp_path, monkeypatch)
    world.gh.add_issue(5, title="Existing", labels=())
    monkeypatch.setattr(seam, "stream_pi", PiStub(_answer_json(EXISTING)))
    asked: list[str] = []
    assert suggest.run_suggest(
        _loaded(world), "o/r", json_output=False,
        dispatch_issue=cli.dispatch_issue,
        stdin=Tty(True), stdout=Tty(True),
        ask=lambda prompt: asked.append(prompt) or "n",
    ) == 0
    assert asked == ["Label #5 ai-ready? [y/N] "]
    assert not any(
        "--milestone" in command for command in world.recorder.commands
    )


def test_suggest_fails_fast_when_the_pi_session_fails(
    tmp_path, monkeypatch, capsys, caplog,
):
    world = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(
        seam, "stream_pi", PiStub(RuntimeError("pi session exploded")),
    )
    with caplog.at_level("INFO", logger="orbi.bootstrap"):
        assert cli.main([
            "suggest", "--json", "--config", str(world.config_path),
        ]) == 1
    assert "suggest_failed" in capsys.readouterr().err
    assert "pi session exploded" in caplog.text


def test_fake_gh_creates_an_issue_without_a_milestone(tmp_path, monkeypatch, capsys):
    world = _world(tmp_path, monkeypatch)
    assert cli.main([
        "add", "A task", "--body", "Do it",
        "--config", str(world.config_path),
    ]) == 0
    out = capsys.readouterr().out
    assert "created: https://github.com/o/r/issues/1" in out
    assert "label: ai-ready" in out
    assert [
        "gh", "issue", "create", "--repo", "o/r",
        "--title", "A task", "--body", "Do it",
    ] in world.recorder.commands
    assert world.gh.issues[1]["labels"] == ["ai-ready"]


def test_fake_gh_resolves_and_rejects_a_milestone(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    gh = world.gh
    gh.add_milestone(1, title="v1")
    gh.add_milestone(2, title="v2")
    assert gh._milestone_number("v2") == 2
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        gh._milestone_number("missing")
    assert "missing" in str(excinfo.value.stderr)
