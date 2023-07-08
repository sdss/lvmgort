#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: __main__.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import os

import click

from sdsstools.daemonizer import DaemonGroup, cli_coro


@click.group()
def gort():
    """Gort CLI."""

    pass


@gort.group(cls=DaemonGroup, prog="gort_ws", workdir=os.getcwd())
@cli_coro()
async def websocket():
    """Launches the websocket server."""

    from gort.websocket import WebsocketServer

    ws = WebsocketServer()
    await ws.start()

    await ws.websocket_server.serve_forever()


def main():
    gort()


if __name__ == "__main__":
    main()
