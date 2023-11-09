#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: tools.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import os
import pathlib
import re
import tempfile
import warnings
from contextlib import suppress
from datetime import datetime
from functools import partial

from typing import TYPE_CHECKING, Any, Callable, Coroutine

import httpx
import numpy
import pandas
import peewee
from astropy import units as uu
from astropy.coordinates import angular_separation as astropy_angular_separation

from gort import config


if TYPE_CHECKING:
    from pyds9 import DS9

    from clu import AMQPClient, AMQPReply

    from gort.devices.telescope import FibSel
    from gort.gort import GortClient


__all__ = [
    "get_valid_variable_name",
    "ds9_agcam_monitor",
    "parse_agcam_filename",
    "ds9_display_frames",
    "get_next_tile_id",
    "get_calibrators",
    "get_next_tile_id_sync",
    "get_calibrators_sync",
    "register_observation",
    "get_ccd_frame_path",
    "move_mask_interval",
    "angular_separation",
    "get_db_connection",
    "run_in_executor",
    "is_interactive",
    "is_notebook",
    "cancel_task",
    "build_guider_reply_list",
    "get_temporary_file_path",
    "insert_to_database",
    "get_md5sum_file",
    "get_md5sum_from_spectro",
    "get_md5sum",
    "mark_exposure_bad",
]

AnyPath = str | os.PathLike

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


def parse_agcam_filename(file_: str | pathlib.Path):
    """Returns the type of an ``agcam`` file in the form ``(telescope, camera)``."""

    file_ = pathlib.Path(file_)
    basename = file_.name

    match = re.match(".+(sci|spec|skyw|skye).+(east|west)", basename)
    if not match:
        return None

    return match.groups()


def get_next_tile_id_sync() -> dict:
    """Retrieves the next ``tile_id`` from the scheduler API. Synchronous version."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    with httpx.Client(base_url=f"http://{host}:{port}/") as client:
        resp = client.get("next_tile")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /next_tile")
        tile_id_data = resp.json()

    return tile_id_data


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


def get_calibrators_sync(
    tile_id: int | None = None,
    ra: float | None = None,
    dec: float | None = None,
) -> dict:
    """Get calibrators for a ``tile_id`` or science pointing. Synchronous version."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    with httpx.Client(base_url=f"http://{host}:{port}/") as client:
        if tile_id:
            resp = client.get("cals", params={"tile_id": tile_id})
        elif ra is not None and dec is not None:
            resp = client.get("cals", params={"ra": ra, "dec": dec})
        else:
            raise ValueError("ra and dec are required.")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /cals")

    return resp.json()


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
            raise RuntimeError(f"Failed registering observation: {resp.text}.")


def mark_exposure_bad(tile_id: int, dither_position: int = 0):
    """Marks a registered tile/dither as bad."""

    db = get_db_connection()

    completion_status = peewee.Table("completion_status", schema="lvmopsdb").bind(db)
    dither = peewee.Table("dither", schema="lvmopsdb").bind(db)

    dither_pk = (
        dither.select(dither.c.pk)
        .where(
            dither.c.tile_id == tile_id,
            dither.c.position == dither_position,
        )
        .namedtuples()
    )

    if len(dither_pk) == 0:
        raise ValueError("No matching tile-position.")

    completion_status.update(done=False).where(
        completion_status.c.pk == dither_pk[0].pk
    ).execute()


