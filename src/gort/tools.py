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
import datetime
import functools
import hashlib
import os
import pathlib
import re
import tempfile
import warnings
from contextlib import asynccontextmanager, contextmanager, suppress
from functools import partial, wraps

from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Generator,
    Literal,
    Sequence,
)

import httpx
import numpy
import peewee
import polars
import redis
from astropy import units as uu
from astropy.coordinates import angular_separation as astropy_angular_separation
from redis import asyncio as aioredis

from clu import AMQPClient
from sdsstools import get_sjd

from gort import config
from gort.exceptions import ErrorCode, GortError


if TYPE_CHECKING:
    from clu import AMQPClient, AMQPReply

    from gort.devices.telescope import FibSel
    from gort.gort import GortClient


__all__ = [
    "get_valid_variable_name",
    "get_next_tile_id",
    "get_calibrators",
    "get_next_tile_id_sync",
    "get_calibrators_sync",
    "register_observation",
    "get_ccd_frame_path",
    "move_mask_interval",
    "angular_separation",
    "get_db_connection",
    "redis_client_async",
    "redis_client_sync",
    "run_in_executor",
    "is_interactive",
    "is_notebook",
    "cancel_task",
    "get_ephemeris_summary",
    "get_ephemeris_summary_sync",
    "get_temporary_file_path",
    "insert_to_database",
    "get_md5sum_file",
    "get_md5sum_from_spectro",
    "get_md5sum",
    "mark_exposure_bad",
    "handle_signals",
    "get_lvmapi_route",
    "GuiderMonitor",
    "overwatcher_is_running",
    "get_by_source_id",
    "is_actor_running",
    "check_overwatcher_not_running",
    "kubernetes_restart_deployment",
    "kubernetes_list_deployments",
    "get_gort_client",
    "add_night_log_comment",
    "async_noop",
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


def get_next_tile_id_sync() -> dict:
    """Retrieves the next ``tile_id`` from the scheduler API. Synchronous version."""

    sch_config = config["services"]["scheduler"]
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

    sch_config = config["services"]["scheduler"]
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

    sch_config = config["services"]["scheduler"]
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

    sch_config = config["services"]["scheduler"]
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

    sch_config = config["services"]["scheduler"]
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


async def set_tile_status(tile_id: int, enabled: bool = True):
    """Enables/disables a tile in the database."""

    sch_config = config["services"]["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    disable = "false" if enabled else "true"

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"http://{host}:{port}/tile_status/?tile_id={tile_id}&disable={disable}",
            json={},
            follow_redirects=True,
        )

        if resp.status_code != 200 or not resp.json()["success"]:
            raise RuntimeError(f"Failed setting tile status: {resp.text}.")


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

    conn = peewee.PostgresqlDatabase(**config["services"]["database"]["connection"])
    assert conn.connect(), "Database connection failed."

    return conn


@asynccontextmanager
async def redis_client_async() -> AsyncGenerator[aioredis.Redis, None]:
    """Returns a Redis connection from the configuration file parameters."""

    redis_config = config["services"]["redis"]
    client = aioredis.from_url(redis_config["url"], decode_responses=True)

    yield client

    await client.aclose()


@contextmanager
def redis_client_sync() -> Generator[redis.Redis, None]:
    """Returns a Redis connection from the configuration file parameters."""

    redis_config = config["services"]["redis"]
    client = redis.from_url(redis_config["url"], decode_responses=True)

    yield client

    client.close()


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


def handle_signals(
    signals: Sequence[int],
    callback: Callable[[], Any] | None = None,
    cancel: bool = True,
):
    """Runs a callback when a signal is received during the execution of a task.

    This function is meant to decorate coroutines. If a signal matching ``signals``
    is received, the callback is run and the coroutine (which is executed as a task)
    is cancelled if ``cancel=True``.

    """

    def _handle_signal(task: asyncio.Task):
        if cancel:
            task.cancel()

        if callback:
            asyncio.get_running_loop().call_soon(callback)

    def _outter_wrapper(coro_func):
        @functools.wraps(coro_func)
        async def _inner_wrapper(*args, **kwargs):
            task = asyncio.create_task(coro_func(*args, **kwargs))

            handler = partial(_handle_signal, task)
            for sgn in signals:
                asyncio.get_running_loop().add_signal_handler(sgn, handler)

            try:
                try:
                    return await task
                except asyncio.CancelledError:
                    raise KeyboardInterrupt()
            finally:
                for sgn in signals:
                    asyncio.get_running_loop().remove_signal_handler(sgn)

        return _inner_wrapper

    return _outter_wrapper


def get_by_source_id(source_id: int) -> dict | None:
    """Returns Gaia DR3 information for a source ID."""

    db = get_db_connection()

    gaia_dr3 = peewee.Table("gaia_dr3_source", schema="catalogdb").bind(db)

    query = gaia_dr3.select(gaia_dr3.star).where(gaia_dr3.c.source_id == source_id)
    data = query.dicts()

    if len(data) == 0:
        return None

    return data[0]


async def get_ephemeris_summary(sjd: int | None = None) -> dict:
    """Returns the ephemeris summary from ``lvmapi``."""

    host = config["services"]["lvmapi"]["host"]
    port = config["services"]["lvmapi"]["port"]
    url = f"http://{host}:{port}/ephemeris/summary"

    sjd = sjd or get_sjd("LCO")

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"sjd": sjd})
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /ephemeris/summary")

    return resp.json()


def get_ephemeris_summary_sync(sjd: int | None = None) -> dict:
    """Returns the ephemeris summary from ``lvmapi``."""

    host = config["services"]["lvmapi"]["host"]
    port = config["services"]["lvmapi"]["port"]
    url = f"http://{host}:{port}/ephemeris/summary"

    sjd = sjd or get_sjd("LCO")

    with httpx.Client() as client:
        resp = client.get(url, params={"sjd": sjd})
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /ephemeris/summary")

    return resp.json()


async def get_lvmapi_route(route: str, params: dict = {}, **kwargs):
    """Gets an ``lvmapi`` route."""

    params.update(kwargs)

    host, port = config["services"]["lvmapi"].values()

    async with httpx.AsyncClient(
        base_url=f"http://{host}:{port}",
        follow_redirects=True,
    ) as client:
        response = await client.get(route, params=params)

        if response.status_code != 200:
            raise ValueError(f"Route {route} failed with error {response.status_code}.")

    return response.json()


class GuiderMonitor:
    """A tool to monitor guider outputs and store them in a dataframe."""

    def __init__(self, gort: GortClient, actor: str | None = None):
        self.gort = gort
        self.actor = actor

        self._data: dict[tuple[int, str], dict[str, Any]] = {}

        self._schema = {
            "frameno": polars.Int32(),
            "telescope": polars.String(),
            "time": polars.Datetime(time_zone="UTC"),
            "n_sources": polars.Int32(),
            "focus_position": polars.Float32(),
            "fwhm": polars.Float32(),
            "ra": polars.Float64(),
            "dec": polars.Float64(),
            "ra_offset": polars.Float32(),
            "dec_offset": polars.Float32(),
            "separation": polars.Float32(),
            "pa": polars.Float32(),
            "pa_offset": polars.Float32(),
            "zero_point": polars.Float32(),
            "mode": polars.String(),
            "ax0_applied": polars.Float32(),
            "ax1_applied": polars.Float32(),
            "rot_applied": polars.Float32(),
        }

    def reset(self):
        """Resets the internal state."""

        self.__init__(self.gort, self.actor)

    def start_monitoring(self):
        """Starts monitoring the guider outputs."""

        if self._handle_guider_reply not in self.gort._callbacks:
            self.gort.add_reply_callback(self._handle_guider_reply)

        self.reset()

    def stop_monitoring(self):
        """Stops monitoring the guider outputs."""

        if self._handle_guider_reply in self.gort._callbacks:
            self.gort.remove_reply_callback(self._handle_guider_reply)

    def __del__(self):
        self.stop_monitoring()

    def get_dataframe(self) -> polars.DataFrame | None:
        """Returns the collected data as a dataframe."""

        if len(self._data) == 0:
            return

        values = self._data.values()
        dfs = [polars.DataFrame([dv], schema=self._schema) for dv in values]

        return polars.concat(dfs).sort(["frameno", "telescope"])

    async def _handle_guider_reply(self, reply: AMQPReply):
        """Processes an actor reply and stores the collected data."""

        if self.actor is not None:
            if self.actor not in str(reply.sender):
                return
        else:
            if ".guider" not in str(reply.sender):
                return

        body = reply.body

        telescope = str(reply.sender).split(".")[1]
        frameno: int | None = None
        new_data: dict[str, Any] = {}

        try:
            if "frame" in body:
                frame = body["frame"]
                frameno = frame["seqno"]
                new_data = {
                    "frameno": frameno,
                    "telescope": telescope,
                    "time": datetime.datetime.now(datetime.timezone.utc),
                    "n_sources": frame["n_sources"],
                    "focus_position": frame["focus_position"],
                    "fwhm": frame["fwhm"],
                }

            elif "measured_pointing" in body:
                measured_pointing = body["measured_pointing"]
                frameno = measured_pointing["frameno"]
                new_data = {
                    "frameno": frameno,
                    "telescope": telescope,
                    "ra": measured_pointing["ra"],
                    "dec": measured_pointing["dec"],
                    "ra_offset": measured_pointing["radec_offset"][0],
                    "dec_offset": measured_pointing["radec_offset"][1],
                    "separation": measured_pointing["separation"],
                    "pa": measured_pointing.get("pa", numpy.nan),
                    "pa_offset": measured_pointing.get("pa_offset", numpy.nan),
                    "zero_point": measured_pointing.get("zero_point", numpy.nan),
                    "mode": measured_pointing["mode"],
                }

            elif "correction_applied" in body:
                correction_applied = body["correction_applied"]
                frameno = correction_applied["frameno"]
                new_data = {
                    "frameno": frameno,
                    "telescope": telescope,
                    "ax0_applied": correction_applied["motax_applied"][0],
                    "ax1_applied": correction_applied["motax_applied"][1],
                    "rot_applied": correction_applied.get("rot_applied", 0.0),
                }
            else:
                return

            if not isinstance(frameno, int):
                return

            key = (frameno, telescope)
            if key in self._data:
                self._data[key].update(new_data)
            else:
                self._data[key] = new_data

        except Exception as err:
            self.gort.log.warning(f"Error processing guider reply: {err}")

    def to_header(self):
        """Returns a header with pointing and guiding information."""

        header: dict[str, Any] = {}

        telescopes = ["sci", "spec", "skye", "skyw"]
        if self.actor is not None:
            telescopes = [self.actor.split(".")[1]]

        df = self.get_dataframe()

        if df is not None:
            for tel in telescopes:
                try:
                    tel_data = df.filter(polars.col.telescope == tel)
                    if len(tel_data) < 2:
                        frame0 = None
                        framen = None
                    else:
                        frame0 = int(tel_data["frameno"].min())  # type: ignore
                        framen = int(tel_data["frameno"].max())  # type: ignore

                    header.update(
                        {
                            f"G{tel.upper()}FR0": (frame0, f"{tel} first guider frame"),
                            f"G{tel.upper()}FRN": (framen, f"{tel} last guider frame"),
                        }
                    )

                except Exception as err:
                    self.gort.specs.write_to_log(
                        f"Failed updating guider header information for {tel}: {err}",
                        "warning",
                    )
                    continue

        return header


