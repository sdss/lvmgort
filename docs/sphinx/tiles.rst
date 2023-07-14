
.. _tiles:

Tiles and offsets
=================

The standard unit of LVM observation is a *tile*, which includes a patch on the sky observed with the science IFU, two sky background patches observed with SkyE and SkyW, and a series of spectrophotometric standards (usually 12) observed in quick succession.

In `gort` the information from a tile is defined as an instance of the `.Tile` class. A `.Tile` is mainly a set of coordinates for and observation with each one of the telescopes. `.Tile` objects can be instantiated manually, but usually they are created using one of the class methods that provide access to the scheduler and target database.

The simplest way to define a `.Tile` is by requesting the scheduler to provide the next tile to observe ::

    >>> from gort import Tile
    >>> tile = Tile.from_scheduler()
    >>> tile
    <Tile (tile_id=1026052, science ra=240.000000, dec=-87.977528; n_skies=2; n_standards=12)>
    >>> tile.tile_id
    1026052
    >>> tile.sci_coords
    <ScienceCoordinates (ra=240.000000, dec=-87.977528)>
    >>> tile.spec_coords
    [<StandardCoordinates (ra=229.927715, dec=-85.679574)>,
     <StandardCoordinates (ra=208.563696, dec=-85.959177)>,
     <StandardCoordinates (ra=219.537850, dec=-85.293839)>,
     <StandardCoordinates (ra=187.304935, dec=-85.579045)>,
     <StandardCoordinates (ra=125.073041, dec=-87.727509)>,
     <StandardCoordinates (ra=155.052188, dec=-86.556699)>,
     <StandardCoordinates (ra=158.575261, dec=-86.371041)>,
     <StandardCoordinates (ra=174.398855, dec=-85.486432)>,
     <StandardCoordinates (ra=199.035681, dec=-84.463135)>,
     <StandardCoordinates (ra=228.290166, dec=-83.688737)>,
     <StandardCoordinates (ra=104.719172, dec=-87.304929)>]
     >>> tile.sky_coords
     {'skye': <SkyCoordinates (ra=185.383051, dec=4.415931)>,
      'skyw': <SkyCoordinates (ra=193.700076, dec=-78.920651)>}

The ``tile_id`` attribute identifies the science tile uniquely and allows to register an observation of that tile. In addition to the science pointing, the scheduler also provides the sky and spectrophotometric targets from a list of valid calibrators. The ``spec_coords`` attribute contains a list of standards that will be observed in order, while ``sky_coords`` is a dictionary of sky positions for the East and West sky telescopes.

A tile can also be defined from coordinates. Normally this includes only the RA and Dec of the science field, in which case the scheduler is used to find appropriate calibrators ::

    >>> tile = Tile.from_coordinates(ra=250.1, dec=-5.2)
    >>> tile
    <Tile (tile_id=None, science ra=250.100000, dec=-5.200000; n_skies=2; n_standards=12)>
    >>> tile["skye"]
    <SkyCoordinates (ra=223.224052, dec=-13.400939)>

The last example shows that it's possible to access the coordinates of a given telescope as a dictionary. It's also possible to specify the calibrators ::

    >>> tile = Tile.from_coordinates(ra=250.1, dec=-5.2, sky_coords={'skye': (240.5, -10)})
    <Tile (tile_id=None, science ra=250.100000, dec=-5.200000; n_skies=1; n_standards=12)>

Each coordinate is an instance of the `.Coordinate` class which includes the RA, Dec, and an astropy ``SkyCoord`` object ::

    >>> tile.sci_coords.skycoord
    <SkyCoord (FK5: equinox=J2000.000): (ra, dec) in deg
       (250.1, -5.2)>
    >>> tile.sci_coords.calculate_altitude()
    5.543376243187401


Observing a tile
----------------

Observing tiles is a task for the `.GortObserver` class, which receives a `.Tile` with the pointing information and performs the tasks of slewing all telescopes, acquiring the fields, exposing, keeping the guider engaged, and rotate over the various standard stars. `.GortObserver` is the highest-level class in `gort` and handles as much automatic troubleshooting as possible.

To instantiate an observer object ::

    >>> from gort import Gort, GortObserver, Tile
    >>> g = await Gort().init()
    >>> tile = Tile.from_scheduler()
    >>> observer = GortObserver(g, tile)
    >>> observer
    <GortObserver (tile_id=1026052)>

A normal sequence of observation with `.GortObserver` would be ::

    await observer.slew()
    await observer.acquire()
    await observer.expose(900)
    await observer.finish_observation()

These commands are wrapped in the `.Gort.observe_tile` method so one can simply do ::

    g = await Gort().init()
    tile = Tile.from_scheduler()
    await g.observe_tile(tile)

At the end of the sequence the telescopes will remain tracking at the current positions but the guiders will be stopped.


Offsetting targets
------------------

`gort` and the guider handle target offsets using the paradigm of the *master frame* coordinate system. This frame is defined as one coplanar with the IFU face and the FoV of the auto-guider cameras, and centred on the central fibre of the IFU (the frame is applicable to all four telescope). The frame has dimensions of 5000 by 2000 *pixels*, each one the angular size of one auto-guider pixel (approximately 1 arcsec), and thus the centre of the IFU has coordinates :math:`(2500, 1000)`.

.. image:: images/master_frame.png
    :width: 600px
    :align: center

When the image is perfectly derotated the master frame is aligned such that RA increases in the x direction and Dec decreases as z increases (from the metrology, the master frame plane is denoted using :math:`xz` coordinates).

It's possible to centre a point source on any coordinates of the master frame regardless of derotation. For example, to centre a star on fibre P1-1 of the `spec` telescope, we would guide on master frame pixel :math:`(x, z)=(2658.7, 1570.6)`.

To introduce an offset to a target there are two basic options:

1) Offset the target coordinates with the usual :math:`\alpha'=\alpha+\alpha_{\rm off}/\cos(\delta);\quad \delta'=\delta+\delta_{\rm off}`.
2) Maintain the nominal coordinates of the target and define an offset in master frame coordinates (currently this is only available for the science target).

`gort` provides some tools to determine the master frame coordinates of a fibre or a RA/Dec offset. To offset a target to a given fibre one can use :obj:`.fibre_to_master_frame` ::

    >>> from gort.transforms import fibre_to_master_frame
    >>> fibre_to_master_frame("S2-324")
    (2436.4, 1220.0)

where ``"S2-324"`` is the name of the fibre as a combination of the ``ifulabel`` and ``finifu`` from the ``lvmcore`` fibre map. Alternatively one can set this in the `.ScienceCoordinates` object in a `.Tile` ::

    >>> tile.sci_coords.set_mf_pixel('S2-324')
    (2436.4, 1220.0)

which is equivalent to ::

    tile.sci_coords.set_mf_pixel(xz=(2436.4, 1220.0))

To offset a target by an arbitrary RA and Dec offset in arcsec one can use :obj:`.offset_to_master_frame_pixel` ::

    >>> from gort.transforms import offset_to_master_frame_pixel
    >>> xz = offset_to_master_frame_pixel(ra=10, dec=-5)
    >>> xz
    (2510.0, 1005.0)
    >>> tile.sci_coords.set_mf_pixel(xz=xz)

.. warning::
    :obj:`.offset_to_master_frame_pixel` provides approximate conversion that assumes the IFU is perfectly aligned with the AG cameras in the focal plane and that the field de-rotation is perfect.
