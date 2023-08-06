.. _observing:

Remote observing with GORT
==========================

This section provides an overlook on how to use ``gort`` to observe remotely with the LVM facility. This is not a comprehensive guide, and many safety precautions are not covered. Please review the remote observing documentation alongside this guide.

We assume you'll be running this code in an LVM server, most likely in a Jupyter notebook. It's possible to use ``gort`` locally and connect it a remote RabbitMQ exchange. For that, ssh to the LVM servers and forward port 5672 in ``lvm-hub.lco.cl`` to localhost on a random port, say 9876. Then you can connect to the remote exchange with

.. code:: python

    from gort import Gort
    g = await Gort(host='localhost', port=9876).init()

.. warning::
    It may seem from this that security to access the actor system and associated hardware is lax. In reality neither the host nor any of the services are open to the outside world, and the RabbitMQ exchange can only be accessed by secure shell, which requires the user being granted access. Given that, we consider that additional security in the form of passwords is unnecessary.


Afternoon checkouts
-------------------

As usual, we start by creating an instance of `.Gort`. Multiple instance can run at the same time as the client name is unique.

.. code:: python

    from gort import Gort
    g = await Gort().init()

Note that we instantiate `.Gort` and then call `~.GortClient.init`, a coroutine (thus the ``await``) which actually creates the connection to the RabbitMQ exchange, loads the remote actors, etc. For convenience, `~.GortClient.init` returns the same object so everything can be written in a single line.

Next we set the logging level to ``info``. By default ``gort`` will only raise an error on a critical failure, and won't emit any visible message if a routine succeeds. This follows the rule that successful processes should not write to stdout. However, for normal operations, it's useful to have a more verbose output. You can set the logging level even lower, to ``debug``.

.. code:: python

    g.set_verbosity('info')

A first good step every afternoon is to home the telescopes, which may have lost their zero points during the day. For that do

.. code:: python

    >>> await g.telescopes.home(home_subdevices=True)
    02:31:17 [INFO]: (sci) Homing telescope.
    02:31:17 [INFO]: (spec) Homing telescope.
    02:31:17 [INFO]: (skye) Homing telescope.
    02:31:17 [INFO]: (skyw) Homing telescope.

Note that we are commanding all four telescopes at the same time. This routine connects and energises the axes of the mounts and runs a homing routine, which may take up one minute. If during the night it seems the telescopes are not pointing correctly, it's useful to re-home them. The ``home_subdevices=True`` option also homes the k-mirrors, focusers, and fibre selector.

