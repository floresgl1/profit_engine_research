import httpx
import pytest

from profit_engine.venues.http import ReadOnlyHttp, VenueHttpError


class Script:
    """MockTransport handler that replays responses and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script, **kwargs):
    slept = []
    clock = iter(range(0, 10_000, 10))  # each request 10s apart: throttle never waits
    http = ReadOnlyHttp(
        "https://example.test/api",
        transport=httpx.MockTransport(script),
        sleep=slept.append,
        monotonic=lambda: next(clock),
        **kwargs,
    )
    return http, slept


def test_get_json_success():
    script = Script(httpx.Response(200, json={"ok": True}))
    http, _ = client(script)
    assert http.get_json("/x", {"a": "1", "b": ["2", "3"]}) == {"ok": True}
    request = script.requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://example.test/api/x?a=1&b=2&b=3"


def test_never_sends_credentials():
    script = Script(httpx.Response(200, json={}))
    http, _ = client(script)
    http.get_json("/x")
    headers = {k.lower() for k in script.requests[0].headers}
    assert not headers & {"authorization", "cookie", "kalshi-access-key", "kalshi-access-signature", "poly_api_key"}


def test_retries_429_with_backoff():
    script = Script(httpx.Response(429), httpx.Response(503), httpx.Response(200, json=[1]))
    http, slept = client(script)
    assert http.get_json("/x") == [1]
    assert slept == [2.0, 4.0]


def test_honors_retry_after():
    script = Script(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={}))
    http, slept = client(script)
    http.get_json("/x")
    assert slept == [7.0]


def test_gives_up_after_max_retries():
    script = Script(*[httpx.Response(429)] * 3)
    http, _ = client(script, max_retries=2)
    with pytest.raises(VenueHttpError):
        http.get_json("/x")


def test_client_error_not_retried():
    script = Script(httpx.Response(404, text="nope"))
    http, slept = client(script)
    with pytest.raises(VenueHttpError, match="404"):
        http.get_json("/x")
    assert slept == []


def test_transport_error_retried():
    script = Script(httpx.ConnectError("down"), httpx.Response(200, json={}))
    http, slept = client(script)
    http.get_json("/x")
    assert slept == [2.0]


def test_throttle_spaces_requests():
    script = Script(httpx.Response(200, json={}), httpx.Response(200, json={}))
    slept = []
    times = iter([0.0, 0.03])
    http = ReadOnlyHttp(
        "https://example.test",
        transport=httpx.MockTransport(script),
        min_interval=0.1,
        sleep=slept.append,
        monotonic=lambda: next(times),
    )
    http.get_json("/a")
    http.get_json("/b")
    assert slept == [pytest.approx(0.07)]


def test_public_surface_is_get_only():
    public = {name for name in dir(ReadOnlyHttp) if not name.startswith("_")}
    assert public == {"get_json", "close"}
