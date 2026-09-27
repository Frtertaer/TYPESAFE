#!/usr/bin/env bash
# Install the typesafe CLI from a GitHub release (default) or PyPI.
#
#   curl -fsSL https://github.com/maddygoround/typesafeai-cli/releases/latest/download/install.sh | bash
#   TYPESAFE_CLI_VERSION=v0.3.1 ./install.sh
#   TYPESAFE_CLI_FROM=pypi ./install.sh
#
# Env:
#   TYPESAFE_CLI_VERSION   tag like v0.2.0, or "latest" (default)
#   TYPESAFE_CLI_FROM      github (default) or pypi
set -euo pipefail

REPO="${TYPESAFE_CLI_REPO:-maddygoround/typesafeai-cli}"
PKG="${TYPESAFE_CLI_PKG:-typesafeai-cli}"
BIN="${TYPESAFE_CLI_BIN:-typesafe}"
VERSION="${TYPESAFE_CLI_VERSION:-latest}"
FROM="${TYPESAFE_CLI_FROM:-github}"
API="https://api.github.com/repos/${REPO}"

say() { printf '%s\n' "$*" >&2; }
die() { say "install.sh: $*"; exit 1; }

need_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    die "python3 is required (3.10+)"
  fi
  python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"python 3.10+ required, found {sys.version.split()[0]}")
PY
}

resolve_tag() {
  if [ "$VERSION" != "latest" ]; then
    case "$VERSION" in
      v*) printf '%s\n' "$VERSION" ;;
      *) printf 'v%s\n' "$VERSION" ;;
    esac
    return
  fi
  python3 - <<PY
import json, urllib.request
url = "${API}/releases/latest"
req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "typesafe-install"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.load(resp)
tag = data.get("tag_name") or ""
if not tag:
    raise SystemExit("could not resolve latest GitHub release")
print(tag)
PY
}

wheel_url_for_tag() {
  local tag="$1"
  python3 - <<PY
import json, urllib.request
url = "${API}/releases/tags/${tag}"
req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "typesafe-install"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.load(resp)
for asset in data.get("assets") or []:
    name = asset.get("name") or ""
    if name.endswith(".whl"):
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit("no wheel on GitHub release ${tag}")
PY
}

have() { command -v "$1" >/dev/null 2>&1; }

install_spec() {
  local spec="$1"
  if have pipx; then
    pipx install --force "$spec"
  elif have uv; then
    uv tool install --force "$spec"
  else
    say "pipx/uv not found; using python3 -m pip --user"
    python3 -m pip install --user --upgrade "$spec"
  fi
}

main() {
  need_python
  if [ "$FROM" = "pypi" ]; then
    local spec
    if [ "$VERSION" = "latest" ]; then
      spec="$PKG"
    else
      spec="${PKG}==${VERSION#v}"
    fi
    say "installing ${spec} from PyPI"
    install_spec "$spec"
  else
    local tag url tmp
    tag="$(resolve_tag)"
    say "using GitHub release ${tag}"
    url="$(wheel_url_for_tag "$tag")"
    say "wheel ${url}"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    python3 - <<PY
import urllib.request
urllib.request.urlretrieve("${url}", "${tmp}/pkg.whl")
PY
    install_spec "${tmp}/pkg.whl"
  fi

  if have "$BIN"; then
    say "ok: $(command -v "$BIN")"
  else
    say "installed the package, but '${BIN}' is not on PATH."
    say "add ~/.local/bin to PATH, then run: ${BIN} --help"
  fi
}

main "$@"
