#!/usr/bin/env bash
# Install what scripts/collect-minio.sh needs on macOS.
#
# Two things here are not obvious and cost an afternoon to rediscover:
#
#  1. libcypher-parser has no Homebrew formula, and the libcypher-parser-sys
#     crate does not vendor it: it probes pkg-config and links statically. On
#     Debian the header lands in /usr/include so it works by accident. Here it
#     must be built. Installed to ~/.local, so removing it is one rm -rf.
#
#  2. That crate's build.rs passes pkg-config's *link* flags to the linker but
#     never gives the include path to bindgen, so the build fails with
#     "'cypher-parser.h' file not found" even when pkg-config resolves fine.
#     BINDGEN_EXTRA_CLANG_ARGS is the fix; export it before building.
#
# Docker is required because the engine's benchmark starts its own MinIO
# container. Colima is installed rather than Docker Desktop since it needs no
# GUI. Note MinIO's own Homebrew build segfaults on macOS 26 (a cgo crash in
# go-m1cpu), which is why the container image is used instead of a local binary.
set -euo pipefail

PREFIX="${HYDRADB_BENCH_PREFIX:-$HOME/.local}"
SRC="${TMPDIR:-/tmp}/libcypher-parser-build"

command -v brew >/dev/null || { echo "Homebrew required" >&2; exit 1; }

echo "==> Homebrew packages"
brew install rustup suite-sparse cmake pkg-config \
             autoconf automake libtool peg colima docker

echo "==> Rust toolchain"
export PATH="/opt/homebrew/opt/rustup/bin:$PATH"
rustup default stable

if pkg-config --exists cypher-parser 2>/dev/null || \
   PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" pkg-config --exists cypher-parser 2>/dev/null; then
  echo "==> libcypher-parser already present, skipping"
else
  echo "==> Building libcypher-parser into $PREFIX"
  export PATH="/opt/homebrew/opt/libtool/libexec/gnubin:$PATH"
  rm -rf "$SRC"
  git clone --depth 1 https://github.com/cleishm/libcypher-parser.git "$SRC"
  cd "$SRC"
  ./autogen.sh
  # -Wno-error: the 2021 release trips newer clang's default warning set.
  ./configure --prefix="$PREFIX" --disable-shared --enable-static \
              CFLAGS="-Wno-error -O2"
  make -j"$(sysctl -n hw.ncpu)"
  make install
  cd - >/dev/null
fi

if ! docker info >/dev/null 2>&1; then
  echo "==> Starting Colima (Docker runtime)"
  colima start --cpu 4 --memory 8 --disk 60
fi

cat <<EOF

Done. Export these before running the collector, or add them to your shell:

    export PATH="/opt/homebrew/opt/rustup/bin:\$PATH"
    export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:\${PKG_CONFIG_PATH:-}"
    export BINDGEN_EXTRA_CLANG_ARGS="-I$PREFIX/include"

Then:  ./scripts/collect-minio.sh
EOF
