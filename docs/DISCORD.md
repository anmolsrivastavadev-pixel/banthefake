# BanTheFake Discord bot

The Discord bot is a thin client for a BanTheFake server. It downloads a Discord-hosted image or video into
memory, sends it to the server's `/api/scan` endpoint, and shows the returned indicators. It never runs the
detectors itself and never stores files or results.

## Create the Discord application

You must create and own the Discord application yourself. BanTheFake cannot create an application or token for
your server.

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and
   add a bot on its **Bot** page.
2. Keep all privileged gateway intents disabled. The bot does not need Message Content, Server Members, or
   Presence intents.
3. On **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes.
4. Grant only **Send Messages** and **Use Application Commands**. Do not grant Administrator.
5. Use the generated URL to install the bot in the server you administer.

Reset the bot token if it has ever been exposed. Put it in `DISCORD_TOKEN` through your process manager or a
local `.env` file, restrict who can read it, and never commit it. The bot reads configuration only from its
environment; it does not parse `.env` automatically.

## Run locally

Start BanTheFake at `http://localhost:8000`, then install the bot dependencies and export its configuration:

```bash
bot/.venv/bin/pip install -r bot/requirements.txt
export DISCORD_TOKEN='replace-with-your-bot-token'
export BANTHEFAKE_URL='http://localhost:8000'
export BTF_DISCORD_GUILD_ID='replace-with-your-test-server-id'  # optional: fast command sync
bot/.venv/bin/python -m bot
```

`BTF_DISCORD_GUILD_ID` copies the commands to one development server so changes appear quickly. Leave it unset
for global command sync, which Discord can take longer to propagate.

Configuration:

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | required | Discord bot token. Read from the environment and never logged. |
| `BANTHEFAKE_URL` | required | HTTPS BanTheFake base URL. Plain HTTP is accepted only for `localhost`. |
| `BTF_MAX_UPLOAD_MB` | `50` | Bot-side download cap. Streaming stops when the cap is exceeded. Match the server limit. |
| `BTF_DISCORD_PUBLIC_RESULTS` | `off` | `off` keeps replies ephemeral; `on` makes results visible in the channel. |
| `BTF_DISCORD_GUILD_ID` | unset | Optional development server ID for guild-only command sync. |

The commands are `/scan`, `/help`, and the message context command **Apps → Check with BanTheFake**.

## Deploy

Run one bot process alongside the BanTheFake server and set `BANTHEFAKE_URL` to the server's public HTTPS base
URL. The bot needs outbound HTTPS access to Discord's CDN, the Discord gateway/API, and that BanTheFake server.
It does not need direct access to detector models or the BanTheFake database. Keep `BTF_DISCORD_PUBLIC_RESULTS`
off unless server members understand that scans will be public.

Use your deployment platform's secret store for `DISCORD_TOKEN`. If you use an environment file, keep it out of
the image and source it through the process manager. Run the bot as an unprivileged user and restart it on
failure. Deploying the bot does not replace the BanTheFake server; `/api/scan` must be reachable.

## Limits and privacy

- Results are indicators. **No tool can prove a file is real.** Verified capture credentials establish signed
  origin, not whether a photographed scene was real. Camera metadata is never presented as verification.
- The bot considers the first image/video attachment. For an embed it accepts only an image backed by a Discord
  CDN attachment URL; it does not fetch arbitrary links or follow redirects.
- Files are held in memory only long enough to send them to the configured BanTheFake server. The bot does not
  persist the file or result. The BanTheFake server has its own privacy and operational configuration.
- Logs contain only the command name, a per-process salted hash of the guild ID, and a status. They omit users,
  file names, URLs, results, and the token.
- Each user can start five scans per minute. Server-side limits, media support, scan timeouts, Discord's own
  attachment limits, and `BTF_MAX_UPLOAD_MB` still apply.
- Error messages distinguish files rejected before upload from failures after data was sent. A network failure
  during upload can be ambiguous, so the bot says the file may have been sent.
