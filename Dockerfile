FROM python:3.11-slim-bullseye

MAINTAINER Jose Sanchez-Gallego, gallegoj@uw.edu
LABEL org.opencontainers.image.source https://github.com/sdss/lvmsauron

WORKDIR /opt

COPY . lvmsauron

RUN pip3 install -U pip setuptools wheel
RUN cd lvmsauron && pip3 install .
RUN rm -Rf lvmsauron

ENTRYPOINT sauron actor start --debug
