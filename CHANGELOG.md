# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- "Check this video with BanTheFake" in the browser extension: captures up to 8 frames (about 8 seconds, or the
  current frame when paused) from the video playing in the user's tab, including on YouTube and TikTok, and sends
  only those frames. Protected (DRM) and cross-origin videos are refused with nothing sent.
- `POST /api/scan-frames`: scans up to 16 captured frames as an ordered video sample with the existing isolated
  image parser and video aggregation rule. The server never fetches videos or URLs. Accuracy on AI-generated video
  has not been benchmarked yet.

## [1.0.0] - 2026-11-01

### Added

- A self-hosted image and video scanner that combines metadata and generator-marker checks with C2PA Content
  Credentials. Trusted provenance requires a valid manifest from a signer on the bundled official C2PA trust
  list.
- Two pinned CPU image detectors with an agreement rule for the strongest AI verdict, plus documented benchmark
  results and limits.
- Process-isolated media and C2PA parsing with time, memory, file, pixel, frame and IPC limits.
- A SQLite-backed petition with admin export and erasure, and a browser-only letter generator.
- An MCP server for scanning media from compatible clients.
- A street-campaign guide and printable sign, flyer and cue-card generator.
- Docker, Docker Compose, Caddy, Hetzner cloud-init, backup and rollback tooling, plus Fly.io deployment config.

### Not included

- Face-swap detection is not shipped. The evaluated candidates did not meet the project's accuracy, resource and
  training-data-rights requirements.

[Unreleased]: https://github.com/anmolsrivastavadev-pixel/banthefake/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/anmolsrivastavadev-pixel/banthefake/releases/tag/v1.0.0
