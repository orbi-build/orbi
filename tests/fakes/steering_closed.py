import json
import subprocess
from pathlib import Path

import orbi.pi_session as pi_session
from seam import seam


def patch_closed_mid_delivery_deps(monkeypatch, tmp_path: Path, poll_states):
    """Install fake gh/git/pi dependencies for mid-delivery Issue close tests."""
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    comments: list[tuple] = []
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: comments.append((number, repo, body)),
    )
    gh_calls: list[list[str]] = []
    comment_id = 100

    def fake_run_command(command, **kwargs):
        nonlocal comment_id
        if command[:1] == ["gh"]:
            gh_calls.append(command)
            if command[:2] == ["gh", "api"]:
                if "-X" in command or "--method" in command or any(
                    a.startswith("body=") for a in command
                ):
                    comment_id += 1
                    return json.dumps({"id": comment_id})
                return json.dumps([])
            if command[:3] == ["gh", "issue", "view"]:
                return json.dumps({"labels": [{"name": "ai-ready"}], "state": "OPEN"})
            return ""
        res = subprocess.run(
            command, cwd=kwargs.get("cwd"),
            capture_output=True, text=True, check=True,
        )
        return res.stdout.strip()

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, **kw: {
            "comments": [], "body": "Body", "state": next(poll_states),
        },
    )

    def fake_build_pi_command(config, role, excluded, session_dir, system_prompt, context, **kw):
        script = tmp_path / "fake_pi.py"
        script.write_text(
            "import subprocess, time\n"
            "from pathlib import Path\n"
            "Path('work.txt').write_text('committed work\\n', encoding='utf-8')\n"
            "subprocess.run(['git', 'add', 'work.txt'], check=True)\n"
            "subprocess.run(['git', 'commit', '-m', 'partial work'], check=True)\n"
            "for _ in range(200):\n"
            "    print('working', flush=True)\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        ctx_file = tmp_path / "ctx.txt"
        ctx_file.write_text(context, encoding="utf-8")
        cmd = ["python3", str(script), f"@{ctx_file}"]
        return cmd, cmd

    monkeypatch.setitem(pi_session.__dict__, "build_pi_command", fake_build_pi_command)
    monkeypatch.setitem(pi_session.__dict__, "prepare_pi_agent_dir", lambda *a, **k: None)
    monkeypatch.setitem(pi_session.__dict__, "pi_extension_env", lambda *a, **k: {})
    return comments, gh_calls


def patch_closed_no_commit_deps(monkeypatch, tmp_path: Path, base_sha: str):
    """Install fake dependencies for deliver_pr(issue_closed=True) without commits."""
    from tests.test_bootstrap_delivery import _seed_deliver_run_state, fake_deliver_run

    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return base_sha
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    comments: list[tuple] = []
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: comments.append((number, repo, body)),
    )
    return comments
