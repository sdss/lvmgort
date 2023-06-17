FROM python:3.11-slim-bullseye

MAINTAINER Jose Sanchez-Gallego, gallegoj@uw.ed
LABEL org.opencontainers.image.source https://github.com/sdss/lvmtrurl

WORKDIR /opt

COPY . lvmtrurl

RUN pip3 install -U pip setuptools wheel
RUN cd lvmtrurl && pip3 install .
RUN rm -Rf lvmtrurl

ENTRYPOINT trurl actor start --debug
