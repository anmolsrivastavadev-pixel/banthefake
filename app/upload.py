from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, MultipartState, parse_options_header
from starlette.requests import Request

MULTIPART_OVERHEAD = 64 * 1024


class UploadError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class UploadInfo:
    filename: str
    size: int


async def receive_file(request: Request, dest: BinaryIO, field: str, max_bytes: int) -> UploadInfo:
    """Stream the multipart `field` into `dest`, aborting with 413 as soon as it exceeds `max_bytes`."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise UploadError(400, "Expected a multipart/form-data upload.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + MULTIPART_OVERHEAD:
        raise UploadError(413, "File is too large.")

    state = {"header_field": b"", "header_value": b"", "headers": {}, "in_target": False}
    info = UploadInfo(filename="", size=0)
    found = False

    def on_part_begin() -> None:
        state["headers"] = {}
        state["in_target"] = False

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["header_field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["header_value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][state["header_field"].lower()] = state["header_value"]
        state["header_field"] = state["header_value"] = b""

    def on_headers_finished() -> None:
        nonlocal found
        _, disp = parse_options_header(state["headers"].get(b"content-disposition", b""))
        if disp.get(b"name") == field.encode() and b"filename" in disp and not found:
            found = True
            state["in_target"] = True
            info.filename = disp[b"filename"].decode("utf-8", "replace")

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if not state["in_target"]:
            return
        info.size += end - start
        if info.size > max_bytes:
            raise UploadError(413, "File is too large.")
        dest.write(data[start:end])

    def on_part_end() -> None:
        state["in_target"] = False

    try:
        parser = MultipartParser(boundary, {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        })
    except FormParserError as exc:
        raise UploadError(400, "Malformed upload.") from exc
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes + MULTIPART_OVERHEAD:
            raise UploadError(413, "File is too large.")
        try:
            parser.write(chunk)
        except UploadError:
            raise
        except Exception as exc:
            raise UploadError(400, "Malformed upload.") from exc
    try:
        parser.finalize()
    except FormParserError as exc:
        raise UploadError(400, "Malformed upload.") from exc
    if parser.state != MultipartState.END:  # e.g. the closing boundary is missing: the upload is incomplete
        raise UploadError(400, "Malformed upload: the multipart body is incomplete.")
    if not found:
        raise UploadError(400, f"No file in field '{field}'.")
    dest.flush()
    return info


@dataclass
class PartInfo:
    path: Path
    size: int


async def receive_parts(
    request: Request, directory: Path, field: str, max_parts: int, max_part_bytes: int, max_total_bytes: int,
) -> list[PartInfo]:
    """Stream every multipart part named `field` into its own file in `directory`, in order.

    Aborts with 413 as soon as there are more than `max_parts` such parts, one part exceeds `max_part_bytes`, or
    the body exceeds `max_total_bytes` (plus multipart overhead). Other fields are ignored. The caller owns
    `directory` and must delete it, also when this raises.
    """
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise UploadError(400, "Expected a multipart/form-data upload.")
    limit = max_total_bytes + MULTIPART_OVERHEAD
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise UploadError(413, "The frames are too large in total.")

    state: dict = {"header_field": b"", "header_value": b"", "headers": {}, "file": None}
    parts: list[PartInfo] = []
    total = 0

    def close_current() -> None:
        if state["file"] is not None:
            state["file"].close()
            state["file"] = None

    def on_part_begin() -> None:
        state["headers"] = {}

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["header_field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["header_value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][state["header_field"].lower()] = state["header_value"]
        state["header_field"] = state["header_value"] = b""

    def on_headers_finished() -> None:
        _, disp = parse_options_header(state["headers"].get(b"content-disposition", b""))
        if disp.get(b"name") != field.encode():
            return
        if len(parts) >= max_parts:
            raise UploadError(413, f"Too many frames: at most {max_parts} per request.")
        path = directory / f"frame-{len(parts):02d}"
        state["file"] = path.open("xb")
        parts.append(PartInfo(path=path, size=0))

    def on_part_data(data: bytes, start: int, end: int) -> None:
        nonlocal total
        if state["file"] is None:
            return
        part = parts[-1]
        part.size += end - start
        total += end - start
        if part.size > max_part_bytes:
            raise UploadError(413, "A frame is too large.")
        if total > max_total_bytes:
            raise UploadError(413, "The frames are too large in total.")
        state["file"].write(data[start:end])

    def on_part_end() -> None:
        close_current()

    try:
        parser = MultipartParser(boundary, {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        })
    except FormParserError as exc:
        raise UploadError(400, "Malformed upload.") from exc
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise UploadError(413, "The frames are too large in total.")
            try:
                parser.write(chunk)
            except UploadError:
                raise
            except Exception as exc:
                raise UploadError(400, "Malformed upload.") from exc
        try:
            parser.finalize()
        except FormParserError as exc:
            raise UploadError(400, "Malformed upload.") from exc
    finally:
        close_current()
    if parser.state != MultipartState.END:
        raise UploadError(400, "Malformed upload: the multipart body is incomplete.")
    if not parts:
        raise UploadError(400, f"No frames in field '{field}'.")
    if any(p.size == 0 for p in parts):
        raise UploadError(400, "A frame is empty.")
    return parts
