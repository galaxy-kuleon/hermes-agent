FROM ubuntu:24.04

# Keep logs immediate and prevent imports from mutating the image's source
# tree with __pycache__ files at runtime.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DEBIAN_FRONTEND=noninteractive

# ── Locale setup ─────────────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends locales && \
    localedef -i en_US -f UTF-8 en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

# ── System dependencies ──────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Python (system version from Ubuntu repos)
    python3 \
    python3-venv \
    python3-dev \
    # Build tools
    build-essential \
    curl \
    ca-certificates \
    git \
    sudo \
    tini \
    gosu \
    # Document processing
    libreoffice \
    poppler-utils \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fontconfig \
    # Other tools
    ripgrep \
    tmux \
    vim \
    ffmpeg \
    openssh-client \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# ── Create hermes user ───────────────────────────────────────────────────────
RUN useradd -m -s /bin/bash -G sudo hermes && \
    echo "hermes ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/hermes && \
    chmod 0440 /etc/sudoers.d/hermes

# ── Install uv (system-wide) ─────────────────────────────────────────────────
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx && \
    rm -rf /root/.local /root/.cargo

# ── Install Node.js LTS via NodeSource ───────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# ── Install a real, image-baked Chromium ─────────────────────────────────────
# Ubuntu's `chromium` package is only a Snap launcher stub. Containers do
# not run snapd, so the stub exists and passes executable-presence checks but
# fails every launch. Use Playwright's pinned headless shell instead and expose
# it through one stable path for both Hermes setup and agent-browser runtime.
ARG PLAYWRIGHT_VERSION=1.61.1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN set -eux; \
    npx --yes "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium --only-shell; \
    chromium_path="$(find "${PLAYWRIGHT_BROWSERS_PATH}" -type f -name headless_shell -print -quit)"; \
    test -n "${chromium_path}"; \
    ln -s "${chromium_path}" /usr/local/bin/playwright-chromium; \
    /usr/local/bin/playwright-chromium --version; \
    rm -rf /root/.npm /var/lib/apt/lists/*

ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/local/bin/playwright-chromium
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/local/bin/playwright-chromium

# ── Install OpenCode CLI system-wide ─────────────────────────────────────────
# Fetch a versioned release asset directly and verify its platform checksum.
# Do not pipe the mutable installer branch into the build: the image revision
# must determine the exact CLI bytes for both gateway and writer.
ARG TARGETARCH
COPY scripts/install_opencode.sh /tmp/install_opencode.sh
RUN set -eux; \
    bash /tmp/install_opencode.sh --targetarch "${TARGETARCH}"; \
    rm -f /tmp/install_opencode.sh

# ── Copy hermes-agent source ─────────────────────────────────────────────────
WORKDIR /opt/hermes
COPY --chown=hermes:hermes . .
# Ensure hermes can write to the workdir (for venv creation)
RUN chown -R hermes:hermes /opt/hermes

# ── Run setup-hermes.sh as hermes user ───────────────────────────────────────
USER hermes
ENV HOME=/home/hermes
ENV DOCKER_BUILD=true
# Tell uv to use system python, not download its own
ENV UV_PYTHON_PREFERENCE=only-system
RUN bash /opt/hermes/setup-hermes.sh

# ── Seal the runtime install ──────────────────────────────────────────────────
USER root
RUN set -eux; \
    test -f /opt/hermes/ui-tui/dist/entry.js; \
    ln -sf /opt/hermes/venv /opt/hermes/.venv; \
    printf 'docker\n' > /opt/hermes/.install_method; \
    rm -rf /home/hermes/.cache /home/hermes/.npm; \
    chown -R root:root /opt/hermes; \
    chmod -R a+rX,a-w /opt/hermes; \
    chown -R hermes:hermes /opt/hermes/venv; \
    chmod -R u+w /opt/hermes/venv

# ── Runtime config ───────────────────────────────────────────────────────────
ENV HERMES_HOME=/home/hermes
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_TUI_DIR=/opt/hermes/ui-tui
ENV PATH="/opt/hermes/venv/bin:/usr/local/bin:/home/hermes/.npm-global/bin:/home/hermes/.local/bin:$PATH"

# Source, bundled skills and frontend assets are root-owned and read-only.
# The venv is the deliberate exception: opt-in platform backends use the
# allowlisted lazy dependency installer, so only /opt/hermes/venv remains
# writable by the unprivileged hermes runtime user.

# No VOLUME instruction. Declaring one makes Docker create an anonymous
# read-write volume at that path for any container that does not mount
# something there itself, which had three consequences:
#
#   * `read_only: true` stopped meaning read-only. hermes-skill-admin declares
#     read_only, cap_drop ALL, no-new-privileges and network_mode none, and
#     still received two writable volumes, because volume paths are not part
#     of the read-only rootfs.
#   * Every such container stranded a volume. Creating one added two and
#     `docker rm` without `-v` left both behind; four orphans accumulated on
#     the deployment host, one holding 8,246 entries / 276 MB of abandoned
#     Hermes state.
#   * Each one copied the image layer in first — 7,916 files per container.
#
# Every service that needs persistence at these paths mounts it explicitly in
# docker-compose.yml, so nothing loses data. A container that mounts nothing
# now writes to its own layer and discards it on removal, which is the honest
# behaviour for a path nobody asked to persist.
#
# Verified before removal: no container outside this Compose project uses this
# image, and the repository contains no `docker run` invocation of it.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes/docker/entrypoint.sh"]
CMD ["hermes", "gateway", "run"]
