"""A small client for the one Anthropic endpoint this app calls.

Why not the official SDK: this app makes exactly one kind of request, a POST to
/v1/messages, and reads one field out of the reply.  The SDK brings jiter,
pydantic-core, httpx2, anyio and the rest with it, about 21MB installed, and jiter
publishes no musllinux wheel at all, which is what forced the docker image off Alpine
and onto Debian.

Switching vendors does not help.  The openai SDK requires jiter too, and litellm
depends on openai and adds tiktoken, tokenizers and fastuuid on top, all of which are
Rust extensions with the same problem, so both are strictly worse.  urllib is in the
standard library, speaks the API perfectly well, and costs nothing.

What the SDK does that this does not: streaming, which is unused here, and typed
response models, which are unused too since the reply is walked as plain JSON either
way.  Retries are the one thing worth keeping, so they are kept.
"""

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

#: The vision pass sends several images and can think for a while.
TIMEOUT_SECONDS = 180
ATTEMPTS = 3

#: Worth trying again: rate limits, timeouts, and anything the far end broke on.  A 400
#: is a bad request and will be just as bad the second time.
RETRY_CODES = frozenset([408, 409, 429])


class ClaudeError(RuntimeError):
    """The API refused the request, or the network would not carry it."""


def _detail(error):
    """The human-readable half of an API error, if it sent one."""
    try:
        body = json.loads(error.read().decode("utf-8"))
        message = (body.get("error") or {}).get("message")
        if message:
            return message
    except Exception:
        pass
    return error.reason or "no detail"


def _retry_after(error):
    try:
        return max(0.0, min(30.0, float(error.headers.get("retry-after"))))
    except (TypeError, ValueError, AttributeError):
        return None


class _Messages:
    def __init__(self, client):
        self._client = client

    def create(self, **payload):
        """POST to /v1/messages.  Returns the decoded reply as a plain dict."""
        return self._client.post("/v1/messages", payload)


class Client:
    """Enough of an Anthropic client for one endpoint.

    The shape matches the SDK's, ``client.messages.create(...)``, so the calling code
    and its tests do not care which is underneath.
    """

    def __init__(self, api_key=None, base_url=None, timeout=TIMEOUT_SECONDS):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        if not self.api_key:
            raise ClaudeError("no ANTHROPIC_API_KEY set")
        # Honouring the base url lets this sit behind a gateway or a proxy without
        # any other change, which is also how it gets tested.
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.messages = _Messages(self)

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "user-agent": "sheeter",
            },
        )
        failure = None
        for attempt in range(ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                    return json.loads(reply.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = _detail(exc)
                if exc.code not in RETRY_CODES and exc.code < 500:
                    raise ClaudeError("HTTP %d: %s" % (exc.code, detail))
                failure = ClaudeError("HTTP %d: %s" % (exc.code, detail))
                pause = _retry_after(exc)
            except urllib.error.URLError as exc:
                failure = ClaudeError("could not reach %s: %s"
                                      % (self.base_url, exc.reason))
                pause = None
            except (ValueError, OSError) as exc:
                failure = ClaudeError("bad reply from %s: %s" % (self.base_url, exc))
                pause = None
            if attempt + 1 < ATTEMPTS:
                time.sleep(pause if pause is not None else 2 ** attempt)
        raise failure
