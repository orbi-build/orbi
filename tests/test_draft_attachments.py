"""orbi draft's attached files (Issue #2364): the draft-level list through
drafting, filing and delivery.

The fake Pi (tests/fakes/draft.py) answers through the answer-file variable,
so the REAL session build — argv, agent directory, environment — is what the
assertions read; gh is the in-memory FakeGh, so the created Issue, its body
and the uploads it saw are the observable result.
"""
from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest

import orbi.draft as draft
import orbi.draft_cli as draft_cli
import orbi.draft_marker as draft_marker
from orbi import clarify
from orbi.config import RunnerConfig

from tests.fakes import draft as draft_fakes
from tests.fakes.github import FakeGh

REPO = "acme/app"
RUN_ID = "a1b2c3d4"
KEY = "draft-abc123"

OUTCOME = "## Outcome\n\nA user can tag a note."
ACCEPTANCE = "## Acceptance\n\n- Adding a tag makes the note findable."
ONE_ISSUE = [{"title": "Tag a note", "body": f"{OUTCOME}\n\n{ACCEPTANCE}"}]

DEEPSEEK = json.loads(
    (Path(__file__).resolve().parent.parent
     / "templates" / "pi-providers" / "deepseek.json").read_text("utf-8")
)


@pytest.fixture
def gh() -> FakeGh:
    return FakeGh(REPO)


@pytest.fixture
def recorder(monkeypatch, gh):
    return draft_fakes.install_gh(monkeypatch, gh)


@pytest.fixture
def downloads(monkeypatch):
    return draft_fakes.install_downloads(monkeypatch)


@pytest.fixture
def sessions(monkeypatch):
    return draft_fakes.install_sessions(monkeypatch)


@pytest.fixture
def config(tmp_path, recorder, monkeypatch) -> RunnerConfig:
    """A run whose model declares image input (the shipped DeepSeek template)."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    draft_fakes.stop_repo_view(monkeypatch)
    deploy = tmp_path / "deploy"
    rules = deploy / draft.FIDELITY_REL
    rules.parent.mkdir(parents=True)
    rules.write_text("Fidelity: remove what the person did not ask for.", "utf-8")
    return RunnerConfig(
        repo_dir=checkout, deploy_home=deploy, workspace_root=tmp_path,
        source_repos=(REPO,), pi_provider="deepseek",
        pi_model="deepseek-flash", pi_providers_data=DEEPSEEK,
    )


@pytest.fixture
def silent_model(config) -> RunnerConfig:
    """The same run without a catalog: no model declares image input."""
    return dataclasses.replace(
        config, pi_provider=None, pi_model=None, pi_providers_data=None,
    )


def write(tmp_path: Path, name: str, data: bytes = draft_fakes.PNG_BYTES) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def staged(session: Path, name: str) -> Path:
    return session / draft.DRAFT_SESSION_DIR / "attachments" / name


def run_round_cli(config, payload: dict, session_dir: Path, attach=()):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, session_dir=session_dir, attach=list(attach),
        stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def run_create(config, payload: dict, attach=()):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, create=True, attach=list(attach),
        stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def context_file(command: list[str]) -> Path:
    """The one argv element that is Pi's context file (it is last)."""
    assert command[-1].startswith("@")
    return Path(command[-1].removeprefix("@"))


def image_arguments(command: list[str]) -> list[str]:
    """Every file argument that is not the context file."""
    return [item for item in command
            if item.startswith("@") and item != command[-1]]


# --- 1: a request with no file ------------------------------------------

