"""The Telegram HTTP boundary the notification tests run against (Issue #1673).

The notification adapter POSTs to the module constant
``notify.TELEGRAM_API_BASE``; this fake starts a REAL ``http.server`` on
``127.0.0.1:0``, records every request it receives (path, content type and
the decoded JSON body) and answers the scripted status/body. The assertions
therefore cover the wire contract the person's bot endpoint would see, over
the adapter's real ``urllib`` transport — not a stubbed client.

Only the HTTP BOUNDARY is replaced here: ``TELEGRAM_API_BASE`` (the origin
the adapter POSTs to) and ``SEND_TIMEOUT_SECONDS`` (the request budget, so
a test never waits the real 10 s). Everything on the adapter's side of that
boundary stays real: the state it sends with is bound the way a tick binds
it — ``notify.tick_start`` with the bot token in the environment
(``ORBI_TELEGRAM_BOT_TOKEN``) — so no test writes ``notify`` internals and
the pure-bypass guards stay under test (Issue #1698).
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The Telegram API's success body (the documented `{"ok": true, ...}`).
OK_BODY = b'{"ok": true, "result": {"message_id": 1}}'


def reset_notify_state(monkeypatch, repo_dir) -> None:
    """Leave the adapter off: the state a tick without a chat id binds.

    The adapter keeps the loaded config and the bot token in module
    globals, so one test that bound a live configuration would otherwise
    make every later label test reach a real endpoint. The state changes
    through ``notify.tick_start`` only. ``repo_dir`` is a tmp directory:
    with notifications off the tick deletes the merged-PR watermark under
    it and reads no repository.
    """
    from orbi import config as config_domain

    bind_notify(
        monkeypatch,
        config_domain.RunnerConfig(
            telegram_chat_id=None, repo_dir=Path(repo_dir),
        ),
        None,
    )


def bind_notify(monkeypatch, config, token: str | None) -> None:
    """Bind one tick's config and token exactly like ``notify.tick_start``.

    The token travels the way the tick reads it, through the environment;
    the config goes through the module's own entry point, so nothing here
    writes ``notify`` internals. A config carrying a source repository also
    runs ``tick_start``'s merged-PR scan, which reads GitHub: a scene that
    binds the message state alone passes ``source_repos=()``.
    """
    import orbi.notify as notify

    if token is None:
        monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(notify.TOKEN_ENV, token)
    notify.tick_start(config)


class TelegramEndpoint:
    """A real local HTTP endpoint recording every request it receives."""

    def __init__(self, *, status: int = 200, body: bytes | None = None,
                 delay: float = 0.0) -> None:
        self.status = status
        self.body = OK_BODY if body is None else body
        self.delay = delay
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """The origin the adapter must be pointed at."""
        assert self._server is not None, "endpoint is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def point(self, monkeypatch, *, timeout: float = 5.0) -> str:
        """Point the adapter at this endpoint (base URL and timeout)."""
        return point_at(monkeypatch, self.base_url, timeout=timeout)

    def messages(self) -> list[dict]:
        """The decoded JSON body of every request that reached the endpoint."""
        return [entry["json"] for entry in self.requests]

    def texts(self) -> list[str]:
        """The `text` field of every message the endpoint received."""
        return [message["text"] for message in self.messages()]

    def __enter__(self) -> "TelegramEndpoint":
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                payload = json.loads(raw)
                endpoint.requests.append({
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "json": payload,
                    "raw": raw,
                })
                if endpoint.delay:
                    time.sleep(endpoint.delay)
                self.send_response(endpoint.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(endpoint.body)))
                self.end_headers()
                try:
                    self.wfile.write(endpoint.body)
                except OSError:
                    pass

            def log_message(self, *args) -> None:
                """Silence the handler's stderr chatter."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def closed_origin(monkeypatch, *, timeout: float = 5.0) -> str:
    """Point the adapter at a real local port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return point_at(monkeypatch, f"http://127.0.0.1:{port}", timeout=timeout)


@contextlib.contextmanager
def unanswered_origin(monkeypatch, *, timeout: float = 0.2):
    """Point the adapter at a listening socket whose connect never answers.

    ``listen(0)`` queues one connection and drops the rest: with that one
    slot taken, the adapter's TCP connect itself times out — the shape
    urllib wraps into ``URLError(reason=TimeoutError)``.
    """
    listener = socket.socket()
    queued = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(0)
        host, port = listener.getsockname()
        # The one queue slot listen(0) offers; the listener never accepts,
        # so every later connect waits for an answer.
        queued.connect((host, port))
        yield point_at(monkeypatch, f"http://{host}:{port}", timeout=timeout)
    finally:
        queued.close()
        listener.close()


def point_at(monkeypatch, base_url: str, *, timeout: float = 5.0) -> str:
    """Point the adapter at an arbitrary origin (e.g. a closed port)."""
    import orbi.notify as notify

    monkeypatch.setattr(notify, "TELEGRAM_API_BASE", base_url)
    monkeypatch.setattr(notify, "SEND_TIMEOUT_SECONDS", timeout)
    return base_url
