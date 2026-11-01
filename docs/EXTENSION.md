# Browser extension

`extension/` is a Manifest V3 extension for Chrome and Edge (Firefox 128+ should work, see
[Limits](#limits)). Right-click an image, choose **Check with BanTheFake**, and a small window shows the
scanner's verdict, the signals and a link to the site's help resources. Right-click a video you are watching and
choose **Check this video with BanTheFake** to check frames of it (see [Checking a video](#checking-a-video)).
It is not published to any store.

Privacy details are in [`extension/PRIVACY.md`](../extension/PRIVACY.md).

## Install unpacked

- **Chrome / Edge:** open `chrome://extensions` (or `edge://extensions`), turn on **Developer mode**, click
  **Load unpacked** and pick the `extension/` folder.
- **Firefox:** open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on** and pick
  `extension/manifest.json`. Temporary add-ons are removed when Firefox restarts.

## Configure

The options page opens on install (or click the toolbar icon).

1. Enter your BanTheFake server, for example `https://banthefake.your-domain.org`. The shipped default
   `https://YOUR-DOMAIN.example` is a placeholder; the extension sends nothing until you replace it.
   Only `https://` addresses are accepted, except `http://localhost` and `http://127.0.0.1` for development.
   A path prefix (`https://example.org/banthefake`) is allowed; queries, fragments and credentials are not.
2. Click **Save**. The browser asks to let the extension connect to that one host. Accept it.
3. Click **Test connection** to check `/api/health`.

### Server side

No server change is needed when the user grants the host permission: Chrome, Edge and Firefox let an
extension read responses from hosts it has permission for.

If you want the extension to work **without** that permission (for example, when you publish the extension
yourself and know its ID), list its origin in `BTF_CORS_ORIGINS`:

```bash
BTF_CORS_ORIGINS=chrome-extension://abcdefghijklmnopabcdefghijklmnop
```

The options page shows the extension's origin. The setting is a comma-separated list of exact, lowercase
origins (no wildcard, path or trailing slash; the app refuses to start otherwise). It only affects
`POST /api/scan`, `POST /api/scan-frames` and `GET /api/health`: matching origins get `Access-Control-Allow-Origin` and preflights
are answered; the petition, admin and page routes never get CORS headers. Credentials are never allowed.
Empty (the default) means no CORS headers at all, exactly as before. The site's strict CSP and other security
headers are unchanged. Firefox gives every installation a random `moz-extension://` UUID, so on Firefox
use the host permission instead.

## How it works

1. **Menu click.** The background worker opens the result window right away and starts the job.
2. **Reading the image.** The extension runs one `fetch` inside the clicked frame (`activeTab` +
   `scripting`), with the page's own origin. Same-origin images and images whose site allows cross-origin
   reads work; the HTTP cache is preferred, so usually nothing is downloaded again. If the page cannot read
   the image, the worker tries once itself, without cookies, for `https:` and `data:` images only. That
   fetch is still subject to CORS, because the extension has no host permission for the image's site. If
   both fail, nothing is sent and the window says so.
3. **Scanning.** The worker POSTs the bytes as `multipart/form-data` to `<server>/api/scan`, with no
   cookies, no referrer and redirects refused. Images over 50 MB are not sent.
4. **Result.** The worker stores the response in `storage.session` (memory only, last five jobs). The result
   window renders it with DOM text nodes only, using the site's wording: the server's verdict title and
   detail, a caveat for verified verdicts ("origin, not scene"), "No tool can prove a file is real", the
   signals list, C2PA and model rows, and a link to `<server>/#report` (the site's "Get it taken down"
   section).

## Checking a video

1. Start the video and let it play (or pause it on the moment you want checked).
2. Right-click the video and choose **Check this video with BanTheFake**.
   - **YouTube and TikTok:** the player's overlays catch the right-click, and YouTube shows its own menu first.
     Right-click again on YouTube to get the browser's menu, or use **Check the video on this page with
     BanTheFake**, which appears in the page menu on `youtube.com` and `tiktok.com` only. Either entry finds the
     largest visible, playing video in the tab.
3. Keep it playing for about 8 seconds. The result window says "Capturing frames…", then shows the verdict, how
   many frames were checked over how many seconds, and the site's hostname.

What happens:

- On your click, `activeTab` lets the extension inject `capture.js` once into the clicked frame. It picks the
  video you right-clicked (or the largest visible, playing one) and copies a frame to a canvas about once a
  second, up to 8 frames (fewer near the end of the video). It never seeks, pauses, changes the speed or reloads
  the video, and it fetches nothing: the pixels come from the `<video>` element already playing. A paused video
  gives one frame ("Paused — 1 frame checked").
- Frames are downscaled to at most 1280 px on the long side and encoded as JPEG (quality 0.9). The upload is
  capped at about 12 MB in total.
- If the canvas is **tainted** (a video from another site without CORS permission) or the frames are black or a
  single colour (DRM/EME-protected video draws black), the capture stops and the window says "This video can't be
  captured (protected or cross-origin) — nothing was sent".
- Only the frames go to `<server>/api/scan-frames`. The page address, title, cookies, referrer and audio are not
  sent; the hostname is shown in the result window only. The server never downloads the video or any URL.
- The result never says a video is real. It states the number of frames and seconds checked, that audio is not
  analysed, and on youtube.com and tiktok.com: "Platform labels (like 'AI-generated' tags) aren't read by this
  tool."

### Video limits

- **Accuracy on AI-generated video has NOT been benchmarked yet.** The detectors were chosen and calibrated on
  still images ([`DETECTOR-BENCHMARK.md`](DETECTOR-BENCHMARK.md)); treat a video result as a hint.
- **Screen capture and compression.** Streaming platforms re-encode heavily, and the captured frames are
  re-compressed again as JPEG. Both remove the fine detail detectors rely on, so misses and false alarms are
  more likely than with the original file.
- **DRM.** Protected video (most paid films and series, some music videos) draws as black and cannot be checked.
- **Cross-origin video.** Sites that serve video from another domain without CORS cannot be read (tainted
  canvas). Videos inside iframes from another site (for example an embedded player on a news page) cannot be
  scripted with `activeTab`; open the video on its own site instead.
- **No audio.** Voice clones and dubbed speech are not analysed at all.
- **About 8 seconds.** Only the stretch you are watching is checked, not the whole video. A real clip with a
  short synthetic part may be missed.
- **No metadata.** Captured frames carry no EXIF, C2PA or generator markers, so only the detector can say
  anything, and platform labels are not read.

### Permissions and why

| Permission | Why it is needed | Why not something broader |
|---|---|---|
| `contextMenus` | The right-click entries (images; videos; and, on youtube.com and tiktok.com only, the page). The page entry is limited with `documentUrlPatterns`, which needs no host permission. | - |
| `activeTab` | Temporary access to the tab you right-clicked, granted only by that click, so the image can be read with the page's own rules, or frames copied from the video playing there. | `<all_urls>` would give standing access to every page. With `activeTab` there is none until you click. |
| `scripting` | Runs the one-off fetch function, or injects `capture.js` once, in that tab. No content scripts are declared. | - |
| `storage` | Server address (`local`) and recent results (`session`, memory only). | - |
| `optional_host_permissions` (`https://*/*`, `http://localhost/*`, `http://127.0.0.1/*`) | Only the **configured server's host** is requested at runtime, from the options page. The list is just the range a user may pick from; nothing in it is granted at install. | A fixed `host_permissions` entry is impossible because the server is user-chosen, and requesting `<all_urls>` is not needed: image bytes are read through `activeTab`. |

Match patterns cannot name a port, so the permission for `http://localhost:8000` covers every port on
`localhost`. When you change the server, the old host's permission is removed.

### Content Security Policy

`extension_pages`: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src https:
http://localhost:* http://127.0.0.1:* data:; object-src 'none'; base-uri 'none'; form-action 'none';
frame-ancestors 'none'`. `connect-src` has to allow any `https:` host because the server is user-chosen;
the code only connects to the configured server (and to `https:`/`data:` images in the fallback).

## Tests

```bash
node --test extension/      # Node 20+, no npm dependencies
```

They cover URL validation, the permission pattern, verdict rendering (honest wording, unknown verdicts,
untrusted C2PA claims, model readings, plain-text signals), error messages, the in-page fetch helper and
manifest/CSP guarantees. `test/video.test.js` covers video capture: the frame-sampling scheduler, downscaling,
video choice, taint and black-frame detection, the payload cap, `capture()` against a fake page (no seeking, no
playback changes) and the result wording. CI runs them in the `extension` job. The server option is tested in
`tests/test_cors.py`.

## Limits

- **Some images cannot be read.** Images from another site that does not allow cross-origin reads, and
  `blob:` images in frames the extension cannot script, are refused rather than fetched with broader
  access. Save the image and upload it on the site instead.
- **Videos** are checked from captured frames only; see [Video limits](#video-limits). CSS background images do
  not get a menu entry.
- **Metadata is often gone.** Images on social sites are usually re-encoded, so EXIF and C2PA data are
  missing and the verdict is often "No conclusive signals found". That does not mean the image is real.
- **Firefox:** loads as a temporary add-on; `executeScript` runs in the content-script context, so a few
  same-origin images may behave differently than in Chrome. Not tested in CI.
- **Rate limits** are the server's (`BTF_SCAN_RATE`, default 10 scans per minute per IP).

## Packaging notes (not published)

The extension is not published to any store. To build a zip for a store or for enterprise deployment:

```bash
cd extension && zip -r ../banthefake-extension.zip . -x 'test/*' 'package.json' 'PRIVACY.md'
```

Before publishing:

- Replace the placeholder Firefox ID in `browser_specific_settings.gecko.id` with your own (AMO requires one).
  Chrome may list `browser_specific_settings` and the Firefox `background.scripts` fallback as manifest
  warnings; they are harmless (Chrome 121+ ignores `background.scripts` when `service_worker` is set).
- Consider shipping a build with your own server as the default and its host in `host_permissions`, or
  setting `BTF_CORS_ORIGINS` to the published extension ID.
- Store listings need a privacy policy: use `extension/PRIVACY.md`, and the single-purpose description
  "Check a right-clicked image or video with your BanTheFake server".
- Bump `version` in `manifest.json` for every upload.
