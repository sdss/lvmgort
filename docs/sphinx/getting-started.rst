Getting started with GORT
=========================

What is GORT?
-------------

``gort`` (`Genetically Organized Robotic Technology <https://en.wikipedia.org/wiki/Gort_(The_Day_the_Earth_Stood_Still)>`__) is a high-level library to interact with the Local Volume Mapper (LVM) observatory.

The LVM software is based on the concept of :ref:`actor <clu:new-actors>`. An actor is a consumer that receives commands from a client, executes an action (expose an spectrograph, open the dome), and communicates back to the user via replies. LVM uses AMQP/RabbitMQ as the message passing system for commands and replies.

`gort` provides three levels of abstraction for accessing the LVM infrastructure:

- The lowest level access is a :ref:`programmatic API <actor-programmatic>` to interact with the individual actors that run in the LVM infrastructure.
- On top of that, `gort` defines a series of `.GortDeviceSet` and `.GortDevice` classes that expose specific :ref:`device functionality <device-sets>` that is of general use. These classes provide less comprehensive access to the actor devices, but encapsulate the main features that a user is likely to use, and allow to command multiple devices as one, e.g., to send all four telescopes to zenith.
- The highest level part of `gort` provides the tools for unassisted observing, e.g., observing a tile. Ultimately, `gort` provides a robotic mode that takes care of all aspects of observing.

Minimal example
---------------

The following is a minimum example of how to use the `.Gort` client to connect to the actor system and retrieve the status of the science telescope.

This code must be run in one of the LVM mountain servers with access to the RabbitMQ exchange.

.. code-block:: python

    >>> from gort import Gort

    >>> g = await Gort().init()
    >>> g.connected()
    True

    >>> await g.telescopes.sci.update_status()
    {'is_tracking': False,
    'is_connected': True,
    'is_slewing': False,
    'is_enabled': False,
    'ra_j2000_hours': 9.31475952237581,
    'dec_j2000_degs': 26.2017449132552,
    'ra_apparent_hours': 9.33749451003047,
    'dec_apparent_degs': 26.1043875064315,
    'altitude_degs': -61.6417256185769,
    'azimuth_degs': 88.1701770599825,
    'field_angle_rate_at_target_degs_per_sec': 0.0,
    'field_angle_here_degs': -14.3931305251519,
    'field_angle_at_target_degs': 0.0,
    'axis0': {'dist_to_target_arcsec': 0.0,
    'is_enabled': False,
    'position_degs': 308.137461864407,
    'rms_error_arcsec': 0.0,
    'servo_error_arcsec': 0.0},
    'axis1': {'dist_to_target_arcsec': 0.0,
    'is_enabled': False,
    'position_degs': 308.137461864407,
    'rms_error_arcsec': 0.0,
    'servo_error_arcsec': 0.0,
    'position_timestamp': '2023-03-11 16:54:22.5626'},
    'model': {'filename': 'DefaultModel.pxp',
    'num_points_enabled': 99,
    'num_points_total': 111,
    'rms_error_arcsec': 18.1248458630523,
    'position_degs': 22.8931398305085,
    'position_timestamp': '2023-03-11 16:54:22.5626'},
    'geometry': 1}

``gort`` is an asynchronous library which enables multiple processes to run at the same time without blocking the event loop. ``gort`` is written using `asyncio`.  A certain familiarity with asynchronous programming is recommended, but at a minimum, many of ``gort``'s functions and methods are actually *coroutines* that need to be *awaited* when called. Awaiting a coroutine informs the event manager that other coroutines can be run at the same time, allowing concurrency. In the :ref:`API <api>` documentation, coroutines are prefixed by an *async* label. Those methods and functions need to be awaited.

.. _actor-programmatic:

Programmatic actor access
-------------------------

AMQP actors typically receive commands as a string with CLI-like format. For example, to expose spectrograph ``sp1`` and take a dark of 900 seconds one would do ::

    lvmscp.sp1 expose --dark 900

The programmatic interface allows to convert this command to an asynchronous coroutine like ::

    await remote_actor.commands.expose(dark=True, exposure_time=900)

where ``remote_actor`` is a `.RemoteActor` instance that represents the ``lvmscp.sp1`` actor.

Remote actors can be added to a `.Gort` instance by calling the `~.Gort.add_actor` method with the name of the actor. This requires the actor to be running CLU 2.0+ and accept the ``get-command-model`` command ::

    >>> g = await Gort().init()
    >>> lvmscp_sp1 = g.add_actor('lvmscp.sp1')
    >>> await lvmscp_sp1.init()
    >>> type(lvmscp_sp1)
    gort.core.RemoteActor

In practice, when an instance of `.Gort` is created, most if not relevant actors are added as remote actors and initialised, and can be accessed from the ``Gort.actors`` dictionary ::

    >>> lvmscp_sp1 = g.actors['lvmscp.sp1']
    >>> type(lvmscp_sp1)
    gort.core.RemoteActor

