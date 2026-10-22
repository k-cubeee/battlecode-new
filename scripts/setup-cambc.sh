#!/usr/bin/env bash
# Install the cambc CLI and pre-seed the C++ toolchain it would otherwise download.
#
# On its first build cambc downloads the WASI SDK, CMake and Ninja from GitHub.
# Its HTTP client ships its own root certificates and ignores SSL_CERT_FILE, so
# behind a TLS-intercepting proxy (such as the Claude Code cloud sandbox) those
# downloads fail with "invalid peer certificate: UnknownIssuer". This script
# fetches the same archives with curl, checks them against the SHA-256 hashes
# pinned in cambc, and unpacks them where cambc looks. cambc skips any tool it
# already finds, so running this on a normal machine is harmless.
#
# The tool versions and hashes below belong to cambc 3.0.4; update them
# together when bumping CAMBC_VERSION.
set -euo pipefail

CAMBC_VERSION=3.0.4
cache="${CAMBC_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/cambc}"

python3 -m pip install --quiet --upgrade "cambc==$CAMBC_VERSION"

# Download, verify and unpack one archive into $cache/<name>.
install_tool() {
    local name=$1 url=$2 sha256=$3
    if [[ -e "$cache/$name" ]]; then
        echo "$name: already installed"
        return
    fi
    mkdir -p "$cache"
    local tmp
    tmp=$(mktemp -d "$cache/.setup-$name.XXXXXX")
    echo "$name: downloading $url"
    curl -fsSL -o "$tmp/archive" "$url"
    echo "$sha256  $tmp/archive" | sha256sum --check --quiet -
    mkdir "$tmp/out"
    case "$url" in
        *.zip) unzip -q "$tmp/archive" -d "$tmp/out" ;;
        *) tar -xzf "$tmp/archive" -C "$tmp/out" --strip-components=1 ;;
    esac
    mv "$tmp/out" "$cache/$name"
    rm -rf "$tmp"
}

install_tool wasi-sdk-33 \
    https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-33/wasi-sdk-33.0-x86_64-linux.tar.gz \
    0ba8b5bfaeb2adf3f29bab5841d76cf5318ab8e1642ea195f88baba1abd47bce
install_tool cmake-3.31.8-linux-x86_64 \
    https://github.com/Kitware/CMake/releases/download/v3.31.8/cmake-3.31.8-linux-x86_64.tar.gz \
    630615d8e98ac33eba7fbe472626dff5c899c85af3c024585ae109166a6909d0
install_tool ninja-1.12.1-linux-x86_64 \
    https://github.com/ninja-build/ninja/releases/download/v1.12.1/ninja-linux.zip \
    6f98805688d19672bd699fbbfa2c2cf0fc054ac3df1f0e6a47664d963d530255

cambc --version
