#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-12-29
# @Filename: health.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import time

from typing import TYPE_CHECKING, Sequence

from lvmopstools.retrier import Retrier

from gort import config
from gort.tools import (
    get_lvmapi_route,
    kubernetes_list_deployments,
    kubernetes_restart_deployment,
)


if TYPE_CHECKING:
    from gort.gort import Gort


@Retrier(max_attempts=3, delay=1)
async def ping_actors() -> dict[str, bool]:
    """Pings all actors in the system."""

    return await get_lvmapi_route("/actors/ping")


async def get_actor_ping(
    discard_disabled: bool = False,
    discard_overwatcher: bool = False,
) -> dict[str, bool]:
    """Returns a list of failed actors."""

    disabled_actors = config["overwatcher.disabled_actors"] or []

    actor_status = await ping_actors()

    for actor in disabled_actors:  # Ignore actors we know are disabled.
        if discard_disabled and actor in actor_status:
            actor_status.pop(actor)

    if discard_overwatcher and "lvm.overwatcher" in actor_status:
        actor_status.pop("lvm.overwatcher")

    return actor_status


async def restart_actors(
    actors: str | Sequence[str],
    gort: Gort,
    post_restart_actions: bool = True,
    timeout: float = 60,
) -> None:
    """Restarts one or more actors.

    This is a high-level function that tries to restart a series of actors, along
    with their deployments, in an optimal way. If multiple actors correspond to the
    same deployment, only one deployment restart is triggered.

    Parameters
    ----------
    actors
        The actor or actors to restart.
    gort
        The Gort instance. Used to refresh the command set in the affected actors.
    post_restart_actions
        Whether to run post-restart actions. This may include resetting spectrographs,
        homing devices, etc.
    timeout
        The timeout in seconds to wait for the restart to complete.

    """

    if isinstance(actors, str):
        actors = [actors]

    try:
        actor_to_deployment = await get_lvmapi_route("/actors/actor-to-deployment")
        deployment_to_actors = await get_lvmapi_route("/actors/deployment-to-actors")
    except Exception as ee:
        raise RuntimeError("Unable to retrieve actor-deployment mappings.") from ee

    deployments_to_restart = set()
    for actor in actors:
        if actor not in actor_to_deployment:
            raise ValueError(f"Actor {actor} not found in actor-to-deployment mapping.")
        deployments_to_restart.add(actor_to_deployment[actor])

    for deployment in deployments_to_restart:
        if deployment not in deployment_to_actors:
            raise ValueError(f"Actors for deployment {deployment!r} cannot be found.")

        await restart_deployment(
            gort,
            deployment,
            deployment_to_actors[deployment],
            timeout=timeout,
        )

    for actor in actors:
        if actor in gort.actors:
            gort.log.debug(f"Refreshing command list for actor {actor!r} in Gort.")
            await gort.actors[actor].refresh()
        else:
            gort.log.warning(f"Actor {actor!r} not found in Gort. Adding it.")
            gort.add_actor(actor)
            await gort.actors[actor].init()

    if post_restart_actions:
        gort.log.debug("Running post-restart actions.")
        await run_post_restart_actions(gort, actors)


async def restart_deployment(
    gort: Gort,
    deployment: str,
    actors: list[str],
    timeout: float = 60,
):
    """Restarts a deployment, waiting until all its actors ping."""

    start_time = time.time()

    gort.log.warning(f"Restarting deployment {deployment}.")
    await kubernetes_restart_deployment(deployment)

    await asyncio.sleep(5)

    while True:
        if deployment in await kubernetes_list_deployments():
            gort.log.info(f"Deployment {deployment} restarted.")
            break

        if time.time() - start_time > timeout:
            raise RuntimeError(
                f"Timed out waiting for deployment {deployment!r} to restart."
            )

        await asyncio.sleep(1)

    while True:
        actor_pings = {actor: False for actor in actors}

        for actor in actors:
            ping_cmd = await gort.send_command(actor, "ping", time_limit=2)
            if ping_cmd.status.did_succeed:
                actor_pings[actor] = True

        if all(actor_pings.values()):
            gort.log.info(f"All actors in deployment {deployment} are pinging.")
            break

        if time.time() - start_time > timeout:
            raise RuntimeError(
                f"Timed out waiting actors in deployment {deployment!r} to ping."
            )


async def run_post_restart_actions(gort: Gort, actors: Sequence[str]):
    """Runs post-restart actions for a list of actors."""

    # Start with the easy ones. If the AG actors or specs have been restarted,
    # reset them.
    agcam_restarted = any(".agcam" in actor for actor in actors)
    scp_restarted = any("lvmscp" in actor for actor in actors)

    if agcam_restarted:
        gort.log.info("AG actors restarted. Reconnecting AG cameras.")
        await gort.ags.reconnect()

    if scp_restarted:
        gort.log.info("SCP actors restarted. Resetting spectrographs.")
        await gort.specs.abort()
        await gort.specs.reset(full=True)

    # Now re-home telescope devices.
    for telescope in ["sci", "spec", "skye", "skyw"]:
        for device in ["km", "fibsel", "foc"]:
            if f"lvm.{telescope}.{device}" not in actors:
                continue

            gort.log.info(f"Re-homing {device!r} on telescope {telescope}.")

            tel_dev = gort.telescopes[telescope]
            dev = getattr(tel_dev, "focuser" if device == "foc" else device)
            if dev is None:
                continue

            try:
                await dev.home()
            except Exception:
                gort.log.warning(f"Failed to home {device!r} on telescope {telescope}.")
