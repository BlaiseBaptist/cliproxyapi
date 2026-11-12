# CLIProxyAPI deployment

> This is a personal setup for one machine, published so others can borrow from
> it. It is not supported: there are no guarantees, and issues and requests are
> unlikely to get a response.

Our Docker deployment settings and local edge proxy for CLIProxyAPI. The API
server binary comes from `eceasy/cli-proxy-api`; this repository contains our
configuration and customizations, rather than an upstream source fork.

## What's tracked

- `docker-compose.yml`: upstream API plus a Python edge proxy on loopback ports
  8317 and 8319, plus a read-only traffic viewer on loopback port 8320.
- `config.example.yaml`: our active settings with credentials replaced and
  comments removed. The live `config.yaml` remains private.
- `edge/edge.py`: preserves streaming responses, converts quota cooldown errors
  to `usage_limit_reached`, normalizes bare `cyber_policy` SSE errors, and records
  request/response traffic in a local SQLite database.
- `panel-patch.py` and `prepare-panel.sh`: reproduce the pinned management panel
  with these changes: the sponsor Quick Start navigation is removed; quota
  cards load on their own instead of behind a click; and the Quota windows
  timeline draws recorded windows instead of a fixed grid extrapolated from
  the current one. The downloaded bundle stays local; the source release is
  linked in the download script.
- The edge sends two management routes to cliproxy-dash (`DASH_UPSTREAM`):
  `/v8/management/quota-history` (the recorded windows) and quota GETs on
  `/v8/management/requests/api-call` (served from its shared cache, so loading
  the panel does not hit Anthropic or OpenAI). Everything else goes to the API.
- `login.sh`: interactive Claude and Codex account login helper.
- `ui/`: read-only web UI for live request bodies and imported historical
  conversation records. Fixture checks: `python3 ui/test_app.py`.

Credentials, auth files, databases, request bodies, logs, backups, downloaded
panels and unknown files are excluded through an explicit Git allowlist.

## Setup

Requires Docker Compose, Python 3, Bash, curl, and sha256sum.

```sh
cp config.example.yaml config.yaml
chmod 600 config.yaml
mkdir -p auths logs data
chmod 700 auths logs data
```

Replace both `CHANGE_ME` values in `config.yaml` with independent random secrets
before starting. `management.secret-key` protects the management API;
`access.api-keys` protects client access. Never commit the populated config.
The edge container runs as UID/GID 1000; ensure it owns `data/`, or adjust its
`user` setting to the deployment account.

```sh
./prepare-panel.sh
docker compose up -d
./login.sh codex   # or: ./login.sh claude
```

The main API has no published host port. Both loopback ports route through the
edge. Use a trusted authenticated ingress if remote access is needed; this
repository does not publish hostnames or ingress credentials. Management remote
access is enabled for the internal Docker network and requires its secret.

The edge records full request and response bodies in `data/requests.sqlite3`.
Selected credential headers are redacted; bodies can still contain sensitive
content. Keep `data/` private and manage its retention separately. This data and
all account login files remain outside Git.

## How Docker tracks this repository

The API uses `eceasy/cli-proxy-api:latest`, not a local build. Docker does not
rebuild or redeploy on Git changes. It bind-mounts `config.yaml`, `auths/`,
`logs/`, and `static/management.html` from this checkout. The edge uses
`python:3-alpine` and bind-mounts `edge/` and `data/`. The traffic viewer uses
the same image with `ui/` and `data/` mounts, opens SQLite read-only, and has no
write endpoints. Its body downloads retain the complete stored content.

- Restart the edge to load Python edits: `docker compose restart cliproxy-edge`.
- Restart the viewer after UI edits: `docker compose restart agent-traffic-ui`.
- Restart the API after config edits: `docker compose restart cliproxyapi`.
- Recreate containers after Compose or mount changes: `docker compose up -d`.
- Update the API image deliberately with `docker compose pull cliproxyapi`
  followed by `docker compose up -d cliproxyapi`.

The existing host manages this deployment via an enclosing Compose file that
includes this `docker-compose.yml`. On that host, run those commands using
`docker compose -f ../compose.yml` to preserve its project and network.

At initial publication, the installed API reports version `v8.0.32`, upstream
commit `911f49c`, built 2026-10-16. Its pulled image digest is:

```
eceasy/cli-proxy-api@sha256:68f6e2d691f7b71aca21cfe87c77b7076cbea26c8ef490b15619b1623a7c0699
```

The Compose and login helper retain the deployment's `latest` tag, which can
move. For a reproducible deployment, use the same reviewed digest in both files.
The panel is separately pinned to management-center release `v1.26.5`, with its
SHA-256 checked before patching; automatic panel updates are disabled.

To publish a settings change, update the sanitized example separately from the
private config. Review `git diff --cached` before each commit; never force-add
private files.
