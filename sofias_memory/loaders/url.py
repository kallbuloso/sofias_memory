"""Safe single-URL downloader for remember URL ingest."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import unquote, urldefrag, urljoin, urlsplit, urlunsplit

import httpx

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.loaders.text import (
    CSV_FILE_MIME_TYPE,
    DOCX_FILE_MIME_TYPE,
    HTML_FILE_MIME_TYPE,
    JSON_FILE_MIME_TYPE,
    MARKDOWN_FILE_MIME_TYPE,
    PDF_FILE_MIME_TYPE,
    TEXT_FILE_MIME_TYPE,
)
from sofias_memory.schemas.common import ErrorCode

URL_FETCH_TIMEOUT_SECONDS = 20.0
URL_MAX_REDIRECTS = 5
URL_USER_AGENT = "Sofias-Memory/0.1"
URL_FETCH_CHUNK_SIZE_BYTES = 1024 * 1024

SUPPORTED_URL_MEDIA_TYPES = {
    TEXT_FILE_MIME_TYPE: ".txt",
    MARKDOWN_FILE_MIME_TYPE: ".md",
    JSON_FILE_MIME_TYPE: ".json",
    CSV_FILE_MIME_TYPE: ".csv",
    HTML_FILE_MIME_TYPE: ".html",
    "application/xhtml+xml": ".html",
    PDF_FILE_MIME_TYPE: ".pdf",
    DOCX_FILE_MIME_TYPE: ".docx",
}
URL_ACCEPT_HEADER = ", ".join(SUPPORTED_URL_MEDIA_TYPES)
METADATA_ENDPOINT_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("100.100.100.200"),
    }
)

type HostResolver = Callable[[str, int], list[ipaddress.IPv4Address | ipaddress.IPv6Address]]


@dataclass(frozen=True)
class FetchedUrlContent:
    """Fetched remote bytes mapped to an existing file loader extension."""

    requested_url: str
    final_url: str
    filename: str
    media_type: str
    body: bytes


async def fetch_https_url(
    url: str,
    *,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: HostResolver | None = None,
) -> FetchedUrlContent:
    """Fetch one HTTPS URL with bounded redirects and SSRF checks."""

    requested_url = normalize_and_validate_https_url(url)
    current_url = requested_url
    seen_urls = {current_url}
    resolver = resolver or resolve_host_ips
    headers = {
        "User-Agent": URL_USER_AGENT,
        "Accept": URL_ACCEPT_HEADER,
    }
    timeout = httpx.Timeout(URL_FETCH_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
        transport=transport,
        headers=headers,
    ) as client:
        for _ in range(URL_MAX_REDIRECTS + 1):
            await validate_url_destination(current_url, resolver)
            try:
                async with client.stream("GET", current_url) as response:
                    if is_redirect_response(response):
                        location = response.headers.get("Location")
                        if not location:
                            raise DependencyUnavailableError("Remote URL is unavailable.")
                        current_url = normalize_and_validate_https_url(
                            urljoin(current_url, location)
                        )
                        if current_url in seen_urls:
                            raise invalid_url_error("URL redirect loop detected.")
                        seen_urls.add(current_url)
                        continue

                    if response.status_code >= HTTPStatus.BAD_REQUEST:
                        raise DependencyUnavailableError("Remote URL is unavailable.")

                    media_type = response_media_type(response)
                    extension = extension_for_media_type(media_type)
                    validate_content_length(response, max_bytes=max_bytes)
                    body = await read_bounded_response_body(response, max_bytes=max_bytes)
                    return FetchedUrlContent(
                        requested_url=requested_url,
                        final_url=str(response.url),
                        filename=logical_url_filename(str(response.url), extension),
                        media_type=media_type,
                        body=body,
                    )
            except SofiasMemoryError:
                raise
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                raise DependencyUnavailableError("Remote URL is unavailable.") from exc

    raise invalid_url_error("URL redirect limit exceeded.")


def normalize_and_validate_https_url(url: str) -> str:
    candidate = urldefrag(url.strip())[0]
    parsed = urlsplit(candidate)
    if parsed.scheme != "https":
        raise invalid_url_error("Only HTTPS URLs are supported.")
    if not parsed.hostname:
        raise invalid_url_error("URL must include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise invalid_url_error("URL credentials are not supported.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise invalid_url_error("URL port is invalid.") from exc
    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost."} or hostname.endswith(".localhost"):
        raise invalid_url_error("URL hostname is not allowed.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


async def validate_url_destination(url: str, resolver: HostResolver) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise invalid_url_error("URL must include a hostname.")
    port = parsed.port or 443
    try:
        ips = await asyncio.to_thread(resolver, host, port)
    except OSError as exc:
        raise DependencyUnavailableError("Remote URL is unavailable.") from exc
    if not ips:
        raise DependencyUnavailableError("Remote URL is unavailable.")
    for ip in ips:
        validate_public_ip(ip)


def resolve_host_ips(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(sockaddr[0]) for *_, sockaddr in addresses]


def validate_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if ip in METADATA_ENDPOINT_IPS or not ip.is_global:
        raise invalid_url_error("URL resolves to a blocked network.")
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    ):
        raise invalid_url_error("URL resolves to a blocked network.")


def response_media_type(response: httpx.Response) -> str:
    content_type = response.headers.get("Content-Type")
    if not isinstance(content_type, str) or not content_type:
        raise invalid_url_error("Remote URL content type is not supported.")
    return content_type.split(";", 1)[0].strip().lower()


def extension_for_media_type(media_type: str) -> str:
    extension = SUPPORTED_URL_MEDIA_TYPES.get(media_type)
    if extension is None:
        raise invalid_url_error("Remote URL content type is not supported.")
    return extension


def validate_content_length(response: httpx.Response, *, max_bytes: int) -> None:
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        return
    try:
        declared_size = int(content_length)
    except ValueError:
        return
    if declared_size > max_bytes:
        raise request_too_large_error()


async def read_bounded_response_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    async for chunk in response.aiter_bytes(chunk_size=URL_FETCH_CHUNK_SIZE_BYTES):
        total_size += len(chunk)
        if total_size > max_bytes:
            raise request_too_large_error()
        chunks.append(chunk)
    return b"".join(chunks)


def is_redirect_response(response: httpx.Response) -> bool:
    return response.status_code in {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
        HTTPStatus.MULTIPLE_CHOICES,
    }


def logical_url_filename(final_url: str, extension: str) -> str:
    parsed = urlsplit(final_url)
    path_name = unquote(parsed.path.rsplit("/", 1)[-1]).strip()
    if path_name and path_name not in {".", ".."}:
        stem = path_name.rsplit(".", 1)[0] or "download"
    else:
        stem = parsed.hostname or "download"
    return f"{stem}{extension}"


def invalid_url_error(message: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.BAD_REQUEST,
        message=message,
    )


def request_too_large_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.REQUEST_TOO_LARGE,
        status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        message="Remote URL body exceeds the configured source size limit.",
    )
