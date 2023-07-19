#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: websocket.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
from functools import partial

from typing import TYPE_CHECKING, Callable, Coroutine

from websockets.legacy.protocol import broadcast
from websockets.server import WebSocketServerProtocol, serve

from gort import config
from gort.gort import Gort


if TYPE_CHECKING:
    from clu import AMQPReply


__all__ = ["WebsocketServer"]


CALLBACKS: dict[str, Callable] = {}
CB_TYPE = Callable[["WebsocketServer", WebSocketServerProtocol, str], None | Coroutine]


def route(name: str):
    """Defines a WebsocketServer router."""

    def decorator(fn: CB_TYPE):
        CALLBACKS[name] = fn
        return fn

    return decorator


class WebsocketServer:
    """A websocket server that allows communication with Gort.

    Parameters
    ----------
    whost
        The host where to run the websocket server. Defaults to
        ``config.websocket.host``.
    wport
        The TCP port on which to run the websocket server. Defaults to
        ``config.websocket.port``
    client_kwargs
        Arguments to pass to the `.Gort` client.

    """

    def __init__(
        self,
        whost: str | None = None,
        wport: int | None = None,
        **client_kwargs,
    ):
        self.gort = Gort(**client_kwargs)

        self.wparams = (
            whost or config["websocket"]["host"],
            wport or config["websocket"]["port"],
        )
        self.wclients: set[WebSocketServerProtocol] = set()

    async def start(self):
        """Start the server and AMQP client."""

        # self.gort.add_reply_callback(self._handle_reply)
        await self.gort.init()

        self.websocket_server = await serve(
            self._handle_websocket_connection,
            *self.wparams,
        )

        return self

    async def stop(self):
        """Stop the server and AMQP client."""

        await self.gort.stop()
        self.websocket_server.close()

    async def _handle_websocket_connection(self, websocket: WebSocketServerProtocol):
        """Handle a connection to the websocket server."""

        # Register the client
        self.wclients.add(websocket)

        async for data in websocket:
            try:
                message = json.loads(data)
                if not isinstance(message, dict):
                    continue
            except ValueError:
                continue

            route = message.get("route", "")
            command_id = message.get("command_id", "")
            params = message.get("params", {})

            if route in CALLBACKS:
                cb = CALLBACKS[route]
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(
                        cb(
                            self,
                            client=websocket,
                            command_id=command_id,
                            **params,
                        )
                    )
                else:
                    loop = asyncio.get_running_loop()
                    cb = partial(cb, self, command_id, **params)
                    loop.call_soon(cb)

        self.wclients.remove(websocket)

    async def _handle_reply(self, reply: AMQPReply):
        """Broadcast a reply to the connected websockets."""

        message = reply.message
        data = dict(
            headers=message.headers,
            exchange=message.exchange,
            message_id=message.message_id,
            routing_key=message.routing_key,
            timestamp=message.timestamp.isoformat() if message.timestamp else None,
            body=reply.body,
        )
        broadcast(self.wclients, json.dumps(data))

    async def reply_to_client(
        self,
        client: WebSocketServerProtocol,
        command_id: str,
        message: dict = {},
        **message_kwargs,
    ):
        """Reply to a client command.

        Parameters
        ----------
        client
            The client websocket connection to use to reply to the client.
        command_id
            The command ID associated with the client command.
        message
            The message to send to the client.
        message_kwargs
            Keyword arguments that will be used to update the message,

        """

        final_message = {"command_id": command_id}
        final_message.update(message)
        final_message.update(message_kwargs)

        try:
            await client.send(json.dumps(final_message))
        except Exception as err:
            self.gort.log.warning(f"Failed replying to WS client: {err}.")

    @route("enclosure_status")
    async def enclosure_status(
        self,
        client: WebSocketServerProtocol,
        command_id: str,
        **_,
    ):
        """Returns the enclosure status."""

        status = await self.gort.enclosure.status()
        await self.reply_to_client(client, command_id, status)

    @route("enclosure_action")
    async def enclosure_action(
        self,
        client: WebSocketServerProtocol,
        command_id: str,
        action: str | None = None,
    ):
        """Opens/closes/stops the enclosure."""

        if action == "open":
            await self.gort.enclosure.open()
            await self.reply_to_client(client, command_id, {"text": "Dome open"})
        elif action == "close":
            await self.gort.enclosure.close()
            await self.reply_to_client(client, command_id, {"text": "Dome closed"})
        elif action == "stop":
            await self.gort.enclosure.stop()
            await self.reply_to_client(client, command_id, {"text": "Dome stopped"})
        else:
            await self.reply_to_client(client, command_id, {"error": "Invalid action"})
