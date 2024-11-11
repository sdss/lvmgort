#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-16
# @Filename: gort.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
import uuid
from copy import deepcopy

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Generic,
    Literal,
    Sequence,
    Type,
    TypeVar,
)

from lvmopstools.retrier import Retrier
from packaging.version import Version
from rich import pretty, traceback
from rich.logging import RichHandler
from typing_extensions import Self

from clu.client import AMQPClient, AMQPReply
from sdsstools.logger import SDSSLogger, get_logger
from sdsstools.time import get_sjd

from gort import config
from gort.core import RemoteActor
from gort.enums import Event
from gort.exceptions import ErrorCode, GortError
from gort.observer import GortObserver
from gort.pubsub import notify_event
from gort.recipes import recipes as recipe_to_class
from gort.tile import Tile
from gort.tools import (
    get_temporary_file_path,
    kubernetes_list_deployments,
    kubernetes_restart_deployment,
    overwatcher_is_running,
    run_in_executor,
    set_tile_status,
)


if TYPE_CHECKING:
    from rich.console import Console


try:
    IPYTHON = get_ipython()  # type: ignore
    IPYTHON_DEFAULT_HOOKS = [
        IPYTHON._showtraceback,
        IPYTHON.showtraceback,
        IPYTHON.showsyntaxerror,
    ]
except NameError:
    IPYTHON = None
    IPYTHON_DEFAULT_HOOKS = []


__all__ = ["GortClient", "Gort", "GortDeviceSet", "GortDevice"]


DevType = TypeVar("DevType", bound="GortDeviceSet | GortDevice")