def test_a_draft_without_files_adds_nothing(
    config, sessions, monkeypatch, tmp_path,
):
    """Acceptance 1: no entry, no naming line, no image argument."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    code, out, err = run_round_cli(
        config, {"message": "notes need tags"}, tmp_path / "session",
    )
    assert code == 0, err
    assert sessions.calls[0].attached.files == ()
    assert "The person attached" not in sessions.calls[0].context
    assert sessions.calls[0].attached.arguments() == []


def test_the_context_file_stays_the_last_argument(
    config, monkeypatch, tmp_path,
):
    """Acceptance 1: with no attachment the argv still ends with the context."""
    draft_fakes.judge_passing(monkeypatch)
    calls = draft_fakes.stub_pi_answers(
        monkeypatch, {"kind": "draft", "reply": "x", "issues": ONE_ISSUE},
    )
    code, out, err = run_round_cli(
        config, {"message": "tags"}, tmp_path / "session",
    )
    assert code == 0, err
    command = calls[0]["command"]
    assert command[-1] == f"@{context_file(command)}"
    assert image_arguments(command) == []
    assert "The person attached" not in context_file(command).read_text("utf-8")


def test_a_created_draft_without_files_carries_no_section(
    config, recorder, gh,
):
    """Acceptance 1: the created body is the drafted text plus the marker."""
    code, out, err = run_create(config, {"key": KEY, "issues": ONE_ISSUE})
    assert code == 0, err
    body = gh.issues[json.loads(out)["created"][0]]["body"]
    assert "## Attachments" not in body
    assert not any("--attach" in command for command in recorder.commands)


# --- 2: the files reach the session -------------------------------------

def test_the_attach_flag_stages_every_file_under_a_generated_name(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: three files, copied in, listed in one line."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    image = write(tmp_path, "screen.png")
    spec = write(tmp_path, "spec.html", draft_fakes.HTML_BYTES)
    data = write(tmp_path, "data.csv", b"a,b\n1,2\n")
    code, out, err = run_round_cli(
        config, {"message": "tags"}, tmp_path / "session",
        attach=[str(image), str(spec), str(data)],
    )
    assert code == 0, err
    files = sessions.calls[0].attached.files
    assert [file.name for file in files] == ["1.png", "2.html", "3.csv"]
    assert [file.source for file in files] == [
        "screen.png", "spec.html", "data.csv",
    ]
    session = tmp_path / "session"
    assert [file.path for file in files] == [
        staged(session, "1.png"), staged(session, "2.html"),
        staged(session, "3.csv"),
    ]
    assert staged(session, "1.png").read_bytes() == draft_fakes.PNG_BYTES
    assert (
        "The person attached: 1.png = screen.png, 2.html = spec.html, "
        "3.csv = data.csv" in sessions.calls[0].context
    )


def test_the_document_list_is_staged_like_the_flag(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: the round document's own list stages the same way."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    image = write(tmp_path, "screen.png")
    code, out, err = run_round_cli(
        config,
        {"message": "tags", "attachments": [
            {"name": "screen.png", "src": str(image)},
        ]},
        tmp_path / "session",
    )
    assert code == 0, err
    assert [file.name for file in sessions.calls[0].attached.files] == ["1.png"]


def test_an_http_source_is_downloaded_the_same_way(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: an http(s) src is downloaded, then staged."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    downloads.answers["https://cdn.example.test/shot.png"] = draft_fakes.PNG_BYTES
    code, out, err = run_round_cli(
        config,
        {"message": "tags", "attachments": [
            {"name": "shot.png", "src": "https://cdn.example.test/shot.png"},
        ]},
        tmp_path / "session",
    )
    assert code == 0, err
    file = sessions.calls[0].attached.files[0]
    assert (file.name, file.source) == ("1.png", "shot.png")
    assert file.path.read_bytes() == draft_fakes.PNG_BYTES
    assert downloads.urls() == ["https://cdn.example.test/shot.png"]


def test_a_draft_download_uses_the_delivery_downloader(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """Acceptance 5: ONE downloader — the orbi User-Agent and the image check.

    orbi.build answers urllib's default User-Agent with HTTP 403, so
    `orbi draft` sends the same request a delivery sends; an attachment
    declared as an image whose bytes are not one is refused here too.
    """
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    address = "https://beta.orbi.build/api/attachments/key"
    downloads.answers[address] = draft_fakes.PNG_BYTES
    code, out, err = run_round_cli(
        config,
        {"message": "tags", "attachments": [
            {"name": "shot.png", "src": address},
        ]},
        tmp_path / "session",
    )
    assert code == 0, err
    paths = downloads.urls()
    assert paths == [address]
    assert downloads.requests[0].headers.get("User-agent") == "orbi"


def test_a_download_that_is_not_an_image_is_refused(
    config, tmp_path, downloads,
):
    """Acceptance 5: the bytes decide, exactly as they do in a delivery."""
    address = "https://cdn.example.test/not-an-image.png"
    downloads.answers[address] = b"<html>not an image</html>"
    failure = refused(
        config,
        {"message": "tags", "attachments": [
            {"name": "shot.png", "src": address},
        ]},
        tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert "not an image" in failure["detail"]


def test_a_name_that_climbs_out_stays_inside(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: the generated name is the only name ever written."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    outside = write(tmp_path, "outside.png")
    code, out, err = run_round_cli(
        config,
        {"message": "tags", "attachments": [
            {"name": "../../outside.png", "src": str(outside)},
        ]},
        tmp_path / "session",
    )
    assert code == 0, err
    file = sessions.calls[0].attached.files[0]
    assert file.name == "1.png"
    assert file.path == staged(tmp_path / "session", "1.png")
    assert not list((tmp_path / "session").rglob("outside.png"))


def test_the_image_is_passed_before_the_context_when_the_model_reads_images(
    config, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: the file argument sits in front of the context file."""
    draft_fakes.judge_passing(monkeypatch)
    calls = draft_fakes.stub_pi_answers(
        monkeypatch, {"kind": "draft", "reply": "x", "issues": ONE_ISSUE},
    )
    image = write(tmp_path, "screen.png")
    code, out, err = run_round_cli(
        config, {"message": "tags"}, tmp_path / "session",
        attach=[str(image)],
    )
    assert code == 0, err
    command = calls[0]["command"]
    assert command[-2] == f"@{staged(tmp_path / 'session', '1.png')}"
    assert context_file(command).name == "task-context.md"
    assert "cannot be viewed" not in context_file(command).read_text("utf-8")


