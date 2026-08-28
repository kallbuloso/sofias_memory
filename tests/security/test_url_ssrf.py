"""SSRF regression coverage for URL ingestion (GATE-B5 SS8, carrying forward
the SM-516 finding that ``sofias_memory/loaders/url.py``'s guards had no
dedicated regression suite).

Exercises the production guard functions in ``sofias_memory.loaders.url``
directly, with a deterministic resolver/transport test double standing in
for real DNS/network I/O -- no request ever reaches a real host. Also
proves the worker step (``PrepareAndIngestStep``) that is the only caller
of ``fetch_https_url`` in B5 turns an SSRF rejection into a safe, generic
pipeline failure that leaks neither the blocked URL nor internal detail.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import PipelineType
from sofias_memory.loaders.url import (
    fetch_https_url,
    normalize_and_validate_https_url,
    validate_url_destination,
)
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.errors import PermanentPipelineStepError, RetryablePipelineStepError
from sofias_memory.pipelines.steps.remember import (
    REMEMBER_RESOURCES_RESOURCE,
    PrepareAndIngestStep,
    RememberPipelineResources,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
MAX_BYTES = 1_000_000

Resolver = Callable[[str, int], list[ipaddress.IPv4Address | ipaddress.IPv6Address]]


def fixed_resolver(mapping: dict[str, str]) -> Resolver:
    def resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if host not in mapping:
            raise OSError(f"no fixture entry for host {host!r}")
        return [ipaddress.ip_address(mapping[host])]

    return resolve


def counting_resolver(mapping: dict[str, str], calls: list[str]) -> Resolver:
    inner = fixed_resolver(mapping)

    def resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        calls.append(host)
        return inner(host, port)

    return resolve


def json_transport(body: bytes = b'{"ok": true}') -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=body,
        )

    return httpx.MockTransport(handler)


def redirect_then_json_transport(redirect_to: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == redirect_to:
            return httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"{}")
        return httpx.Response(302, headers={"Location": redirect_to})

    return httpx.MockTransport(handler)


# 1. HTTPS public URL syntactically valid.


def test_public_https_url_is_syntactically_valid() -> None:
    assert normalize_and_validate_https_url("https://example.com/doc") == (
        "https://example.com/doc"
    )


@pytest.mark.asyncio
async def test_public_https_url_fetch_succeeds_end_to_end() -> None:
    resolver = fixed_resolver({"example.com": "93.184.216.34"})
    fetched = await fetch_https_url(
        "https://example.com/doc",
        max_bytes=MAX_BYTES,
        transport=json_transport(),
        resolver=resolver,
    )
    assert fetched.final_url == "https://example.com/doc"
    assert fetched.body == b'{"ok": true}'


# 2. Literal loopback rejected (both IPv4 literal-in-URL and hostname "localhost").


@pytest.mark.asyncio
async def test_literal_loopback_ip_in_url_rejected() -> None:
    resolver = fixed_resolver({"127.0.0.1": "127.0.0.1"})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://127.0.0.1/admin",
            max_bytes=MAX_BYTES,
            transport=json_transport(),
            resolver=resolver,
        )


def test_localhost_hostname_rejected_at_syntax_check() -> None:
    with pytest.raises(SofiasMemoryError):
        normalize_and_validate_https_url("https://localhost/admin")


def test_localhost_subdomain_rejected_at_syntax_check() -> None:
    with pytest.raises(SofiasMemoryError):
        normalize_and_validate_https_url("https://foo.localhost/admin")


# 3. localhost/private hostname resolution rejected (hostname resolves to a
#    private address rather than being named "localhost").


@pytest.mark.asyncio
async def test_private_hostname_resolution_rejected() -> None:
    resolver = fixed_resolver({"internal.example.test": "10.1.2.3"})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://internal.example.test/",
            max_bytes=MAX_BYTES,
            transport=json_transport(),
            resolver=resolver,
        )


# 4. RFC1918 address rejected.


@pytest.mark.parametrize("rfc1918_ip", ["10.0.0.5", "172.16.0.5", "192.168.1.5"])
@pytest.mark.asyncio
async def test_rfc1918_address_rejected(rfc1918_ip: str) -> None:
    resolver = fixed_resolver({"private.example.test": rfc1918_ip})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://private.example.test/",
            max_bytes=MAX_BYTES,
            transport=json_transport(),
            resolver=resolver,
        )


# 5. Link-local rejected, including the cloud metadata endpoints.


@pytest.mark.parametrize(
    "link_local_ip", ["169.254.1.1", "169.254.169.254", "169.254.170.2", "100.100.100.200"]
)
@pytest.mark.asyncio
async def test_link_local_and_metadata_endpoint_rejected(link_local_ip: str) -> None:
    resolver = fixed_resolver({"metadata.example.test": link_local_ip})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://metadata.example.test/latest/meta-data/",
            max_bytes=MAX_BYTES,
            transport=json_transport(),
            resolver=resolver,
        )


# 6. IPv6 loopback/private/link-local rejected.


@pytest.mark.parametrize("ipv6_addr", ["::1", "fc00::1", "fe80::1", "fd00::1"])
@pytest.mark.asyncio
async def test_ipv6_private_addresses_rejected(ipv6_addr: str) -> None:
    resolver = fixed_resolver({"v6.example.test": ipv6_addr})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://v6.example.test/",
            max_bytes=MAX_BYTES,
            transport=json_transport(),
            resolver=resolver,
        )


@pytest.mark.asyncio
async def test_ipv6_public_address_accepted() -> None:
    # 2001:4860:4860::8888 is a public, globally routable address (Google DNS).
    resolver = fixed_resolver({"v6.example.test": "2001:4860:4860::8888"})
    fetched = await fetch_https_url(
        "https://v6.example.test/",
        max_bytes=MAX_BYTES,
        transport=json_transport(),
        resolver=resolver,
    )
    assert fetched.body == b'{"ok": true}'


# 7 & 8. Redirect from public to private rejected, and each redirect hop is
#        independently re-resolved (not just the first URL).


@pytest.mark.asyncio
async def test_redirect_from_public_to_private_rejected() -> None:
    calls: list[str] = []
    resolver = counting_resolver(
        {"public.example.test": "93.184.216.34", "internal.example.test": "10.0.0.9"}, calls
    )
    transport = redirect_then_json_transport("https://internal.example.test/secret")
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://public.example.test/",
            max_bytes=MAX_BYTES,
            transport=transport,
            resolver=resolver,
        )
    # Both hops were independently resolved -- the redirect target was not
    # fetched on the strength of the first hop's public resolution alone.
    assert calls == ["public.example.test", "internal.example.test"]


@pytest.mark.asyncio
async def test_redirect_target_is_independently_re_resolved_when_public() -> None:
    calls: list[str] = []
    resolver = counting_resolver(
        {"hop1.example.test": "93.184.216.1", "hop2.example.test": "93.184.216.2"}, calls
    )
    transport = redirect_then_json_transport("https://hop2.example.test/final")
    fetched = await fetch_https_url(
        "https://hop1.example.test/",
        max_bytes=MAX_BYTES,
        transport=transport,
        resolver=resolver,
    )
    assert fetched.final_url == "https://hop2.example.test/final"
    assert calls == ["hop1.example.test", "hop2.example.test"]


# 9. DNS response containing a private IP is rejected (covered structurally
#    by the resolver-driven tests above: validate_url_destination rejects
#    on every resolved address, regardless of how many a lookup returns).


@pytest.mark.asyncio
async def test_any_private_address_in_multi_answer_dns_response_rejected() -> None:
    def resolver(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        # One public, one private answer -- the private one must still block.
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("10.0.0.1")]

    with pytest.raises(SofiasMemoryError):
        await validate_url_destination("https://multi.example.test/", resolver)


# 10. URL credentials/userinfo rejected.


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com/",
        "https://user:pass@example.com/",
        "https://:pass@example.com/",
    ],
)
def test_url_userinfo_rejected(url: str) -> None:
    with pytest.raises(SofiasMemoryError):
        normalize_and_validate_https_url(url)


# 11. Unsupported scheme rejected.


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
    ],
)
def test_unsupported_scheme_rejected(url: str) -> None:
    with pytest.raises(SofiasMemoryError):
        normalize_and_validate_https_url(url)


# 12. Response size limit preserved -- both the declared Content-Length and
#     an adversarial stream that lies about its size.


@pytest.mark.asyncio
async def test_content_length_over_limit_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Length": str(MAX_BYTES * 2)},
            content=b"{}",
        )

    resolver = fixed_resolver({"big.example.test": "93.184.216.34"})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://big.example.test/",
            max_bytes=MAX_BYTES,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        )


@pytest.mark.asyncio
async def test_streamed_body_over_limit_rejected_even_without_content_length() -> None:
    oversized_body = b"x" * (MAX_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=oversized_body,
        )

    resolver = fixed_resolver({"lying.example.test": "93.184.216.34"})
    with pytest.raises(SofiasMemoryError):
        await fetch_https_url(
            "https://lying.example.test/",
            max_bytes=MAX_BYTES,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        )


# Worker-level proof: PrepareAndIngestStep is the sole B5 caller of
# fetch_https_url, and an SSRF rejection there must surface as a generic,
# safe PermanentPipelineStepError -- no rejected URL or internal SSRF
# reasoning in the message, and no Source/Document is ever created.


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


def make_url_context(
    *, tmp_path: Path, url: str, transport: httpx.AsyncBaseTransport, resolver: Resolver
) -> PipelineContext:
    resources = RememberPipelineResources(
        settings=make_settings(tmp_path),
        cognify_service=None,  # type: ignore[arg-type]
        url_transport=transport,
        url_resolver=resolver,
    )
    return PipelineContext(
        run_id=uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=uuid4(),
        source_id=None,
        run_input={"source_kind": "url", "url": url},
        step_outputs={},
        session_factory=None,  # type: ignore[arg-type] - unused by execute()
        resources={REMEMBER_RESOURCES_RESOURCE: resources},
    )


@pytest.mark.asyncio
async def test_worker_step_rejects_ssrf_target_with_safe_generic_error(
    tmp_path: Path,
) -> None:
    resolver = fixed_resolver({"internal.example.test": "10.0.0.9"})
    context = make_url_context(
        tmp_path=tmp_path,
        url="https://internal.example.test/secret-path",
        transport=json_transport(),
        resolver=resolver,
    )

    with pytest.raises(PermanentPipelineStepError) as exc_info:
        await PrepareAndIngestStep().execute(context)

    message = str(exc_info.value)
    assert "internal.example.test" not in message
    assert "secret-path" not in message
    assert "10.0.0.9" not in message
    # No ingress artifact was ever durably written for the blocked target.
    assert not (tmp_path / "runs" / str(context.run_id)).exists()


@pytest.mark.asyncio
async def test_worker_step_dependency_failure_is_retryable_not_permanent(
    tmp_path: Path,
) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    resolver = fixed_resolver({"unreachable.example.test": "93.184.216.34"})
    context = make_url_context(
        tmp_path=tmp_path,
        url="https://unreachable.example.test/",
        transport=httpx.MockTransport(failing_handler),
        resolver=resolver,
    )

    with pytest.raises(RetryablePipelineStepError):
        await PrepareAndIngestStep().execute(context)
