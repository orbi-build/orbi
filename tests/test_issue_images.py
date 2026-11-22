"""The images one Issue hands a delivery or review session (Issue #2364).

The network boundary is faked (tests/fakes/draft.py) and so is gh (FakeGh),
so the assertions read what actually left the process: the URLs asked for,
the headers they carried, the argv the session received and the journal.
"""
from __future__ import annotations

import functools
import json
import logging
import urllib.error
from pathlib import Path

import pytest

import orbi.attachments as attachments
import orbi.failure as failure
import orbi.failure_report as failure_report
import orbi.language as language
import orbi.issue_images as issue_images
import orbi.model_catalog as model_catalog
import orbi.pi_session as pi_session
import orbi.draft_marker as draft_marker
from orbi.config import RunnerConfig
from orbi.delivery_scene import RunContext

from tests.fakes import draft as draft_fakes
from tests.fakes.github import FakeGh

REPO = "acme/app"
RUN_ID = "a1b2c3d4"
NATIVE = "https://github.com/user-attachments/assets/abc-123"
SIGNED = "https://private-user-images.githubusercontent.com/1/1.png?jwt=signed-1"
DEEPSEEK = json.loads(
    (Path(__file__).resolve().parent.parent
     / "templates" / "pi-providers" / "deepseek.json").read_text("utf-8")
)
BODY = "## Outcome\n\nA user can tag a note.\n\n## Acceptance\n\n- It works."


@pytest.fixture
def gh(monkeypatch) -> FakeGh:
    fake = FakeGh(REPO, login="orbi-bot")
    draft_fakes.install_gh(monkeypatch, fake)
    return fake


@pytest.fixture
def downloads(monkeypatch):
    return draft_fakes.install_downloads(monkeypatch)