async def is_actor_running(actor: str, client: AMQPClient | None = None):
    """Returns :obj:`True` if the actor is running.

    This assumes that the actor implements a `ping` command.

    """

    close_client: bool = False
    if not client:
        client = await AMQPClient().start()
        close_client = True

    # TODO: this can fail if the event loop has closed the client connection.
    ping = await client.send_command(actor, "ping")

    if close_client:
        await client.stop()

    if ping.status.did_succeed:
        return True

    return False


async def overwatcher_is_running(client: AMQPClient | None = None):
    """Returns :obj:`True` if the overwatcher is running."""

    overwatcher_actor_name = config["overwatcher"]["actor"]["name"]
    return await is_actor_running(overwatcher_actor_name, client=client)


async def overwatcher_is_enabled(client: AMQPClient | None = None):
    """Returns :obj:`True` if the overwatcher is enabled."""

    close_client: bool = False
    if not client:
        client = await AMQPClient().start()
        close_client = True

    if not (await overwatcher_is_running(client)):
        return False

    overwatcher_actor_name = config["overwatcher"]["actor"]["name"]
    status = await client.send_command(overwatcher_actor_name, "status")

    if close_client:
        await client.stop()

    if status.status.did_succeed:
        return status.replies.get("status")["enabled"]

    raise GortError("Cannot determine if the Overwatcher is running.")


def check_overwatcher_not_running(coro):
    """Decorator that fails a coroutine if the overwatcher is running."""

    @wraps(coro)
    async def wrapper(*args, **kwargs):
        if await overwatcher_is_running():
            raise GortError(
                f"Overwatcher is running. Cannot execute {coro.__name__}.",
                ErrorCode.OVERATCHER_RUNNING,
            )
        return await coro(*args, **kwargs)

    return wrapper


async def kubernetes_list_deployments():
    """Retrieves the Kubernetes deployments from the API."""

    return await get_lvmapi_route("/kubernetes/deployments/list")


async def kubernetes_restart_deployment(name: str):
    """Restarts a Kubernetes deployment via the API."""

    return await get_lvmapi_route(f"/kubernetes/deployments/{name}/restart")


@asynccontextmanager
async def get_gort_client(override_overwatcher: bool | None = None):
    """Returns a GORT client."""

    from gort import Gort

    override_envvar = os.environ.get("GORT_OVERRIDE_OVERWATCHER", "0")
    if override_overwatcher is None:
        override_overwatcher = False if override_envvar == "0" else True

    gort = await Gort(
        override_overwatcher=override_overwatcher,
        verbosity="debug",
    ).init()

    yield gort

    await gort.stop()


NightLogCategories = Literal["weather", "issues", "other", "observers", "overwatcher"]


async def add_night_log_comment(comment: str, category: NightLogCategories = "other"):
    """Adds a comment to the night log."""

    payload = {
        "mjd": get_sjd("LCO"),
        "category": category or "other",
        "comment": comment,
    }

    host, port = config["services"]["lvmapi"].values()

    async with httpx.AsyncClient(
        base_url=f"http://{host}:{port}",
        follow_redirects=True,
    ) as client:
        response = await client.post("/logs/night-logs/comments/add", json=payload)

        code = response.status_code
        if code != 200:
            raise ValueError(f"Failed adding night log comment. Code {code}.")


async def async_noop(*args, **kwargs):
    """A no-op coroutine."""

    return None
