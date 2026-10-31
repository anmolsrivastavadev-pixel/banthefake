# Deploying BanTheFake

This guide covers a production deployment of the Docker image described in [README.md](../README.md#deployment).
It recommends two ways to host it, gives step-by-step instructions for both, and ends with a pre-launch checklist.
In a hurry? The [fastest path](#fastest-path-hetzner-with-cloud-init-and-deploysh) is four steps on Hetzner.

Ready-to-use config in this repository:

| File | For |
|---|---|
| [`deploy/hetzner/docker-compose.yml`](../deploy/hetzner/docker-compose.yml) | Option A: the app plus Caddy (automatic HTTPS) on one VPS |
| [`deploy/hetzner/Caddyfile`](../deploy/hetzner/Caddyfile) | Option A: TLS, upload size limit, timeouts, retry during restarts, no access log |
| [`deploy/hetzner/.env.example`](../deploy/hetzner/.env.example) | Option A: domain, ACME email, admin token and app settings |
| [`deploy/hetzner/backup-db.sh`](../deploy/hetzner/backup-db.sh) | Option A: consistent, compressed and optionally encrypted SQLite backups |
| [`deploy/hetzner/cloud-init.yaml`](../deploy/hetzner/cloud-init.yaml) | Option A: one-step server provisioning (deploy user, SSH hardening, Docker, ufw, auto-updates, backup timer, code checkout) |
| [`deploy/hetzner/deploy.sh`](../deploy/hetzner/deploy.sh) | Option A: `./deploy.sh <git-ref>`: checks `.env`, deploys a tag/commit, waits for health, rolls back on failure |
| [`fly.toml`](../fly.toml) | Option B: Fly.io Machine size, volume, health check, always-on |

## What the app needs from a host

These requirements come from the README and the benchmarks in it:

- **Memory: 4 GB for one scan worker; 8 GB for two.** Two CPU detector models use about 2.5 GB at peak.
  Each isolated parser worker may use up to `BTF_SCAN_MAX_RSS_MB` (1 GB by default), so the sizing rule is
  2.5 GB + `BTF_SCAN_CONCURRENCY × 1 GB`, plus container and host headroom. The shipped 4 GB profile uses
  concurrency 1. Use an 8 GB host and the adjustments below for concurrency 2.
- **CPU: 2 vCPU.** A scan takes about 0.33 s per image on an Apple M5 and is slower on small cloud vCPUs.
  Sustained CPU matters more than burst CPU when traffic peaks.
- **Exactly one process, one instance.** The rate limiter and the scan slots are in memory and per process.
  Two workers or two replicas double the effective limits. A single always-on instance is enough for
  moderate traffic.
- **A persistent volume** for the SQLite petition database (`BTF_DB_PATH`, which is `/data/banthefake.db` in
  the image). It holds personal data (names and emails) and needs backups.
- **TLS termination in front of the app**, plus a trustworthy client-IP path so rate limits are per visitor.
  The Caddy deployment trusts only its fixed proxy address through Uvicorn. Fly uses the edge-set
  `Fly-Client-IP` header through the opt-in `BTF_CLIENT_IP_HEADER` setting.
- **An image of about 3 GB.** The image includes about 840 MB of model weights and runs fully offline. A cold
  start takes a few seconds to load the models (`/api/health` reports `"model": "loading"`), and the first scan
  waits for them. Scale-to-zero hosting therefore gives a slow first request.

The Compose app limit is 4 GiB: 2.5 GiB model peak + one worker's 1 GiB limit + 0.5 GiB of app headroom.
The host's swap absorbs brief pressure outside the container, but sustained swapping is a signal to move to
the 8 GB profile. For concurrency 2, use an 8 GB host, set `BTF_SCAN_CONCURRENCY=2`, and raise Compose
`mem_limit` to `5g` (2.5 + 2 × 1 + 0.5 GiB). On Fly, set the Machine memory to `8gb` with concurrency 2.

## Hosting options compared

Prices were read on **2026-10-31** from the providers' pricing pages (sources at the end of this file).
They exclude VAT and assume an always-on 2 vCPU / 4 GB instance, about 1 GB of storage for the database, and
modest traffic.

| Option | Monthly cost (2 vCPU / 4 GB) | Persistent storage | TLS | Cold starts | Egress | Single instance fits? |
|---|---|---|---|---|---|---|
| **Hetzner Cloud CPX22** (EU) + Docker + Caddy | **€19.99**, or **€23.99** with Hetzner's daily server backups (+20%) | 80 GB local NVMe included | Caddy + Let's Encrypt (free) | None (always on) | 20 TB included, then €1/TB | Yes, one server |
| Hetzner Cloud CPX32 (8 GB, 4 vCPU) | €35.99 (+€7.20 backups) | 160 GB included | as above | None | 20 TB included | Yes. Use this for concurrency 2 with the documented 5 GiB app limit |
| Hetzner Cost-Optimized CX23 / CAX11 (2 vCPU / 4 GB) | €5.99 / €6.49 | 40 GB included | as above | None | 20 TB | Yes, but listed as **"Currently not available"** on 2026-10-31 |
| Hetzner US (CPX21: 3 vCPU / 4 GB) | €32.49 | 80 GB | as above | None | 2 TB included | Yes |
| DigitalOcean Droplet (Basic, 2 vCPU / 4 GiB) | $24.00, or $28.80 with weekly backups (+20%) / $31.20 with daily (+30%) | 80 GiB included | Caddy (same config as Option A) | None | 4,000 GiB included, then $0.01/GiB | Yes |
| **Fly.io** shared-cpu-2x, 4 GB | **~$25.54** ($13.39 + 2 GB × $6 + 1 GB volume $0.15), priced for iad/ewr | Fly Volume, $0.15/GB, daily snapshots (first 10 GB free) | Managed, first 10 hostnames free | None if kept always on (as in `fly.toml`) | $0.02/GB (NA/EU) | Yes, one Machine + one volume |
| Fly.io performance-2x, 4 GB | $66.00 | as above | as above | as above | as above | Yes |
| Railway (Hobby $5, includes $5 of usage) | Usage-based: ~$10 per GB-month of RAM and ~$20 per vCPU-month actually used. An idle process at 1.5 to 2.5 GB costs about **$15 to $30** (estimate, not a quote) | Volume, ~$0.15/GB-month | Managed | None if always on | $0.05/GB | Yes. Replicas cannot be used with a volume; brief downtime on redeploy |
| Render (Pro instance: 2 CPU / 4 GB) | $85 + disk $0.25/GB (Hobby workspace $0, Pro workspace +$25) | Persistent disk; daily snapshots kept 7 days | Managed | None | 5 GB included (Hobby), then $0.15/GB | Yes. A disk blocks zero-downtime deploys and scaling |
| DigitalOcean App Platform | n/a | **No volumes**; the filesystem is lost on every deploy | | | | Not suitable without a managed DB |
| Google Cloud Run | Always-on, instance-based billing: 2 × $0.000018/vCPU-s + 4 × $0.000002/GiB-s ≈ **$114/month**, ~$109 after the free tier. Scale-to-zero is cheap at low traffic | **No persistent disk.** SQLite on a Cloud Storage FUSE mount is unsafe, so this needs a managed database and code changes | Managed | Scale-to-zero means pulling a ~3 GB image and loading the models on the first request | Not checked | `max-instances=1` is possible, but a deploy briefly runs two revisions |
| Home server | Hardware you own plus electricity | Local disk | Caddy, or a tunnel provider | None | Your uplink | Yes, but see below |

Notes:

- **Hetzner Cloud** prices include a primary IPv4 address. Hetzner backups are "starting at €1.1980", which is
  20% of the €5.99 CX23. The backup cost above assumes the same 20% for other plans. Seven daily backup slots
  are kept.
- **Fly.io shared CPUs** have a baseline of 6.25% of a core per vCPU (12.5% of one core for `shared-cpu-2x`),
  with a burst balance of up to 500 s. A low-traffic campaign site stays within the burst. A traffic spike or a
  long video queue gets throttled hard, so watch `fly_instance_cpu_throttle` and move to `performance-2x` if it
  stays high. Fly lists Machine prices for iad/ewr. Other regions, such as `fra`, can cost more.
- **Render and Railway** are more convenient PaaS options. For this workload they cost more (Render) or are
  less predictable (Railway's usage billing), and their volumes force the same single-instance,
  restart-with-downtime model as the cheaper options.
- **Cloud Run** gives up the two things this app relies on: a local SQLite file and a warm, single, long-lived
  process. It is the wrong shape unless the petition store is moved to a managed database.
- **Home server**: this is the cheapest option if the hardware already exists. Downsides: residential uptime
  and power, and exposing your home IP (or terminating TLS at a tunnel provider, which then sees every
  uploaded file in clear text). Uploads are personal data under the privacy notice, so use this option only
  with a provider you would also name in `app/templates/privacy.html`.

### Recommendation

- **Primary: Hetzner Cloud CPX22 + Docker Compose + Caddy (Option A), about €24/month with backups.** This is
  the cheapest robust option that is available today: 4 GB of RAM with no CPU throttling, EU hosting for EU
  petition data, 20 TB of traffic, automatic HTTPS, and the whole setup in two files you control. If the
  Cost-Optimized CX23 (€5.99) or the Arm CAX11 (€6.49) becomes available again, the same config works there.
  The image is built for both amd64 and arm64. For a US audience, use the same config on a DigitalOcean
  Droplet ($24, or $28.80 with backups).
- **Alternative: Fly.io, `shared-cpu-2x` with 4 GB (Option B), about $26/month.** Choose this if you do not
  want to maintain a server (OS updates, firewall, Docker). It has managed TLS and certificates, a
  one-command deploy and volume snapshots. The trade-offs are shared-CPU throttling under sustained load and
  higher network costs.

## Environment variables

The full reference is the README's [Configuration](../README.md#configuration) table. These are the ones a
deployment sets or must think about:

| Variable | Option A (compose) | Option B (Fly) | Notes |
|---|---|---|---|
| `BTF_ADMIN_TOKEN` | `.env` | `fly secrets set` | **Required** for export and erase. At least 24 characters, or the admin endpoints stay disabled (404) and an error is logged. |
| `BTF_PRIVACY_CONTACT` | `.env` | `fly secrets set` or `[env]` | **Required** in production. Monitored email address or `https://` URL shown on `/privacy.html` for privacy and deletion requests. Validated at startup; HTML-escaped into the page. |
| `BTF_ENV` | `production` (`.env`) | `production` (`[env]`) | With `production`, the app refuses to start (exit 1, `Refusing to start: ...` in the log) while `BTF_PRIVACY_CONTACT` is unset or a placeholder. Default `development` shows a "Contact not configured" notice instead. |
| `BTF_ADMIN_FAIL_RATE` | `10` | `10` | Failed admin authentications allowed per client IP per hour. Once full, that IP receives 429 on all admin requests until the window passes. |
| `FORWARDED_ALLOW_IPS` | fixed in compose: `172.31.250.1` | default; unused for client identity | Caddy's fixed proxy address. **Never `*`**: clients could spoof `X-Forwarded-For`. Fly does not depend on Uvicorn's forwarding-header selection. |
| `BTF_CLIENT_IP_HEADER` | unset | `Fly-Client-IP` | Opt-in name of an edge-controlled header containing exactly one IPv4 or IPv6 address. Invalid or absent values fall back to the socket peer. Do not enable this for a header clients can supply unchanged. |
| `BTF_HSTS` | unset | `on` | Adds `Strict-Transport-Security: max-age=31536000`. Fly emits it in the app. Hetzner leaves it unset because Caddy emits HSTS, keeping exactly one source. |
| `BTF_DB_PATH` | `/data/banthefake.db` (on the `btf-data` volume) | `/data/banthefake.db` (on the `btf_data` volume) | Must be on the persistent volume. |
| `BTF_MODEL_THREADS` | `2` | `2` | Number of vCPUs. |
| `BTF_SCAN_CONCURRENCY` | `1` | `1` | Shipped 4 GB profile. Concurrency 2 needs the 8 GB profile described above. |
| `BTF_MAX_UPLOAD_MB` | `50` | default | With Option A, keep the Caddyfile `request_body max_size` slightly above it. |
| `BTF_UPLOAD_TIMEOUT_S` | `60` | default | App-side upload fallback. Option A's Caddy `read_body 35s` rejects slower request bodies first. Upload time does not consume `BTF_SCAN_TIMEOUT_S`. |
| `BTF_PETITION_GOAL` | `.env` | `[env]` | Shown on the site. |
| `BTF_DOMAIN`, `ACME_EMAIL` | `.env` (Caddy only) | n/a | Domain to serve; ACME account contact. |
| `BTF_SCAN_ISOLATION`, `BTF_SCAN_MAX_RSS_MB`, `BTF_SCAN_TIMEOUT_S`, ... | defaults | defaults | See the README. Keep isolation on in production. |

`HF_HOME`, `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` are set by the image. Do not override them.

---

## Fastest path: Hetzner with cloud-init and `deploy.sh`

This is Option A with the host preparation automated. Read Option A once for how the pieces fit together.

1. **Create the server with the cloud-init file.** Copy
   [`deploy/hetzner/cloud-init.yaml`](../deploy/hetzner/cloud-init.yaml) and edit the two `REPLACE-ME`
   values: your SSH public key (for the `deploy` user) and `BTF_REF`, the release tag or commit to check out
   (for example `v1.0.0`, see [Releases](#releases-manual-tagging)). In the Hetzner Console, create a server
   as in [A1](#a1-create-the-server) (CPX22, Ubuntu 24.04, IPv4 + IPv6, backups on, the Cloud Firewall) and
   paste the file into **Cloud config**. Point DNS at it ([A2](#a2-dns)).

   On first boot it creates the `deploy` user (key-only, passwordless sudo, in the `docker` group), enables
   unattended security upgrades, adds 2 GB of swap, installs Docker
   Engine and the compose plugin from Docker's official apt repository (per
   <https://docs.docker.com/engine/install/ubuntu/>), enables `ufw` with only 22/tcp, 80/tcp, 443/tcp and
   443/udp open, clones the repository to `/opt/banthefake` at `BTF_REF`, creates `deploy/hetzner/.env` from
   `.env.example` (mode 600), and installs the `btf-backup.timer` systemd timer (daily backup with
   `backup-db.sh` into `/var/backups/banthefake`). This takes a few minutes; progress is in
   `/var/log/cloud-init-output.log` and `/var/log/btf-provision.log`. After `ufw` has allowed port 22, the
   provisioner validates `/home/deploy/.ssh/authorized_keys` with `ssh-keygen -l -f`. Only a successful check
   makes it disable root and password SSH login and restart sshd. If the placeholder was not replaced or the
   key is invalid, hardening is skipped, provider-injected root access remains available, and a prominent
   explanation is written to `/etc/motd` and both provisioning logs. Install a valid deploy key and re-run
   `/usr/local/sbin/btf-provision.sh` before relying on the `deploy` login.

2. **SSH in:** `ssh deploy@SERVER_IP`. (`cloud-init status --wait` blocks until provisioning has finished.)

3. **Edit `.env`:**

   ```bash
   cd /opt/banthefake/deploy/hetzner
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # paste into BTF_ADMIN_TOKEN
   nano .env   # BTF_DOMAIN, ACME_EMAIL, BTF_ADMIN_TOKEN, BTF_PRIVACY_CONTACT (BTF_ENV=production stays)
   ```

   To encrypt backups, put `AGE_RECIPIENT=age1...` in `/etc/banthefake/backup.env` (`sudo nano`), and set up
   an off-site copy as in [A7](#a7-backups).

4. **Deploy:** `./deploy.sh main` (or a tag, `./deploy.sh v1.0.0`).

`deploy.sh <git-ref>` is idempotent and is also how you update later. It:

- refuses to run unless `.env` has `BTF_DOMAIN`, `ACME_EMAIL` (no `example.*` placeholders),
  `BTF_ADMIN_TOKEN` (at least 24 characters) and a non-placeholder `BTF_PRIVACY_CONTACT`, and refuses if
  tracked files in the checkout were edited;
- fetches and resolves the ref (a branch name means the latest fetched commit of that branch), then compares
  the target commit and expanded Compose configuration hash with labels on both running containers. If those
  match and the complete health check passes, it skips build, pull and `up`;
- records the running app and Caddy container IDs, reads their immutable image IDs, and gives each a unique
  per-run rollback tag before checking out the target. A normal update builds the app image, pulls Caddy and
  runs `docker compose up -d`;
- waits up to `HEALTH_TIMEOUT` seconds (default 600) for both Compose services to be running, for the direct
  app endpoint at `http://127.0.0.1:8000/api/health` to report `"model":"loaded"` (or `"off"` if `.env` sets
  `BTF_MODEL=off`), and for the same endpoint through Caddy and local TLS resolution to succeed. It retries
  HTTPS for `CERT_TIMEOUT` seconds (default 60) while Caddy obtains its first certificate. If HTTPS is not
  ready but port 80 returns an HTTP-to-HTTPS redirect to the configured domain, that redirect is accepted as
  proof that Caddy is serving; certificate completion must then be checked after DNS reaches the server;
- if build, start, or any health condition fails, stops the attempted stack, checks out the previous commit,
  and starts only the service images captured at invocation start through a temporary Compose override. It
  prints the final state even when a rollback step fails. On a first deploy, where nothing was running, it
  stops the attempted stack and never starts an unrelated stale image. Successful deploys remove the per-run
  rollback tags.

The first build takes roughly 10 to 20 minutes. Afterwards, do [A6](#a6-verify-client-ip-handling) and the
[pre-launch checklist](#pre-launch-checklist). Prefer deploying tags or commits that passed CI over `main`.

Not verified here: the cloud-init file was checked against cloud-init's published JSON schema and its
provisioning script with `shellcheck`, but it has not been booted on a real server from this repository.
Check `/var/log/btf-provision.log` on the first run.

---

## Option A: Hetzner Cloud VPS with Docker Compose and Caddy

The [fastest path](#fastest-path-hetzner-with-cloud-init-and-deploysh) automates A3 to A5 and A8. The manual
steps below describe the same setup.

### A1. Create the server

1. In the Hetzner Cloud Console, create a project and add your SSH public key.
2. Create a server with these settings:
   - **Type:** CPX22 (Regular Performance, 2 vCPU / 4 GB / 80 GB) for concurrency 1. Use CPX32 (8 GB),
     set concurrency 2, and raise the app limit to `5g` for the two-worker profile.
   - **Location:** NBG1, FSN1 or HEL1.
   - **Image:** Ubuntu 24.04.
   - **Networking:** IPv4 + IPv6.
   - **Backups:** enable them (+20%).
   - **SSH key:** yours. No root password.
3. Create a **Cloud Firewall** and attach it to the server. Allow inbound TCP 22 (from your IP only if you
   can), TCP 80, TCP 443 and UDP 443 (HTTP/3). Deny everything else.

### A2. DNS

Point an `A` record (IPv4) and an `AAAA` record (IPv6) for your domain, for example `petition.example.org`,
at the server. Caddy can only get a certificate once the records resolve to it.

### A3. Prepare the host

```bash
ssh root@SERVER_IP
apt-get update && apt-get -y upgrade
apt-get -y install docker.io docker-compose-v2 git age unattended-upgrades
systemctl enable --now docker

# 2 GB of swap as a safety net for memory peaks (the models themselves should stay in RAM)
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Automatic security updates (on by default on Ubuntu, check it)
dpkg-reconfigure -f noninteractive unattended-upgrades
```

Optionally, create a non-root deploy user in the `docker` group and disable root SSH login.

### A4. Get the code and configure it

```bash
git clone https://github.com/anmolsrivastavadev-pixel/banthefake.git /opt/banthefake
cd /opt/banthefake
git checkout <commit-or-tag-that-passed-CI>

cd deploy/hetzner
cp .env.example .env && chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # paste into BTF_ADMIN_TOKEN
nano .env   # set BTF_DOMAIN, ACME_EMAIL, BTF_ADMIN_TOKEN, BTF_PRIVACY_CONTACT
```

Before you build, do the content items in the [pre-launch checklist](#pre-launch-checklist). With
`BTF_ENV=production` (as in `.env.example`) the app refuses to start until `BTF_PRIVACY_CONTACT` is a real
contact; `docker compose logs app` then shows `Refusing to start: ...`.

### A5. Build and start

```bash
cd /opt/banthefake/deploy/hetzner
docker compose up -d --build        # first build: roughly 10 to 20 min (torch CPU wheel + ~840 MB of weights)
docker compose ps                   # app becomes "healthy" once the models are loaded
docker compose logs -f app          # no access log; startup and errors only
curl -fsS https://YOUR-DOMAIN/api/health
# {"ok":true,"model":"loaded","c2pa":true}
```

How it fits together:

- The **app** container publishes port 8000 on `127.0.0.1` only. Caddy runs on the host network so it sees
  the real IPv4 and IPv6 client addresses, and proxies to `127.0.0.1:8000`. Docker forwards that hop from the
  compose network's gateway, `172.31.250.1`, which is the only address in `FORWARDED_ALLOW_IPS`. Caddy sets
  `X-Forwarded-For` to the real client address and, because it trusts no upstream proxies, ignores any value
  a client sends.
- The database is on the named volume `banthefake_btf-data`. Docker initialises it from the image, so it is
  owned by the app user (uid 10001). Caddy's certificates are kept in `banthefake_caddy-data`.
- The app root filesystem is read-only. `/data` is its only persistent writable path. `/tmp` is a 512 MiB
  tmpfs for streamed uploads and private parser-worker directories: it covers a 50 MiB upload, the 64 MiB
  per-worker temp quota, worker replacement and several queued uploads. If `BTF_MAX_UPLOAD_MB`,
  `BTF_SCAN_TEMP_MB`, or concurrency increases, recalculate the tmpfs size. Docker's standard `/dev/shm`
  remains an ephemeral writable mount for Python multiprocessing primitives.
- Model weights under `/opt/hf` are baked into the image and loaded with offline mode enabled, so the cache
  is safe on the read-only root filesystem.
- Both containers prohibit privilege gain. The app drops all capabilities; Caddy drops all capabilities and
  adds back only `NET_BIND_SERVICE` for ports 80 and 443. The app is limited to 4 GiB by the calculation above.
- Caddy allows 35 seconds to read a request body. This is the deployment's primary upload-duration bound; the
  app's `BTF_UPLOAD_TIMEOUT_S` default of 60 seconds is a fallback for deployments without that proxy rule.
  Upload receipt and concurrency-queue time do not consume the scan deadline, which starts only after the app
  acquires a scan slot. Caddy is the sole HSTS source on this deployment; leave `BTF_HSTS` unset in `.env`.

### A6. Verify client-IP handling

The default scan limit is 10 per minute per IP. The limiter counts a request before the upload is read, so
a tiny invalid file is enough to test it:

```bash
echo hi > /tmp/x.txt
for i in $(seq 1 11); do curl -s -o /dev/null -w '%{http_code} ' -F file=@/tmp/x.txt https://YOUR-DOMAIN/api/scan; done
# expect ten 415s, then 429
for i in $(seq 1 3); do curl -s -o /dev/null -w '%{http_code} ' -H 'X-Forwarded-For: 203.0.113.9' -F file=@/tmp/x.txt https://YOUR-DOMAIN/api/scan; done
# still 429: a spoofed header must not reset the bucket
```

Then, within the same minute, scan from a different network, such as a phone on mobile data. It must
succeed. If it gets 429 too, every visitor shares one bucket and `FORWARDED_ALLOW_IPS` is wrong. Check the
gateway with `docker network inspect banthefake_app-net`.

### A7. Backups

There are three layers:

1. **Hetzner server backups** (enabled in A1): daily, seven slots, whole disk. These are for disaster
   recovery, not for restoring a single file.
2. **`deploy/hetzner/backup-db.sh`**: makes a consistent online copy using SQLite's backup API inside the
   container, runs an integrity check, then gzips the copy and encrypts it with `age` when `AGE_RECIPIENT` is
   set. It keeps the newest 14 copies.

   ```bash
   age-keygen -o ~/btf-backup-key.txt     # do this on YOUR machine; keep the private key off the server
   # on the server:
   AGE_RECIPIENT=age1... /opt/banthefake/deploy/hetzner/backup-db.sh
   crontab -e
   # 17 3 * * * AGE_RECIPIENT=age1... /opt/banthefake/deploy/hetzner/backup-db.sh >>/var/log/btf-backup.log 2>&1
   ```

3. **Off-site copy**: push `/var/backups/banthefake` somewhere else, for example a Hetzner Storage Box in
   another location via `restic` or `rclone`, or pull it from your own machine with `rsync`. Backups contain
   personal data: encrypt them, restrict access, and delete old ones in line with the privacy notice. An
   erased signature stays in older backups until they rotate out, so say so in the notice or keep retention
   short.

**Restore:**

Decrypt and decompress on a trusted admin machine, never on the server that stores the encrypted backups.
Create the plaintext under `umask 077`, then transfer it with mode 0600. For an encrypted backup:

```bash
umask 077
age -d -i ~/btf-backup-key.txt banthefake-XXXX.db.gz.age > restore.db.gz
gunzip restore.db.gz
chmod 600 restore.db
scp -p restore.db root@SERVER:/opt/banthefake/deploy/hetzner/restore.db
```

If `AGE_RECIPIENT` was not set when the backup ran, start from the unencrypted
`banthefake-XXXX.db.gz` instead:

```bash
umask 077
gunzip -c banthefake-XXXX.db.gz > restore.db
chmod 600 restore.db
scp -p restore.db root@SERVER:/opt/banthefake/deploy/hetzner/restore.db
```

Then, on the server, preserve mode 0600 and stop the app before replacing the live database:

```bash
cd /opt/banthefake/deploy/hetzner
chmod 600 restore.db
docker compose stop app
docker compose cp restore.db app:/data/banthefake.db   # works on a stopped container; lands root-owned
docker compose run --rm --no-deps --user root app chown 10001:10001 /data/banthefake.db
docker compose start app
rm restore.db
```

Test a restore once before launch.

### A8. Updates and restarts

```bash
cd /opt/banthefake
git fetch && git checkout <new-commit-or-tag-that-passed-CI>
cd deploy/hetzner
docker compose build app            # the old container keeps serving while this runs
docker compose up -d app            # replace it: a few seconds of downtime plus model load
docker compose ps && curl -fsS https://YOUR-DOMAIN/api/health
docker image prune -f
```

- **There is no true rolling restart.** One process owns the volume, the scan slots and the rate limiter, so
  never run `--scale app=2`. During the swap, Caddy holds incoming requests and retries for up to 30 s
  (`lb_try_duration`), so most visitors see a slow response rather than an error. Scans that are in flight
  when the old container stops are lost, and clients get an error and can retry.
- **Every restart resets the in-memory rate limiter.** Counters start from zero, so a restart briefly gives
  everyone a fresh quota. This is acceptable for deploys, but do not restart in a loop to clear a block, and
  do not run a second app container "for zero downtime": it would double every limit.
- **Rollback:** `deploy.sh` captures the immutable image IDs used by both running containers before an update
  and restores those exact app and Caddy images if the update fails. For a manual rollback, deploy the prior
  release tag with `./deploy.sh <previous-tag>`.
- **Caddy:** `docker compose pull caddy && docker compose up -d caddy`.
- **TODO (requires upstream verification):** pin `caddy:2` to a verified image digest in the Compose file.
  This checkout was repaired offline, so no digest is guessed here. Verify the digest against Caddy's
  official image publication before pinning it. Until then, `deploy.sh` preserves the running Caddy image ID
  alongside the app image so rollback cannot select a different image behind the mutable tag.

---

## Option B: Fly.io

Install `flyctl` (see Fly's docs) and run `fly auth login`. Every command below runs from the repository
root, where `fly.toml` is.

### B1. Name the app and region

Edit `fly.toml`. Set `app` to a unique name and `primary_region` close to your audience (`fra`, `ams`, `iad`,
and so on). Check the region's Machine price, because the listed prices are for iad/ewr.

### B2. Create the app and the volume

```bash
fly apps create banthefake-CHANGE-ME
fly volumes create btf_data --app banthefake-CHANGE-ME --region fra --size 1 --snapshot-retention 14
```

`--snapshot-retention` sets daily snapshot retention (1 to 60 days, default 5). Check
`fly volumes create --help` if your flyctl version rejects the flag. 1 GB holds many thousands of
signatures, and volumes can be extended later but not shrunk.

### B3. Make the volume writable by the app user

A new Fly volume is mounted as a root-owned filesystem, so the image's non-root user (uid 10001) cannot
create the database. The app then fails at startup. Fix the ownership once with a throwaway Machine:

```bash
fly machine run busybox:stable chown 10001:10001 /data \
  --app banthefake-CHANGE-ME --region fra --volume btf_data:/data --restart no --rm
```

Wait until the Machine has exited and been destroyed (`fly machine list`), so the volume is free again. The
ownership is stored on the volume and survives deploys.

### B4. Secrets and first deploy

```bash
fly secrets set --stage BTF_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --app banthefake-CHANGE-ME
fly secrets set --stage BTF_PRIVACY_CONTACT='privacy@your-domain.org' --app banthefake-CHANGE-ME
# store the token in your password manager first, or set it from a value you already saved
fly deploy --ha=false
fly scale count 1 --app banthefake-CHANGE-ME     # must stay 1: in-memory rate limiter + one volume
fly status
curl -fsS https://banthefake-CHANGE-ME.fly.dev/api/health
```

Fly's remote builder builds the Dockerfile, including the BuildKit cache mounts. `--ha=false` stops Fly from
creating a second Machine. `fly.toml` keeps the Machine running (`auto_stop_machines = "off"`,
`min_machines_running = 1`), so visitors never wait for a cold model load.

### B5. Verify `Fly-Client-IP`

`fly.toml` sets `BTF_CLIENT_IP_HEADER=Fly-Client-IP`, so the application uses Fly's edge-set client address
for scan, petition, and failed-admin-authentication limits. The app accepts the value only when it parses as
exactly one IPv4 or IPv6 address; an absent or malformed value falls back to the socket peer. This avoids any
dependency on a guessed proxy CIDR or on Uvicorn's `X-Forwarded-For` selection.

Run the A6 rate-limit test through the Fly hostname, then try from a second network within the same minute.
The second network must have a fresh bucket. Do not set `BTF_CLIENT_IP_HEADER` to a header that an internet
client can pass through unchanged.

### B6. Custom domain and TLS

```bash
fly certs add petition.example.org --app banthefake-CHANGE-ME
fly certs show petition.example.org --app banthefake-CHANGE-ME   # shows the DNS records to create
```

Create the records shown (A/AAAA, or a CNAME to `banthefake-CHANGE-ME.fly.dev`). Fly issues and renews the
certificate. `force_https = true` redirects HTTP to HTTPS. `BTF_HSTS=on` makes the app emit
`Strict-Transport-Security: max-age=31536000`; there is no second HSTS source on Fly.

### B7. Backups

- **Fly volume snapshots** are daily, with the retention set in B2. Fly itself warns that a snapshot may not
  contain the very latest writes. Restore one with `fly volumes create --snapshot-id ...`.
- **Your own copies:**

  ```bash
  fly ssh console --app banthefake-CHANGE-ME -C "python -c \"import sqlite3; s=sqlite3.connect('file:/data/banthefake.db?mode=ro', uri=True); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); print('ok')\""
  fly ssh sftp get /data/backup.db ./banthefake-$(date -u +%Y%m%d).db --app banthefake-CHANGE-ME
  fly ssh console --app banthefake-CHANGE-ME -C "rm /data/backup.db"
  age -r age1... -o banthefake-$(date -u +%Y%m%d).db.age banthefake-$(date -u +%Y%m%d).db && rm banthefake-*.db
  ```

  Run this on a schedule from a machine you control.

### B8. Updates and restarts

`fly deploy`. With one Machine and one volume, Fly replaces the Machine in place, so expect a short outage
while the new Machine boots and loads the models. As in Option A, every deploy or restart resets the
in-memory rate limiter. Do not add Machines to get zero downtime. Roll back with
`fly deploy --image <previous image ref from fly releases --image>`.

Machine size changes (`[[vm]]` in `fly.toml`) apply on the next `fly deploy`. The shipped 4 GB profile uses
one parser worker. For concurrency 2, set `BTF_SCAN_CONCURRENCY = "2"` and `memory = "8gb"`. To get dedicated
CPUs, select a performance Machine size with enough memory for the same profile.

---

## Operations (both options)

### Health checks

`GET /api/health` returns `{"ok": true, "model": ..., "c2pa": ...}` as soon as the server accepts requests.
The `model` field moves from `loading` to `loaded`. `degraded` means one of the two detectors failed to load:
results can then be "possible" at most, so treat it as an alert. `unavailable` means the ML extra is missing,
which is wrong for this image.

- Option A: the compose healthcheck reports `healthy` only when `model` is `loaded` (or `off`). See it with
  `docker compose ps`.
- Option B: the Fly check only looks at HTTP 200. After each deploy, run
  `curl -fsS https://YOUR-DOMAIN/api/health | grep '"model":"loaded"'`.
- Add an external uptime monitor on `https://YOUR-DOMAIN/api/health` that alerts on non-200 responses and,
  if it supports it, on a missing `"model":"loaded"`.

### Logs and privacy

- The image runs uvicorn with `--no-access-log`, so the app never logs client IPs. Uploads are never logged.
- Caddy writes no access log unless you add a `log` directive. Do not add one without updating
  `app/templates/privacy.html`.
- Docker logs are rotated (3 × 10 MB per container). On Fly, platform logs (`fly logs`) are kept by Fly. The
  app writes no visitor data to them, but name your hosting provider in the privacy notice in either case.
- The host provider and any CDN or tunnel you put in front terminate TLS. Under the privacy notice they are
  processors: say who they are.

### Updating the C2PA trust list

The trust files are baked into the image (`app/trust/`). The bundled list's next-update date is
**2027-01-21**, so set a reminder.

1. Follow [README "Updating the C2PA trust list"](../README.md#updating-the-c2pa-trust-list): replace the
   three files, update `app/trust/VERSION` and `app/trust/README.md`, run both test suites, and commit.
2. Wait for CI to pass, then deploy that commit (A8 or B8). A restart alone does not pick up new trust files.

### Rotating `BTF_ADMIN_TOKEN`

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Option A
cd /opt/banthefake/deploy/hetzner && nano .env && docker compose up -d app   # recreates the container
# Option B
fly secrets set BTF_ADMIN_TOKEN='NEW-TOKEN' --app banthefake-CHANGE-ME          # restarts the Machine
```

The old token stops working as soon as the restart finishes. The restart also resets the rate limiter. Rotate
after anyone with access leaves, after the token has appeared in a terminal recording, ticket or chat, and
otherwise on a regular schedule. Check the new token with an export
(`curl -fsS -H "Authorization: Bearer $NEW" https://YOUR-DOMAIN/api/petition/export.csv -o /dev/null`).

## Pre-launch checklist

- [ ] **Privacy contact:** `BTF_PRIVACY_CONTACT` is a real, monitored inbox (or `https://` contact page) and
      `BTF_ENV=production` is set, so the app refuses to start without it. `https://YOUR-DOMAIN/privacy.html`
      shows your contact twice and no "Contact not configured" notice. The page
      (`app/templates/privacy.html`) names your host (Hetzner or Fly.io), your backup location and retention,
      and any CDN.
- [ ] **Real domain:** DNS A/AAAA (or the Fly certificate records) point at the deployment. HTTPS works, with
      HTTP redirecting to HTTPS. `BTF_DOMAIN` (Option A) or `fly certs` (Option B) uses the real name, and no
      `example.org` or `CHANGE-ME` values are left.
- [ ] **CI green on the exact commit you deploy:** `gh run list --branch main --limit 5`, or the Actions tab.
      All three jobs (`test`, `test-ml`, `docker`) pass. The `docker` job is the same image smoke test that
      production depends on.
- [ ] **`BTF_ADMIN_TOKEN`** is at least 24 random characters, stored in a password manager, and works for an
      export. With a short or missing token, admin endpoints return 404 and GDPR erasure is impossible.
- [ ] **Client IP:** Option A's `FORWARDED_ALLOW_IPS` passes A6; Option B's `BTF_CLIENT_IP_HEADER` passes B5.
      Rate limits are per visitor, and a client-supplied forwarding header cannot reset a bucket.
- [ ] **Single instance:** one container (`docker compose ps`) or one Machine (`fly scale show`). The image
      starts uvicorn with `--workers 1`.
- [ ] **Persistence:** sign the petition once, restart the app, and the count is unchanged. Delete the test
      signature with the admin `DELETE /api/petition` call.
- [ ] **Backups:** automatic backups run, a copy exists off-site, and a restore has been tested once.
- [ ] **Health:** `/api/health` shows `"model":"loaded"` and `"c2pa":true`. An uptime monitor is set up.
- [ ] **Smoke test:** scan a known AI image (for example `tests/fixtures/ai_dalle_mars.jpg`) and a camera
      photo through the public URL, and test that a 50 MB upload is accepted and a larger one gets 413.
- [ ] **Memory:** after a few scans, including a video, `docker stats` (A) or Fly metrics (B) show headroom
      under the configured limit. The 4 GB profile uses concurrency 1; concurrency 2 uses the 8 GB profile.
- [ ] **C2PA trust list** is current (`app/trust/VERSION`), with a reminder set for the next-update date.
- [ ] **Firewall** (Option A): only 22, 80 and 443 (TCP and UDP) are open, and port 8000 is not reachable from
      outside (`curl http://SERVER_IP:8000` from your machine must fail).

## Releases (manual tagging)

There is no release automation: a release is an annotated git tag on a commit whose CI run is green.
To cut `v1.0.0` (not created yet):

```bash
git checkout main && git pull --ff-only
git status                                  # clean
gh run list --branch main --limit 5         # test, test-ml and docker all green for this exact commit
git log -1 --format='%H %s'                 # the commit you are tagging
git tag -a v1.0.0 -m "BanTheFake v1.0.0"    # add -s to sign it if you have a signing key set up
git push origin v1.0.0
```

Then deploy it with `./deploy.sh v1.0.0` (or set `BTF_REF=v1.0.0` in `cloud-init.yaml` for a new server).
Tags are immutable by convention: never move or re-push a published tag; fix forward with `v1.0.1`. Use
`vMAJOR.MINOR.PATCH`. Optionally write release notes on GitHub (`gh release create v1.0.0 --notes ...`).
If a release is bad, roll back by deploying the previous tag: `./deploy.sh <previous-tag>`.

## Sources (read 2026-10-31)

- Docker Engine on Ubuntu (official apt repository, `docker.sources`, conflicting packages, ufw caveat):
  <https://docs.docker.com/engine/install/ubuntu/>

- Hetzner Cloud overview, plan families and starting prices: <https://www.hetzner.com/cloud/>
- Hetzner Regular Performance (CPX) plans, traffic, IPv4 included: <https://www.hetzner.com/cloud/regular-performance/>
- Hetzner Cost-Optimized (CX/CAX) plans, backups "starting at €1.1980", snapshots €0.0143/GB:
  <https://www.hetzner.com/cloud/cost-optimized/>
- Hetzner backups (daily, seven slots): <https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/>
- Fly.io pricing (Machines, RAM, volumes, snapshots, egress, IPv4, certificates):
  <https://docs.fly.io/about/pricing>
- Fly.io CPU quotas (shared 6.25% baseline, burst): <https://docs.fly.io/machines/cpu-performance>
- Fly.io volumes (one Machine per volume, daily snapshots, downtime on deploy): <https://docs.fly.io/volumes/overview>
- Render pricing (instance types, persistent disk $0.25/GB, bandwidth): <https://render.com/pricing>
- Render disks (no zero-downtime deploys, no scaling, daily snapshots): <https://render.com/docs/disks>
- Railway pricing (plans, per-second rates, volumes, egress): <https://railway.com/pricing>
- Railway volumes (no replicas, redeploy downtime): <https://docs.railway.com/reference/volumes>
- DigitalOcean Droplet pricing and backups: <https://www.digitalocean.com/pricing/droplets>
- DigitalOcean bandwidth overage ($0.01/GiB): <https://docs.digitalocean.com/platform/billing/bandwidth/>
- DigitalOcean App Platform limits (no volumes): <https://docs.digitalocean.com/products/app-platform/details/limits/>
- Google Cloud Run pricing (Tier 1, instance- and request-based billing, free tier): <https://cloud.google.com/run/pricing>
