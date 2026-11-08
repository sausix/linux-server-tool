# One runtime image, two commands: frontend (default) or backend.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM python:3.14-slim-trixie AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /frontend

FROM python-base AS builder
WORKDIR /workspace
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/runtime/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY common/pyproject.toml common/LICENSE ./common/
COPY common/src ./common/src
COPY frontend/pyproject.toml frontend/LICENSE ./frontend/
COPY frontend/src ./frontend/src
COPY backend/pyproject.toml backend/LICENSE ./backend/
COPY backend/src ./backend/src
RUN uv sync --locked --all-packages --no-dev --no-editable --python /usr/local/bin/python

FROM python-base AS runtime
# Tools are available to the daemon; the web process receives no host privileges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nftables iptables docker-cli docker-compose \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system frontend \
    && adduser --system --ingroup frontend frontend \
    && install -d -m 0700 -o frontend -g frontend /frontend/data \
    && install -d -m 0700 /var/lib/lsdfm/backend
COPY --from=builder /opt/runtime/venv /opt/runtime/venv
COPY docker/entrypoint.sh /usr/local/bin/application-entrypoint
RUN chmod 0755 /usr/local/bin/application-entrypoint
COPY LICENSE /usr/share/licenses/application/LICENSE
ENV PATH="/opt/runtime/venv/bin:$PATH"
USER frontend
EXPOSE 8000
ENTRYPOINT ["application-entrypoint"]
CMD ["frontend"]
