"""The images one Issue hands a delivery or review session (Issue #2364).

The network boundary is faked (tests/fakes/draft.py) and so is gh (FakeGh),
so the assertions read what actually left the process: the URLs asked for,
the headers they carried, the argv the session received and the journal.
"""
from __future__ import annotations

import functools
import json
import logging
import time
import urllib.error
from pathlib import Path

import pytest

from conftest import rendered_prompt

import orbi.attachments as attachments
import orbi.clarify as clarify
import orbi.failure as failure
import orbi.failure_report as failure_report
import orbi.language as language
import orbi.issue_images as issue_images
import orbi.issue_material as issue_material
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
NATIVE_2 = "https://github.com/user-attachments/assets/def-456"
NATIVE_3 = "https://github.com/user-attachments/assets/ghi-789"
SIGNED = "https://private-user-images.githubusercontent.com/1/1.png?jwt=signed-1"
SIGNED_2 = (
    "https://private-user-images.githubusercontent.com/1/2.png?jwt=signed-2"
)
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


def session_images(
    issue: dict, *, config, repo: str, worktree: Path, review: bool = False,
) -> attachments.Attached:
    """The images ONE session receives, through the ONE shared entry point.

    Issue #2390: `orbi.issue_material.material` is the only builder of an
    Issue session's material, so these image assertions read the image half
    of exactly what every session is handed. `with_comments=False` keeps the
    text half out — it is the same shape a session whose prompt already
    carries `{{ISSUE_COMMENTS}}` gets, and the images still read the trusted
    comments.
    """
    return issue_material.material(
        issue, config, repo=repo, directory=worktree, review=review,
        with_comments=False,
    ).images


# --- the sources ---------------------------------------------------------