We can now take a set of autoguider dark frames with `~.GuiderSet.take_darks`. The telescopes will be pointed to the ground (since the AG cameras don't have shutters) and an exposure will be taken with each one of them.

.. code:: python

    >>> await g.guiders.take_darks()
    07:07:45 [INFO]: (GuiderSet) Moving telescopes to park position.
    07:07:46 [INFO]: (sci) Moving to alt=-60.000000 az=90.000000.
    07:07:46 [INFO]: (spec) Moving to alt=-60.000000 az=90.000000.
    07:07:46 [INFO]: (skye) Moving to alt=-60.000000 az=90.000000.
    07:07:46 [INFO]: (skyw) Moving to alt=-60.000000 az=90.000000.
    07:07:59 [INFO]: (GuiderSet) Taking darks.
    07:07:59 [DEBUG]: (skyw) {'text': 'Taking agcam exposure skyw-1.'}
    07:07:59 [DEBUG]: (sci) {'text': 'Taking agcam exposure sci-1.'}
    07:07:59 [DEBUG]: (skye) {'text': 'Taking agcam exposure skye-1.'}
    07:07:59 [DEBUG]: (spec) {'text': 'Taking agcam exposure spec-1.'}
    07:08:08 [DEBUG]: (spec) {'frame': {'seqno': 1, 'filenames': ['/data/agcam/60135/lvm.spec.agcam.east_00000001.fits'], 'flavour': 'dark', 'n_sources': 0, 'fwhm': None}}
    07:08:08 [DEBUG]: (skye) {'frame': {'seqno': 1, 'filenames': ['/data/agcam/60135/lvm.skye.agcam.west_00000001.fits', '/data/agcam/60135/lvm.skye.agcam.east_00000001.fits'], 'flavour': 'dark', 'n_sources': 0, 'fwhm': None}}
    07:08:08 [DEBUG]: (skyw) {'frame': {'seqno': 1, 'filenames': ['/data/agcam/60135/lvm.skyw.agcam.west_00000001.fits', '/data/agcam/60135/lvm.skyw.agcam.east_00000001.fits'], 'flavour': 'dark', 'n_sources': 0, 'fwhm': None}}
    07:08:08 [DEBUG]: (sci) {'frame': {'seqno': 1, 'filenames': ['/data/agcam/60135/lvm.sci.agcam.west_00000001.fits', '/data/agcam/60135/lvm.sci.agcam.east_00000001.fits'], 'flavour': 'dark', 'n_sources': 0, 'fwhm': None}}

Next, we take a spectrograph calibration sequence

.. code:: python

    await g.spec.calibrate(sequence='normal')

This will take calibration flats and arcs, and a series of biases and darks. The full sequence can take over an hour and the routine will output log messages indicating what it's doing. In the background, this sequence moves all the telescopes to point to the flat field screen, turns on the necessary lamps, and exposes the spectrographs. More details on running calibrations sequences can be found :ref:`here <calibrations>`.

When the sequence finishes and we are ready to start observations, it's time to open the dome

.. code:: python

    await g.enclosure.open()

This command will block until the dome is fully open, and will return an error if it fails. The movement can be stopped by doing

.. code:: python

    await g.enclosure.stop()

.. warning::
    Jupyter notebooks don't allow to run more than one cell at the same time, so in practice it's not possible to have concurrency. If you need to do an emergency stop of the enclosure while it is already moving, you'll need to first stop the running cell (note that this won't stop the command that opens the dome) and then run another cell with the stop command.

Once the dome is open we can focus the telescopes with

.. code:: python

    await g.guiders.focus()

By default this performs a 9-step focus sweep for each telescope around focuser position 40 DT. If it seems the sweep is not sampling the best focus position you can change the `~.GuiderSet.focus` parameters, for example by passing ``guess=XXX``, or focus an individual telescope with `.Guider.focus`.

At this point you should be ready to being science observations.


Observing
---------

See the section about :ref:`tiles`.


Misc
----

The following is an unsorted list of operations and troubleshooting using ``gort``.

Moving the k-mirror to any position
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Normally ``gort`` will determine if the k-mirror of a telescope needs to be moved, parked, or set to tracking. To move it manually you can do

.. code:: python

    >>> await g.telescopes.sci.km.move(80)
    03:02:20 [INFO]: (sci.km) Moving k-mirror to 80.000 degrees.
    03:02:20 [DEBUG]: (sci.km) Stopping slew.
    03:02:20 [DEBUG]: (sci.km) Moving k-mirror to absolute position.

The k-mirror can be parked with

.. code:: python

    >>> await g.telescopes.sci.km.park()
    03:09:29 [INFO]: (sci.km) Moving k-mirror to 90.000 degrees.
    03:09:29 [DEBUG]: (sci.km) Stopping slew.
    03:09:29 [DEBUG]: (sci.km) Moving k-mirror to absolute position.

Lower level access to the k-mirror features can be obtained using the programmatic actor interface

.. code:: python

    >>> g.telescopes.sci.km.actor.commands
    {'getAbsoluteEncoderPosition': <gort.core.RemoteCommand at 0x7f5a715e3450>,
     ...
     'setVelocity': <gort.core.RemoteCommand at 0x7f5a715e3f50>,
     'slewStart': <gort.core.RemoteCommand at 0x7f5a715e3fd0>,
     'slewStop': <gort.core.RemoteCommand at 0x7f5a715dc090>,
     'status': <gort.core.RemoteCommand at 0x7f5a715dc110>,
     'version': <gort.core.RemoteCommand at 0x7f5a715dc190>}

    >>> await g.telescopes.sci.km.actor.commands.slewStop()
