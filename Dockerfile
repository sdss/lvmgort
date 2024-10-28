FROM python:3.11-slim-bookworm

MAINTAINER Jose Sanchez-Gallego, gallegoj@uw.edu
LABEL org.opencontainers.image.source https://github.com/albireox/lvmgort

WORKDIR /opt

COPY . lvmgort

RUN pip3 install -U pip setuptools wheel
RUN cd lvmgort && pip3 install .
RUN cp -r lvmgort/scripts .
RUN rm -Rf lvmgort

# Checkout lvmcore
RUN apt-get update && apt-get install -y git
RUN git clone https://github.com/sdss/lvmcore /opt/lvmcore
RUN rm -Rf /opt/lvmcore/.git
ENV LVMCORE_DIR=/opt/lvmcore

ENTRYPOINT lvmgort overwatcher
