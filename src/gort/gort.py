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
import pathlib
import signal
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

from packaging.version import Version
from rich import pretty, traceback
from rich.logging import RichHandler
from typing_extensions import Self

from clu.client import AMQPClient, AMQPReply
from sdsstools.logger import SDSSLogger, get_logger
from sdsstools.time import get_sjd

from gort import config
from gort.core import RemoteActor
from gort.exceptions import GortError
from gort.kubernetes import Kubernetes
from gort.observer import GortObserver
from gort.recipes import recipes as recipe_to_class
from gort.tile import Tile
from gort.tools import get_temporary_file_path, run_in_executor


if TYPE_CHECKING:
    from rich.console import Console

    from gort.exposure import Exposure


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

        try:
            self.kubernetes = Kubernetes(log=self.log)
        except Exception:
            self.log.warning(
                "Gort cannot access the Kubernets cluster. "
                "The Kubernetes module won't be available."
            )
            self.kubernetes = None

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

        if self.gort.kubernetes is None:
            raise GortError("The Kubernetes cluster is not accessible.")

        self.write_to_log("Restarting Kubernetes deployments.", "info")
        for deployment in self.__DEPLOYMENTS__:
            self.gort.kubernetes.restart_deployment(deployment, from_file=True)

        self.write_to_log("Waiting 15 seconds for deployments to be ready.", "info")
        await asyncio.sleep(15)

        # Check that deployments are running.
        running_deployments = self.gort.kubernetes.list_deployments()
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
    verbosity
        The level of logging verbosity.
    on_interrupt
        Action to perform if the loop receives an interrupt signal during
        execution. The only options are :obj:`None` (do nothing, currently
        running commands will continue), or ``stop'`` which will stop the
        telescopes and guiders before exiting.

    """

    def __init__(
        self,
        *args,
        verbosity: str | None = None,
        on_interrupt: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        if verbosity:
            self.set_verbosity(verbosity)

        self.set_signals(mode=on_interrupt)

    def set_signals(self, mode: str | None = None):
        """Defines the behaviour when the event loop receives a signal."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.log.warning(
                "No event loop found. Signals cannot be set and "
                "this may cause other problems."
            )
            return

        async def _stop():
            await asyncio.gather(*[self.telescopes.stop(), self.guiders.stop()])
            sys.exit(1)

        if mode == "stop":
            self.log.debug(f"Adding signal handler {mode!r}.")
            loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(_stop()))

    async def emergency_close(self):
        """Parks and closes the telescopes."""

        tasks = []
        tasks.append(self.telescopes.park(disable=True))
        tasks.append(self.enclosure.close(force=True))

        self.log.warning("Closing and parking telescopes.")
        await asyncio.gather(*tasks)

    async def observe(self, n_tiles: int | None = None):
        """Runs a fully automatic science observing loop."""

        # TODO: add some exception handling for keyboard interrupts.

        self.log.info("Running the cleanup recipe.")
        await self.execute_recipe("cleanup")

        self.log.info("Starting observing loop.")

        n_completed = 0
        while True:
            await self.observe_tile(run_cleanup=False)
            n_completed += 1

            if n_tiles is not None and n_completed >= n_tiles:
                self.log.info("Number of tiles reached. Finishing observing loop.")
                break

    async def observe_tile(
        self,
        tile: Tile | int | None = None,
        ra: float | None = None,
        dec: float | None = None,
        pa: float = 0.0,
        use_scheduler: bool = True,
        exposure_time: float = 900.0,
        n_exposures: int = 1,
        async_readout: bool = True,
        keep_guiding: bool = False,
        guide_tolerance: float = 1.0,
        acquisition_timeout: float = 180.0,
        show_progress: bool | None = None,
        run_cleanup: bool = True,
    ):
        """Performs all the operations necessary to observe a tile.

        Parameters
        ----------
        tile
            The ``tile_id`` to observe, or a :obj:`.Tile` object. If not
            provided, observes the next tile suggested by the scheduler
            (requires ``use_scheduler=True``).
        ra,dec
            The RA and Dec where to point the science telescopes. The other
            telescopes are pointed to calibrators that fit the science pointing.
            Cannot be used with ``tile``.
        pa
            Position angle of the IFU. Defaults to PA=0.
        use_scheduler
            Whether to use the scheduler to determine the ``tile_id`` or
            select calibrators.
        exposure_time
            The length of the exposure in seconds.
        n_exposures
            Number of exposures to take while guiding.
        async_readout
            Whether to wait for the readout to complete or return as soon
            as the readout begins. If :obj:`False`, the exposure is registered
            but the observation is not finished. This should be :obj:`True`
            during normal science operations to allow the following acquisition
            to occur during readout.
        keep_guiding
            If :obj:`True`, keeps the guider running after the last exposure.
            This should be :obj:`False` during normal science operations.
        guide_tolerance
            The guide tolerance in arcsec. A telescope will not be considered
            to be guiding if its separation to the commanded field is larger
            than this value.
        acquisition_timeout
            The maximum time allowed for acquisition. In case of timeout
            the acquired fields are evaluated and an exception is
            raised if the acquisition failed.
        show_progress
            Displays a progress bar with the elapsed exposure time.
        run_cleanup
            Whether to run the cleanup routine.

        """

        # Create tile.
        if isinstance(tile, Tile):
            pass
        elif tile is not None or (tile is None and ra is None and dec is None):
            if use_scheduler:
                tile = await run_in_executor(Tile.from_scheduler, tile_id=tile)
            else:
                raise GortError("Not enough information to create a tile.")

        elif ra is not None and dec is not None:
            if use_scheduler:
                tile = await run_in_executor(Tile.from_scheduler, ra=ra, dec=dec, pa=pa)
            else:
                tile = await run_in_executor(Tile.from_coordinates, ra, dec, pa=pa)

        else:
            raise GortError("Not enough information to create a tile.")

        assert isinstance(tile, Tile)

        # Set the initial dither position. This will make
        # the initial acquisition on that pixel.
        dither_positions = tile.dither_positions
        tile.set_dither_position(dither_positions[0])

        # Create observer.
        observer = GortObserver(self, tile)

        # Run the cleanup routine to be extra sure.
        if run_cleanup:
            await self.cleanup(turn_off=False)

        if tile.tile_id is not None:
            dither_positions_str = ", ".join(map(str, dither_positions))
            self.log.info(
                f"Observing tile_id={tile.tile_id} on "
                f"dither positions #{dither_positions_str}."
            )

        exposures: list[Exposure] = []

        try:
            # Slew telescopes and move fibsel mask.
            await observer.slew()

            # Start guiding.
            await observer.acquire(
                guide_tolerance=guide_tolerance,
                timeout=acquisition_timeout,
            )

            # Loop over the dither positions. For the first one do nothing
            # since we have already acquired for that position.
            for idither, dpos in enumerate(dither_positions):
                if idither != 0:
                    self.log.info(f"Acquiring dither position #{dpos}")
                    await observer.set_dither_position(dpos)

                    # Need to restart the guider monitor so that the new exposure
                    # gets the range of guider frames that correspond to this dither.
                    # GortObserver.expose() doesn't do this because we ask for a single
                    # exposure.
                    await observer.guider_monitor.restart()

                self.log.info(f"Taking exposure for dither position #{dpos}")

                # Should we keep the guider alive during readout?
                keep_guiding_exp = keep_guiding or idither != len(dither_positions) - 1

                # Exposing
                exposure = await observer.expose(
                    exposure_time=exposure_time,
                    show_progress=show_progress,
                    count=n_exposures,
                    async_readout=async_readout,
                    keep_guiding=keep_guiding_exp,
                    dither_position=dpos,
                )

                if isinstance(exposure, list):
                    exposures += exposure
                else:
                    exposures.append(exposure)

        finally:
            # Finish observation.
            await observer.finish_observation()

        return exposures

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

    async def cleanup(self, readout: bool = True, turn_off: bool = True):
        """Executes the :obj:`shutdown <.CleanupRecipe>` sequence."""

        return await self.execute_recipe("cleanup", readout=readout, turn_off=turn_off)
