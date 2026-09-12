# Phase 7 — production packaging and recovery

Implementation and native acceptance: 2026-09-12. **Container acceptance is still
pending: this Mac has no Docker/Podman/Colima runtime.** Do not equate a Dockerfile or
green native tests with a successful image build. No server deployment, live Anki
mutation, external backup upload, public endpoint, or CI run was performed.

Final native verification: **601 backend tests passed**, **8 frontend tests passed**;
both normal and `VITE_HIDE_LEGACY=true` frontend builds passed. The production process
test covers startup backup, rejection of another process, graceful SIGTERM, restart,
and unchanged non-empty review/FSRS state. Backup failure redaction and cancellation
of collection open are covered. These results do not replace container acceptance.

## What is implemented

- Multi-stage Dockerfile: Python 3.12.13, Node 25.3.0 (build only), uv 0.11.29;
  verified multi-platform image digests, frozen Python lock and `npm ci` locks.
  Anki and downloaded AQT assets remain 25.9.4. Runtime has no Node/Qt app.
- Non-root UID/GID 10001, read-only application root, dropped capabilities, bounded
  writable `/tmp`, persistent `/data/anki` and `/data/backups`, 150-second stop grace.
  `.dockerignore` excludes study collections, media outside source, exports and secrets.
- One production process with one collection owner. A lifetime OS advisory lock is
  held through export/reopen and released on close/process exit. A second process
  fails before opening Anki. Use a **local filesystem**, not NFS/network locks;
  never share this collection with desktop Anki or another program ignoring the lock.
- `python -m ankiweb.production` validates authentication, secure cookies, exact
  hosts, HTTPS origins and the exact source-commit URL before serving. It never
  starts AnkiConnect and does not register legacy RPC, WebSocket/admin or docs routes.
  Development `python -m ankiweb` retains the upstream local behavior.
- Auth/CSRF, strict production Origin checks (including login), 2 MiB streamed
  request limit, no access logging, HSTS; a visible Source/About link. Legacy
  settings link is omitted from the Docker-built product UI.
- `/healthz` exposes only `ok`, exercises the real collection backend and reports
  503 if backups fail/become overdue or either data volume has <256 MiB free.
  `/api/health` remains authenticated and includes pinned scheduler metadata.

## Daily backups

Backups run inside the application process, under the **same adapter mutation lock**
as reviews/imports, not from a second Anki worker or cron process. The first backup
is immediate; later ones run after 24 hours. Restart reuses the most recent valid
daily backup. Failed exports retry after five minutes and mark health unhealthy.
Log events contain only `backup_complete` or `backup_failed`, not card text/secrets.

Each private bundle contains:

- official `collection.colpkg`, including scheduling and media;
- content-hashed audit snapshot for post-restore verification;
- lesson-import receipts;
- a final completion manifest with SHA-256 checksums.

Incomplete or corrupt bundles do not count as successful backups. **All successful
daily backups are retained**, exceeding the minimum 30-day retention after warm-up;
there is no automatic deletion/pruning. Configure disk alerts, and separately approve
any future pruning. Copy completed bundles to independent/off-host encrypted storage
before relying on this for hardware/disaster recovery. A second volume on the same
disk is not off-site protection; no destination/provider has been selected yet.

Export closes/reopens the owned Anki handle and invalidates in-flight review tokens.
If a backup happens during review, the client may need to reload the card; a stale
answer is never silently accepted or rescheduled.

Auth sessions/secrets are deliberately **not** in the study backup. The auth sidecar
is persistent at `/data/anki/auth.sqlite3`; keep secrets in the hosting secret store.
Rotate `ANKIWEB_APP_SECRET` at recovery cutover to invalidate pre-recovery sessions.

## Restore without overwriting the current collection

Always stop the app first. The lock refuses offline maintenance while it is running.
The restore target must be in a **new directory**, never an existing profile/folder.
The old collection is preserved, and a full pre-restore backup is mandatory before
import. A failed state comparison leaves the new target inactive with a failure report.

Run from the fork root (or equivalently inside an offline maintenance container with
both volumes mounted; paths here are examples, not commands already executed):

