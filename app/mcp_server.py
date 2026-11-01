"""Dependency-free MCP stdio server for the BanTheFake media scanner.

The official MCP SDK is an optional dependency.  Keeping this small transport in
the core package lets the scanner run in offline and minimal installations too.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, TextIO

from app import scan
from app.config import Settings
from app.sniff import sniff

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {PROTOCOL_VERSION, "2025-03-26", "2024-11-05"}
SERVER_NAME = "banthefake"
SERVER_VERSION = "0.1.0"
NO_PROOF = "No tool can prove a file is real."
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
SUPPORTED_TYPES = "JPG, PNG, WEBP, GIF, HEIC/HEIF, MP4, MOV or WEBM"


class ToolError(Exception):
    """A safe, user-facing tool error."""


HELP_RESOURCES: tuple[dict[str, str], ...] = (
    {
        "name": "Report Remove",
        "url": "https://www.childline.org.uk/info-advice/bullying-abuse-safety/online-mobile-safety/report-remove/",
        "countries": "UK",
        "description": "Under 18s in the UK can confidentially report a nude image or video to get help removing it online.",
    },
    {
        "name": "Childline",
        "url": "https://www.childline.org.uk/get-support/",
        "countries": "UK",
        "description": "Free, confidential support for anyone under 19 in the UK; call 0800 1111 or use online chat.",
    },
    {
        "name": "CEOP Safety Centre",
        "url": "https://www.ceop.police.uk/Safety-Centre/",
        "countries": "UK",
        "description": "Report online sexual abuse or grooming of a child to the UK's Child Exploitation and Online Protection command.",
    },
    {
        "name": "Revenge Porn Helpline",
        "url": "https://revengepornhelpline.org.uk/",
        "countries": "UK",
        "description": "UK advice and support for adults affected by intimate-image abuse, including synthetic intimate images.",
    },
    {
        "name": "StopNCII.org",
        "url": "https://stopncii.org/",
        "countries": "GLOBAL",
        "description": "Adults can create an on-device fingerprint of intimate images so participating platforms can block them; the image is not uploaded.",
    },
    {
        "name": "Take It Down (NCMEC)",
        "url": "https://takeitdown.ncmec.org/",
        "countries": "GLOBAL",
        "description": "Free and anonymous help limiting the spread of nude, partially nude or sexually explicit images taken when the person was under 18, including synthetic images.",
    },
    {
        "name": "FBI Internet Crime Complaint Center (IC3)",
        "url": "https://www.ic3.gov/",
        "countries": "US",
        "description": "Report US internet crime involving fake media, including voice-clone scams, extortion and fraud.",
    },
    {
        "name": "CCRI Crisis Helpline",
        "url": "https://cybercivilrights.org/ccri-crisis-helpline/",
        "countries": "US",
        "description": "Free, confidential support in the US for victims of image-based sexual abuse.",
    },
)


REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A BanTheFake scan report.",
    "required": ["verdict", "verdict_title", "verdict_detail", "signals", "c2pa", "model", "file"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["verified_capture", "verified_ai", "ai", "edited", "camera", "unknown"],
        },
        "verdict_title": {"type": "string"},
        "verdict_detail": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "object"}},
        "c2pa": {"type": ["object", "null"]},
        "model": {"type": ["object", "null"]},
        "file": {"type": "object"},
    },
    "additionalProperties": True,
}


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "scan_media",
        "title": "Scan local media",
        "description": (
            "Scan one local image or video with BanTheFake's metadata, Content Credentials and optional "
            "ML pipeline. The first call can be slow while detector models load. The path must be local; "
            "this server never fetches URLs. Results are evidence, not proof that a depicted scene is real."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to a local image or video file."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"report": REPORT_SCHEMA, "summary": {"type": "string"}},
            "required": ["report", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "explain_verdict",
        "title": "Explain a scan verdict",
        "description": "Explain an existing BanTheFake report in cautious, plain English.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "report_json": {
                    "description": "The report as a JSON object or a JSON-encoded string.",
                    "oneOf": [{"type": "object"}, {"type": "string"}],
                }
            },
            "required": ["report_json"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"explanation": {"type": "string"}},
            "required": ["explanation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_help_resources",
        "title": "Get help and reporting resources",
        "description": "Return static reporting, removal and support resources. This tool makes no network requests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "country": {
                    "type": "string",
                    "default": "UK",
                    "description": "Country name or code; UK is the default. Global services are always included.",
                }
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "country": {"type": "string"},
                "resources": {"type": "array", "items": {"type": "object"}},
                "note": {"type": "string"},
            },
            "required": ["country", "resources", "note"],
            "additionalProperties": False,
        },
    },
)


def _allowed_directories() -> tuple[Path, ...] | None:
    raw = os.environ.get("BTF_MCP_ALLOWED_DIRS")
    if raw is None or not raw.strip():
        return None
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        if not item.strip():
            continue
        try:
            root = Path(item).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolError("BTF_MCP_ALLOWED_DIRS contains a path that does not exist or cannot be resolved.") from exc
        if not root.is_dir():
            raise ToolError("Every BTF_MCP_ALLOWED_DIRS entry must be a directory.")
        roots.append(root)
    if not roots:
        raise ToolError("BTF_MCP_ALLOWED_DIRS does not contain a directory.")
    return tuple(roots)


def _validated_media_path(raw_path: str) -> tuple[Path, str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolError("path must be a non-empty string.")
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ToolError("The file does not exist.") from exc
    except (OSError, RuntimeError) as exc:
        raise ToolError("The path could not be resolved safely.") from exc

    allowed = _allowed_directories()
    if allowed is not None and not any(resolved.is_relative_to(root) for root in allowed):
        raise ToolError("The resolved file is outside BTF_MCP_ALLOWED_DIRS.")

    try:
        file_stat = resolved.stat()
    except OSError as exc:
        raise ToolError("The file cannot be inspected.") from exc
    if stat.S_ISDIR(file_stat.st_mode):
        raise ToolError("Directories cannot be scanned; pass an image or video file.")
    if not stat.S_ISREG(file_stat.st_mode):
        raise ToolError("Only regular files can be scanned.")
    if file_stat.st_size == 0:
        raise ToolError("The file is empty.")

    try:
        settings = Settings.from_env()
    except (TypeError, ValueError) as exc:
        raise ToolError("The BanTheFake size or concurrency configuration is invalid.") from exc
    if file_stat.st_size > settings.max_upload_bytes:
        raise ToolError(f"The file is too large. The limit is {settings.max_upload_mb} MB.")

    try:
        with resolved.open("rb") as media_file:
            head = media_file.read(64)
    except OSError as exc:
        raise ToolError("The file cannot be read.") from exc
    detected = sniff(head)
    if detected is None:
        raise ToolError(f"Unsupported file type. Use {SUPPORTED_TYPES}.")
    return resolved, detected[1]


def _scan_summary(report: dict[str, Any]) -> str:
    verdict = report.get("verdict")
    title = str(report.get("verdict_title") or "Scan complete")
    if verdict == "verified_capture":
        meaning = (
            "Trusted Content Credentials establish the file's signed capture origin and signed history. "
            "That proves origin/provenance; it does not prove the depicted scene was real."
        )
    elif verdict == "verified_ai":
        meaning = "Trusted Content Credentials declare generative-AI involvement in this file or one of its ingredients."
    elif verdict == "ai":
        meaning = "The scan found AI-generation evidence, but metadata can be forged and detectors can be wrong."
    elif verdict == "edited":
        meaning = "The scan found evidence of editing, which does not by itself show that the edit was deceptive or AI-made."
    elif verdict == "camera":
        meaning = "Camera metadata was found, but it can be copied, changed or forged."
    else:
        meaning = "The scan found no conclusive evidence either way; missing markers and low detector scores do not mean real."
    return f"{title}: {meaning} {NO_PROOF}"


def scan_media(path: str) -> dict[str, Any]:
    resolved, mime = _validated_media_path(path)
    settings = Settings.from_env()
    scan.configure_isolation(settings.scan_concurrency)
    try:
        report = scan.scan_file(resolved, resolved.name[:255], mime)
    except scan.UnsupportedMedia as exc:
        raise ToolError(str(exc)) from exc
    except scan.ScanTimeout as exc:
        raise ToolError("The scan exceeded its configured time limit.") from exc
    except scan.ScanFailed as exc:
        raise ToolError("The isolated media parser failed safely; the file was not scanned.") from exc
    summary = _scan_summary(report)
    return {"report": report, "summary": summary}


def _coerce_report(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ToolError("report_json is too large.")
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise ToolError("report_json is not valid JSON.") from exc
    if isinstance(value, dict) and "report" in value and isinstance(value["report"], dict):
        value = value["report"]
    if not isinstance(value, dict):
        raise ToolError("report_json must be a BanTheFake report object or a JSON-encoded report.")
    verdicts = {"verified_capture", "verified_ai", "ai", "edited", "camera", "unknown"}
    if value.get("verdict") not in verdicts:
        raise ToolError("report_json does not contain a recognized BanTheFake verdict.")
    return value


def explain_verdict(report_json: Any) -> dict[str, str]:
    report = _coerce_report(report_json)
    parts = [_scan_summary(report)]
    detail = report.get("verdict_detail")
    if isinstance(detail, str) and detail.strip():
        parts.append(detail.strip())
    signals = report.get("signals")
    evidence: list[str] = []
    if isinstance(signals, list):
        for signal in signals[:4]:
            if isinstance(signal, dict) and isinstance(signal.get("text"), str):
                evidence.append(signal["text"].strip())
    if evidence:
        parts.append("Main reported signals: " + " ".join(evidence))
    explanation = " ".join(parts)
    if NO_PROOF not in explanation:
        explanation += f" {NO_PROOF}"
    return {"explanation": explanation}


def _country_code(country: str) -> str:
    normalized = country.strip().casefold()
    if normalized in {"uk", "gb", "gbr", "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland"}:
        return "UK"
    if normalized in {"us", "usa", "united states", "united states of america"}:
        return "US"
    return country.strip().upper() or "UK"


def get_help_resources(country: str = "UK") -> dict[str, Any]:
    if not isinstance(country, str):
        raise ToolError("country must be a string.")
    code = _country_code(country)
    resources = [dict(item) for item in HELP_RESOURCES if item["countries"] in {"GLOBAL", code}]
    note = (
        "If someone is in immediate danger, contact local emergency services. Save evidence such as links, "
        "usernames, dates and screenshots before requesting removal. These links are static; availability and "
        "eligibility can change."
    )
    return {"country": code, "resources": resources, "note": note}


def _text_result(payload: dict[str, Any], text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload}


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return _tool_error("Tool arguments must be an object.")
    try:
        if name == "scan_media":
            if set(arguments) != {"path"}:
                raise ToolError("scan_media requires only the path argument.")
            payload = scan_media(arguments.get("path"))
            text = payload["summary"] + "\n\nReport JSON:\n" + json.dumps(
                payload["report"], ensure_ascii=False, indent=2
            )
            return _text_result(payload, text)
        if name == "explain_verdict":
            if set(arguments) != {"report_json"}:
                raise ToolError("explain_verdict requires only the report_json argument.")
            payload = explain_verdict(arguments.get("report_json"))
            return _text_result(payload, payload["explanation"])
        if name == "get_help_resources":
            if not set(arguments) <= {"country"}:
                raise ToolError("get_help_resources accepts only the optional country argument.")
            payload = get_help_resources(arguments.get("country", "UK"))
            lines = [payload["note"]]
            lines.extend(f"- {item['name']}: {item['description']} {item['url']}" for item in payload["resources"])
            return _text_result(payload, "\n".join(lines))
        raise ToolError(f"Unknown tool: {name}")
    except ToolError as exc:
        return _tool_error(str(exc))
    except Exception:
        # Do not leak paths, parser internals or tracebacks over the protocol.
        return _tool_error("The tool failed unexpectedly.")


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_message(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    method = message["method"]
    request_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params", {})
    if is_notification:
        # initialized and cancellation notifications need no response. Calls are handled serially,
        # so cancellation cannot interrupt an active in-process model call.
        return None
    if not isinstance(params, dict):
        return _error(request_id, -32602, "Invalid params")
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return _response(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use scan_media only for local paths the user chose. Detector results can be wrong. "
                    "A verified_capture result proves signed origin/provenance, not that the scene is real. "
                    + NO_PROOF
                ),
            },
        )
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": list(TOOLS)})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(request_id, -32602, "Tool name must be a string")
        return _response(request_id, call_tool(name, params.get("arguments", {})))
    return _error(request_id, -32601, "Method not found")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Serve newline-delimited JSON-RPC until stdin closes."""
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    try:
        while True:
            raw = input_stream.readline(MAX_MESSAGE_BYTES + 1)
            if raw == "":
                break
            if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                response = _error(None, -32700, "Message too large")
            else:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, UnicodeError, RecursionError):
                    response = _error(None, -32700, "Parse error")
                else:
                    response = handle_message(message)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()
    finally:
        scan.shutdown()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
