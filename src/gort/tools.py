#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: tools.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import pathlib
import re

from typing import TYPE_CHECKING

import httpx

from gort import config


if TYPE_CHECKING:
    from pyds9 import DS9

    from clu import AMQPClient, AMQPReply


__all__ = [
    "get_valid_variable_name",
    "ds9_agcam_monitor",
    "parse_agcam_filename",
    "ds9_display_frames",
    "get_next_tile_id",
    "get_calibrators",
    "register_observation",
    "tqdm_timer",
    "get_ccd_frame_path",
]

CAMERAS = [
    "sci.west",
    "sci.east",
    "skye.west",
    "skye.east",
    "skyw.west",
    "skyw.east",
    "spec.east",
]


def get_valid_variable_name(var_name: str):
    """Converts a string to a valid variable name."""

    return re.sub(r"\W|^(?=\d)", "_", var_name)


async def ds9_agcam_monitor(
    amqp_client: AMQPClient,
    cameras: list[str] | None = None,
    replace_path_prefix: tuple[str, str] | None = None,
    **kwargs,
):
    """Shows guider images in DS9."""

    images_handled = set([])

    # Clear all frames and get an instance of DS9.
    ds9 = await ds9_display_frames([], clear_frames=True, preserve_frames=False)

    if cameras is None:
        cameras = CAMERAS.copy()

    agcam_actors = set(
        [
            "lvm." + (cam.split(".")[0] if "." in cam else cam) + ".agcam"
            for cam in cameras
        ]
    )

    async def handle_reply(reply: AMQPReply):
        sender = reply.sender
        if sender not in agcam_actors:
            return

        message: dict | None = None
        if "east" in reply.body:
            message = reply.body["east"]
        elif "west" in reply.body:
            message = reply.body["west"]
        else:
            return

        if message is None or message.get("state", None) != "written":
            return

        filename: str = message["filename"]
        if filename in images_handled:
            return
        images_handled.add(filename)

        if replace_path_prefix is not None:
            filename = filename.replace(replace_path_prefix[0], replace_path_prefix[1])

        await ds9_display_frames([filename], ds9=ds9, **kwargs)

    amqp_client.add_reply_callback(handle_reply)

    while True:
        await asyncio.sleep(1)


async def ds9_display_frames(
    files: list[str | pathlib.Path] | dict[str, str | pathlib.Path],
    ds9: DS9 | None = None,
    order=CAMERAS,
    ds9_target: str = "DS9:*",
    show_all_frames=True,
    preserve_frames=True,
    clear_frames=False,
    adjust_zoom=True,
    adjust_scale=True,
    show_tiles=True,
):
    """Displays a series of images in DS9."""

    if ds9 is None:
        try:
            import pyds9
        except ImportError:
            raise ImportError("pyds9 is not installed.")

        ds9 = pyds9.DS9(target=ds9_target)

    if clear_frames:
        ds9.set("frame delete all")

    files_dict: dict[str, str] = {}
    if not isinstance(files, dict):
        for file_ in files:
            tel_cam = parse_agcam_filename(file_)
            if tel_cam is None:
                raise ValueError(f"Cannot parse type of file {file_!s}.")
            files_dict[".".join(tel_cam)] = str(file_)
    else:
        files_dict = {k: str(v) for k, v in files.items()}

    nframe = 1
    for cam in order:
        if cam in files_dict:
            file_ = files_dict[cam]
            ds9.set(f"frame {nframe}")
            ds9.set(f"fits {file_}")
            if adjust_scale:
                ds9.set("zscale")
            if adjust_zoom:
                ds9.set("zoom to fit")
            nframe += 1
        else:
            if show_all_frames:
                if preserve_frames is False:
                    ds9.set(f"frame {nframe}")
                    ds9.set("frame clear")
                nframe += 1

    if show_tiles:
        ds9.set("tile")

    return ds9


def parse_agcam_filename(file_: str | pathlib.Path) -> tuple[str, str] | None:
    """Returns the type of an ``agcam`` file in the form ``(telescope, camera)``."""

    file_ = pathlib.Path(file_)
    basename = file_.name

    match = re.match(".+(sci|spec|skyw|skye).+(east|west)", basename)
    if not match:
        return None

    return match.groups()


async def get_next_tile_id() -> dict:
    """Retrieves the next ``tile_id`` from the scheduler API."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    async with httpx.AsyncClient(base_url=f"http://{host}:{port}/") as client:
        resp = await client.get("next_tile")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /next_tile")
        tile_id_data = resp.json()

    return tile_id_data


async def get_calibrators(
    tile_id: int | None = None,
    ra: float | None = None,
    dec: float | None = None,
):
    """Get calibrators for a ``tile_id`` or science pointing."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    async with httpx.AsyncClient(base_url=f"http://{host}:{port}/") as client:
        if tile_id:
            resp = await client.get("cals", params={"tile_id": tile_id})
        elif ra is not None and dec is not None:
            resp = await client.get("cals", params={"ra": ra, "dec": dec})
        else:
            raise ValueError("ra and dec are required.")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /cals")

    return resp.json()


async def register_observation(payload: dict):
    """Registers an observation with the scheduler."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"http://{host}:{port}/register_observation",
            json=payload,
            follow_redirects=True,
        )

        if resp.status_code != 200 or not resp.json()["success"]:
            raise RuntimeError("Failed registering observation.")


def is_notebook() -> bool:
    """Returns `True` if the code is run inside a Jupyter Notebook.

    https://stackoverflow.com/questions/15411967/how-can-i-check-if-code-is-executed-in-the-ipython-notebook

    """

    try:
        shell = get_ipython().__class__.__name__  # type: ignore
        if shell == "ZMQInteractiveShell":
            return True  # Jupyter notebook or qtconsole
        elif shell == "TerminalInteractiveShell":
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False  # Probably standard Python interpreter


def tqdm_timer(seconds: int):
    """Creates a task qith a tqdm progress bar."""

    if is_notebook():
        from tqdm.notebook import tqdm
    else:
        from tqdm import tqdm

    bar_format = "{l_bar}{bar}| {n_fmt}/{total_fmt}s"

    async def _progress():
        for _ in tqdm(range(seconds), bar_format=bar_format):
            await asyncio.sleep(1)

    return asyncio.create_task(_progress())


def get_ccd_frame_path(
    frame_id: int,
    sjd: int | None = None,
    cameras: str | list[str] | None = None,
    spectro_path="/data/spectro",
) -> list[str]:
    """Returns the paths for the files for a spectrograph frame.

    Parameters
    ----------
    frame_id
        The spectrograph frame for which the paths are searched.
    mjd
        The SJD in which the frames where taken. If not provided, all the
        directories under ``spectro_path`` are searched.
    cameras
        The cameras to be returned. If `None`, all cameras found are returned.
    spectro_path
        The path to the ``spectro`` directory where spectrograph files are
        stored under an SJD structure.

    Returns
    -------
    paths
        The list of paths to CCD frames that match ``frame_id``.

    """

    if isinstance(cameras, str):
        cameras = [cameras]

    base_path = pathlib.Path(spectro_path)
    recursive = True
    if sjd:
        base_path /= str(sjd)
        recursive = False

    # Glob all files that match the frame_id.
    globp = f"*{frame_id}.fits.*"
    if recursive:
        globp = f"**/{globp}"

    files = [str(path) for path in base_path.glob(globp)]

    if cameras is None:
        return files

    files_camera = []
    for camera in cameras:
        for file in files:
            if f"-{camera}-" in file:
                files_camera.append(file)

    return files_camera
