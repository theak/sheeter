"""Tests for the small Anthropic client.

Driven against a real HTTP server on a loopback port rather than by patching urlopen,
so the request that goes out is the one the API would receive: headers, body and all.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sheeter import claude


class Recorder(BaseHTTPRequestHandler):
    """Replies from a queue its test set up, and keeps what it was sent."""

    replies = []
    seen = []

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        Recorder.seen.append({
            "path": self.path,
            # urllib title-cases the header names it is given, so compare lowered.
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": json.loads(body) if body else None,
        })
        status, payload, extra = (Recorder.replies.pop(0) if Recorder.replies
                                  else (200, {"ok": True}, {}))
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    Recorder.replies = []
    Recorder.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(claude, "ATTEMPTS", 3)
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def client(server):
    return claude.Client(api_key="test-key", base_url=server, timeout=10)


def test_needs_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(claude.ClaudeError):
        claude.Client()


def test_reads_the_key_and_base_url_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/")
    made = claude.Client()
    assert made.api_key == "from-env"
    assert made.base_url == "https://gateway.example"      # trailing slash trimmed


def test_sends_what_the_api_expects(client):
    Recorder.replies.append((200, {"content": [{"type": "text", "text": "hi"}]}, {}))
    reply = client.messages.create(model="claude-sonnet-5", max_tokens=8,
                                   messages=[{"role": "user", "content": "hello"}])
    assert reply["content"][0]["text"] == "hi"

    sent = Recorder.seen[0]
    assert sent["path"] == "/v1/messages"
    assert sent["headers"]["x-api-key"] == "test-key"
    assert sent["headers"]["anthropic-version"] == claude.API_VERSION
    assert sent["headers"]["content-type"] == "application/json"
    assert sent["body"]["model"] == "claude-sonnet-5"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hello"}]


def test_a_bad_request_is_not_retried(client):
    """A 400 fails the same way twice, and the message says why."""
    Recorder.replies.append(
        (400, {"error": {"message": "tools.0.custom: bad schema"}}, {}))
    with pytest.raises(claude.ClaudeError) as caught:
        client.messages.create(model="m", max_tokens=8, messages=[])
    assert "400" in str(caught.value)
    assert "bad schema" in str(caught.value)
    assert len(Recorder.seen) == 1


def test_a_rate_limit_is_retried(client, monkeypatch):
    monkeypatch.setattr(claude.time, "sleep", lambda _seconds: None)
    Recorder.replies.append((429, {"error": {"message": "slow down"}},
                             {"retry-after": "0"}))
    Recorder.replies.append((200, {"content": [{"type": "text", "text": "second go"}]}, {}))
    reply = client.messages.create(model="m", max_tokens=8, messages=[])
    assert reply["content"][0]["text"] == "second go"
    assert len(Recorder.seen) == 2


def test_a_server_error_is_retried_then_given_up_on(client, monkeypatch):
    waits = []
    monkeypatch.setattr(claude.time, "sleep", lambda seconds: waits.append(seconds))
    for _ in range(claude.ATTEMPTS):
        Recorder.replies.append((503, {"error": {"message": "overloaded"}}, {}))
    with pytest.raises(claude.ClaudeError) as caught:
        client.messages.create(model="m", max_tokens=8, messages=[])
    assert "503" in str(caught.value)
    assert len(Recorder.seen) == claude.ATTEMPTS
    assert waits == [1, 2]                     # backs off between tries, not after


def test_an_unreachable_host_raises_rather_than_hanging():
    made = claude.Client(api_key="k", base_url="http://127.0.0.1:1", timeout=2)
    with pytest.raises(claude.ClaudeError) as caught:
        made.messages.create(model="m", max_tokens=8, messages=[])
    assert "could not reach" in str(caught.value)


def test_the_verifier_reports_a_failure_instead_of_raising(client, monkeypatch):
    """The whole point of the wrapper: sheeter.verify never lets a call take the
    reading down with it."""
    from sheeter import verify

    monkeypatch.setattr(claude.time, "sleep", lambda _seconds: None)
    for _ in range(claude.ATTEMPTS):
        Recorder.replies.append((500, {"error": {"message": "boom"}}, {}))

    out = verify.verify_analysis({"systems": [], "warnings": [],
                                  "engine": {"geometry": "1", "verifier": None,
                                             "verified": False,
                                             "verifier_error": None}},
                                 b"not a png", client=client)
    assert out["engine"]["verified"] is False
    assert "500" in out["engine"]["verifier_error"]
