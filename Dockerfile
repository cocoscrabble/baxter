FROM python:3.14-slim

# Install Node.js (needed for tabulator-tables via django-node-assets) and uv.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install Python dependencies (production only, no dev group).
COPY pyproject.toml uv.lock ./
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