```sh
python tools/maintenance.py backup --collection /data/anki/current/collection.anki2 --backups /data/backups --dry-run
python tools/maintenance.py backup --collection /data/anki/current/collection.anki2 --backups /data/backups --apply

python tools/maintenance.py restore --collection /data/anki/current/collection.anki2 --backups /data/backups --bundle /data/backups/CHOSEN-BUNDLE --target /data/anki/restored-NEW/collection.anki2 --dry-run
python tools/maintenance.py restore --collection /data/anki/current/collection.anki2 --backups /data/backups --bundle /data/backups/CHOSEN-BUNDLE --target /data/anki/restored-NEW/collection.anki2 --apply --sha256 SHA-FROM-PREVIEW --confirm-target /data/anki/restored-NEW/collection.anki2
```

Only after `restore-report.json` passes: approve cutover, change
`ANKIWEB_COLLECTION_PATH` to the new collection, rotate the app secret and start one
worker. The tool does **not** change deployment config or activate the restored copy.
Rollback is an explicit switch back to the preserved old path, never file deletion.
If the current collection is corrupt and cannot be exported, this conservative tool
stops; preserve the damaged files and request a separate recovery plan rather than
silently bypassing the pre-restore backup requirement.

Executed local drill: `data/phase7-backup-drill/` → `data/phase7-restored/`, with
`data/phase7-pre-restore/` as rollback backup. All 87 copied cards, limits and FSRS
matched after restore; the original desktop was not accessed or changed in Phase 7.
Synthetic tests additionally cover non-empty review history, FSRS memory and media.

## Docker / Coolify configuration (not deployed)

1. Use this fork as build context and its `Dockerfile`. No data belongs in the build
   context or repository. Keep `LICENSE`, `LICENSES`, third-party notices and source pins.
2. Set the values listed in `.env.example` in the hosting secret store. Generate the
   Argon2id hash with `python -m ankiweb.auth hash-password`; generate the independent
   app secret privately. Never paste real secrets into logs or commit `.env`.
   Compose `.env` hashes need single quotes so `$` characters stay literal.
3. Run **one replica**, no autoscaling/rolling overlap. Stop the old owner before
   starting the replacement. Keep the two named/bind volumes between releases;
   never run `docker compose down -v` on real data. Bind mounts need UID/GID 10001
   write access, including imported media and SQLite sidecars.
4. In Coolify, expose internal port 8000 only to its HTTPS proxy, set the real domain,
   enforce TLS and redirect HTTP to HTTPS. Do not publish AnkiConnect or raw legacy
   routes. For host-managed Compose the example publishes **127.0.0.1 only**; connect
   an HTTPS reverse proxy explicitly. No automatic firewall/DNS changes are made.
5. Forward Host and Origin unchanged, use request-size/time limits at the proxy too.
   Proxy-header trust is disabled: no arbitrary client can spoof trusted forwarding
   headers. Login rate limits therefore share the proxy address (acceptable for this
   single-user app); enabling trusted proxy IP handling needs an exact allow-list.
6. Set `ANKIWEB_SOURCE_URL` to the public source tree/archive for the exact 40-character
   deployed commit, verify anonymous access and complete build sources. Merely passing
   the URL validation is not proof of AGPL source availability. Stage 8 verifies it.
7. Monitor `/healthz`, disk usage, backup failure logs and off-host backup delivery.

Docker references: [Compose services](https://docs.docker.com/reference/compose-file/services/),
[uv Docker builds](https://docs.astral.sh/uv/guides/integration/docker/).

## Remaining acceptance gate

On a Docker-enabled machine, build the image and check read-only/non-root runtime,
persistent volumes, first backup, restart, and rejection of a duplicate owner. The
manual-only `.github/workflows/container-check.yml` automates these checks on disposable
CI volumes and **does not deploy anything**. It has been written, not run. Also rerun
the restore drill inside that built container before server migration approval.

Native verification commands:

```sh
.venv/bin/pytest -q
npm --prefix web run build
npm --prefix web test
```

Stage 7 is not marked fully accepted until the Docker build/runtime gate is green.
Installing a container runtime or using another machine needs the user's direction.
