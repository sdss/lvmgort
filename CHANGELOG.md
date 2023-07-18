# Changelog

## 0.2.0 - July 18, 2023

### 🚀 New

* Added support for spectrograph IEBs.
* Added `SpectrographSet.get_calibration_sequence()`.`
* Added `initialise`` (init) and `abort`` methods for the spectrographs.

### ✨ Improved

* [#4](https://github.com/sdss/lvmgort/pull/4) Use the `rich` library for status bars. It provides better style and works better with stdout and logger outputs.
* Expose various parameters in `Gort.observe_tile()`.
* Various fixes and additional routes for the websockets server.
* Allow to pass a calibration sequence as a dictionary, and validate against JSON schema.
* Avoid having all MoTan devices moving at once by introducing a delay.

### ⚙️ Engineering

* Update `unclick`` to 0.1.0b5.
* Use the `rich` library logger but customise it to look like the usual console formatter.
* Allow to define the configuration file to use as `$GORT_CONFIG_FILE`.


## 0.1.1 - July 14, 2023

### ✨ Improved

* Made the `ScienceCoordinates.set_mf_pixel()` method public.
* Allow to set master frame pixel in `ScienceCoordinates` with `xz` tuple.
* Added `fibre_to_master_frame()` transformation function.
* Improve `__repr__` for `Tile` and `GortObserver`.
* Complete documentation for tiles, observing, and offsets.


## 0.1.0 - July 14, 2023

### 🚀 New

* Initial version. Bugs are likely but most features are functional.
* `DeviceSet` classes for telescopes, AGs, guiders, enclosure, spectrographs, and NPS.
* `Tile`, `GortObserver` classes to perform observations.
* `Kubernetes` class to interact with the Kubernetes cluster.
* Various tools and transformation functions.
* Very preliminary websocket server.
