# cerebro code unit: TokenSave (MIT, one Rust binary) + ripgrep + git + the cerebro bridge (cerebro.bridge).
# The same image runs the unit (bridge, port 8045) and its index job (`cerebro index run --unit <name>`).
#
# Build from the REPOSITORY ROOT (the bridge is installed from this checkout):
#     docker build -f images/code-unit/Dockerfile -t cerebro/code-unit:2.0.0a0 .
#
# TokenSave is downloaded from its GitHub release and checked against the published SHA256SUMS
# (https://github.com/aovestdipaperino/tokensave/releases/tag/v7.12.1; assets tokensave-v7.12.1-{x86_64,aarch64}-linux.tar.gz,
# a tarball holding the single `tokensave` binary). Verified 2026-09-13.
FROM python:3.12-slim
ARG TOKENSAVE_VERSION=7.12.1
ARG TOKENSAVE_SHA256_X86_64=184612db16800e384a1bcdc7fadcc53fa73bda70240f9c0416ac4b88c7e924fb
ARG TOKENSAVE_SHA256_AARCH64=7de5c95b51d508f39a83d9420ad0700502de107a61dbb38dad3d008d4f6f4261
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) arch=x86_64; sum="$TOKENSAVE_SHA256_X86_64" ;; \
      arm64) arch=aarch64; sum="$TOKENSAVE_SHA256_AARCH64" ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    f="tokensave-v${TOKENSAVE_VERSION}-${arch}-linux.tar.gz"; \
    curl -fsSL -o "/tmp/$f" "https://github.com/aovestdipaperino/tokensave/releases/download/v${TOKENSAVE_VERSION}/$f"; \
    echo "$sum  /tmp/$f" | sha256sum -c -; \
    tar -xzf "/tmp/$f" -C /usr/local/bin tokensave; chmod 0755 /usr/local/bin/tokensave; rm "/tmp/$f"; \
    tokensave --version

# the bridge + adapters, installed from this repository (build context = repo root)
WORKDIR /src
COPY pyproject.toml README.md ./
COPY cerebro ./cerebro
RUN pip install --no-cache-dir '.[bridge]' && rm -rf /src

# Non-root. /workspace is the per-unit volume (shared with the index job); ~/.tokensave holds TokenSave's user
# config, where uploads of the token counter are switched off (org-control rule: nothing leaves the unit).
RUN useradd -m -u 1000 cerebro && mkdir -p /workspace /home/cerebro/.tokensave \
 && printf 'upload_enabled = false\n' > /home/cerebro/.tokensave/config.toml \
 && chown -R cerebro:cerebro /workspace /home/cerebro
USER cerebro
ENV HOME=/home/cerebro \
    CEREBRO_WORKSPACE=/workspace \
    TOKENSAVE_UPDATE_CHECK=off \
    TOKENSAVE_REPORT_SAVINGS=off \
    TOKENSAVE_DISABLE_GREP_HOOK=1 \
    PYTHONUNBUFFERED=1
WORKDIR /workspace
EXPOSE 8045
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8045/health || exit 1
CMD ["cerebro", "bridge", "serve", "--workspace", "/workspace", "--port", "8045"]
