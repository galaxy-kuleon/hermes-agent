FROM ubuntu:26.04

# Disable Python stdout buffering to ensure logs are printed immediately
ENV PYTHONUNBUFFERED=1

# ── Locale setup ─────────────────────────────────────────────────────────────
# Generate en_US.UTF-8 locale so tools that depend on it work correctly.
RUN apt-get update && \
    apt-get install -y --no-install-recommends locales && \
    localedef -i en_US -f UTF-8 en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

# ── System dependencies ──────────────────────────────────────────────────────
# Install in one layer, clear APT cache.
# tini reaps orphaned zombie processes (MCP stdio subprocesses, git, bun, etc.)
# that would otherwise accumulate when hermes runs as PID 1. See #15012.
#
# Key packages:
#   - fonts-noto-cjk, fonts-noto-cjk-extra: CJK font support for PDF/DOCX
#   - libreoffice: headless document conversion (soffice)
#   - poppler-utils: pdftotext, pdfinfo, pdfimages for PDF processing
#   - fontconfig: fc-list for font availability checking
#   - curl, ca-certificates, git: needed for uv/nvm installers + runtime
#   - ripgrep: fast file search
#   - ffmpeg: audio/video processing
#   - openssh-client, docker-cli: remote execution tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates python3 \
    ripgrep ffmpeg gcc python3-dev libffi-dev procps git openssh-client \
    docker-cli sudo tini \
    locales fontconfig \
    fonts-noto-cjk fonts-noto-cjk-extra \
    libreoffice \
    poppler-utils \
    chromium && \
    rm -rf /var/lib/apt/lists/*

# ── Non-root user ────────────────────────────────────────────────────────────
# User 'hermes' with home at /home/hermes (standard Linux layout).
# /opt/data remains available as a separate persistent volume for legacy data.
RUN useradd -u 10000 -m -d /home/hermes hermes && \
    echo "hermes ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/hermes && \
    chmod 0440 /etc/sudoers.d/hermes

# ── Install gosu (for privilege dropping in entrypoint) ──────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

# ── Install uv (system-wide, as root) ────────────────────────────────────────
# Official installer from astral.sh — always gets the latest version.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx && \
    rm -rf /root/.cargo

# ── Install Node.js LTS (from NodeSource) ────────────────────────────────────
# Use NodeSource repo for reliable Node.js LTS installation in Docker.
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# npm global prefix for hermes user (packages persist in /home/hermes volume)
USER hermes
ENV HOME=/home/hermes
RUN mkdir -p "$HOME/.npm-global" && \
    npm config set prefix "$HOME/.npm-global"
ENV PATH="/home/hermes/.npm-global/bin:/home/hermes/.local/bin:${PATH}"
USER root

# ── Copy hermes-agent source ─────────────────────────────────────────────────
WORKDIR /opt/hermes
COPY --chown=hermes:hermes . .
# Ensure the workdir itself is writable by hermes (for venv creation etc.)
RUN chown -R hermes:hermes /opt/hermes

# ── Run setup-hermes.sh (non-interactive) ────────────────────────────────────
# This handles: uv python install, uv sync, npm install, playwright, web builds.
# DOCKER_BUILD=true triggers non-interactive mode (skips read -p prompts).
# PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH tells setup to use system chromium.
USER hermes
ENV DOCKER_BUILD=true
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser
RUN bash /opt/hermes/setup-hermes.sh && \
    # Entrypoint expects .venv/ (dot-prefix); setup-hermes.sh creates venv/
    ln -sf /opt/hermes/venv /opt/hermes/.venv

# ── Permissions ──────────────────────────────────────────────────────────────
USER root
RUN chmod -R a+rX /opt/hermes && \
    chown -R hermes:hermes /opt/hermes/.venv /opt/hermes/ui-tui /opt/hermes/node_modules 2>/dev/null || true

# ── Runtime ──────────────────────────────────────────────────────────────────
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_HOME=/home/hermes
# Use system Chromium (Playwright doesn't support ubuntu:26.04 arm64 browser downloads)
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser
ENV PATH="/home/hermes/.npm-global/bin:/home/hermes/.local/bin:${PATH}"
VOLUME [ "/home/hermes", "/opt/data" ]
ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/opt/hermes/docker/entrypoint.sh" ]
