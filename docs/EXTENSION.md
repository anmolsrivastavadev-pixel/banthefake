# Browser extension

`extension/` is a Manifest V3 extension for Chrome and Edge (Firefox 128+ should work, see
[Limits](#limits)). Right-click an image, choose **Check with BanTheFake**, and a small window shows the
scanner's verdict, the signals and a link to the site's help resources. It is not published to any store.

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
`POST /api/scan` and `GET /api/health`: matching origins get `Access-Control-Allow-Origin` and preflights
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

### Permissions and why

| Permission | Why it is needed | Why not something broader |
|---|---|---|
| `contextMenus` | The right-click entry. | - |
| `activeTab` | Temporary access to the tab you right-clicked, granted only by that click, so the image can be read with the page's own rules. | `<all_urls>` would give standing access to every page. With `activeTab` there is none until you click. |
| `scripting` | Runs the one-off fetch function in that tab. No content scripts are declared. | - |
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
manifest/CSP guarantees. CI runs them in the `extension` job. The server option is tested in
`tests/test_cors.py`.

## Limits

- **Some images cannot be read.** Images from another site that does not allow cross-origin reads, and
  `blob:` images in frames the extension cannot script, are refused rather than fetched with broader
  access. Save the image and upload it on the site instead.
- **Images only.** Video elements and CSS background images do not get the menu entry.
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
  "Check a right-clicked image with your BanTheFake server".
- Bump `version` in `manifest.json` for every upload.