def test_an_attachments_section_image_is_downloaded_with_no_token(
    gh, downloads, worktree,
):
    """Acceptance 5: the section's own address, fetched directly."""
    address = "https://cdn.example.test/shot.png"
    body = drafted(BODY, section=f"## Attachments\n\n![shot.png]({address})")
    attached = session_images(
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
    session_images(
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
        session_images(
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
    session_images(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert gh.markdown_calls == [NATIVE]
    assert downloads.urls() == [SIGNED]
    # No credential of ours: the signed address IS the credential.
    assert "headers" not in downloads.requests[0].kwargs
    assert "data" not in downloads.requests[0].kwargs


def test_a_private_issue_receives_its_section_and_body_native_images(
    gh, downloads, worktree,
):
    """Acceptance 1: a private repository's native images arrive, no stop.

    The fake signs an image only for the documented `gfm` render with
    `context`, and answers the bare native address with HTTP 404 (a
    download carrying no credential) — the measurement in Issue #2387, whose
    delivery stopped with `attachment_unavailable` without the mode.
    """
    section = f"## Attachments\n\n![shot]({NATIVE})"
    body = drafted(f"{BODY}\n\n![pasted]({NATIVE_2})", section=section)
    attached = session_images(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert gh.markdown_calls == [NATIVE, NATIVE_2]
    # Only the rendered addresses were fetched: the bare ones answer 404.
    assert downloads.urls() == [
        "https://private-user-images.githubusercontent.com/1/1.png"
        "?jwt=signed-1",
        "https://private-user-images.githubusercontent.com/1/2.png"
        "?jwt=signed-2",
    ]
    assert [file.source for file in attached.files] == ["shot", "pasted"]
    assert [file.path.name for file in attached.files] == ["1.png", "2.png"]


def test_the_render_asks_for_gfm_mode_with_the_repository_as_context(
    gh, downloads, worktree,
):
    """Acceptance 1 / Issue #73: the request is GitHub's documented one.

    POST /markdown reads `context` only in `gfm` mode
    (https://docs.github.com/en/rest/markdown/markdown#render-a-markdown-document);
    without it the render keeps the bare native address.
    """
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    session_images(
        {"number": 1, "body": body}, config=image_input_config(), repo=REPO,
        worktree=worktree,
    )
    assert gh.markdown_fields == [{
        "text": f"![image]({NATIVE})", "mode": "gfm", "context": REPO,
    }]


def test_a_native_image_of_a_trusted_comment_is_read(gh, downloads, worktree):
    """Acceptance 5: a comment whose author can write is a source."""
    gh.add_issue(1, body=drafted(BODY))
    gh.collaborators["maintainer"] = "write"
    gh.comment(1, f"look: ![shot]({NATIVE})", login="maintainer")
    session_images(
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
    attached = session_images(
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
    attached = session_images(
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
    attached = session_images(
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
    attached = session_images(
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
        session_images(
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
        session_images(
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
        attached = session_images(
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
        session_images(
            {"number": 1, "body": body}, config=image_input_config(),
            repo=REPO, worktree=worktree,
        )
    assert "jwt=" not in caplog.text
    assert "secret" not in caplog.text


# --- the sessions -------------------------------------------------------

def test_a_model_without_image_input_reads_no_image(
    gh, downloads, worktree,
):
    """Acceptance 2: a text-only model neither reads nor names an image."""
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    attached = session_images(
        {"number": 1, "body": body}, config=RunnerConfig(), repo=REPO,
        worktree=worktree,
    )
    assert attached.files == ()
    assert gh.markdown_calls == []
    assert downloads.urls() == []


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


def test_the_ticket_agent_receives_the_issue_images(
    gh, downloads, worktree, monkeypatch,
):
    """Issue #2390: the ticket-only/clarify launcher uses the same entry.

    'run_ticket_agent' is the one no-tools launcher: the clarify gate and the
    ticket-only content agent both read the Issue through it. It stages and
    names the images exactly like the implement/review sessions, so no session
    path assembles the Issue's text and images on its own.
    """
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.add_issue(4, title="Tag a note", body=body)
    calls = draft_fakes.stub_stream_pi_with_context(monkeypatch, "content")
    config = image_input_config(source_repos=(REPO,))
    pi_session.run_ticket_agent(
        {"number": 4, "title": "Tag a note", "body": body}, config, REPO,
    )
    command = calls[0]["command"]
    assert command[-2].startswith("@")
    assert command[-2].endswith(".png")
    assert command[-1].startswith("@")
    assert "The person attached: 1.png = shot" in calls[0]["context"]
    assert gh.markdown_calls == [NATIVE]
    assert downloads.urls() == [SIGNED]


def test_the_review_session_reads_the_body_and_comment_images_every_round(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 2: every round rebuilds the body's and comment's images.

    The review session re-reads the Issue for each round, so a trusted
    writer's native image and the body's native image both arrive again.
    """
    (worktree / "prompt_review.md").write_text(
        "REVIEW {{ISSUE_BODY}}", "utf-8",
    )
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.add_issue(4, title="Tag a note", body=body)
    gh.collaborators["maintainer"] = "write"
    gh.comment(4, f"spec: ![spec]({NATIVE_2})", login="maintainer")
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
    staged = [
        worktree / ".orbi" / "attachments" / f"{position}.png"
        for position in (1, 2)
    ]
    assert len(calls) == 2
    for call in calls:
        assert call["command"][-3:-1] == [f"@{path}" for path in staged]
        context = Path(call["command"][-1].removeprefix("@")).read_text("utf-8")
        assert "1.png = shot" in context
        assert "2.png = spec" in context
    # The fake numbers each render, so the second round signs 3 and 4.
    assert downloads.urls() == [
        SIGNED, SIGNED_2,
        "https://private-user-images.githubusercontent.com/1/3.png"
        "?jwt=signed-3",
        "https://private-user-images.githubusercontent.com/1/4.png"
        "?jwt=signed-4",
    ]


def test_the_implement_session_receives_section_body_and_comment_images(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 1 + 2: a private Issue's native images reach the session.

    The Attachments section, the pasted body image and a trusted
    maintainer's comment image all arrive as staged '@' arguments and are
    named in the context; the fake signs only the documented gfm render, so
    a bare native address would have stopped the session.
    """
    (worktree / "prompt.md").write_text("SYSTEM", "utf-8")
    section = f"## Attachments\n\n![shot]({NATIVE})"
    body = drafted(f"{BODY}\n\n![pasted]({NATIVE_2})", section=section)
    gh.add_issue(4, title="Tag a note", body=body)
    gh.collaborators["maintainer"] = "write"
    gh.comment(4, f"spec: ![spec]({NATIVE_3})", login="maintainer")
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
    staged = [
        worktree / ".orbi" / "attachments" / f"{position}.png"
        for position in (1, 2, 3)
    ]
    assert command[-4:-1] == [f"@{path}" for path in staged]
    context = Path(command[-1].removeprefix("@")).read_text("utf-8")
    assert "1.png = shot" in context
    assert "2.png = pasted" in context
    assert "3.png = spec" in context
    assert gh.markdown_calls == [NATIVE, NATIVE_2, NATIVE_3]


def test_an_untrusted_comment_image_never_reaches_the_session(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 2: an outsider's native image is still not fetched."""
    (worktree / "prompt.md").write_text("SYSTEM", "utf-8")
    body = drafted(f"{BODY}\n\n![shot]({NATIVE})")
    gh.add_issue(4, title="Tag a note", body=body)
    gh.collaborators["stranger"] = "read"
    gh.comment(4, f"look: ![outsider]({NATIVE_2})", login="stranger")
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
    context = Path(command[-1].removeprefix("@")).read_text("utf-8")
    assert "1.png = shot" in context
    assert "outsider" not in context
    assert gh.markdown_calls == [NATIVE]


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


# --- Issue #2390: ONE material for every Issue session --------------------

NATIVE_4 = "https://github.com/user-attachments/assets/jkl-012"
SECTION_IMAGE = "https://cdn.example.test/section.png"
HTML_BLOCK = (
    "<details><summary>spec.html</summary>\n\n"
    "```html\n<p>the spec</p>\n```\n\n</details>"
)


def _material_issue(gh) -> dict:
    """One Issue shaped the way `orbi draft --create` writes it.

    The Attachments section, a pasted body image, one trusted comment with
    an image and one untrusted comment with an image: every session must
    receive the same title, body, trusted text and the first three images,
    never the outsider's.
    """
    body = drafted(
        f"{BODY}\n\n![pasted]({NATIVE})",
        section=(
            f"## Attachments\n\n![section]({SECTION_IMAGE})\n\n{HTML_BLOCK}"
        ),
    )
    gh.add_issue(4, title="Tag a note", body=body)
    gh.collaborators["maintainer"] = "write"
    gh.collaborators["stranger"] = "read"
    gh.comment(4, f"spec: ![spec]({NATIVE_2})", login="maintainer")
    gh.comment(4, f"outside: ![bad]({NATIVE_3})", login="stranger")
    return {"number": 4, "title": "Tag a note", "body": body}


def _assert_shared_material(text: str, *, header: bool = True) -> None:
    """The title, body, trusted text and the HTML block every session sees."""
    assert "Tag a note" in text
    if header:
        assert "Issue #4: Tag a note" in text
    assert BODY in text
    assert "maintainer" in text and "spec: ![spec]" in text
    assert "<p>the spec</p>" in text
    assert "outside:" not in text and "bad" not in text


def _assert_three_images(command: list[str]) -> None:
    """Exactly the section, body and trusted-comment images travel."""
    images = [
        item for item in command
        if item.startswith("@") and item.endswith(".png")
    ]
    assert len(images) == 3
    assert command[-1].startswith("@")


def test_every_issue_session_receives_the_same_material(
    gh, downloads, worktree, monkeypatch,
):
    """Acceptance 1: clarify, ticket-only, implement, both review rounds and
    a steering restart all read the ONE Issue material.

    The fixture is the creation shape: title, body with an Attachments
    section image and a native image, a trusted comment with an image, an
    untrusted comment with an image and an HTML block. The untrusted link is
    never rendered by GitHub nor downloaded.
    """
    issue = _material_issue(gh)
    gh.add_pr(9, head="b", url="https://x/pull/9")
    config = image_input_config(
        prompt=worktree / "prompt.md", prompt_review=worktree / "prompt_review.md",
        source_repos=(REPO,), workspace_root=worktree,
    )
    (worktree / "prompt.md").write_text(
        "SYSTEM {{ISSUE_TITLE}} {{ISSUE_BODY}} {{ISSUE_COMMENTS}}", "utf-8",
    )
    (worktree / "prompt_review.md").write_text(
        "REVIEW {{ISSUE_TITLE}} {{ISSUE_BODY}} {{ISSUE_COMMENTS}}", "utf-8",
    )
    ctx = RunContext(
        run_id=RUN_ID, issue=4, branch="b", worktree=worktree,
        source_repo=REPO,
    )
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}

    # 1. The clarify gate, through the shared no-tools launcher.
    clarify_sessions = draft_fakes.stub_stream_pi_with_context(monkeypatch)
    clarify.judge_issue_body(issue, config, REPO, RUN_ID)
    _assert_shared_material(clarify_sessions[0]["context"])
    _assert_three_images(clarify_sessions[0]["command"])

    # 2. The ticket-only content run, through the same launcher.
    ticket_sessions = draft_fakes.stub_stream_pi_with_context(monkeypatch)
    pi_session.run_ticket_agent(issue, config, REPO)
    _assert_shared_material(ticket_sessions[0]["context"])
    _assert_three_images(ticket_sessions[0]["command"])

    # 3. Implementation (and ops: the same run_pi build point).
    implement = draft_fakes.stub_stream_pi(monkeypatch, "done")
    pi_session.run_pi(issue, ctx, config)
    prompt = rendered_prompt(implement[0]["command"])
    _assert_shared_material(prompt, header=False)
    context = Path(implement[0]["command"][-1].removeprefix("@")).read_text("utf-8")
    assert "The person attached: 1.png = section" in context
    _assert_three_images(implement[0]["command"])

    # 4. Every review and fix round.
    for round_number in (1, 2):
        reviews = draft_fakes.stub_stream_pi(monkeypatch, "REVIEW_VERDICT: pass")
        pi_session.run_review(ctx, pr, config, round_number)
        prompt = rendered_prompt(reviews[0]["command"])
        _assert_shared_material(prompt, header=False)
        _assert_three_images(reviews[0]["command"])

    # 5. A steering restart, with a NEW trusted image attached mid-session.
    draft_fakes.stub_steering_snapshot(monkeypatch)
    steering = draft_fakes.stub_stream_pi(monkeypatch, "done")
    pi_session.run_pi(issue, ctx, config)
    gh.comment(4, f"later: ![later]({NATIVE_4})", login="maintainer")
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    gh.issues[4]["comments"][-1]["createdAt"] = soon
    request = steering[0]["watch"].steering_check()
    assert request is not None
    _assert_shared_material(request.context)
    assert "later: ![later]" in request.context
    assert (
        "The person attached: 1.png = section, 2.png = pasted, "
        "3.png = spec, 4.png = later"
    ) in request.context
    assert request.command is not None
    assert len([
        item for item in request.command
        if item.startswith("@") and item.endswith(".png")
    ]) == 4
    assert request.command[-1].startswith("@")

    # The outsider's image never reached GitHub's renderer nor the network.
    assert NATIVE_3 not in gh.markdown_calls
    assert not any(NATIVE_3 in url or "bad" in url for url in downloads.urls())