class GortClient(AMQPClient):
    """The main ``gort`` client, used to communicate with the actor system.

    A subclass of :obj:`~clu.client.AMQPClient` with defaults for host and logging,
    it loads the :obj:`.GortDeviceSet` and :obj:`.RemoteActor` instances
    for the LVM system.

    Parameters
    ----------
    host
        The host on which the RabbitMQ exchange is running.
    port
        The port on which the RabbitMQ exchange listens to connections.
    user
        The user to connect to the exchange.
    password
        The password to connect to the exchange.
    use_rich_output
        If :obj:`True`, uses ``rich`` to provide colourised tracebacks and
        prettier outputs.
    log_file_path
        The path where to save GORT's log. File logs are always saved with ``DEBUG``
        logging level. If :obj:`None`, a temporary file will be used whose path
        can be retrieved by calling :obj:`.get_log_path`. If :obj:`False`, no file
        logging will happen.

    """

    def __init__(
        self,
        host: str = "lvm-hub.lco.cl",
        port: int = 5672,
        user: str = "guest",
        password: str = "guest",
        use_rich_output: bool = True,
        log_file_path: str | pathlib.Path | Literal[False] | None = None,
    ):
        from gort.devices.ag import AGSet
        from gort.devices.enclosure import Enclosure
        from gort.devices.guider import GuiderSet
        from gort.devices.nps import NPSSet
        from gort.devices.spec import SpectrographSet
        from gort.devices.telemetry import TelemetrySet as TelemSet
        from gort.devices.telescope import TelescopeSet as TelSet

        self.client_uuid = str(uuid.uuid4()).split("-")[0]

        self._console: Console

        log = self._prepare_logger(
            log_file_path,
            use_rich_output=use_rich_output,
        )

        self._setup_exception_hooks(log, use_rich_output=use_rich_output)

        self._connect_lock = asyncio.Lock()

        super().__init__(
            f"Gort-client-{self.client_uuid}",
            host=host,
            port=port,
            user=user,
            password=password,
            log=log,
        )

        self.actors: dict[str, RemoteActor] = {}

        self.config = deepcopy(config)

        self.__device_sets = []

        self.ags = self.add_device(AGSet, self.config["ags"]["devices"])
        self.guiders = self.add_device(GuiderSet, self.config["guiders"]["devices"])
        self.telescopes = self.add_device(TelSet, self.config["telescopes"]["devices"])
        self.nps = self.add_device(NPSSet, self.config["nps"]["devices"])
        self.specs = self.add_device(SpectrographSet, self.config["specs"]["devices"])
        self.enclosure = self.add_device(Enclosure, name="enclosure", actor="lvmecp")
        self.telemetry = self.add_device(TelemSet, self.config["telemetry"]["devices"])

    def _prepare_logger(
        self,
        log_file_path: str | pathlib.Path | Literal[False] | None = None,
        use_rich_output: bool = True,
    ):
        """Creates a logger and start file logging."""

        log = get_logger(
            f"lvmgort-{self.client_uuid}",
            use_rich_handler=True,
            rich_handler_kwargs={"rich_tracebacks": use_rich_output},
        )

        if log_file_path is None:
            path = config["logging"]["path"]
            sjd = get_sjd()
            path = path.format(SJD=sjd)

            try:
                log.start_file_logger(str(path), rotating=False, mode="a")
                log.info("Starting GORT.")
                log.info(f"Logging to {str(path)}")

            except Exception as err:
                tmp_path = get_temporary_file_path(
                    prefix="gort-",
                    suffix=f"-{self.client_uuid}.log",
                )
                log.warning(
                    f"Failed starting file logging to {str(path)}: {err}. "
                    f"Using temporary path {str(tmp_path)}."
                )

                log.start_file_logger(str(tmp_path), rotating=False)

        elif log_file_path is not False:
            log.start_file_logger(str(log_file_path), rotating=False)

        assert isinstance(log.sh, RichHandler)
        self._console = log.sh.console

        return log

    def _setup_exception_hooks(self, log: SDSSLogger, use_rich_output: bool = True):
        """Setup various hooks for exception handling."""

        def custom__showtraceback_closure(default__showtraceback):
            def _showtraceback(*args, **kwargs):
                assert IPYTHON

                exc_tuple = IPYTHON._get_exc_info()
                log.handle_exceptions(*exc_tuple)

                default__showtraceback(*args, **kwargs)

            if IPYTHON:
                IPYTHON._showtraceback = _showtraceback

        if use_rich_output:
            traceback.install(console=self._console)
            pretty.install(console=self._console)

            # traceback.install() overrides the excepthook, which means that
            # tracebacks are not logged to file anymore. Restore that.
            sys.excepthook = log.handle_exceptions

        else:
            # Make sure we are not using rich tracebacks anymore, in
            # case we installed them at some point.
            # See https://github.com/Textualize/rich/pull/2972/files

            sys.excepthook = log.handle_exceptions

            if IPYTHON:
                IPYTHON._showtraceback = IPYTHON_DEFAULT_HOOKS[0]
                IPYTHON.showtraceback = IPYTHON_DEFAULT_HOOKS[1]
                IPYTHON.showsyntaxerror = IPYTHON_DEFAULT_HOOKS[2]

        # if IPYTHON:
        #     # One more override. We want that any exceptions raised in IPython
        #     # also gets logged to file, but IPython overrides excepthook completely
        #     # so here we make a custom call to log.handle_exceptions() and then
        #     # just let it do whatever it was its default (whether that means it
        #     # was overridden by rich or not).
        #     custom__showtraceback_closure(IPYTHON._showtraceback)

    def get_log_path(self):
        """Returns the path of the log file. :obj:`None` if not logging to file."""

        return self.log.log_filename

    async def init(self) -> Self:
        """Initialises the client.

        Returns
        -------
        object
            The same instance of :obj:`.GortClient` after initialisation.

        """

        if not self.connected:
            async with self._connect_lock:
                await self.start()

        override_overwatcher = getattr(self, "_override_overwatcher", None)
        override_envvar = os.environ.get("GORT_OVERRIDE_OVERWATCHER", "0")
        if override_overwatcher is None:
            override_overwatcher = False if override_envvar == "0" else True

        if override_overwatcher is None:
            pass
        else:
            overwatcher_running = await overwatcher_is_running(self)
            if overwatcher_running:
                if override_overwatcher:
                    self.log.warning("Overwatcher is running, be careful!")
                else:
                    raise GortError(
                        "Overwatcher is running. If you really want to use GORT "
                        "initialise it with Gort(override_overwatcher=True).",
                        error_code=ErrorCode.OVERATCHER_RUNNING,
                    )

        await asyncio.gather(*[ractor.init() for ractor in self.actors.values()])

        # Initialise device sets.
        await asyncio.gather(*[dev.init() for dev in self.__device_sets])

        return self

    def add_device(self, class_: Type[DevType], *args, **kwargs) -> DevType:
        """Adds a new device or device set to Gort."""

        ds = class_(self, *args, **kwargs)
        self.__device_sets.append(ds)

        return ds

    @property
    def connected(self):
        """Returns :obj:`True` if the client is connected."""

        return self.connection and self.connection.connection is not None

    def add_actor(self, actor: str, device: GortDevice | None = None):
        """Adds an actor to the programmatic API.

        Parameters
        ----------
        actor
            The name of the actor to add.
        device
            A device associated with this actor.

        """

        if actor not in self.actors:
            self.actors[actor] = RemoteActor(self, actor, device=device)

        return self.actors[actor]

    def set_verbosity(self, verbosity: str | int | None = None):
        """Sets the level of verbosity to ``debug``, ``info``, or ``warning``.

        Parameters
        ----------
        verbosity
            The level of verbosity. Can be a string level name, an integer, or
            :obj:`None`, in which case the default verbosity will be used.

        """

        verbosity = verbosity or "warning"
        if isinstance(verbosity, int):
            verbosity = logging.getLevelName(verbosity)

        assert isinstance(verbosity, str)

        verbosity = verbosity.lower()
        if verbosity not in ["debug", "info", "warning"]:
            raise ValueError("Invalid verbosity value.")

        verbosity_level = logging.getLevelName(verbosity.upper())
        self.log.sh.setLevel(verbosity_level)


