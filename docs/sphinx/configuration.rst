
.. _configuration-file:

Configuration
=============

`gort`'s configuration is stored in a file shipped with the package in ``etc/lvmgort.yml``. The contents of the default configuration are shown below.

Currently the only way to override the configuration file is to define an environment variable ``$GORT_CONFIG_FILE`` pointing to a different YAML file with the configuration to load. Then exit your Python interpreter and reimport ``gort``.

Configuration file
------------------

.. literalinclude :: ../../src/gort/etc/lvmgort.yml
   :language: yaml
