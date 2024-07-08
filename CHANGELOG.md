# Changelog

## Next version

### 🚀 New

* Replace all instances of Pandas with Polars.

### ✨ Improved

* In `Gort.observe()`, retry after a wait period if the scheduler cannot find a valid tile to observe.
* Output a message after the shutdown recipe instructing observers to check the dome and confirm it's fully closed.

### 🔧 Fixed

* Manually refresh the progress bar only once a second, and only after an update. Hopefully this helps with the event loop getting sluggish after a while.
* Turn off spectrograph room lights on startup.

### ⚙️ Engineering

* Format code using `ruff` and updated dependencies and workflows.


## 0.11.0 - March 29, 2024

### 🚀 New

* [#23](https://github.com/sdss/lvmgort/pull/23) Added observer calibration scripts as recipes `quick_cals`, `bias_sequence`, and `twilight_flats`.

### 🔧 Fixed

* Fixed a serious issue in which if the slew fails, the retry did not send the PA and it would default to PA=0.
* After the initial slew, subsequent slews of the spec telescope do not check for whether the enclosure state is remote.


## 0.10.0 - February 7, 2024

### 🚀 New

* [#20](https://github.com/sdss/lvmgort/pull/20) Added `GuiderSet.monitor()` which can be used to guide at zenith with low cadence and monitor transparency and FWHM while conditions are bad.

### ✨ Improved

* [#19](https://github.com/sdss/lvmgort/pull/19) Improvemts to focusing. After a focus sweep, the focus is adjusted based on the delta temperature before each exposure.
* [#21](https://github.com/sdss/lvmgort/pull/21) Added `GuiderMonitor` which replaces `build_guider_reply_list`. All Pandas dataframes now use `pyarrow` dtypes, and some deprecations and errors have been addressed.


## 0.9.0 - January 31, 2024

### 🚀 New

* Tile standard coordinates can now be defined as a list of Gaia DR3 source IDs. If the standard coordinates is a single integer, it will be assumed to be a source ID and the coordinates will be retrieved from the database.
* Added code for telescope pointing models.

### ✨ Improved

* Break out of `Gort.observe()`` on keyboard interrupt
* Added `utilities_room` light to enclosure.
* Update astropy to 6.0.0 and avoid it requiring to download orientation data from the internet.

### 🔧 Fixed

* Fixed exposure loop not restarting after a keyboard interrupt.


## 0.8.0 - January 13, 2024

### 🚀 New

* [#13](https://github.com/sdss/lvmgort/issues/13) Adds a signal handler for `SIGINT` and `SIGTERM`. When `GortObserver` is running and an interrupt is received the signal handler will run the cleanup routine.
* Added `spectrograph_room` light to enclosure.

### ✨ Improved

* `SpectrographSet.reset` now accepts `full=True` which performs additional checks and opens/closes the shutter and hartmann doors as needed. `reset` with `full=True` is run automatically during the `cleanup` recipe.
* The GORT websocket now uses the CLU model for `lvmecp` instead of asking the PLC to report its status on each call to `WebsocketServer.enclosure_status()`.


## 0.7.1 - December 20, 2023

### ✨ Improved

* [#14](https://github.com/sdss/lvmgort/issues/14) Turn off dome lights as part of the `startup` and `cleanup` recipes.
* Use `lvmnps all-off`` to turn off all lamps faster.

### 🏷️ Changed

* Disable marking tiles as bad on error for now. This could cause confusion if an exposure failed and the tile was marked bad, but then the images were recovered using lockfiles.
* Skip the calibration sequence on startup by default.


## 0.7.0 - November 24, 2023

### 🚀 New

* Added `selfie` position.

### ✨ Improved

* Added NPS devices for telescopes and MOCON.
* Added spectrograph exposure and enclosure timeouts.
* Support exposing only some spectrographs.
* Verify the number of files writtent to disk by the spectrographs and their MD5s.
* Mark exposure bad in post-readout if it fails.

### 🏷️ Changed

* Relaxed spec and sky telescope guiding tolerances to 3 arcsec.

### 🔧 Fixed

* Updated `NPS` to handle `lvmnps` 1.0.0 (including upgrade to `unclick` 0.1.0).


## 0.6.2 - November 4, 2023

### ✨ Improved

* File logger logs to `logging.path` (defaults to `/data/logs/lvmgort/<SJD>.log`).

### 🏷️ Changed

* Disable guider corrections for `spec` telescope after acquisition. This prevents field rotation affecting the exposure at the cost of tracking error.
* Pass `-PA` to the guider due to different handiness for PA between the guider and the tiling database.

### 🔧 Fixed

* Fixed a bug in the recording of the standards acquisition and exposure times.


## 0.6.1 - September 28, 2023

### ✨ Improved

* Pass `force` to `_prepare_telescopes()`.

### 🏷️ Changed

* Do not run cleanup after each iteration in `Gort.observe()`
* Set `stop_degs_before.sci` to 1.5.


## 0.6.0 - September 14, 2023

### 🚀 New

* [#11](https://github.com/sdss/lvmgort/pull/11) Support asynchronous readout during `GortObserved`-enabled observations and implement dithering:
  * Remove calls to `lvmguider stop --now`. The new `stop` command always cancels the guider task.
  * Added `pre-readout` hook for `Exposure`.
  * Allow `GortObserver` to read out an exposure asynchronously.
  * `Gort.observe_tile()` will by default read the last exposure asynchronously to allow for the next slew and acquisition to happen during readout.
  * Added a context manager `GortObserver.register_overhead()` that measured the elapsed time in different parts of the slew and acquisition process and records the values to the database.
  * The dither positions associated with a `Tile` are now a list of positions that the scheduler wants us to observe. `Gort.observe_tile()` will try to observe all dither positions without stopping the guider.
  * Added `Gort.observe()` with a simple loop for observing tiles.
* Add dither information to `Tile` and support exposing multiple dither positions in the same observation.

### ✨ Improved

* Pass the observed sky and standard pks to the scheduler during registration.
* The K-mirror offset angle is now wrapped between -180 and 180 degrees. Hopefully this prevents some cases in which offsets angles very close to 360 degrees cause the K-mirror to fail because of a software limit.

### 🔧 Fixed

* Fixed deprecations in Pandas `fillna()` with `method='bfill'`.


## 0.5.0 - September 12, 2023

### 🚀 New

* Added support for telescope enclosure lights.

### ✨ Improved

* [#10](https://github.com/sdss/lvmgort/pull/10) `GortObserver.slew()` and the standard iteration task now calculate the adjusted target coordinates for the spec telescope so that the slew puts the star on top of the desired fibre to be observed. Most of this relies on code copied from `lvmtipo` used to calculate the field angle through the siderostat.
* `SkyCoordinates` now accepts a `name` attribute that is passed to the `SKYENAME` and `SKYWNAME` header keywords.
* IFU PA angles passed to the headers.
* Pass exposure time when registering an exposure.
* Support new outputs from `lvmguider 0.4.0b1`.
* Get actor versions on init.
* Support guiding in PA (disabled for now).
* Reconnect AGs during `startup` recipe.
* Add `tile_id` as `OBJECT`` if no object string user-defined.

### 🏷️ Changed

* Set `GR{telescope}FR{0|N}` with the range of guider frames in which we were guiding (as opposed to the entire guide loop including acquisition frames).
* Changed default guide tolerance to 1 arcsec.

### 🔧 Fixed

* Fixed some additional issues with the exception hooks that cause a recursion loop in IPython in some cases.
* Fixed a bug that would accumulate the range of guider frames for an spectrograph exposure if `GortObserver.expose()` was called multiple times.
* Fixed a slicing issue with newer versions of Pandas.
* Fixed a bug that caused the exposure flavour not being passed to the headers.
* Deal with cases when the scheduler cannot find a good tile to observe.


## 0.4.0 - August 26, 2023

### 🚀 New

* Guider frames for each telescope taken during an exposure are added to the spectrograph headers.
* Standards observed are added to the spectrograph headers. The standard information can be accessed as `GortObserver.standards`.
* Added a `cleanup` recipe.
* Added exposure frame to the progress bar.
* Add `PA` parameter to tiles and propagate it to the k-mirror slew commands.
* Use `rich` tracebacks. This can be disabled by calling `Gort` with `use_rich_output=False`. Tracebacks generated in an interactive IPython session are now saved to the file log.
* Log `Gort` output to a temporary file by default. `Gort` can also be called with an argument `log_file_path` to indicate where to save the log, or with `log_file_path=False` to disable logging. The current file log path can be retrieved as `Gort.get_log_path()`.

### ✨ Improved

* [#9](https://github.com/sdss/lvmgort/pull/9) Refactoring of `Exposure` and `SpectrographSet.expose()`. Most of the code in the latter has been moved to the former. Added a system of hooks for the exposure process. For now, only a `pre-readout` hook that is called with the header before the exposure is read.
* Concurrently stop spec guiding and reslew.
* Pass `seg_time` and `seg_min_num` to `slewStart`.
* The code used to build the spectrograph header has been cleaned up and consolidated in `GortObserver`.

### 🔧 Fixed

* Explicitely define site coordinates to avoid astropy needing to download files.


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
