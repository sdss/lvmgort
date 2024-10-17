
GORT's documentation
====================

This is the documentation for the SDSS Python product ``gort``. The current version is |lvmgort_version|. You can install the package by doing

.. code:: console

  $ pip install lvmgort

``lvmgort`` provides the library ``gort``

.. code:: python

  >>> from gort import Gort
  >>> g = await Gort(verbosity='info').init()
  >>> await g.specs.expose(flavour='bias')
  06:49:21 [INFO]: (SpectrographSet) Taking spectrograph exposure 1165.
  <Exposure (exp_no=1165, error=False, reading=False, done=True)>


Contents
--------

.. toctree::
  :maxdepth: 2

  getting-started
  observing
  tiles
  recipes
  configuration
  troubleshooting
  api
  Changelog <changelog>


Links
-----

* `Repository <https://github.com/sdss/lvmgort>`__
* `Issue tracking <https://github.com/sdss/lvmgort/issues>`__


Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
