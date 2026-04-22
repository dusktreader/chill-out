# ==== Base stage: install base dependencies ===========================================================================
FROM ubuntu:24.04 AS base

RUN groupadd -g 5000 chill-out \
  && useradd -d /home/chill-out -m -u 5000 -g 5000 chill-out

ENV PATH="/venv/bin:$PATH"

RUN mkdir /app && \
  chown chill-out:chill-out /app && \
  mkdir /venv && \
  chown chill-out:chill-out /venv && \
  mkdir /py-bin && \
  chown chill-out:chill-out /py-bin


# ==== Build stage: build the app ======================================================================================
FROM base AS build

USER chill-out
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON_INSTALL_DIR=/py-bin

COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /uvx /bin/
RUN uv python install 3.14 && \
    ln -s $(uv python find 3.14) /py-bin/python

WORKDIR /app

# This is so dumb, but uv won't build if the README isn't there
RUN touch /app/README.md

RUN --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=src/,target=src/ \
    uv sync \
        --locked \
        --no-dev \
        --no-editable


# ==== Final stage: copy venv and run ==================================================================================
FROM base AS final

ENV PATH="/venv/bin:/py-bin:$PATH"

COPY --from=build --chown=chill-out:chill-out /venv /venv
COPY --from=build --chown=chill-out:chill-out /py-bin /py-bin

# Smoke test!
RUN python -Ic 'import chill_out'