GortDeviceType = TypeVar("GortDeviceType", bound="GortDevice")


class GortDeviceSet(dict[str, GortDeviceType], Generic[GortDeviceType]):
    """A set to gort-managed devices.

    Devices can be accessed as items of the :obj:`.GortDeviceSet` dictionary
    or using dot notation, as attributes.

    Parameters
    ----------
    gort
        The :obj:`.GortClient` instance.
    data
        A mapping of device to device info. Each device must at least include
        an ``actor`` key with the actor to use to communicated with the device.
        Any other information is passed to the :obj:`.GortDevice` on instantiation.
    kwargs
        Other keyword arguments to pass wo the device class.

    """

    __DEVICE_CLASS__: ClassVar[Type["GortDevice"]]
    __DEPLOYMENTS__: ClassVar[list[str]] = []

    def __init__(self, gort: GortClient, data: dict[str, dict], **kwargs):
        self.gort = gort

        _dict_data = {}
        for device_name in data:
            device_data = data[device_name].copy()
            actor_name = device_data.pop("actor")
            _dict_data[device_name] = self.__DEVICE_CLASS__(
                gort,
                device_name,
                actor_name,
                **device_data,
                **kwargs,
            )

        dict.__init__(self, _dict_data)

    async def init(self):
        """Runs asynchronous tasks that must be executed on init."""

        # Run devices init methods.
        results = await asyncio.gather(
            *[dev.init() for dev in self.values()],
            return_exceptions=True,
        )

        for idev, result in enumerate(results):
            if isinstance(result, Exception):
                self.write_to_log(
                    f"Failed initialising device {list(self)[idev]} "
                    f"with error {str(result)}",
                    "error",
                )

        return

    def __getattribute__(self, __name: str) -> Any:
        if __name in self:
            return self.__getitem__(__name)
        return super().__getattribute__(__name)

    async def call_device_method(self, method: Callable, *args, **kwargs):
        """Calls a method in each one of the devices.

        Parameters
        ----------
        method
            The method to call. This must be the abstract class method,
            not the method from an instantiated object.
        args,kwargs
            Arguments to pass to the method.

        """

        if not callable(method):
            raise GortError("Method is not callable.")

        if hasattr(method, "__self__"):
            # This is a bound method, so let's get the class method.
            method = method.__func__

        if not hasattr(self.__DEVICE_CLASS__, method.__name__):
            raise GortError("Method does not belong to this class devices.")

        devices = self.values()

        return await asyncio.gather(*[method(dev, *args, **kwargs) for dev in devices])

    async def send_command_all(
        self,
        command: str,
        *args,
        devices: Sequence[str] | None = None,
        **kwargs,
    ):
        """Sends a command to all the devices.

        Parameters
        ----------
        command
            The command to call.
        args, kwargs
            Arguments to pass to the :obj:`.RemoteCommand`.

        """

        tasks = []
        for name, dev in self.items():
            if devices is not None and name not in devices:
                continue

            actor_command = dev.actor.commands[command]
            tasks.append(actor_command(*args, **kwargs))

        return await asyncio.gather(*tasks)

    def write_to_log(
        self,
        message: str,
        level: str = "debug",
        header: str | None = None,
    ):
        """Writes a message to the log with a custom header.

        Parameters
        ----------
        message
            The message to log.
        level
            The level to use for logging: ``'debug'``, ``'info'``, ``'warning'``, or
            ``'error'``.
        header
            The header to prepend to the message. By default uses the class name.

        """

        if header is None:
            header = f"({self.__class__.__name__}) "

        message = f"{header}{message}"

        level = logging.getLevelName(level.upper())
        assert isinstance(level, int)

        self.gort.log.log(level, message)

    async def restart(self):
        """Restarts the set deployments and resets all controllers.

        Returns
        -------
        result
            A boolean indicting if the entire restart procedure succeeded.

        """

        failed: bool = False

        self.write_to_log("Restarting Kubernetes deployments.", "info")
        for deployment in self.__DEPLOYMENTS__:
            await kubernetes_restart_deployment(deployment)

        self.write_to_log("Waiting 15 seconds for deployments to be ready.", "info")
        await asyncio.sleep(15)

        # Check that deployments are running.
        running_deployments = await kubernetes_list_deployments()
        for deployment in self.__DEPLOYMENTS__:
            if deployment not in running_deployments:
                failed = True
                self.write_to_log(f"Deployment {deployment} did not restart.", "error")

        # Refresh the command models for all the actors.
        await asyncio.gather(*[actor.refresh() for actor in self.gort.actors.values()])

        # Refresh the device set.
        await self.init()

        return not failed


