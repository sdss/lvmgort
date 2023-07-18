# Changelog

## Next version

### 🚀 New

* Added support for spectrograph IEBs.

### ✨ Improved

* [#4](https://github.com/sdss/lvmgort/pull/4) Use the `rich` library for status bars. It provides better style and works better with stdout and logger outputs.

### ⚙️ Engineering

* Use the `rich` library logger but customise it to look like the usual console formatter.


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