@pytest.fixture
def worktree(tmp_path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


def image_input_config(**overrides) -> RunnerConfig:
    values = {
        "pi_provider": "deepseek", "pi_model": "deepseek-flash",
        "pi_providers_data": DEEPSEEK,
    }
    values.update(overrides)
    return RunnerConfig(**values)


def drafted(body: str, *, section: str = "") -> str:
    """One created body: the drafted text, the marker, then the section."""
    return draft_marker.with_marker(
        body, key="draft-abc123", position=1, section=section,
    )


# --- the sources ---------------------------------------------------------

def test_an_attachments_section_image_is_downloaded_with_no_token(
    gh, downloads, worktree,
):
    """Acceptance 5: the section's own address, fetched directly."""
    address = "https://cdn.example.test/shot.png"
    body = drafted(BODY, section=f"## Attachments\n\n![shot.png]({address})")
    attached = issue_images.for_session(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert [file.path.name for file in attached.files] == ["1.png"]
    assert attached.files[0].source == "shot.png"
    assert downloads.urls() == [address]
    assert gh.markdown_calls == []


def test_an_attachments_section_image_is_requested_as_orbi(
    gh, downloads, worktree,
):
    """Acceptance 1: orbi.build answers urllib's default UA with HTTP 403.

    Measured 2026-11-23 on beta.orbi.build: the same attachment address
    answers 200 to `User-Agent: orbi` and 403 to `Python-urllib/3.x`, so
    the download names this project instead of urllib.
    """
    address = "https://beta.orbi.build/api/attachments/key"
    body = drafted(BODY, section=f"## Attachments\n\n![shot.png]({address})")
    issue_images.for_session(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    headers = downloads.requests[0].headers
    assert headers.get("User-agent") == "orbi"
    assert not any("Python-urllib" in value for value in headers.values())


def test_a_403_on_the_section_image_still_stops_the_delivery(
    gh, downloads, worktree, caplog,
):
    """Acceptance 2: an address that still fails stops, exactly as today."""
    address = "https://orbi.build/api/attachments/key"
    downloads.failures[address] = urllib.error.HTTPError(
        address, 403, "Forbidden", None, None,
    )
    body = drafted(BODY, section=f"## Attachments\n\n![shot.png]({address})")
    with caplog.at_level(logging.WARNING), pytest.raises(
        issue_images.Unreadable, match="shot.png",
    ) as raised:
        issue_images.for_session(
            {"number": 1, "body": body}, config=image_input_config(),
            repo=REPO, worktree=worktree,
        )
    assert raised.value.failure_record.reason_code == "attachment_unavailable"
    assert "HTTPError" in caplog.text


def test_a_native_image_in_the_body_is_read_through_the_markdown_render(
    gh, downloads, worktree,
):
    """Acceptance 5: rendered, then the signed address downloads with no token."""
    body = drafted(f"{BODY}\n\n![shots]({NATIVE})")
    issue_images.for_session(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert gh.markdown_calls == [NATIVE]
    assert downloads.urls() == [SIGNED]
    # No credential of ours: the signed address IS the credential.
    assert "headers" not in downloads.requests[0].kwargs
    assert "data" not in downloads.requests[0].kwargs


def test_a_native_image_of_a_trusted_comment_is_read(gh, downloads, worktree):
    """Acceptance 5: a comment whose author can write is a source."""
    gh.add_issue(1, body=drafted(BODY))
    gh.collaborators["maintainer"] = "write"
    gh.comment(1, f"look: ![shot]({NATIVE})", login="maintainer")
    issue_images.for_session(
        {"number": 1, "body": gh.issues[1]["body"]}, config=image_input_config(),
        repo=REPO, worktree=worktree,
    )
    assert gh.markdown_calls == [NATIVE]


def test_a_link_in_an_untrusted_comment_is_never_fetched(
    gh, downloads, worktree,
):
    """Acceptance 5: an outsider's link is not read."""
    gh.add_issue(1, body=drafted(BODY))
    gh.collaborators["stranger"] = "read"
    gh.comment(1, f"look: ![shot]({NATIVE})", login="stranger")
    attached = issue_images.for_session(
        {"number": 1, "body": gh.issues[1]["body"]}, config=image_input_config(),
        repo=REPO, worktree=worktree,
    )
    assert attached.files == ()
    assert gh.markdown_calls == []
    assert downloads.urls() == []


def test_an_image_repeated_in_two_places_is_read_once(gh, downloads, worktree):
    """The same native image in the body and in the section is one file."""
    section = f"## Attachments\n\n![shot]({NATIVE})"
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})", section=section)
    attached = issue_images.for_session(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert len(attached.files) == 1
    assert gh.markdown_calls == [NATIVE]


def test_a_body_without_a_marker_still_offers_its_images(
    gh, downloads, worktree,
):
    """A hand-written Issue has no marker: its native images still arrive."""
    body = f"{BODY}\n\n![shot]({NATIVE})"
    attached = issue_images.for_session(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert len(attached.files) == 1
    assert gh.markdown_calls == [NATIVE]


def test_a_downloaded_image_keeps_its_own_suffix(gh, downloads, worktree):
    """The suffix comes from the bytes, not from the address."""
    address = "https://cdn.example.test/shot.jpg"
    downloads.answers[address] = b"\xff\xd8\xff\xe0" + b"0" * 8
    body = drafted(BODY, section=f"## Attachments\n\n![shot]({address})")
    attached = issue_images.for_session(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert [file.path.name for file in attached.files] == ["1.jpg"]


def test_a_download_that_is_not_an_image_is_unreadable(
    gh, downloads, worktree, caplog,
):
    """The bytes decide: anything else is an image nobody can read."""
    address = "https://cdn.example.test/not-an-image.png"
    downloads.answers[address] = b"<html>not an image</html>"
    body = drafted(BODY, section=f"## Attachments\n\n![shot]({address})")
    with caplog.at_level(logging.WARNING), pytest.raises(
        issue_images.Unreadable, match="shot",
    ):
        issue_images.for_session(
            {"number": 1, "body": body}, config=image_input_config(),
            repo=REPO, worktree=worktree,
        )
    assert "ValueError" in caplog.text


def test_an_unreadable_image_stops_and_names_the_file(
    gh, downloads, worktree, caplog,
):
    """Acceptance 6: unreadable means stop, with the file named."""
    address = "https://cdn.example.test/gone.png"
    downloads.failures[address] = OSError("no route")
    other = "https://cdn.example.test/ok.png"
    body = drafted(
        BODY,
        section=f"## Attachments\n\n![gone]({address})\n\n![ok]({other})",
    )
    with caplog.at_level(logging.WARNING), pytest.raises(
        issue_images.Unreadable, match="gone",
    ) as raised:
        issue_images.for_session(
            {"number": 1, "body": body}, config=image_input_config(),
            repo=REPO, worktree=worktree,
        )
    assert raised.value.names == ("gone",)
    assert "issue_images_unreadable" in caplog.text
    assert "OSError" in caplog.text


def test_a_failed_comment_read_leaves_the_body_images(
    gh, downloads, worktree, caplog,
):
    """A bypass: the body's images still arrive."""
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.comment_read_failure = "gh: HTTP 502"
    with caplog.at_level(logging.WARNING):
        attached = issue_images.for_session(
            {"number": 1, "body": body}, config=image_input_config(),
            repo=REPO, worktree=worktree,
        )
    assert len(attached.files) == 1
    assert "issue_images_unreadable" in caplog.text


def test_the_signed_address_is_never_logged(
    gh, downloads, worktree, caplog,
):
    """Acceptance 6: the credential rides the query and never reaches the log."""
    downloads.failures[SIGNED] = OSError("no route")
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    with caplog.at_level(logging.DEBUG), pytest.raises(
        issue_images.Unreadable,
    ):
        issue_images.for_session(
            {"number": 1, "body": body}, config=image_input_config(),
            repo=REPO, worktree=worktree,
        )
    assert "jwt=" not in caplog.text
    assert "secret" not in caplog.text


# --- the sessions -------------------------------------------------------

def test_the_implement_session_receives_the_issue_images(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 5: the image argument before the context, named in it."""
    (worktree / "prompt.md").write_text("SYSTEM", "utf-8")
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.add_issue(4, title="Tag a note", body=body)
    calls = draft_fakes.stub_stream_pi(monkeypatch, "done")
    config = image_input_config(
        prompt=worktree / "prompt.md", source_repos=(REPO,),
        workspace_root=worktree,
    )
    pi_session.run_pi(
        {"number": 4, "title": "Tag a note", "body": body},
        RunContext(run_id=RUN_ID, issue=4, branch="b", worktree=worktree,
                   source_repo=REPO),
        config,
    )
    command = calls[0]["command"]
    staged = worktree / ".orbi" / "attachments" / "1.png"
    assert command[-2] == f"@{staged}"
    assert command[-1].startswith("@")
    context = Path(command[-1].removeprefix("@")).read_text("utf-8")
    assert "The person attached: 1.png = shot" in context
    assert staged.read_bytes() == draft_fakes.PNG_BYTES


def test_the_review_session_reads_the_images_again_every_round(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 5: each review round re-reads them."""
    (worktree / "prompt_review.md").write_text(
        "REVIEW {{ISSUE_BODY}}", "utf-8",
    )
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.add_issue(4, title="Tag a note", body=body)
    calls = draft_fakes.stub_stream_pi(monkeypatch, "REVIEW_VERDICT: pass")
    config = image_input_config(
        prompt_review=worktree / "prompt_review.md", source_repos=(REPO,),
        workspace_root=worktree,
    )
    ctx = RunContext(
        run_id=RUN_ID, issue=4, branch="b", worktree=worktree,
        source_repo=REPO,
    )
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    pi_session.run_review(ctx, pr, config, 1)
    pi_session.run_review(ctx, pr, config, 2)
    staged = worktree / ".orbi" / "attachments" / "1.png"
    for call in calls:
        assert call["command"][-2] == f"@{staged}"
    assert len(downloads.requests) == 2


def test_an_unreadable_image_stops_before_any_session(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 6: no session starts; one comment names the file and fix."""
    (worktree / "prompt.md").write_text("SYSTEM", "utf-8")
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.add_issue(4, title="Tag a note", body=body)
    downloads.failures[SIGNED] = OSError("no route")
    calls = draft_fakes.stub_stream_pi(monkeypatch, "done")
    config = image_input_config(
        prompt=worktree / "prompt.md", source_repos=(REPO,),
        workspace_root=worktree,
    )
    with pytest.raises(issue_images.Unreadable, match="shot"):
        pi_session.run_pi(
            {"number": 4, "title": "Tag a note", "body": body},
            RunContext(run_id=RUN_ID, issue=4, branch="b", worktree=worktree,
                       source_repo=REPO),
            config,
        )
    assert calls == []
    # The failure report posts the ONE comment, so the session boundary
    # posts none (Issue #2364: exactly one stop comment).
    assert gh.issues[4]["comments"] == []
    assert not (worktree / ".orbi" / "attachments" / "1.png").exists()


def test_the_stop_carries_its_own_failure_record():
    """Acceptance 6: the reason is decided at the source, not by the text."""
    exc = issue_images.Unreadable(("shot", "spec.png"))
    assert exc.failure_record.reason_code == issue_images.REASON_CODE
    assert exc.failure_record.reason_code in failure.REASON_CODES
    assert exc.failure_record.retry_safe is False
    assert str(exc) == "the attached images shot, spec.png could not be read"
    assert failure_report.is_unrecoverable_failure(exc) is True


def _stop_report(gh, exc, *, classify: bool, action: str, reason: str):
    """Report the stop the way the delivery (`classify=False`) or the
    review round (`classify=True`) does: the caller supplies the readable
    Reason/Action exactly as `implementation_delivery` and
    `review_merge` build them."""
    gh.add_issue(4, title="Tag a note", labels=[{"name": "ai-in-progress"}])
    failure_report.report_delivery_failure(
        exc,
        issue={"number": 4, "title": "Tag a note",
               "labels": [{"name": "ai-in-progress"}]},
        source_repo=REPO, run_id=None, pr_url=None, worktree=None,
        branch="b", role="implement", language_code="en",
        classify=classify, evidence=False, finish=False,
        ready_label="ai-ready", current_labels={"ai-in-progress", "ai-ready"},
        action=action, reason=reason,
    )
    return gh.issues[4]


@pytest.mark.parametrize("review", [False, True])
def test_an_unreadable_image_blocks_the_issue_with_one_comment(gh, review):
    """Acceptance 6: `ai-blocked` and ONE comment naming the file and the fix.

    `classify=False` is the implement-phase terminal report and
    `classify=True` an open PR's review round: both stop this way, and a
    blocked Issue is not claimed again, so no second tick comments. The
    failure block carries `attachment_unavailable` either way.
    """
    exc = issue_images.Unreadable(("shot",))
    record = failure_report._classify_failure(exc, outcome="blocked")
    repair = failure_report.terminal_action(record, "en")
    issue = _stop_report(
        gh, exc, classify=review,
        action=(
            failure_report.blocked_action(
                exc, functools.partial(language.text, "en"),
            ) if review else repair
        ),
        reason=(
            failure.failure_detail(exc) if not review
            else language.text(
                "en", "review.blocked_failed_reason", detail=str(exc),
                pr_url="https://x/pull/9",
            )
        ),
    )
    assert "ai-blocked" in issue["labels"]
    assert "ai-in-progress" not in issue["labels"]
    [comment] = issue["comments"]
    body = comment["body"]
    assert "shot" in body
    assert "could not be read" in body
    assert '"reason_code":"attachment_unavailable"' in body
    assert "ai-ready" in body and "ai-fix-needed" in body
    assert "Upload the image" in body
    assert "jwt=" not in body


# --- the catalog --------------------------------------------------------

def test_the_declaration_is_the_models_own_input_list(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = image_input_config()
    assert model_catalog.declares_image_input(config) is True
    assert model_catalog.declares_image_input(
        image_input_config(pi_model="another-model"),
    ) is False
    assert model_catalog.declares_image_input(
        image_input_config(pi_provider=None),
    ) is False
    assert model_catalog.declares_image_input(
        image_input_config(pi_providers_data=None),
    ) is False


def test_the_review_selection_overrides_the_implementer_one(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = image_input_config(
        review_pi_provider="deepseek", review_pi_model="deepseek-flash",
        pi_provider="other", pi_model="other-model",
    )
    assert model_catalog.declares_image_input(config) is False
    assert model_catalog.declares_image_input(config, review=True) is True


def test_a_model_override_declares_the_input(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    providers = {
        "providers": {
            "custom": {
                "baseUrl": "https://example.test", "api": "openai-completions",
                "models": [{"id": "m", "input": ["text"]}],
                "modelOverrides": {"m": {"input": ["text", "image"]}},
            },
        },
    }
    config = RunnerConfig(
        pi_provider="custom", pi_model="m", pi_providers_data=providers,
    )
    assert model_catalog.declares_image_input(config) is True


def test_the_user_catalog_is_merged_with_the_configured_file(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(json.dumps({"providers": {
        "local": {"models": [{"id": "m"}]},
    }}), "utf-8")
    monkeypatch.setenv("HOME", str(home))
    config = image_input_config()
    merged = model_catalog.merged_providers(config)
    assert set(merged) == {"local", "deepseek"}


def test_an_invalid_user_catalog_fails_fast(monkeypatch, tmp_path):
    home = tmp_path / "home"
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text("{nope", "utf-8")
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ValueError, match="not valid JSON"):
        model_catalog.merged_providers(image_input_config())


# --- the section and the arguments as pure values ------------------------

def test_the_naming_line_and_arguments_follow_the_declaration(tmp_path):
    """A viewable image travels; without image input it is only named."""
    session = tmp_path / "session"
    (tmp_path / "screen.png").write_bytes(draft_fakes.PNG_BYTES)
    attached = attachments.stage(
        [{"name": "screen.png", "src": str(tmp_path / "screen.png")}],
        session, viewable=True,
    )
    assert attached.arguments() == [f"@{session / '1.png'}"]
    hidden = attachments.Attached(files=attached.files, viewable=False)
    assert hidden.arguments() == []
    assert "cannot be viewed" in hidden.naming()
    assert attachments.Attached().naming() == ""
    assert attachments.with_arguments(["pi", "ctx"], []) == ["pi", "ctx"]
    assert attachments.with_arguments(
        ["pi", "--system-prompt", "p.md", "@ctx"], ["@img"],
    ) == ["pi", "--system-prompt", "p.md", "@img", "@ctx"]
