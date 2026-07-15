FROM python:3.14-slim

# Install Node.js (tabulator-tables via django-node-assets), a C toolchain (to
# link the Rust extension), and uv.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Rust toolchain: the `scrabble-pairing-py` PyO3 extension is a path dependency
# built from source during `uv sync` (maturin). A broken wheel build therefore
# fails here at image-build time, never at runtime.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

# Install Python dependencies (production only, no dev group). The extension is
# a path dependency, so its crate sources (and the core crate it depends on)
# must be present for the build — copy them before the sync. They change less
# often than app code, so this layer stays cached across app-only edits.
COPY pyproject.toml uv.lock ./
COPY scrabble-pairing/ ./scrabble-pairing/
COPY scrabble-pairing-py/ ./scrabble-pairing-py/
RUN uv sync --no-dev --no-install-project

# Install Node dependencies (tabulator-tables, etc.) served by django-node-assets.
COPY package.json package-lock.json ./
RUN npm install --omit=dev

# Strip sourceMappingURL comments from third-party CSS and JS so the manifest
# static-files post-processor doesn't choke on missing .map files.
RUN find node_modules \( -name "*.css" -o -name "*.js" \) \
    -exec sed -i -e 's|/\*# sourceMappingURL=.*\*/||g' \
                 -e 's|^//# sourceMappingURL=.*$||g' {} +

# Copy source.
COPY . .

# Collect static files (SECRET_KEY only needed to satisfy Django startup).
RUN SECRET_KEY=build-placeholder uv run manage.py collectstatic --noinput

EXPOSE 8000

CMD ["uv", "run", "gunicorn", "baxter.wsgi", "--bind", "0.0.0.0:8000", "--workers", "2"]
