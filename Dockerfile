FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Deps first (cache layer), then source
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8849 \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
EXPOSE 8849
CMD ["/app/.venv/bin/python", "-m", "ccv2_portal_mcp.server"]
