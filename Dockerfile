FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
ARG IMAGE_TAG=dev
RUN useradd --create-home --uid 1000 simbim
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYDANTIC_AI_NO_BANNER=1 \
    SIM_BIM_IMAGE=sim-bim-agent:${IMAGE_TAG}
USER simbim
WORKDIR /work
ENTRYPOINT ["sim-bim"]
CMD ["--help"]
