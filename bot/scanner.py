from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

DISCORD_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
DISCORD_ATTACHMENT_PATHS = ("/attachments/", "/ephemeral-attachments/")


class ScanFailure(Exception):
    def __init__(self, title: str, detail: str, *, status: str) -> None:
        super().__init__(detail)
        self.title = title
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class MediaSource:
    url: str
    content_type: str | None = None
    declared_size: int | None = None


def is_discord_attachment_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in DISCORD_CDN_HOSTS
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith(DISCORD_ATTACHMENT_PATHS)
    )


HTTP_ERRORS: dict[int, tuple[str, str]] = {
    413: ("File too large", "The file was sent to BanTheFake, but it is over that server's upload limit."),
    415: ("Unsupported file", "The file was sent to BanTheFake, but the server does not support this media type."),
    429: ("Scanner rate limit reached", "The file was sent to BanTheFake. Please wait a minute before trying again."),
    503: ("Scanner busy", "The file was sent to BanTheFake, but the scanner is busy. Please try again shortly."),
    504: ("Scan timed out", "The file was sent to BanTheFake, but analysis took too long. Please try again."),
}


def error_for_status(status_code: int) -> ScanFailure:
    title, detail = HTTP_ERRORS.get(
        status_code,
        ("Scan failed", "The file was sent to BanTheFake, but the server could not complete the scan."),
    )
    return ScanFailure(title, detail, status=f"server_{status_code}")


class Scanner:
    def __init__(
        self,
        server_url: str,
        max_bytes: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.server_url = server_url
        self.max_bytes = max_bytes
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def download(self, source: MediaSource) -> bytes:
        if not is_discord_attachment_url(source.url):
            raise ScanFailure(
                "Unsupported attachment address",
                "For safety, the bot only reads Discord CDN attachments. Nothing was sent to BanTheFake.",
                status="blocked_url",
            )
        if source.declared_size is not None and source.declared_size > self.max_bytes:
            raise self._too_large()

        try:
            async with self.client.stream("GET", source.url) as response:
                if response.status_code != 200:
                    raise ScanFailure(
                        "Could not read the attachment",
                        "Discord did not provide the attachment. Nothing was sent to BanTheFake.",
                        status="discord_download_failed",
                    )
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > self.max_bytes:
                            raise self._too_large()
                    except ValueError:
                        pass

                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > self.max_bytes:
                        raise self._too_large()
                    data.extend(chunk)
        except ScanFailure:
            raise
        except httpx.HTTPError as exc:
            raise ScanFailure(
                "Could not read the attachment",
                "The bot could not download the attachment from Discord. Nothing was sent to BanTheFake.",
                status="discord_network_error",
            ) from exc

        if not data:
            raise ScanFailure(
                "Empty attachment",
                "The attachment contained no data. Nothing was sent to BanTheFake.",
                status="empty",
            )
        return bytes(data)

    async def scan(self, source: MediaSource) -> dict[str, Any]:
        data = await self.download(source)
        content_type = source.content_type or "application/octet-stream"
        try:
            response = await self.client.post(
                f"{self.server_url}/api/scan",
                files={"file": ("discord-upload", data, content_type)},
            )
        except httpx.HTTPError as exc:
            raise ScanFailure(
                "Could not reach the scanner",
                "The connection to BanTheFake failed. The file may have been sent; no result was received.",
                status="server_network_error",
            ) from exc

        if response.status_code != 200:
            raise error_for_status(response.status_code)
        try:
            report = response.json()
        except ValueError as exc:
            raise ScanFailure(
                "Unexpected scanner response",
                "The file was sent, but BanTheFake did not return a valid scan report.",
                status="bad_response",
            ) from exc
        if not isinstance(report, dict) or not isinstance(report.get("verdict"), str) or not isinstance(
            report.get("verdict_title"), str
        ):
            raise ScanFailure(
                "Unexpected scanner response",
                "The file was sent, but BanTheFake did not return a valid scan report.",
                status="bad_response",
            )
        return report

    def _too_large(self) -> ScanFailure:
        megabytes = self.max_bytes / (1024 * 1024)
        return ScanFailure(
            "File too large",
            f"This file is over the bot's {megabytes:g} MB limit, so nothing was sent to BanTheFake.",
            status="local_too_large",
        )
