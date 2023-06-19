FROM python:3.11-slim-bullseye

MAINTAINER Jose Sanchez-Gallego, gallegoj@uw.edu
LABEL org.opencontainers.image.source https://github.com/sdss/lvmgort

WORKDIR /opt

COPY . lvmgort

RUN pip3 install -U pip setuptools wheel
RUN cd lvmgort && pip3 install .
RUN rm -Rf lvmgort

ENTRYPOINT gort actor start --debug
