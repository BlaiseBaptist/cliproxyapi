#!/usr/bin/env bash
# Add a Claude or Codex subscription login; credentials remain in auths/.
# Claude callback: paste the full localhost callback URL when prompted.
set -euo pipefail
provider=${1:?usage: $0 claude|codex}
dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case $provider in
  claude) flag=-claude-login ;;
  codex)  flag=-codex-device-login ;;
  *) echo "unknown provider: $provider" >&2; exit 2 ;;
esac

docker run --rm -it --network host \
  -v "$dir/config.yaml:/CLIProxyAPI/config.yaml:ro" \
  -v "$dir/auths:/root/.cli-proxy-api" \
  eceasy/cli-proxy-api:latest ./CLIProxyAPI "$flag" -no-browser
ls -1 "$dir/auths"
