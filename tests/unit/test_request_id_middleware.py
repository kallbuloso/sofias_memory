from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from io import StringIO
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.middleware.request_id import (
    MAX_REQUEST_ID_LENGTH,
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    resolve_request_id,
)
from sofias_memory.observability.logging import (
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)

ASGIMessage = dict[str, object]
ASGIScope = dict[str, object]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]


@pytest.fixture()
def log_stream() -> StringIO:
    stream = StringIO()
    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    clear_log_context()
    configure_logging("INFO", stream=stream)
    yield stream
    clear_log_context()
    httpx_logger.setLevel(previous_httpx_level)


def read_log_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def make_app(
    status: int = 200,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> ASGIApp:
    async def app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        del scope, receive
        get_logger("sofias_memory.tests.request_id").info("downstream_request")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": list(headers or []),
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def make_client(app: ASGIApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=RequestIdMiddleware(app))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def assert_uuid(value: str) -> UUID:
    parsed = UUID(value)
    assert str(parsed) == value
    return parsed


@pytest.mark.asyncio
async def test_request_without_header_generates_uuid() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/")

    assert_uuid(response.headers[REQUEST_ID_HEADER])


def test_resolve_request_id_generates_uuid_v4_when_missing() -> None:
    parsed = UUID(resolve_request_id(None))

    assert parsed.version == 4


@pytest.mark.asyncio
async def test_response_contains_request_id_header() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/")

    assert REQUEST_ID_HEADER in response.headers


@pytest.mark.asyncio
async def test_valid_client_uuid_is_preserved_semantically() -> None:
    request_id = str(uuid4())

    async with make_client(make_app()) as client:
        response = await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    assert response.headers[REQUEST_ID_HEADER] == request_id


@pytest.mark.asyncio
async def test_valid_representation_is_normalized_to_canonical_format() -> None:
    request_id = uuid4()

    async with make_client(make_app()) as client:
        response = await client.get(
            "/", headers={REQUEST_ID_HEADER: f"{{{str(request_id).upper()}}}"}
        )

    assert response.headers[REQUEST_ID_HEADER] == str(request_id)


@pytest.mark.parametrize(
    "header_value", ["not-a-uuid", "", "   ", "x" * (MAX_REQUEST_ID_LENGTH + 1)]
)
def test_invalid_header_values_are_replaced(header_value: str) -> None:
    resolved = resolve_request_id(header_value)

    parsed = UUID(resolved)
    assert parsed.version == 4
    assert resolved != header_value


@pytest.mark.asyncio
async def test_invalid_original_value_is_not_reflected_in_response() -> None:
    invalid_request_id = "invalid-request-id"

    async with make_client(make_app()) as client:
        response = await client.get("/", headers={REQUEST_ID_HEADER: invalid_request_id})

    assert response.headers[REQUEST_ID_HEADER] != invalid_request_id
    assert_uuid(response.headers[REQUEST_ID_HEADER])


@pytest.mark.asyncio
async def test_request_id_is_bound_in_logging_context_during_downstream(
    log_stream: StringIO,
) -> None:
    request_id = str(uuid4())

    async with make_client(make_app()) as client:
        await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    [record] = read_log_records(log_stream)
    assert record["request_id"] == request_id


@pytest.mark.asyncio
async def test_log_inside_request_contains_request_id(log_stream: StringIO) -> None:
    request_id = str(uuid4())

    async with make_client(make_app()) as client:
        await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    [record] = read_log_records(log_stream)
    assert record["event"] == "downstream_request"
    assert record["request_id"] == request_id


@pytest.mark.asyncio
async def test_context_is_cleared_after_response(log_stream: StringIO) -> None:
    request_id = str(uuid4())

    async with make_client(make_app()) as client:
        await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    get_logger("sofias_memory.tests.request_id").info("outside_request")
    records = read_log_records(log_stream)
    assert records[0]["request_id"] == request_id
    assert "request_id" not in records[1]


@pytest.mark.asyncio
async def test_existing_context_is_not_erased_after_response(log_stream: StringIO) -> None:
    request_id = str(uuid4())
    bind_log_context(run_id="existing-run")

    async with make_client(make_app()) as client:
        await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    get_logger("sofias_memory.tests.request_id").info("outside_request")
    records = read_log_records(log_stream)
    assert records[0]["request_id"] == request_id
    assert records[0]["run_id"] == "existing-run"
    assert "request_id" not in records[1]
    assert records[1]["run_id"] == "existing-run"


@pytest.mark.asyncio
async def test_previous_request_id_is_restored_after_response(log_stream: StringIO) -> None:
    request_id = str(uuid4())
    bind_log_context(request_id="outer-request", run_id="existing-run")

    async with make_client(make_app()) as client:
        await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    get_logger("sofias_memory.tests.request_id").info("outside_request")
    records = read_log_records(log_stream)
    assert records[0]["request_id"] == request_id
    assert records[0]["run_id"] == "existing-run"
    assert records[1]["request_id"] == "outer-request"
    assert records[1]["run_id"] == "existing-run"


@pytest.mark.asyncio
async def test_context_is_cleared_after_downstream_exception(log_stream: StringIO) -> None:
    async def failing_app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        get_logger("sofias_memory.tests.request_id").info("before_failure")
        raise RuntimeError("downstream failed")

    request_id = str(uuid4())
    async with make_client(failing_app) as client:
        with pytest.raises(RuntimeError, match="downstream failed"):
            await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    get_logger("sofias_memory.tests.request_id").info("after_failure")
    records = read_log_records(log_stream)
    assert records[0]["request_id"] == request_id
    assert "request_id" not in records[1]


@pytest.mark.asyncio
async def test_previous_request_id_is_restored_after_downstream_exception(
    log_stream: StringIO,
) -> None:
    async def failing_app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        get_logger("sofias_memory.tests.request_id").info("before_failure")
        raise RuntimeError("downstream failed")

    request_id = str(uuid4())
    bind_log_context(request_id="outer-request", run_id="existing-run")
    async with make_client(failing_app) as client:
        with pytest.raises(RuntimeError, match="downstream failed"):
            await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    get_logger("sofias_memory.tests.request_id").info("after_failure")
    records = read_log_records(log_stream)
    assert records[0]["request_id"] == request_id
    assert records[0]["run_id"] == "existing-run"
    assert records[1]["request_id"] == "outer-request"
    assert records[1]["run_id"] == "existing-run"


@pytest.mark.asyncio
async def test_downstream_exception_is_propagated() -> None:
    async def failing_app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        raise RuntimeError("still propagated")

    async with make_client(failing_app) as client:
        with pytest.raises(RuntimeError, match="still propagated"):
            await client.get("/")


@pytest.mark.asyncio
async def test_two_sequential_requests_do_not_share_request_id(log_stream: StringIO) -> None:
    first_request_id = str(uuid4())
    second_request_id = str(uuid4())

    async with make_client(make_app()) as client:
        first = await client.get("/", headers={REQUEST_ID_HEADER: first_request_id})
        second = await client.get("/", headers={REQUEST_ID_HEADER: second_request_id})

    records = read_log_records(log_stream)
    assert first.headers[REQUEST_ID_HEADER] == first_request_id
    assert second.headers[REQUEST_ID_HEADER] == second_request_id
    assert [record["request_id"] for record in records] == [first_request_id, second_request_id]


@pytest.mark.asyncio
async def test_two_concurrent_requests_do_not_share_request_id(log_stream: StringIO) -> None:
    first_request_id = str(uuid4())
    second_request_id = str(uuid4())
    entered = 0
    entered_lock = asyncio.Lock()
    both_entered = asyncio.Event()

    async def app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        nonlocal entered
        del scope, receive
        async with entered_lock:
            entered += 1
            if entered == 2:
                both_entered.set()
        await both_entered.wait()
        get_logger("sofias_memory.tests.request_id").info("concurrent_downstream")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async with make_client(app) as client:
        first, second = await asyncio.gather(
            client.get("/", headers={REQUEST_ID_HEADER: first_request_id}),
            client.get("/", headers={REQUEST_ID_HEADER: second_request_id}),
        )

    records = read_log_records(log_stream)
    assert first.headers[REQUEST_ID_HEADER] == first_request_id
    assert second.headers[REQUEST_ID_HEADER] == second_request_id
    assert {record["request_id"] for record in records} == {first_request_id, second_request_id}


@pytest.mark.asyncio
async def test_two_supplied_concurrent_request_ids_remain_isolated(log_stream: StringIO) -> None:
    first_request_id = str(uuid4())
    second_request_id = str(uuid4())

    async with make_client(make_app()) as client:
        await asyncio.gather(
            client.get("/", headers={REQUEST_ID_HEADER: first_request_id}),
            client.get("/", headers={REQUEST_ID_HEADER: second_request_id}),
        )

    assert {record["request_id"] for record in read_log_records(log_stream)} == {
        first_request_id,
        second_request_id,
    }


@pytest.mark.asyncio
async def test_response_header_matches_context_used_in_log(log_stream: StringIO) -> None:
    downstream_header = str(uuid4())
    request_id = str(uuid4())

    async with make_client(
        make_app(
            headers=[(REQUEST_ID_HEADER.lower().encode("ascii"), downstream_header.encode("ascii"))]
        )
    ) as client:
        response = await client.get("/", headers={REQUEST_ID_HEADER: request_id})

    [record] = read_log_records(log_stream)
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert record["request_id"] == request_id


@pytest.mark.asyncio
async def test_non_http_scope_is_not_altered() -> None:
    observed_scope_type = None

    async def lifespan_app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        nonlocal observed_scope_type
        del receive, send
        observed_scope_type = scope["type"]

    middleware = RequestIdMiddleware(lifespan_app)

    async def receive() -> ASGIMessage:
        return {"type": "lifespan.startup"}

    sent_messages: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        sent_messages.append(message)

    await middleware({"type": "lifespan"}, receive, send)

    assert observed_scope_type == "lifespan"
    assert sent_messages == []
