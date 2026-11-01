# BanTheFake MCP server

BanTheFake includes a local [Model Context Protocol](https://modelcontextprotocol.io/) server so an AI
assistant can scan an image or video that you explicitly name. It communicates over stdio and does not expose
an HTTP listener.

The server provides three tools:

| Tool | Input | Result |
|---|---|---|
| `scan_media` | `path: string` | The complete BanTheFake report JSON and a short, cautious summary. |
| `explain_verdict` | `report_json: object \| string` | A plain-English explanation of an existing report. |
| `get_help_resources` | `country: string = "UK"` | Static reporting, removal and support resources; it makes no network request. |

There is deliberately no `scan_url` tool. Fetching a caller-supplied URL would expose private network services
to SSRF, reveal the machine's IP address to a remote host, and download media without a clear local privacy
boundary. Download a file yourself, inspect it, and pass its local path to `scan_media`.

## Claude Code

From any directory, replace `/path/to/banthefake` with this repository's absolute path:

```bash
claude mcp add banthefake -- uv run --directory /path/to/banthefake --extra ml python -m app.mcp_server
```

Use `claude mcp list` to check that the server is registered. The `ml` extra installs both detector models'
runtime dependencies. Their pinned weights are loaded lazily on the first scan, which can make that call slow.
Set `HF_HOME` if the weights live in a non-default cache. Set `HF_HUB_OFFLINE=1` when the weights are already
cached and the client must never attempt a download.

## Codex CLI

Add this to `~/.codex/config.toml`, again using the repository's absolute path:

```toml
[mcp_servers.banthefake]
command = "uv"
args = ["run", "--directory", "/path/to/banthefake", "--extra", "ml", "python", "-m", "app.mcp_server"]
```

Restart Codex after changing the configuration.

## Cursor and other MCP clients

Configure a stdio server with this command and argument array:

```json
{
  "mcpServers": {
    "banthefake": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/banthefake", "--extra", "ml", "python", "-m", "app.mcp_server"]
    }
  }
}
```

Clients that accept a single command line can use:

```bash
uv run --directory /path/to/banthefake --extra ml python -m app.mcp_server
```

The core scanner still checks metadata and Content Credentials without ML dependencies. Omit `--extra ml` and
set `BTF_MODEL=off` if those checks alone are wanted.

The core install contains a small, dependency-free MCP stdio implementation because the official Python SDK is
not required at runtime. The optional `mcp` extra records the official SDK for a future migration:

```bash
uv sync --extra ml --extra mcp
```

`uv.lock` was intentionally not regenerated when this server was added because the build environment had no
network access and the `mcp` package was not cached. Regenerate the lock with `uv lock` in a networked checkout
before requiring `--locked` or installing the optional `mcp` extra.

## File access controls

`scan_media` resolves the supplied path and accepts only a readable, non-empty regular file whose magic bytes
identify a supported image or video. Directories, devices, pipes, missing files, unsupported files, and files
larger than `BTF_MAX_UPLOAD_MB` (50 MB by default) are rejected before scanning. Image dimension and decode
budgets are the same ones used by the web scanner.

To confine the server to particular directories, set `BTF_MCP_ALLOWED_DIRS` to an operating-system path-separated
list. On macOS and Linux, for example:

```bash
export BTF_MCP_ALLOWED_DIRS="$HOME/Downloads:$HOME/Pictures"
```

The check uses the resolved path, so a symlink cannot escape an allowed directory. The server only reads the
file named by the tool caller. It does not run shell commands or write output files. Untrusted media parsing
continues to use BanTheFake's isolated worker pool by default; keep `BTF_SCAN_ISOLATION=on`. Existing worker
temporary handling is the only disk writing performed by a scan.

## Example prompts

- `Scan /Users/me/Downloads/suspicious.jpg with BanTheFake and explain the evidence.`
- `Check /home/me/Videos/clip.mp4. Tell me how many frames were sampled and whether coverage was partial.`
- `Explain this BanTheFake report without claiming that a low detector score means real: { ... }`
- `Show me the UK removal and support resources for a deepfake victim.`

## Limits and interpretation

Detectors can be wrong. Compression, screenshots, new generators and deliberate evasion can hide AI signals,
while unusual real images can be flagged. A low detector score, camera EXIF, or an `unknown` verdict does not
mean a file is real. Video scanning samples a limited number of frames and reports its coverage.

Content Credentials provide provenance only when their credentials survive sharing and validate against a
trusted signer. A `verified_capture` verdict proves the file's signed origin and history, not that the depicted
scene is real: a camera can record a screen or a staged scene. Social networks and messaging apps often strip
provenance and ordinary metadata. **No tool can prove a file is real.**
