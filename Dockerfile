FROM ubuntu:26.04

# Disable Python stdout buffering
ENV PYTHONUNBUFFERED=1
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
    docker-cli \
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
# Ubuntu 26.04's `chromium` package is only a Snap launcher stub. Containers do
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
# Use /usr/local as HOME so the official installer does not write under
# /home/hermes, which is volume-mounted and would be hidden at runtime.
RUN set -eux; \
    export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"; \
    curl -fsSL https://opencode.ai/install | HOME=/usr/local bash -s -- --no-modify-path; \
    ln -sf /usr/local/.opencode/bin/opencode /usr/local/bin/opencode; \
    /usr/local/bin/opencode --version

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

# ── Create .venv symlink for entrypoint compatibility ────────────────────────
USER root
RUN ln -sf /opt/hermes/venv /opt/hermes/.venv && \
    chown -R hermes:hermes /opt/hermes

# ── Runtime config ───────────────────────────────────────────────────────────
ENV HERMES_HOME=/home/hermes
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV PATH="/opt/hermes/venv/bin:/home/hermes/.npm-global/bin:/home/hermes/.local/bin:$PATH"

VOLUME ["/home/hermes", "/opt/data"]
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes/docker/entrypoint.sh"]
CMD ["hermes", "gateway", "run"]
