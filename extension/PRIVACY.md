# BanTheFake browser extension: privacy

**Short version:** the extension sends an image only when you right-click it and choose "Check with
BanTheFake" (or a few frames of a video only when you choose "Check this video with BanTheFake"), and only to the
BanTheFake server you set in the options. It has no analytics, no tracking and
no remote code.

## What is sent, when, and where

- **Only on your click.** Nothing is read or sent when you browse. The extension has no content scripts and
  does not watch pages.
- **Only the image you clicked**, as a file upload to `<your server>/api/scan`. The page address, your
  cookies, the referrer and any other browsing data are not sent (`credentials: "omit"`,
  `referrerPolicy: "no-referrer"`). Redirects are refused, so a server cannot forward the upload elsewhere.
- **Only to your configured server.** There is no default server: the shipped address
  (`https://YOUR-DOMAIN.example`) is a placeholder, and nothing is sent until you replace it. Your server's
  operator handles the image under their own privacy notice (`<your server>/privacy.html`). The BanTheFake
  server deletes each upload as soon as the scan finishes and does not store or log it.
- **Videos: only a few frames.** When you choose to check a video, the extension copies up to 8 still frames
  (over about 8 seconds, or 1 if the video is paused) from the video playing in that tab and uploads them to
  `<your server>/api/scan-frames`. No audio, page address, title or video file is sent; the site's hostname is
  shown in the result window only. Protected or cross-origin videos are not captured and nothing is sent.
- **Reading the image.** To get the image bytes, the extension asks the page you are on to fetch the image
  with the page's own permissions (usually from the browser cache). If the page's security rules do not
  allow that, it tries once more without cookies. If that is also blocked, nothing is sent and you are told so.

## What is stored

- **In `storage.local`:** the server address you entered. Nothing else.
- **In `storage.session`** (memory only, cleared when the browser closes): the last five scan results, so the
  result window can show them. They are never written to disk by the extension and never sent anywhere.

## What the extension does not do

- No analytics, telemetry, crash reporting or advertising identifiers.
- No remote code: every script ships in the package, and the extension pages use a strict Content Security
  Policy (`script-src 'self'; object-src 'none'`, no inline scripts, no `eval`).
- No access to your browsing history, tabs or page contents beyond the single image, or the frames of the single
  video, you chose.

## Permissions

| Permission | Why |
|---|---|
| `contextMenus` | Adds "Check with BanTheFake" to the right-click menu on images, "Check this video with BanTheFake" on videos, and (on youtube.com and tiktok.com only) a page entry for the video there. |
| `activeTab` | Lets the extension run one fetch, or copy frames from the playing video, in the tab you right-clicked, only after your click. |
| `scripting` | Needed to run that one fetch or frame capture in the page. |
| `storage` | Saves the server address, and keeps recent results in memory for the result window. |
| Optional host access to your server only | Requested when you save the server in the options, for that one host. Lets the extension read the server's response. The previous server's access is removed when you change it. |

The extension never requests access to all websites.
