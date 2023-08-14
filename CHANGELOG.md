# Changelog

## 0.3.0 - August 14, 2023

### 🚀 New

* [#5](https://github.com/sdss/lvmgort/pull/5) Add option to restart deployments associated with a device set as `GortDeviceSet.restart()`.
* [#7](https://github.com/sdss/lvmgort/pull/7) If guiding during an `Exposure`, a Pandas DataFrame is created with a summary of the guider data. The guider outputs summary guider information as INFO level messages.
* [#8](https://github.com/sdss/lvmgort/pull/8) Added framework for recipes along recipes for startup, shutdown, and calibration.
* Add `TelescopeSet.restart_lvmtan()` to restart Twice-As-Nice controller.
* TAN devices will timeout when homing or moving.
* Add pointing information to the exposure.
* Add `TelemetrySet` and `Telemetry` devices.
* Use the focus-temperature relationship to determine the initial guess for focusing.

### ✨ Improved

* Stop the guiders with `now=True` after 60 seconds.
* The object keyword values is set as an attribute in `Exposure` and shown in the `__repr__`.
* `Coordinates.set_mf_pixel()` is now available for all coordinates.
* Allow to wait only for some telescopes to converge guiding.
* Check if telescopes are already parked before opening/closing the dome.
* Report summary of best focus in `focus()` command as INFO level.
* Print last guider measurement before removing NaNs.
* Better control of which TAN devices to home during `Telescope.home()` or `TelescopeSet.home()`. The previous `home_subdevices` argument has been replaced by `home_kms`, `home_telescopes`, `home_focusers`, and `home_fibsel`.
* Restore previous focuser positions after homing.
* Allow multiple exposures in `GortObserver.expose()`
* Focus initial guesses are now included in the configuration. If `Gort.guiders.focus()` is called without a guess, the configuration values are used. The guess can now also be a dictionary of telescope name to guess value.
* `set_mf_pixel()` can now be used with all coordinates.
* Better logging of remote actor command warnings and errors.
* Spec telescope will now slew to coordinates such that the target falls on the selected fibre.
* Check if TAN devices are reachable.
* Improvements to the documentation.

### 🏷️ Changed

* Removed the options `min_skies` and `require_spec` for `GortObserver.acquire()` and `Gort.observe_tile()`. If no skies or standards are defined a warning is issued but the code will not fail.

### ⚙️ Engineering

* Lint using `ruff`.
* Improve style of the documentation.
* Move `Exposure` to its own file.


## 0.2.2 - July 23, 2023

### 🚀 New

* Added the `testcal` calibration sequence.

### ✨ Improved

* Updated the calibrations section in the documentation.
* Prevent multiple devices reconnecting to RabbitMQ at the same time.
* Set the `OBJECT` header keyword in spectrograph exposures.
* Telescopes keep track of whether they have been homed.
* Telescopes will move to park automatically when trying to open or close the dome.

### 🔧 Fixed

* Fixed an issue that would cause object exposures to be taken as biases.
* Fixed progress bar being affected by stdout and logs by setting the same `rich` `Console` object for logs and progress bars.
* Fixed accessing the configuration from an `Exposure` object.


## 0.2.1 - July 19, 2023

### ✨ Improved

* `SpectrographSet.expose()` now accepts a `count` parameters to expose multiple frames.
* The parameter `flavour` for `SpectrographSet.expose()` is not explicitely exposed and documented.
* `SpectrographSet.calibrate()` accepts a `slew_telescopes` parameters. Whether the telescopes are slewed is also determined by the type of calibrations to take.
* Various improvements to the calibration schema. `flavour`, `warmup`, and the `fibsel` parameters now have defaults in the configuration file to which the calibration routine will revert to. If `fibsel: true`, a default mask iteration will be performed. Both darks and lamps now accept `exposure_time` which can be a float or a list of floats.

### 🏷️ Changed

* `SpectrographSet.calibrate()` does not park the telescope by default.

### ⚙️ Engineering

* Progress bars are now transient.


## 0.2.0 - July 18, 2023

### 🚀 New

* Added support for spectrograph IEBs.
* Added `SpectrographSet.get_calibration_sequence()`.
* Added `initialise` (init) and `abort` methods for the spectrographs.

### ✨ Improved

* [#4](https://github.com/sdss/lvmgort/pull/4) Use the `rich` library for status bars. It provides better style and works better with stdout and logger outputs.
* Expose various parameters in `Gort.observe_tile()`.
* Various fixes and additional routes for the websockets server.
* Allow to pass a calibration sequence as a dictionary, and validate against JSON schema.
* Avoid having all MoTan devices moving at once by introducing a delay.

### ⚙️ Engineering

* Update `unclick` to 0.1.0b5.
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
