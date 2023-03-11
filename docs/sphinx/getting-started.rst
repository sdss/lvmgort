Getting started with Trurl
==========================

Minimal example
---------------

The following is a minimum example of how to use the Trurl client to connect to the actor system and command the telescope to a science field, exposing during 15 minutes.

This code must be run in one of the LVM mountain servers with access to the RabbitMQ exchange.

.. code-block:: python

    >>> from lvmtrurl import Trurl

    >>> trurl = Trurl()
    >>> await trurl.start()
    >>> trurl.connected()
    True

    >>> await trurl.check_system()
    >>> await trurl.acquire_and_expose(10.0, -67.0, exposure_time=900)

Reference
---------

.. automodule:: lvmtrurl.core
   :members: Trurl
   :show-inheritance:
   :noindex:
