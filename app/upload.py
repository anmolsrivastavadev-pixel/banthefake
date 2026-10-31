from dataclasses import dataclass
from typing import BinaryIO

from python_multipart.multipart import MultipartParser, parse_options_header
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

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
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
    parser.finalize()
    if not found:
        raise UploadError(400, f"No file in field '{field}'.")
    dest.flush()
    return info