The list of available commands is accessible as a dictionary of `.RemoteCommand` under the ``commands`` attribute ::

    >>> lvmscp_sp1.commands
    {'abort': <gort.core.RemoteCommand at 0x7f873f216a90>,
     'config': <gort.core.RemoteCommand at 0x7f873f216ad0>,
     'disconnect': <gort.core.RemoteCommand at 0x7f873f216b90>,
     'expose': <gort.core.RemoteCommand at 0x7f873f216bd0>,
     'flush': <gort.core.RemoteCommand at 0x7f873f216c50>,
     'focus': <gort.core.RemoteCommand at 0x7f873f216cd0>,
     'frame': <gort.core.RemoteCommand at 0x7f873f216d50>,
     'get_command_model': <gort.core.RemoteCommand at 0x7f873f216fd0>,
     'get_window': <gort.core.RemoteCommand at 0x7f873f216f10>,
     'get_schema': <gort.core.RemoteCommand at 0x7f873f217010>,
     'hardware_status': <gort.core.RemoteCommand at 0x7f873f217090>,
     'help': <gort.core.RemoteCommand at 0x7f873f217150>,
     'init': <gort.core.RemoteCommand at 0x7f873f2171d0>,
     'keyword': <gort.core.RemoteCommand at 0x7f873f217250>,
     'ping': <gort.core.RemoteCommand at 0x7f873f2172d0>,
     'power': <gort.core.RemoteCommand at 0x7f873f217350>,
     'read': <gort.core.RemoteCommand at 0x7f873f2173d0>,
     'reconnect': <gort.core.RemoteCommand at 0x7f873f217450>,
     'reset': <gort.core.RemoteCommand at 0x7f873f2174d0>,
     'set_window': <gort.core.RemoteCommand at 0x7f873f217550>,
     'status': <gort.core.RemoteCommand at 0x7f873f217610>,
     'system': <gort.core.RemoteCommand at 0x7f873f217690>,
     'talk': <gort.core.RemoteCommand at 0x7f873f217710>,
     'version': <gort.core.RemoteCommand at 0x7f873f217790>}

These `.RemoteCommand` can be called and awaited with the arguments the command accepts ::

    >>> await lvmscp_sp1.commands.hardware_status()
    ActorReply(actor=<RemoteActor (name=lvmscp.sp1)>, command=<Command finished result=...>, replies=[{'error': "Failed routing message to consumer 'lvmieb'."}, {'error': "Failed routing message to consumer 'lvmieb'."}, {'error': "Failed routing message to consumer 'lvmieb'."}, {'error': "Failed routing message to consumer 'lvmieb'."}, {'error': "Failed routing message to consumer 'lvmieb'."}, {'error': "Failed routing message to consumer 'lvmieb'."}])

`.RemoteCommand` returns an `.ActorReply` which includes all the replies generated by the command, which can be accessed as a list under `.ActorReply.replies`. It's often convenient to flatten all the replies into a single dictionary of keyword-value ::

    >>> replies = await lvmscp_sp1.commands.ping()
    >>> replies.flatten()
    {'text': 'Pong.'}

Under the hood, `.RemoteCommand` are implemented using `unclick <https://github.com/albireox/unclick>`__, a reverse parser for `click <https://click.palletsprojects.com/en/8.1.x/>`__. Some features and options may not be fully implemented.


.. _device-sets:

Device sets
-----------

`.Gort` defines a series of `.GortDeviceSet` objects that allow the user to communicate with the various infrastructure devices at a relatively high level. Each `.GortDeviceSet` is composed of one or more `.GortDevice`, each associated to a physical device and with an associated actor.

For example, ``Gort.telescopes`` provides methods to command all four telescopes. The `.TelescopeSet` is composed of four `.Telescope` devices, ``sci``, ``skye``, ``skyw``, ``spec`` that provide access to a single telecope. This allows to, for example, move all telescopes to zenith as one ::

    >>> await g.telescopes.goto_named_position('zenith')

or command only one telescope ::

    >>> await g.telescopes.sci.goto_named_position('zenith')

Devices can have their own subdevices. For example all the `.Telescope` instances have `.Focuser` devices that allow to command the focuser ::

    >>> await g.telescopes.skyw.focuser.home()

More details on how to use the device sets for observing, with code examples, are provided :ref:`here <observing>`.

Using ``gort`` in IPython
-------------------------

``gort`` can generally be used in IPython, but note that there's a small caveat. As described `here <https://ipython.readthedocs.io/en/stable/interactive/autoawait.html#difference-between-terminal-ipython-and-ipykernel>`__, IPython does not keep a running event loop while a command is not being executed. This means that `.Gort` cannot keep a connection open to the RabbitMQ exchange and eventually the connection closes.

`.Gort` will try to recreate the connection to the exchange when needed, if it finds it closed, but this can fail in some corner cases. In this case simply recreate the `.Gort` client with ::

    g = await Gort().init()

This issue should not affect running ``gort`` on an script or in a Jupyter notebook, which runs a persistent background event loop.