class GortDevice:
    """A gort-managed device.

    Parameters
    ----------
    gort
        The :obj:`.GortClient` instance.
    name
        The name of the device.
    actor
        The name of the actor used to interface with this device. The actor is
        added to the list of :obj:`.RemoteActor` in the :obj:`.GortClient`.


    """

    def __init__(self, gort: GortClient, name: str, actor: str):
        self.gort = gort
        self.name = name
        self.actor = gort.add_actor(actor, device=self)

        # Placeholder version. The real one is retrieved on init.
        self.version = Version("0.99.0")

    async def init(self):
        """Runs asynchronous tasks that must be executed on init.

        If the device is part of a :obj:`.DeviceSet`, this method is called
        by :obj:`.DeviceSet.init`.

        """

        # Get the version of the actor.
        if "version" in self.actor.commands:
            try:
                reply = await self.actor.commands.version()
                if (version := reply.get("version")) is not None:
                    self.version = Version(version)
            except Exception:
                pass

        return

    def write_to_log(
        self,
        message: str,
        level: str = "debug",
        header: str | None = None,
    ):
        """Writes a message to the log with a custom header.

        Parameters
        ----------
        message
            The message to log.
        level
            The level to use for logging: ``'debug'``, ``'info'``, ``'warning'``, or
            ``'error'``.
        header
            The header to prepend to the message. By default uses the device name.

        """

        if header is None:
            header = f"({self.name}) "

        message = f"{header}{message}"

        level = logging.getLevelName(level.upper())
        assert isinstance(level, int)

        self.gort.log.log(level, message)

    def log_replies(self, reply: AMQPReply, skip_debug: bool = True):
        """Outputs command replies."""

        if reply.body:
            if reply.message_code in ["w"]:
                level = "warning"
            elif reply.message_code in ["e", "f", "!"]:
                level = "error"
            else:
                level = "debug"
                if skip_debug:
                    return

            self.write_to_log(str(reply.body), level)


