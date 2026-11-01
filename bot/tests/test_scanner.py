import httpx
import pytest

from bot.scanner import MediaSource, ScanFailure, Scanner, error_for_status, is_discord_attachment_url


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.discordapp.com/attachments/1/2/photo.png",
        "https://media.discordapp.net/attachments/1/2/photo.png?width=800",
        "https://cdn.discordapp.com/ephemeral-attachments/1/2/photo.png",
    ],
)
def test_discord_attachment_url_allowed(url):
    assert is_discord_attachment_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/attachments/1/2/photo.png",
        "http://cdn.discordapp.com/attachments/1/2/photo.png",
        "https://cdn.discordapp.com/avatar/1/photo.png",
        "https://cdn.discordapp.com.evil.example/attachments/1/2/photo.png",
        "https://cdn.discordapp.com:444/attachments/1/2/photo.png",
    ],
)
def test_non_attachment_url_blocked(url):
    assert not is_discord_attachment_url(url)


@pytest.mark.asyncio
async def test_size_cap_stops_before_upload_when_stream_is_too_large():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"12345", headers={"content-length": "5"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scanner = Scanner("https://scan.example.org", 4, client=client)
    with pytest.raises(ScanFailure) as caught:
        await scanner.scan(MediaSource("https://cdn.discordapp.com/attachments/1/2/file.png"))
    assert caught.value.status == "local_too_large"
    assert "nothing was sent" in caught.value.detail
    assert [request.method for request in requests] == ["GET"]
    await client.aclose()


@pytest.mark.asyncio
async def test_download_and_multipart_scan_with_mock_transport():
    requests = []
    report = {
        "verdict": "unknown",
        "verdict_title": "Nothing conclusive",
        "verdict_detail": "No strong indicators were found.",
        "signals": [],
    }

    async def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"image bytes", headers={"content-type": "image/png"})
        assert request.url == "https://scan.example.org/api/scan"
        body = await request.aread()
        assert b'name="file"' in body
        assert b"image bytes" in body
        return httpx.Response(200, json=report)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scanner = Scanner("https://scan.example.org", 100, client=client)
    result = await scanner.scan(
        MediaSource("https://cdn.discordapp.com/attachments/1/2/file.png", "image/png", 11)
    )
    assert result == report
    assert [request.method for request in requests] == ["GET", "POST"]
    await client.aclose()


@pytest.mark.parametrize("status", [413, 415, 429, 503, 504])
def test_expected_http_errors_say_file_was_sent(status):
    failure = error_for_status(status)
    assert failure.status == f"server_{status}"
    assert "sent to BanTheFake" in failure.detail


@pytest.mark.asyncio
async def test_arbitrary_url_is_never_requested():
    async def handler(_request):
        pytest.fail("transport should not be called")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scanner = Scanner("https://scan.example.org", 100, client=client)
    with pytest.raises(ScanFailure) as caught:
        await scanner.download(MediaSource("https://example.org/image.png"))
    assert caught.value.status == "blocked_url"
    assert "Nothing was sent" in caught.value.detail
    await client.aclose()
