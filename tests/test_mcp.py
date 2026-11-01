import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from app import scan
from app.mcp_server import NO_PROOF, TOOLS, call_tool, serve
from tests.conftest import FIXTURES


@pytest.fixture(autouse=True)
def _close_parser_pool():
    yield
    scan.shutdown()


def _png(path: Path) -> Path:
    Image.new("RGB", (8, 8), "red").save(path, "PNG")
    return path


def _request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def test_protocol_handshake_ping_and_tools_list_over_stdio():
    incoming = io.StringIO(
        "\n".join(
            [
                _request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                _request(2, "ping"),
                _request(3, "tools/list"),
                _request(4, "tools/call", {"name": "get_help_resources", "arguments": {"country": "UK"}}),
            ]
        )
        + "\n"
    )
    outgoing = io.StringIO()
    serve(incoming, outgoing)
    messages = [json.loads(line) for line in outgoing.getvalue().splitlines()]

    assert [message["id"] for message in messages] == [1, 2, 3, 4]
    assert messages[0]["result"]["protocolVersion"] == "2024-11-05"
    assert messages[0]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert messages[1]["result"] == {}
    assert {tool["name"] for tool in messages[2]["result"]["tools"]} == {
        "scan_media", "explain_verdict", "get_help_resources"
    }
    assert messages[3]["result"]["structuredContent"]["country"] == "UK"


def test_tool_schemas_are_closed_and_scan_url_is_absent():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert "scan_url" not in by_name
    assert by_name["scan_media"]["inputSchema"] == {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to a local image or video file."}},
        "required": ["path"],
        "additionalProperties": False,
    }
    assert "first call can be slow" in by_name["scan_media"]["description"]
    assert all(tool["inputSchema"].get("additionalProperties") is False for tool in TOOLS)


@pytest.mark.ml
def test_scan_media_ai_fixture_is_likely_ai():
    result = call_tool("scan_media", {"path": str(FIXTURES / "ai_dalle_mars.jpg")})
    assert not result.get("isError"), result
    payload = result["structuredContent"]
    assert payload["report"]["verdict"] == "ai"
    assert payload["report"]["model"]["decision"] == "likely"
    assert NO_PROOF in payload["summary"]


def test_scan_media_verified_pixel_describes_origin_not_scene():
    result = call_tool("scan_media", {"path": str(FIXTURES / "pixel10_verified.jpg")})
    assert not result.get("isError"), result
    payload = result["structuredContent"]
    assert payload["report"]["verdict"] == "verified_capture"
    summary = payload["summary"].lower()
    assert "proves origin" in summary
    assert "does not prove the depicted scene" in summary
    assert NO_PROOF in payload["summary"]


def test_scan_media_text_file_returns_clean_tool_error(tmp_path):
    text_file = tmp_path / "not-an-image.jpg"
    text_file.write_text("hello")
    result = call_tool("scan_media", {"path": str(text_file)})
    assert result["isError"] is True
    assert "Unsupported file type" in result["content"][0]["text"]
    assert "Traceback" not in result["content"][0]["text"]


@pytest.mark.parametrize("kind", ["directory", "nonexistent"])
def test_scan_media_rejects_directory_and_nonexistent(tmp_path, kind):
    path = tmp_path if kind == "directory" else tmp_path / "missing.jpg"
    result = call_tool("scan_media", {"path": str(path)})
    assert result["isError"] is True
    expected = "Directories cannot be scanned" if kind == "directory" else "does not exist"
    assert expected in result["content"][0]["text"]


def test_scan_media_enforces_allowed_directories_after_resolving_symlinks(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    outside_file = _png(outside / "image.png")
    link = allowed / "escape.png"
    link.symlink_to(outside_file)
    monkeypatch.setenv("BTF_MCP_ALLOWED_DIRS", str(allowed))

    for path in (outside_file, link):
        result = call_tool("scan_media", {"path": str(path)})
        assert result["isError"] is True
        assert "outside BTF_MCP_ALLOWED_DIRS" in result["content"][0]["text"]


def test_scan_media_accepts_a_file_inside_allowed_directory(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image = _png(allowed / "image.png")
    monkeypatch.setenv("BTF_MCP_ALLOWED_DIRS", str(allowed))
    result = call_tool("scan_media", {"path": str(image)})
    assert not result.get("isError"), result


def test_scan_media_rejects_oversize_file_before_sniffing(tmp_path, monkeypatch):
    path = tmp_path / "large.jpg"
    path.write_bytes(b"\xff\xd8\xff" + b"\0" * (1024 * 1024 + 1))
    monkeypatch.setenv("BTF_MAX_UPLOAD_MB", "1")
    result = call_tool("scan_media", {"path": str(path)})
    assert result["isError"] is True
    assert "too large" in result["content"][0]["text"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are not available on this platform")
def test_scan_media_rejects_non_regular_files(tmp_path):
    fifo = tmp_path / "media.pipe"
    os.mkfifo(fifo)
    result = call_tool("scan_media", {"path": str(fifo)})
    assert result["isError"] is True
    assert "regular files" in result["content"][0]["text"]


def test_explain_verdict_accepts_json_text_and_keeps_caveat():
    report = {
        "verdict": "verified_capture",
        "verdict_title": "Verified camera capture",
        "verdict_detail": "Trusted signed capture provenance.",
        "signals": [{"kind": "c2pa", "text": "The signature checks out."}],
        "c2pa": {"valid": True, "trusted": True, "capture": True},
        "model": None,
        "file": {"type": "image", "mime": "image/jpeg", "size_bytes": 10},
    }
    result = call_tool("explain_verdict", {"report_json": json.dumps(report)})
    explanation = result["structuredContent"]["explanation"]
    assert "proves origin" in explanation.lower()
    assert "does not prove the depicted scene" in explanation.lower()
    assert NO_PROOF in explanation
    assert "signature checks out" in explanation


def test_help_resources_include_uk_and_global_services_without_network():
    result = call_tool("get_help_resources", {})
    payload = result["structuredContent"]
    names = {resource["name"] for resource in payload["resources"]}
    assert {"Report Remove", "Childline", "CEOP Safety Centre", "StopNCII.org", "Take It Down (NCMEC)"} <= names
    assert all(resource["url"].startswith("https://") for resource in payload["resources"])
