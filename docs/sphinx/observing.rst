.. _observing:

Remote observing with GORT
==========================

This section provides an overlook on how to use ``gort`` to observe remotely with the LVM facility. This is not a comprehensive guide, and many safety precautions are not covered. Please review the remote observing documentation alongside this guide.

We assume you'll be running this code in an LVM server, most likely in a Jupyter notebook. It's possible to use ``gort`` locally and connect it a remote RabbitMQ exchange. For that, ssh to the LVM servers and forward port 5672 in ``lvm-hub.lco.cl`` to localhost on a random port, say 9876. Then you can connect to the remote exchange with ::

    from gort import Gort
    g = await Gort(host='localhost', port=9876).init()

.. warning::
    It may seem from this that security to access the actor system and associated hardware is lax. In reality neither the host nor any of the services are open to the outside world, and the RabbitMQ exchange can only be accessed by secure shell, which requires the user being granted access. Given that, we consider that additional security in the form of passwords is unnecessary.

Afternoon checkouts
-------------------

As usual, we start by creating an instance of `.Gort`. Multiple instance can run at the same time as the client name is unique. ::

    from gort import Gort
    g = await Gort().init()

Note that we instantiate `.Gort` and then call `~.GortClient.init`, a coroutine (thus the ``await``) which actually creates the connection to the RabbitMQ exchange, loads the remote actors, etc. For convenience, `~.GortClient.init` returns the same object so everything can be written in a single line.

Next we set the logging level to ``info``. By default ``gort`` will only raise an error on a critical failure, and won't emit any visible message if a routine succeeds. This follows the rule that successful processes should not write to stdout. However, for normal operations, it's useful to have a more verbose output. You can set the logging level even lower, to ``debug``. ::

    g.set_verbosity('info')

A first good step every afternoon is to home the telescopes, which may have lost their zero points during the day. For that do ::

    >>> await g.telescopes.home()
    02:31:17 [INFO]: (sci) Homing telescope.
    02:31:17 [INFO]: (spec) Homing telescope.
    02:31:17 [INFO]: (skye) Homing telescope.
    02:31:17 [INFO]: (skyw) Homing telescope.

Note that we are commanding all four telescopes at the same time. This routine connects and energises the axes of the mounts and runs a homing routine, which may take up one minute. If during the night it seems the telescopes are not pointing correctly, it's useful to re-home them.

Next, we take a calibration sequence ::

    await g.spec.calibrate(sequence='normal')

This will take calibration flats and arcs, and a series of biases and darks. The full sequence can take over an hour and the routine will output log messages indicating what it's doing. In the background, this sequence moves all the telescopes to point to the flat field screen, turns on the necessary lamps, and exposes the spectrographs.

When the sequence finishes and we are ready to start observations, it's time to open the dome ::

    await g.enclosure.open()

This command will block until the dome is fully open, and will return an error if it fails. The movement can be stopped by doing ::

    await g.enclosure.stop()

.. warning::
    Jupyter notebooks don't allow to run more than one cell at the same time, so in practice it's not possible to have concurrency. If you need to do an emergency stop of the enclosure while it is already moving, you'll need to first stop the running cell (note that this won't stop the command that opens the dome) and then run another cell with the stop command.

Misc
----

The following is an unsorted list of operations and troubleshooting using ``gort``.

Moving the k-mirror to any position
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Normally ``gort`` will determine if the k-mirror of a telescope needs to be moved, parked, or set to tracking. To move it manually you can do ::

    >>> await g.telescopes.sci.km.move(80)
    03:02:20 [INFO]: (sci.km) Moving k-mirror to 80.000 degrees.
    03:02:20 [DEBUG]: (sci.km) Stopping slew.
    03:02:20 [DEBUG]: (sci.km) Moving k-mirror to absolute position.

The k-mirror can be parked with ::

    >>> await g.telescopes.sci.km.park()
    03:09:29 [INFO]: (sci.km) Moving k-mirror to 90.000 degrees.
    03:09:29 [DEBUG]: (sci.km) Stopping slew.
    03:09:29 [DEBUG]: (sci.km) Moving k-mirror to absolute position.

Lower level access to the k-mirror features can be obtained using the programmatic actor interface ::

    >>> g.telescopes.sci.km.actor.commands
    {'__commands': <gort.core.RemoteCommand at 0x7f5a715e33d0>,
     'chat': <gort.core.RemoteCommand at 0x7f5a715e3410>,
     'get_command_model': <gort.core.RemoteCommand at 0x7f5a715e3510>,
     'getAbsoluteEncoderPosition': <gort.core.RemoteCommand at 0x7f5a715e3450>,
     'getCurrentTime': <gort.core.RemoteCommand at 0x7f5a715e3490>,
     'getDeviceEncoderPosition': <gort.core.RemoteCommand at 0x7f5a715e34d0>,
     'getIncrementalEncoderPosition': <gort.core.RemoteCommand at 0x7f5a715e3550>,
     'getNamedPosition': <gort.core.RemoteCommand at 0x7f5a715e35d0>,
     'getPosition': <gort.core.RemoteCommand at 0x7f5a715e3650>,
     'getPositionSwitchStatus': <gort.core.RemoteCommand at 0x7f5a715e36d0>,
     'getVelocity': <gort.core.RemoteCommand at 0x7f5a715e3750>,
     'get_schema': <gort.core.RemoteCommand at 0x7f5a715e37d0>,
     'help': <gort.core.RemoteCommand at 0x7f5a715e3850>,
     'isAtHome': <gort.core.RemoteCommand at 0x7f5a715e38d0>,
     'isAtLimit': <gort.core.RemoteCommand at 0x7f5a715e3950>,
     'isMoving': <gort.core.RemoteCommand at 0x7f5a715e39d0>,
     'isReachable': <gort.core.RemoteCommand at 0x7f5a715e3a50>,
     'keyword': <gort.core.RemoteCommand at 0x7f5a715e3ad0>,
     'moveAbsolute': <gort.core.RemoteCommand at 0x7f5a715e3b50>,
     'moveRelative': <gort.core.RemoteCommand at 0x7f5a715e3bd0>,
     'moveToHome': <gort.core.RemoteCommand at 0x7f5a715e3c50>,
     'moveToLimit': <gort.core.RemoteCommand at 0x7f5a715e3cd0>,
     'moveToNamedPosition': <gort.core.RemoteCommand at 0x7f5a715e3d50>,
     'ping': <gort.core.RemoteCommand at 0x7f5a715e3dd0>,
     'scanAllReferenceSwitches': <gort.core.RemoteCommand at 0x7f5a715e3e50>,
     'setPosition': <gort.core.RemoteCommand at 0x7f5a715e3ed0>,
     'setVelocity': <gort.core.RemoteCommand at 0x7f5a715e3f50>,
     'slewStart': <gort.core.RemoteCommand at 0x7f5a715e3fd0>,
     'slewStop': <gort.core.RemoteCommand at 0x7f5a715dc090>,
     'status': <gort.core.RemoteCommand at 0x7f5a715dc110>,
     'version': <gort.core.RemoteCommand at 0x7f5a715dc190>}

    >>> await g.telescopes.sci.km.actor.commands.slewStop()