def test_an_image_is_named_as_not_viewable_without_image_input(
    silent_model, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: no declaration, no file argument — and the line says so."""
    draft_fakes.judge_passing(monkeypatch)
    calls = draft_fakes.stub_pi_answers(
        monkeypatch, {"kind": "draft", "reply": "x", "issues": ONE_ISSUE},
    )
    image = write(tmp_path, "screen.png")
    code, out, err = run_round_cli(
        silent_model, {"message": "tags"}, tmp_path / "session",
        attach=[str(image)],
    )
    assert code == 0, err
    command = calls[0]["command"]
    assert image_arguments(command) == []
    text = context_file(command).read_text("utf-8")
    assert "The person attached: 1.png = screen.png" in text
    assert "declares no image input" in text
    assert "1.png cannot be viewed" in text


def test_the_html_and_text_are_files_the_session_reads(
    config, monkeypatch, tmp_path, downloads,
):
    """Acceptance 2: HTML and text never travel as first-prompt arguments."""
    draft_fakes.judge_passing(monkeypatch)
    calls = draft_fakes.stub_pi_answers(
        monkeypatch, {"kind": "draft", "reply": "x", "issues": ONE_ISSUE},
    )
    spec = write(tmp_path, "spec.html", draft_fakes.HTML_BYTES)
    code, out, err = run_round_cli(
        config, {"message": "tags"}, tmp_path / "session", attach=[str(spec)],
    )
    assert code == 0, err
    command = calls[0]["command"]
    assert image_arguments(command) == []
    text = context_file(command).read_text("utf-8")
    assert "read the HTML and text files with read" in text


def test_the_fidelity_pass_receives_every_file(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """Acceptance 3: the pass gets images AND text, or it drops them."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    image = write(tmp_path, "screen.png")
    spec = write(tmp_path, "spec.html", draft_fakes.HTML_BYTES)
    code, out, err = run_round_cli(
        config, {"message": "tags"}, tmp_path / "session",
        attach=[str(image), str(spec)],
    )
    assert code == 0, err
    attached = sessions.fidelity_calls[0].attached
    session = tmp_path / "session"
    assert attached.arguments(text=True) == [
        f"@{staged(session, '1.png')}", f"@{staged(session, '2.html')}",
    ]
    assert attached.arguments() == [f"@{staged(session, '1.png')}"]


# --- 2: the bounds -------------------------------------------------------

def refused(config, payload, tmp_path) -> dict:
    code, out, err = run_round_cli(config, payload, tmp_path / "session")
    assert code == 1, out
    return json.loads(err)


def test_an_eleventh_file_is_refused(config, tmp_path, downloads):
    entries = [
        {"name": f"{position}.png", "src": str(write(tmp_path, f"{position}.png"))}
        for position in range(1, 12)
    ]
    failure = refused(
        config, {"message": "tags", "attachments": entries}, tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert failure["step"] == "read the attachments"
    assert "at most 10 files" in failure["detail"]


def test_a_file_over_the_size_bound_is_refused(config, tmp_path, downloads):
    big = write(tmp_path, "big.png", b"0" * (10 * 1024 * 1024 + 1))
    failure = refused(
        config,
        {"message": "tags", "attachments": [{"name": "big.png", "src": str(big)}]},
        tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert "larger than 10 MB" in failure["detail"]


def test_an_unsupported_type_is_refused(config, tmp_path, downloads):
    pdf = write(tmp_path, "spec.pdf", b"%PDF-1.7")
    failure = refused(
        config,
        {"message": "tags", "attachments": [{"name": "spec.pdf", "src": str(pdf)}]},
        tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert "unsupported file type .pdf" in failure["detail"]
    assert "image (" in failure["detail"]


def test_html_and_text_over_the_character_bound_are_refused(
    config, tmp_path, downloads,
):
    spec = write(tmp_path, "spec.html", b"x" * 25_001)
    log = write(tmp_path, "server.log", b"y" * 25_000)
    failure = refused(
        config,
        {"message": "tags", "attachments": [
            {"name": "spec.html", "src": str(spec)},
            {"name": "server.log", "src": str(log)},
        ]},
        tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert "more than 50000 characters" in failure["detail"]


def test_an_unreadable_local_file_is_refused(config, tmp_path, downloads):
    gone = tmp_path / "gone.png"
    failure = refused(
        config,
        {"message": "tags", "attachments": [{"name": "gone.png", "src": str(gone)}]},
        tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert "cannot be read" in failure["detail"]


def test_a_failed_download_is_refused_with_its_reason(
    config, tmp_path, downloads,
):
    url = "https://cdn.example.test/gone.png"
    downloads.failures[url] = OSError("no route")
    failure = refused(
        config,
        {"message": "tags", "attachments": [{"name": "gone.png", "src": url}]},
        tmp_path,
    )
    assert failure["code"] == "invalid_input"
    assert "could not be downloaded" in failure["detail"]
    assert "OSError" in failure["detail"]


def test_a_malformed_attachment_entry_is_refused(config, tmp_path):
    for entry, fragment in (
        ("not an object", "attachment 1 is not an object"),
        ({"src": "x"}, "attachment 1 has no name"),
        ({"name": "x.png"}, "attachment 1 has no src"),
    ):
        failure = refused(
            config, {"message": "tags", "attachments": [entry]}, tmp_path,
        )
        assert failure["code"] == "invalid_input"
        assert fragment in failure["detail"]
    failure = refused(
        config, {"message": "tags", "attachments": "junk"}, tmp_path,
    )
    assert "must be a list" in failure["detail"]


# --- 4: filing -----------------------------------------------------------

def created_body(gh: FakeGh, out: str) -> str:
    number = json.loads(out)["created"][0]
    return gh.issues[number]["body"]


def test_create_uploads_a_local_image_and_writes_the_section_after_the_marker(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: gh --attach, the section after the marker, the blocks."""
    image = write(tmp_path, "screen.png")
    spec = write(tmp_path, "spec.html", draft_fakes.HTML_BYTES)
    data = write(tmp_path, "data.csv", b"a,b\n1,2\n")
    code, out, err = run_create(
        config, {"key": KEY, "issues": ONE_ISSUE},
        attach=[str(image), str(spec), str(data)],
    )
    assert code == 0, err
    body = created_body(gh, out)
    create = next(
        index for index, command in enumerate(recorder.commands)
        if command[0] == "gh" and command[1] == "issue"
        and command[2] == "create"
    )
    command = recorder.commands[create]
    assert command[command.index("--attach") + 1] == "./1.png"
    assert command.count("--attach") == 1
    assert Path(recorder.kwargs[create]["cwd"]).name.startswith("orbi-filing-")
    assert recorder.kwargs[create]["timeout"] == 300
    assert gh.uploads == ["1.png"]
    marker_line = next(
        line for line in body.splitlines() if "orbi:draft:v1" in line
    )
    assert body.index(marker_line) < body.index("## Attachments")
    assert draft_marker.parse_draft_marker(body) is not None
    assert (
        "![screen.png](https://github.com/user-attachments/assets/1)" in body
    )
    assert "<details><summary>spec.html</summary>" in body
    assert "<html><body><h1>spec</h1></body></html>" in body
    assert "<details><summary>data.csv</summary>" in body
    assert "a,b" in body


def test_a_remote_image_is_written_as_its_own_address(
    config, recorder, gh, downloads,
):
    """Acceptance 1: an http(s) image needs no upload, no download (Issue #2374)."""
    code, out, err = run_create(
        config,
        {"key": KEY, "issues": ONE_ISSUE, "attachments": [
            {"name": "shot.png", "src": "https://cdn.example.test/shot.png"},
        ]},
    )
    assert code == 0, err
    assert (
        "![shot.png](https://cdn.example.test/shot.png)"
        in created_body(gh, out)
    )
    assert not any("--attach" in command for command in recorder.commands)
    assert downloads.urls() == []


def test_an_unreachable_remote_image_does_not_fail_the_creation(
    config, recorder, gh, downloads,
):
    """Acceptance 1/2: no request is made, so an unreachable address cannot
    fail the filing (the fake fails if it is called)."""
    url = "https://cdn.example.test/gone.png"
    downloads.failures[url] = AssertionError("the creation requested the address")
    code, out, err = run_create(
        config,
        {"key": KEY, "issues": ONE_ISSUE, "attachments": [
            {"name": "gone.png", "src": url},
        ]},
    )
    assert code == 0, err
    assert f"![gone.png]({url})" in created_body(gh, out)
    assert downloads.urls() == []


def test_an_upload_gh_refuses_still_counts_as_created(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: created, with the image that did not upload named."""
    first = write(tmp_path, "one.png")
    second = write(tmp_path, "two.png")
    gh.refuse_attachments = {"2.png"}
    code, out, err = run_create(
        config, {"key": KEY, "issues": ONE_ISSUE},
        attach=[str(first), str(second)],
    )
    assert code == 0, err
    created = json.loads(out)["created"]
    assert len(created) == 1
    assert gh.uploads == ["1.png"]
    assert "two.png" in err
    assert "gh auth login" in err
    body = gh.issues[created[0]]["body"]
    assert "![one.png](https://github.com/user-attachments/assets/1)" in body
    assert "![two.png](./2.png)" in body


def test_a_create_that_never_happened_stays_a_failure(
    config, recorder, gh, tmp_path, downloads,
):
    """A gh refusal with no Issue and no URL is still the step's error."""
    gh.create_failure = "gh: Label 'ai-ready' not found (HTTP 422)"
    code, out, err = run_create(
        config, {"key": KEY, "issues": ONE_ISSUE},
        attach=[str(write(tmp_path, "one.png"))],
    )
    assert code == 1
    assert json.loads(err)["code"] == "create_failed"
    assert gh.issues == {}


def test_an_amend_carries_no_attachments(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: adding to an existing Issue carries none."""
    gh.add_issue(7, title="Existing", body="## Request\n\nAdd tags.")
    code, out, err = run_create(
        config, {"key": KEY, "issue": 7, "issues": ONE_ISSUE},
        attach=[str(write(tmp_path, "screen.png"))],
    )
    assert code == 0, err
    assert "## Attachments" not in gh.issues[7]["body"]
    assert not any("--attach" in command for command in recorder.commands)


def test_a_split_carries_no_attachments(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: the children of a split carry none."""
    gh.add_issue(7, title="Existing", body="## Request\n\nTwo things.")
    code, out, err = run_create(
        config,
        {"key": KEY, "issue": 7, "issues": [
            {"title": "One", "body": f"{OUTCOME}\n\n{ACCEPTANCE}"},
            {"title": "Two", "body": f"{OUTCOME}\n\n{ACCEPTANCE}", "after": 1},
        ]},
        attach=[str(write(tmp_path, "screen.png"))],
    )
    assert code == 0, err
    for number in json.loads(out)["created"]:
        assert "## Attachments" not in gh.issues[number]["body"]
    assert not any("--attach" in command for command in recorder.commands)


def test_an_issue_created_with_attachments_passes_the_claim_check(
    config, recorder, gh, monkeypatch, tmp_path, downloads,
):
    """Acceptance 4: the gh rewrite inside the section keeps the fingerprint."""
    draft_fakes.stub_login(monkeypatch)
    judged = draft_fakes.judge_failing(monkeypatch, "single_outcome")
    code, out, err = run_create(
        config, {"key": KEY, "issues": ONE_ISSUE},
        attach=[str(write(tmp_path, "screen.png"))],
    )
    assert code == 0, err
    number = json.loads(out)["created"][0]
    issue = {
        "number": number, "title": ONE_ISSUE[0]["title"],
        "body": gh.issues[number]["body"],
        "author": {"login": "app/orbi-bot"},
    }
    assert "user-attachments" in issue["body"]
    assert clarify.enforce(issue, config, REPO, RUN_ID) is True
    assert judged == []


def test_text_after_the_section_is_outside_the_fingerprint_and_the_body_is_not(
    config, recorder, gh, monkeypatch, tmp_path, downloads,
):
    """The section is outside the fingerprint; the drafted text is not."""
    draft_fakes.stub_login(monkeypatch)
    judged = draft_fakes.judge_sequence(monkeypatch)
    code, out, err = run_create(
        config, {"key": KEY, "issues": ONE_ISSUE},
        attach=[str(write(tmp_path, "screen.png"))],
    )
    assert code == 0, err
    number = json.loads(out)["created"][0]
    body = gh.issues[number]["body"]
    issue = {
        "number": number, "title": ONE_ISSUE[0]["title"],
        "author": {"login": "app/orbi-bot"},
    }
    issue["body"] = body + "\n\nA note below the section."
    assert clarify.enforce(issue, config, REPO, RUN_ID) is True
    assert judged == []
    issue["body"] = body.replace("A user can tag a note.", "Something else.")
    assert clarify.enforce(issue, config, REPO, RUN_ID) is True
    assert len(judged) == 1


def test_each_drafted_issue_writes_only_the_attachments_it_uses(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: [1] gets only file 1, [2] only file 2."""
    image = write(tmp_path, "screen.png")
    data = write(tmp_path, "data.csv", b"a,b\n1,2\n")
    code, out, err = run_create(
        config,
        {"key": KEY, "issues": [
            {**ONE_ISSUE[0], "attachments": [1]},
            {**ONE_ISSUE[0], "title": "Second", "attachments": [2]},
        ]},
        attach=[str(image), str(data)],
    )
    assert code == 0, err
    first, second = json.loads(out)["created"]
    first_body = gh.issues[first]["body"]
    second_body = gh.issues[second]["body"]
    assert "![screen.png](https://github.com/user-attachments/assets/1)" in first_body
    assert "<details><summary>data.csv</summary>" not in first_body
    assert "<details><summary>data.csv</summary>" in second_body
    assert "![screen.png]" not in second_body
    assert gh.uploads == ["1.png"]
    create = next(
        index for index, command in enumerate(recorder.commands)
        if command[0] == "gh" and command[1] == "issue"
        and command[2] == "create"
    )
    assert recorder.commands[create].count("--attach") == 1


def test_a_drafted_issue_carries_its_attachment_numbers_in_the_round_output(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """The draft output names the files each Issue uses, absent = all."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(
        {**ONE_ISSUE[0], "attachments": [1]},
        {**ONE_ISSUE[0], "title": "Second"},
    )
    image = write(tmp_path, "screen.png")
    code, out, err = run_round_cli(
        config, {"message": "tags"}, tmp_path / "session",
        attach=[str(image)],
    )
    assert code == 0, err
    issues = json.loads(out)["issues"]
    assert issues[0]["attachments"] == [1]
    assert "attachments" not in issues[1]


def test_a_split_copies_the_attachment_links_the_child_uses(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: a split child copies the links from the original body."""
    section = (
        "## Attachments\n\n"
        "![screen.png](https://github.com/user-attachments/assets/9)\n\n"
        "<details><summary>spec.html</summary>\n\n"
        "\u0060\u0060\u0060html\n<h1>spec</h1>\n\u0060\u0060\u0060\n\n"
        "</details>"
    )
    parent_body = draft_marker.with_marker(
        "## Request\n\nEverything.", key="parent", position=1,
        section=section,
    )
    gh.add_issue(7, title="Everything", body=parent_body)
    code, out, err = run_create(
        config,
        {"key": KEY, "issue": 7, "issues": [
            {**ONE_ISSUE[0], "title": "One", "attachments": [1]},
            {**ONE_ISSUE[0], "title": "Two", "attachments": [2]},
        ]},
    )
    assert code == 0, err
    first, second = json.loads(out)["created"]
    first_body = gh.issues[first]["body"]
    second_body = gh.issues[second]["body"]
    marker = "<!-- orbi:draft:v1"
    assert first_body.index(marker) < first_body.index("## Attachments")
    assert "![screen.png](https://github.com/user-attachments/assets/9)" in first_body
    assert "<details><summary>spec.html</summary>" not in first_body
    assert "<details><summary>spec.html</summary>" in second_body
    assert "![screen.png]" not in second_body
    assert not any("--attach" in command for command in recorder.commands)


def test_a_split_without_attachments_copies_every_link(
    config, recorder, gh, tmp_path, downloads,
):
    """Acceptance 4: an Issue without attachments gets all of them."""
    section = (
        "## Attachments\n\n"
        "![screen.png](https://github.com/user-attachments/assets/9)\n\n"
        "<details><summary>spec.html</summary>\n\nspec\n\n</details>"
    )
    parent_body = draft_marker.with_marker(
        "## Request\n\nEverything.", key="parent", position=1,
        section=section,
    )
    gh.add_issue(7, title="Everything", body=parent_body)
    code, out, err = run_create(
        config,
        {"key": KEY, "issue": 7, "issues": [
            {**ONE_ISSUE[0], "title": "One"},
            {**ONE_ISSUE[0], "title": "Two", "after": 1},
        ]},
    )
    assert code == 0, err
    for number in json.loads(out)["created"]:
        body = gh.issues[number]["body"]
        assert "![screen.png](https://github.com/user-attachments/assets/9)" in body
        assert "<details><summary>spec.html</summary>" in body


def test_a_split_refuses_a_number_the_original_issue_does_not_have(
    config, recorder, gh, tmp_path, downloads,
):
    """A number outside the original section is a person-readable refusal."""
    body = draft_marker.with_marker(
        "## Request\n\nEverything.", key="parent", position=1,
    )
    gh.add_issue(7, title="Everything", body=body)
    code, out, err = run_create(
        config,
        {"key": KEY, "issue": 7, "issues": [
            {**ONE_ISSUE[0], "title": "One", "attachments": [1]},
            {**ONE_ISSUE[0], "title": "Two", "after": 1},
        ]},
    )
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "invalid_input"
    assert "attachment 1 does not exist" in failure["detail"]


def test_the_interactive_creation_carries_the_attachments(
    config, recorder, gh, monkeypatch, tmp_path, downloads,
):
    """Acceptance 4: the interactive mode's own creation carries them."""
    draft_fakes.judge_passing(monkeypatch)
    calls = draft_fakes.stub_pi_answers(
        monkeypatch, {"kind": "draft", "reply": "x", "issues": ONE_ISSUE},
    )
    image = write(tmp_path, "screen.png")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="notes need tags", attach=[str(image)],
        stdin=io.StringIO("\n"), stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    command = calls[0]["command"]
    assert command[-2].endswith(
        ".pi-session/attachments/1.png"
    ), command[-2]
    [body] = [issue["body"] for issue in gh.issues.values()]
    assert "## Attachments" in body
    assert "![screen.png](https://github.com/user-attachments/assets/1)" in body
    assert gh.uploads == ["1.png"]


def test_a_create_refuses_a_file_it_cannot_read(config, recorder, gh):
    """Acceptance 2: the create names the file it cannot read, once."""
    code, out, err = run_create(
        config, {"key": KEY, "issues": ONE_ISSUE},
        attach=["/nonexistent/screen.png"],
    )
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "invalid_input"
    assert failure["step"] == "read the attachments"
    assert "screen.png" in failure["detail"]
    assert gh.issues == {}


def test_attachment_numbers_without_a_draft_list_are_refused(
    config, recorder, gh,
):
    """An Issue using files the creation does not have is refused."""
    code, out, err = run_create(
        config, {"key": KEY, "issues": [{**ONE_ISSUE[0], "attachments": [1]}]},
    )
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "invalid_input"
    assert "Issue 1: attachments names files" in failure["detail"]


def test_the_document_list_and_the_flag_are_one_list(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """The flag appends after the document's own list, in that order."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    image = write(tmp_path, "screen.png")
    spec = write(tmp_path, "spec.html", draft_fakes.HTML_BYTES)
    code, out, err = run_round_cli(
        config, {
            "message": "tags",
            "attachments": [{"name": "screen.png", "src": str(image)}],
        },
        tmp_path / "session", attach=[str(spec)],
    )
    assert code == 0, err
    files = sessions.calls[0].attached.files
    assert [(file.name, file.source) for file in files] == [
        ("1.png", "screen.png"), ("2.html", "spec.html"),
    ]


def test_a_single_attachment_entry_is_taken_as_one_file(
    config, sessions, monkeypatch, tmp_path, downloads,
):
    """A caller's document may carry one entry; the flag adds beside it."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*ONE_ISSUE)
    image = write(tmp_path, "screen.png")
    spec = write(tmp_path, "spec.html", draft_fakes.HTML_BYTES)
    code, out, err = run_round_cli(
        config, {
            "message": "tags",
            "attachments": {"name": "spec.html", "src": str(spec)},
        },
        tmp_path / "session", attach=[str(image)],
    )
    assert code == 0, err
    files = sessions.calls[0].attached.files
    assert [(file.name, file.source) for file in files] == [
        ("1.html", "spec.html"), ("2.png", "screen.png"),
    ]


def test_the_interactive_creation_reports_an_image_that_did_not_upload(
    config, recorder, gh, monkeypatch, tmp_path, downloads,
):
    """Acceptance 4: created, and the refusal named with the repair."""
    draft_fakes.judge_passing(monkeypatch)
    draft_fakes.stub_pi_answers(
        monkeypatch, {"kind": "draft", "reply": "x", "issues": ONE_ISSUE},
    )
    image = write(tmp_path, "screen.png")
    gh.refuse_attachments = {"1.png"}
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="notes need tags", attach=[str(image)],
        stdin=io.StringIO("\n"), stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    assert len(gh.issues) == 1
    assert "screen.png" in err.getvalue()
    assert "gh auth login" in err.getvalue()


def test_the_local_draft_lists_each_issue_s_attachments(
    config, recorder, gh, monkeypatch, tmp_path, downloads,
):
    """Acceptance 4: the shown draft names each Issue's files as number,
    the person's own name and where the file sits — only those it uses."""
    draft_fakes.judge_passing(monkeypatch)
    draft_fakes.stub_pi_answers(monkeypatch, {
        "kind": "draft", "reply": "x", "issues": [
            {**ONE_ISSUE[0], "attachments": [2]},
            {**ONE_ISSUE[0], "title": "Second"},
        ],
    })
    image = write(tmp_path, "screen.png")
    spec = tmp_path / "screens" / "spec.html"
    spec.parent.mkdir()
    spec.write_text("<h1>spec</h1>", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="notes need tags",
        attach=[str(image), str(spec)],
        stdin=io.StringIO("\n"), stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    shown = out.getvalue()
    first, second = shown.split("2. Second")
    assert f"   2.html  spec.html  {spec}" in first
    assert "1.png" not in first
    assert f"   1.png  screen.png  {image}" in second
    assert f"   2.html  spec.html  {spec}" in second
