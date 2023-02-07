Getting started with lvmbrain
=============================

Minimal example
---------------

The following is a minimum example of how to use the `.LVMBrain` client to connect to the actor system and command the telescope to a science field, exposing during 15 minutes.

This code must be run in one of the LVM mountain servers with access to the RabbitMQ exchange.

.. code-block:: python

    >>> from lvmbrain import LVMBrain

    >>> brain = LVMBrain()
    >>> await brain.start()
    >>> brain.connected()
    True

    >>> await brain.check_system()
    >>> await brain.acquire_and_expose(10.0, -67.0, exposure_time=900)

Reference
---------

.. automodule:: lvmbrain.core
   :members: LVMBrain
   :show-inheritance:
   :noindex:
