# The build environment for ohs-libs' Rust wheels (tools/build-sdr-wheels.sh)
# and for running the workspace's checks somewhere other than a developer's
# machine. The same base, pinned by digest, and the same cross toolchains as
# openhamshack-nb's rust/build.Containerfile, so shack-sdr and ohsdigi are
# built by one compiler:
#
# - Debian bookworm, not 13: a binary linked on 13 needs glibc 2.38 and will
#   not load on Raspberry Pi OS bookworm (2.36).
# - amd64 natively, arm64 with Debian's aarch64 cross-gcc as the linker, and
#   Windows x64 with mingw-w64 (PyO3 links python3.dll by raw-dylib, so no
#   Windows Python is needed). None of these compiles any C into shack_sdr
#   (rust/deny.toml bans cc); the C toolchains are linkers here.
# - maturin pinned by version and hash (shack_sdr/wheel-requirements.txt) in
#   a venv of its own; cargo-deny pinned by version and built --locked.
ARG BASE=docker.io/library/rust:1.98.1-bookworm@sha256:c49256cbe5ea0188bc658a689500d70c41eb51f009a7a7be209caf60a944f3ec
FROM ${BASE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc-aarch64-linux-gnu libc6-dev-arm64-cross gcc-mingw-w64-x86-64 python3-venv \
 && rm -rf /var/lib/apt/lists/*
RUN rustup target add aarch64-unknown-linux-gnu x86_64-pc-windows-gnu
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc
COPY shack_sdr/wheel-requirements.txt /opt/wheel-requirements.txt
RUN python3 -m venv /opt/maturin \
 && /opt/maturin/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/wheel-requirements.txt
RUN cargo install --locked --root /opt/cargo-deny cargo-deny@0.20.2 && rm -rf /usr/local/cargo/registry
