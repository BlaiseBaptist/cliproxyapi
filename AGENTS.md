# AGENTS.md

This repo holds the CLIProxyAPI deployment on one machine: Compose and config,
an edge proxy (`edge/`), a read-only traffic viewer (`ui/`), and a patched
management panel (`panel-patch.py`). The README explains what each file does.

## Public repo

- Never commit secrets or runtime state. That includes `config.yaml`, `auths/`,
  `data/`, `logs/`, `static/`, `api-key`, `management-key`, and backups.
- `.gitignore` is an allowlist. To add a tracked file, add a `!/path` entry for
  it, and never force-add files.
- `config.example.yaml` is the sanitized copy of the live config. When a setting
  changes, update it by hand and keep `CHANGE_ME` placeholders for secrets.
- Review `git diff --cached` before every commit.

## Committing

- Commit your own work when it is done and the checks pass.

## Code

- Use the Python standard library only, and no build step for `ui/`.
- Each `panel-patch.py` substitution must match exactly once, so the patch fails
  loudly when the bundle changes.
- When behavior changes, update its short README description in the same change.

## Checks and deploy

- Run `python3 ui/test_app.py` and `python3 ui/test_migrate.py`.
- Run Compose through the parent project:
  `docker compose -f ../compose.yml ...`.
- You may restart services after edits: `cliproxy-edge` for `edge/`,
  `agent-traffic-ui` for `ui/`, and `cliproxyapi` for config.
- Ask before running migrations, pulling images, or touching anything in `data/`.