def is_notebook() -> bool:
    """Returns :obj:`True` if the code is run inside a Jupyter Notebook.

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


def is_interactive():
    """Returns :obj:`True` is we are in an interactive session."""

    import __main__ as main

    return not hasattr(main, "__file__")


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
        The cameras to be returned. If :obj:`None`, all cameras found are returned.
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


async def move_mask_interval(
    gort: GortClient,
    positions: str | list[str] = "P1-*",
    order_by_steps: bool = False,
    total_time: float | None = None,
    time_per_position: float | None = None,
    notifier: Callable[[str], None] | Callable[[str], Coroutine] | None = None,
):
    """Moves the fibre mask in the spectrophotometric telescope at intervals.

    Parameters
    ----------
    gort
        The instance of :obj:`.Gort` to communicate with the actor system.
    positions
        The positions to iterate over. It can be a string in which case it will
        be treated as a regular expression and any mask position that matches the
        value will be iterated, in alphabetic order. Alternative it can be a list
        of positions to move to which will be executed in that order.
    order_by_steps
        If :obj:`True`, the positions are iterated in order of smaller to larger
        number of step motors.
    total_time
        The total time to spend iterating over positions, in seconds. Each position
        will  be visited for an equal amount of time. The time required to move the
        mask will not be taken into account, which means the total execution
        time will be longer than ``total_time``.
    time_per_position
        The time to spend on each mask position, in seconds. The total execution
        time will be ``len(positions)*total_time+overhead`` where ``overhead`` is
        the time required to move the mask between positions.
    notifier
        A function or coroutine to call every time a new position is reached.
        If it's a coroutine, it is scheduled as a task. If it is a normal
        callback it should run quickly to not perceptibly affect the total
        execution time.

    """

    try:
        fibsel: FibSel = gort.telescopes.spec.fibsel
    except Exception as err:
        raise RuntimeError(f"Cannot find fibre selector: {err}")

    if total_time is not None and time_per_position is not None:
        raise ValueError("Only one of total_time or time_per_position can be used.")

    if total_time is None and time_per_position is None:
        raise ValueError("One of total_time or time_per_position needs to be passed.")

    mask_config = gort.config["telescopes"]["mask_positions"]
    all_positions = list(mask_config)

    if isinstance(positions, str):
        regex = positions
        all_positions = fibsel.list_positions()
        positions = [pos for pos in all_positions if re.match(regex, pos)]

        if order_by_steps:
            positions = sorted(positions, key=lambda p: mask_config[p])

    fibsel.write_to_log(f"Iterating over positions {positions}.")

    if total_time:
        time_per_position = total_time / len(positions)

    assert time_per_position is not None

    for position in positions:
        await fibsel.move_to_position(position)

        # Notify.
        if notifier is not None:
            if asyncio.iscoroutinefunction(notifier):
                asyncio.create_task(notifier(position))
            else:
                notifier(position)

        await asyncio.sleep(time_per_position)


def angular_separation(lon1: float, lat1: float, lon2: float, lat2: float):
    """A wrapper around astropy's ``angular_separation``.

    Returns the separation between two sets of coordinates. All units must
    be degrees and the returned values is also the separation in degrees.

    """

    separation = astropy_angular_separation(
        lon1 * uu.degree,  # type: ignore
        lat1 * uu.degree,  # type: ignore
        lon2 * uu.degree,  # type: ignore
        lat2 * uu.degree,  # type: ignore
    )

    return separation.to("deg").value


def get_db_connection():
    """Returns a DB connection from the configuration file parameters."""

    conn = peewee.PostgresqlDatabase(**config["database"]["connection"])
    assert conn.connect(), "Database connection failed."

    return conn


async def run_in_executor(fn, *args, catch_warnings=False, executor="thread", **kwargs):
    """Runs a function in an executor.

    In addition to streamlining the use of the executor, this function
    catches any warning issued during the execution and reissues them
    after the executor is done. This is important when using the
    actor log handler since inside the executor there is no loop that
    CLU can use to output the warnings.

    In general, note that the function must not try to do anything with
    the actor since they run on different loops.

    """

    fn = partial(fn, *args, **kwargs)

    if executor == "thread":
        executor = concurrent.futures.ThreadPoolExecutor
    elif executor == "process":
        executor = concurrent.futures.ProcessPoolExecutor
    else:
        raise ValueError("Invalid executor name.")

    if catch_warnings:
        with warnings.catch_warnings(record=True) as records:
            with executor() as pool:
                result = await asyncio.get_event_loop().run_in_executor(pool, fn)

        for ww in records:
            warnings.warn(ww.message, ww.category)

    else:
        with executor() as pool:
            result = await asyncio.get_running_loop().run_in_executor(pool, fn)

    return result


async def cancel_task(task: asyncio.Future | None):
    """Safely cancels a task."""

    if task is None or task.done():
        return

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def build_guider_reply_list(
    gort: GortClient,
    reply_list: list[dict],
    actor: str | None = None,
):
    """Tasks that monitors the guider output and builds a list of replies.

    This coroutine is meant to be run as a task. When the task is cancelled it
    will clean itself by removing the callback in the client.

    Parameters
    ----------
    gort
        The Gort client to connect to the actor system.
    reply_list
        A list (usually empty) to which the task will append the replies.
    actor
        The actor to listen to. If :obj:`None`, listens to all the guider actors.

    """

    async def handle_guider_reply(reply: AMQPReply):
        if actor is not None:
            if actor not in str(reply.sender):
                return
        else:
            if ".guider" not in str(reply.sender):
                return

        body = reply.body
        telescope = str(reply.sender).split(".")[1]

        try:
            if "frame" in body:
                frame = body["frame"]
                reply_list.append(
                    {
                        "frameno": frame["seqno"],
                        "time": pandas.to_datetime(datetime.now()),
                        "n_sources": frame["n_sources"],
                        "focus_position": frame["focus_position"],
                        "fwhm": frame["fwhm"],
                        "telescope": telescope,
                    }
                )
            elif "measured_pointing" in body:
                measured_pointing = body["measured_pointing"]
                reply_list.append(
                    {
                        "frameno": measured_pointing["frameno"],
                        "ra": measured_pointing["ra"],
                        "dec": measured_pointing["dec"],
                        "ra_offset": measured_pointing["radec_offset"][0],
                        "dec_offset": measured_pointing["radec_offset"][1],
                        "separation": measured_pointing["separation"],
                        "pa": measured_pointing.get("pa", numpy.nan),
                        "pa_offset": measured_pointing.get("pa_offset", numpy.nan),
                        "zero_point": measured_pointing.get("zero_point", numpy.nan),
                        "mode": measured_pointing["mode"],
                        "telescope": telescope,
                    }
                )
            elif "correction_applied" in body:
                correction_applied = body["correction_applied"]

                reply_list.append(
                    {
                        "frameno": correction_applied["frameno"],
                        "ax0_applied": correction_applied["motax_applied"][0],
                        "ax1_applied": correction_applied["motax_applied"][1],
                        "rot_applied": correction_applied.get("rot_applied", 0.0),
                        "telescope": telescope,
                    }
                )
            else:
                return

        except Exception as err:
            gort.log.warning(f"Error processing guider reply: {err}")

    try:
        gort.add_reply_callback(handle_guider_reply)

        # If the list does not expand every 30s, clean and exit.
        while True:
            nlist = len(reply_list)
            await asyncio.sleep(30)
            if len(reply_list) == nlist:
                return
    finally:
        gort.remove_reply_callback(handle_guider_reply)


def get_temporary_file_path(*args, create_parents: bool = False, **kwargs):
    """Returns a valid path to a temporary file.

    `args` and `kwargs` are directly passed to `tempfile.NamedTemporaryFile`.
    If `create_parents`, the parent directories are created if they don't
    exist.

    """

    tmp_log_file = tempfile.NamedTemporaryFile(*args, **kwargs)
    tmp_log_file.close()

    tmp_path = pathlib.Path(tmp_log_file.name)
    if tmp_path.exists():
        tmp_path.unlink()
    elif create_parents:
        tmp_path.parent.mkdir(parents=True, exist_ok=True)

    return tmp_path


def insert_to_database(
    table_name: str,
    payload: list[dict[str, Any]],
    columns: list[str] | None = None,
):
    """Inserts data into the database.

    Parameters
    ----------
    table_name
        The table in the database where to insert the data. Can be in the format
        ``schema.table_name``.
    payload
        The data to ingest, as a list of dictionaries in which each dictionary
        is a mapping of column name in ``table`` to the value to ingest.
    columns
        A list of table columns. If not passed, the column names are inferred from
        the first element in the payload. In this case you must ensure that all the
        elements in the payload contain entries for all the columns (use :obj:`None`
        to fill missing data).

    """

    if len(payload) == 0:
        return

    columns = columns or list(payload[0].keys())

    conn = get_db_connection()

    schema: str | None
    if "." in table_name:
        schema, table_name = table_name.split(".")
    else:
        schema = None
        table_name = table_name

    table = peewee.Table(table_name, schema=schema, columns=columns)
    table.bind(conn)

    table.insert(payload).execute()


def get_md5sum_file(file: AnyPath):
    """Returns the path to the MD5 file for the spectro files."""

    file = pathlib.Path(file).absolute()
    mjd = file.parts[-2]

    md5sum = file.parent / f"{mjd}.md5sum"

    return md5sum if md5sum.exists() else None


def get_md5sum_from_spectro(file: AnyPath):
    """Returns the MD5 checksum for a file from the spectro checksum file."""

    file = pathlib.Path(file).absolute()
    basename = file.name

    md5sum_file = get_md5sum_file(file)
    if not md5sum_file:
        return None

    data = open(md5sum_file).read()

    match = re.search(rf"([0-9a-f]+)\s+{basename}", data)
    if not match:
        return None

    return match.groups(1)[0]


def get_md5sum(file: AnyPath):
    """Returns the MD5 checksum for a file."""

    data = open(file, "rb").read()

    return hashlib.md5(data).hexdigest()