class Gort(GortClient):
    """Gort's robotic functionality.

    :obj:`.Gort` is subclass of :obj:`.GortClient` that implements higher-level
    robotic functionality. This is the class a user will normally instantiate and
    interact with.

    Parameters
    ----------
    args, kwargs
        Arguments to pass to :obj:`.GortClient`.
    allow_overwatcher
        If the Overwatcher is running an exception will be raised to prevent
        GORT running commands that may interfere with it. If ``allow_overwatcher=True``
        the exception will be suppressed.
    verbosity
        The level of logging verbosity.

    """

    def __init__(
        self,
        *args,
        override_overwatcher: bool | None = None,
        verbosity: str | None = None,
        config_file: str | pathlib.Path | None = None,
        **kwargs,
    ):
        if config_file:
            config.load(str(config_file))

        super().__init__(*args, **kwargs)

        self._override_overwatcher = override_overwatcher

        self.observer = GortObserver(self)
        self.observe_tile = self.observer.observe_tile

        if verbosity:
            self.set_verbosity(verbosity)

    async def notify_event(self, event: Event, payload: dict[str, Any] = {}):
        """Emits an event notification."""

        try:
            await notify_event(event, payload=payload)
        except TypeError as err:
            if "is not JSON serializable" in str(err):
                self.log.error(
                    f"Failed to notify event {event.name}: payload is not"
                    "serialisable. The event will be emitted without payload."
                )
                await notify_event(event)

    @Retrier(max_attempts=3, delay=3)
    async def emergency_shutdown(self):
        """Parks and closes the telescopes."""

        self.log.warning("Emergency shutdown initiated.")
        await self.notify_event(Event.EMERGENCY_SHUTDOWN)

        try:
            await self.shutdown(
                park_telescopes=True,
                disable_overwatcher=True,
                retry_without_parking=True,
                show_message=False,
            )

        except Exception as err:
            self.log.error(f"Failed to shutdown: {err!r}")
            self.log.warning("Trying to just close the dome.")

            await self.enclosure.close(
                park_telescopes=True,
                force=True,
                retry_without_parking=True,
            )

    async def observe(
        self,
        n_tile_positions: int | None = None,
        adjust_focus: bool = True,
        show_progress: bool | None = None,
        disable_tile_on_error: bool = True,
        wait_if_no_tiles: bool | float = 60.0,
    ):
        """Runs a fully automatic science observing loop."""

        # TODO: add some exception handling for keyboard interrupts.

        self.log.info("Running the cleanup recipe.")
        await self.execute_recipe("cleanup")

        self.log.info("Starting the observe loop.")

        n_completed = 0
        while True:
            try:
                tile = await run_in_executor(Tile.from_scheduler)
                break_from_while: bool = False

                for ipos, dpos in enumerate(tile.dither_positions):
                    is_last = ipos == len(tile.dither_positions) - 1
                    result, _ = await self.observer.observe_tile(
                        tile=tile,
                        dither_position=dpos,
                        async_readout=True,
                        keep_guiding=not is_last,
                        skip_slew_when_acquired=True,
                        run_cleanup=False,
                        cleanup_on_interrupt=True,
                        adjust_focus=adjust_focus,
                        show_progress=show_progress,
                    )

                    if result is False:
                        break_from_while = True
                        break

                    n_completed += 1
                    if n_tile_positions is not None and n_completed >= n_tile_positions:
                        self.log.info(
                            "Number of tile positions reached. "
                            "Finishing the observe loop."
                        )
                        break_from_while = True
                        break

                if break_from_while:
                    break

            except GortError as ee:
                if not disable_tile_on_error:
                    raise

                if ee.error_code == ErrorCode.ACQUISITION_FAILED:
                    tile_id: int | None = ee.payload.get("tile_id", None)
                    if tile_id is None:
                        self.log.error(
                            'Cannot disable tile without a "tile_id. '
                            "Continuing observations without disabling tile."
                        )
                        continue

                    await set_tile_status(tile_id, enabled=False)
                    self.log.warning(
                        f"tile_id={tile_id} has been disabled. "
                        "Continuing observations."
                    )

                elif ee.error_code == ErrorCode.SCHEDULER_CANNOT_FIND_TILE:
                    if wait_if_no_tiles is False:
                        raise

                    if wait_if_no_tiles is True:
                        wait_if_no_tiles = 60.0
                    else:
                        wait_if_no_tiles = float(wait_if_no_tiles)

                    self.log.warning(
                        "The scheduler was not able to find a valid tile to "
                        f"observe. Waiting {wait_if_no_tiles} seconds before "
                        "trying again."
                    )
                    await asyncio.sleep(wait_if_no_tiles)
                    continue

                else:
                    self.log.info(
                        "Error found in observe loop. "
                        "Running cleanup before raising the exception."
                    )
                    await self.execute_recipe("cleanup")
                    raise

            else:
                n_completed += 1

    async def run_script(self, script: str):
        """Runs a script."""

        if not script.endswith(".py"):
            script += ".py"

        path = pathlib.Path(__file__).parent / "../../scripts" / script

        cmd = await asyncio.create_subprocess_shell(f"python {path!s}")
        await cmd.communicate()

    def run_script_sync(self, script: str):
        """Runs a script."""

        if not script.endswith(".py"):
            script += ".py"

        self.log.info(f"Running script {script!r}.")

        path = pathlib.Path(__file__).parent / "../../scripts" / script
        subprocess.run(f"python {path!s}", shell=True)

    async def execute_recipe(self, recipe: str, **kwargs):
        """Executes a recipe.

        Parameters
        ----------
        recipe
            The name of the recipe to execute.
        kwargs
            Arguments to be passed to the recipe.

        """

        if recipe not in recipe_to_class:
            raise ValueError(f"Cannot find recipe {recipe!r}.")

        Recipe = recipe_to_class[recipe]

        return await Recipe(self)(**kwargs)

    async def startup(self, **kwargs):
        """Executes the :obj:`startup <.StartupRecipe>` sequence."""

        return await self.execute_recipe("startup", **kwargs)

    async def shutdown(self, **kwargs):
        """Executes the :obj:`shutdown <.ShutdownRecipe>` sequence."""

        return await self.execute_recipe("shutdown", **kwargs)

    async def cleanup(self, readout: bool = True, turn_lamps_off: bool = True):
        """Executes the :obj:`shutdown <.CleanupRecipe>` sequence."""

        return await self.execute_recipe(
            "cleanup",
            readout=readout,
            turn_lamps_off=turn_lamps_off,
        )

    async def restart_kubernetes_deployments(self, deployment: str):
        """Restarts a Kubernetes deployment."""

        self.log.warning(f"Restarting deployment {deployment!r}.")
        await kubernetes_restart_deployment(deployment)
