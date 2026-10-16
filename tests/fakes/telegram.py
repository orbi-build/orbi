"""A real local Telegram endpoint for the notification tests (Issue #1673).

The notification adapter POSTs to the module constant
``notify.TELEGRAM_API_BASE``; this fake starts a REAL ``http.server`` on
``127.0.0.1:0``, records every request it receives (path, content type and
the decoded JSON body) and answers the scripted status/body. The assertions
therefore cover the wire contract the person's bot endpoint would see, over
the adapter's real ``urllib`` transport — not a stubbed client.

The ``notify`` monkeypatch lives HERE on purpose: ``tools/patch_ratchet.py``
excludes the top-level ``tests/fakes/`` from its per-module counters, and a
newly added ``src/orbi`` module starts with a ceiling of 0 patches.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The Telegram API's success body (the documented `{"ok": true, ...}`).
OK_BODY = b'{"ok": true, "result": {"message_id": 1}}'


def reset_notify_state(monkeypatch) -> None:
    """Unbind the tick state ``notify.tick_start`` binds.

    The adapter keeps the loaded config and the bot token in module
    globals, so one test that bound a live configuration would otherwise
    make every later label test reach a real endpoint.
    """
    import orbi.notify as notify

    monkeypatch.setattr(notify, "_config", None)
    monkeypatch.setattr(notify, "_token", None)


def bind_notify(monkeypatch, config, token: str | None) -> None:
    """Bind one tick's configuration exactly like ``notify.tick_start``."""
    import orbi.notify as notify

    monkeypatch.setattr(notify, "_config", config)
    monkeypatch.setattr(notify, "_token", token)


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
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
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


def raise_timeout_url_error(monkeypatch) -> None:
    """Force the classic wrapped timeout: URLError(reason=TimeoutError).

    urllib wraps a socket timeout raised inside do_open into a
    URLError, so the adapter classifies it by its cause; this helper
    reproduces that shape without waiting.
    """
    import urllib.error
    import urllib.request

    def boom(*args, **kwargs):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def point_at(monkeypatch, base_url: str, *, timeout: float = 5.0) -> str:
    """Point the adapter at an arbitrary origin (e.g. a closed port)."""
    import orbi.notify as notify

    monkeypatch.setattr(notify, "TELEGRAM_API_BASE", base_url)
    monkeypatch.setattr(notify, "SEND_TIMEOUT_SECONDS", timeout)
    return base_url
