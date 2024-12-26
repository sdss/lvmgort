FROM ghcr.io/astral-sh/uv:0.5.12-python3.13-bookworm-slim

LABEL org.opencontainers.image.authors="Jose Sanchez-Gallego, gallegoj@uw.edu"
LABEL org.opencontainers.image.source=https://github.com/sdss/lvmgort

WORKDIR /opt

COPY . lvmgort

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

ENV LVMCORE_DIR=/opt/lvmcore

RUN apt-get update && apt-get install -y git
RUN git clone https://github.com/sdss/lvmcore /opt/lvmcore
RUN apt-get remove -y git && apt-get autoremove -y

# Sync the project
RUN cd lvmgort && uv sync --frozen --no-cache

CMD ["/opt/lvmgort/.venv/bin/lvmgort", "overwatcher"]
