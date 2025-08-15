# Changelog

## 1.10.3 - August 15, 2025

### ✨ Improved

* Check that all cameras have been reconnected during pre-observing task.
* Implement a less verbose way to handle times when the scheduler cannot find a valid tile to observe.
* Better logging of when the dome has been actually closed during a shutdown.


## 1.10.2 - August 10, 2025

### ✨ Improved

* When scheduled, run long term calibrations at least 1 hour after sunset and at lower priority then the daily calibrations.
* Always take biases in the long-term calibrations.
* Updated `sdss-clu` to version 2.5.3 and `lvmopstools` to version 0.5.19.

### 🔧 Fixed

* Increase the timeout for the API request to email the night log.


## 1.10.1 - July 14, 2025

### ✨ Improved

* Do not fail the twilight flats in the Overwatcher if the exposure time is too long for the last fibre. Instead, just log a warning and mark the calibration as done.
* Add option `clip_to_max_exposure_time` to the twilight flats recipe to clip the exposure time to the maximum defined in the configuration file. This is `True` by default.


## 1.10.0 - June 17, 2025

### ✨ Improved

* Assume that conditions are safe if the enclosure engineering mode is enabled.
* Prevent attempts to close the dome or park the telescopes in local mode.
* Allow twilight flats with exposure times rounded up to a tenth of a second.

### 🔧 Fixed

* [#50](https://github.com/sdss/lvmgort/pull/50) Take into account the dither position when calculating `POSCIRA` and `POSCIDE`.
* [#51](https://github.com/sdss/lvmgort/pull/51) Add an alert when conditions are safe but the Overwatcher has been idle for over 10 minutes.
* [#52](https://github.com/sdss/lvmgort/pull/52) Fix a bug that prevented an emergency shutdown from completing.
* Fix logging the configuration in the pre-observing recipe.
* Disable the current tile when the acquisition fails and the troubleshooter cannot find an issue with the cameras.
* Prevent recursion error when shutting down due to bad transparency.


## 1.9.1 - April 1, 2025

### 🔧 Fixed

* Change the default `bias` argument for the long-term calibrations to `None`.


## 1.9.0 - April 1, 2025

### ✨ Improved

* [#49](https://github.com/sdss/lvmgort/pull/49) Add a calibration recipe for long-term calibration and support scheduling them in the Overwatcher.
* Improved the AG camera disconnection troubleshooting recipe.
* Added a recipe to manually power-cycle and reconnect AG cameras.
* Reboot all AG cameras in the pre-observing task.
* Added `reload-config` command to the Overwatcher actor.

### 🏷️ Changed

* Added a fudge factor to the twilight flats exposure time to prevent saturation in the IR band.

### 🔧 Fixed

* Ensure the Overwatcher is disabled in the morning even when it was not observing.
* Fix a regression in the acquisition failed troubleshooting recipe.


## 1.8.0 - March 18, 2025

### 🚀 New

* [#48](https://github.com/sdss/lvmgort/pull/48) Added script to observe the lunar eclipse for March 2025.

### ✨ Improved

* Add Overwatcher daily task to power cycle AG cameras before observations.
* Check AG cameras before each tile.
* Improve cancellation of the observe loop during startup.
* Observe each standard for at least 60 seconds.


## 1.7.2 - March 2, 2025

### ✨ Improved

* Improved and simplified handling of user override configuration file.

### 🏷️ Changed

* Start evening twilight flats two minutes after sunset by default.

### 🔧 Fixed

* Fix checking of AG camera pings in `AcquisitionFailedRecipe`.
* Try-except errors during camera disconnected handling.
* Use `None` as default when a `RemoteCommand` does not complete successfully.
* Explicitly require `lvmopstools>=0.5.9`.
* Fix logic when handling the `EMIT_EVENT` attrinute of `GortError`.


## 1.7.1 - February 27, 2025

### ✨ Improved

* Check if AG cameras are connected after an acquisition error and power cycle them if necessary.
* Add a note to the DB when disabling a tile.

### 🏷️ Changed

* Evening twilight flats now start three minutes after sunset.
* Door alert now triggers unsafe conditions.


## 1.7.0 - February 3, 2025

### ✨ Improved

* Try-except the final closure of the dome after a calibration.
* Ensure that the observing loop and calibrations are stopped if the dome is closed outside of the Overwatcher.
* Add flag `--close-dome` to `lvm.overwatcher disable` to close the dome after disabling the Overwatcher. Requires using `--now`.

### 🏷️ Changed/removed

* Prevent automatic dome closure by the Overwatcher if it is disabled and the alert is not critical (wind, humidity, etc.)
* Removed the `reset_lockout` argument for the shutdown recipe since it's not required to allow the dome to move.
* Do not close the dome during daytime if the Overwatcher is disabled.
* Remove dome lockout after unsafe conditions. It may be reimplemented differently in the future.

### 🔧 Fixed

* Fixed a regression that would mark a retrying calibration as done.


## 1.6.0 - January 26, 2025

### ✨ Improved

* [#47](https://github.com/sdss/lvmgort/pull/47) Add `DomeHelper` lock when the dome fails to move.
* Added try-excepts and timeouts to the different tasks in the Overwatcher shutdown routine to ensure that the dome closure is always attempted.
* Move `DomeHelper.startup()` to `Overwatcher.startup()` and clean up observer code.
* Retry emitting Overwatcher heartbeats.
* Add additional checks to prevent concurrent attempts to open/close the dome.
* Improve handling of errors during the focusing sequence in the Overwatcher.
* Clarify the behaviour of `min_time_between_repeat_notifications` in the `NotifierMixIn` class.

### 🔧 Fixed

* Fixed a race condition that could prevent the shutdown of the dome when twilight was reached. Instead of commanding a shutdown inside the Overwatcher observing loop (which cancels the same task that is commanding it), the loop will now complete normally and the main Overwatcher task will command the shutdown.


## 1.5.1 - January 15, 2025

### 🔧 Fixed

* Fixed an issue setting the initial calibrations schedule SJD.
* Prevent a failed notification to raise an error by default.


## 1.5.0 - January 14, 2025

### 🚀 New

* Monitor and handle e-stops in the Overwatcher.

### ✨ Improved

* Require two consecutive ping failures before restarting an actor.
* Use overcurrent mode to close the dome if the normal mode fails.
* Removed the `shutdown()` method in the `DomeHelper` and moved the logic to `Overwatcher.shutdown()`.

### 🔧 Fixed

* Added a timeout to the `lvmbeat set overwatcher` command to prevent it from hanging indefinitely.
* Fixed an issue that would crash the alerts task when checking the internet connection if the request timed out.
* Ensure that `CalibrationsOverwatcher.reset()` is called.


## 1.4.0 - January 1, 2025

### 🚀 New

* [#45](https://github.com/sdss/lvmgort/pull/45) Added a `health` module that emits a heartbeat to `lvmbeat` and monitors actor health by ping, restarting them if found unresponsive. The pre-observing task now also perform that check.

### ✨ Improved

* [#44](https://github.com/sdss/lvmgort/pull/44) RORR RID-019: disables the Overwatcher if rain is detected and requires a human to re-enable it when conditions are safe.
* [#46](https://github.com/sdss/lvmgort/pull/46) RORR RID-017: treat lost connectivity to the internet or LCO as an unsafe condition and close.
* Create the night log during the pre-observing task.
* The `gort overwatcher` command now accepts a `--verbose` flat that allow to set the verbosity level of the messages output to the console while the Overwatcher is running (the file log level is always DEBUG). The default level is now WARNING.

### 🏷️ Changed

* Removed the `pubsub` module and use `lvmopstools` instead.
* Rename and expose `RemoteCommand._run_command()` to `RemoteCommand.run()`.

### 🔧 Fixed

* Do not run the post-exposure checks when cancelling the loop.
* Fix call to `/notifications/create` for new API version.


## 1.3.0 - November 29, 2024

### 🚀 New

* [#39](https://github.com/sdss/lvmgort/pull/39) Implement transparency monitoring.
* Add `observer schedule-focus-sweep` command to Overwatcher actor to schedule a focus sweep before the next tile.

### ✨ Improved

* [#40](https://github.com/sdss/lvmgort/pull/40) Slight internal restructuring of the core classes `Gort`, `GortClient`, device and remote actor classes. The main goal was to avoid any other part of the library knowing about `GortClient`, which does not include anything not related to its AMQP client function any more.
* Use `Retrier` from `lvmopstools` to handle remote command retries.
* Prevent repeat notifications with the same message.
* Add retries to NPS commands.

### 🔧 Fixed

* Prevent trying to observe while a calibration is ongoing even if it's night.
* Add `max_start_time` to `bias_sequence` to prevent if from running after twilight.


## 1.2.1 - November 20, 2024

### ✨ Improved

* [#41](https://github.com/sdss/lvmgort/pull/41) Only emit an error event when the exception is actually raised.
* Report if the observer is focusing or troubleshooting.
* Stop MoTAN devices before a new move and improve error reporting.
* Allow fibre selector to rehome if a move fails.

### 🔧 Fixed

* Fix a bug that would cause the calibration module to always add a night log comment indicating that the calibration had failed.
* Prevent the K-mirror from being homed and parked at the same time.


## 1.2.0 - November 17, 2024

### 🚀 New

* Overwatcher now reports error events via notifications. If the error happens while a tile is being observed, a comment in the night log is added.

### ✨ Improved

* Roll over the GORT log when the SJD changes.
* Improve the logic handling how the Overwatcher observer decides when to open or close the dome near evening or morning twilight.
* Run a clean-up first in pre-observing in case the spectrographs are not in a good state.
* Run some pre-observing checks before calling each `GortObserver.observe_tile()` in the `ObserverOverwatcher`. Currently only checks if the spectrographs have an error state and resets them.
* Handle `SPECTROGRAPH_NOT_IDLE` errors in the troubleshooter.
* Disable the Overwatcher and cancel observations if the dome fails to move.
* Add retries for safe enclosure operations.

### 🔧 Fixed

* Fixed a bug that would prevent a new SJD to trigger an update of the ephemeris and calibrations.
* Fix a bug that would leave the Overwatcher in cancelling mode if `start_observing` failed.


## 1.1.2 - November 11, 2024

### ✨ Improved

* Take AG darks during the pre-observing task.
* Add `retry_without_parking` option to the shutdown recipe.
* Emit events for dome opening and closing and report them as notifications.
* Modified `emergency_shutdown()` to close the dome if the shutdown recipe fails.

### 🔧 Fixed

* Prevent the calibrations module from trying to close the dome when a calibration is retrying.
* Prevent a case in which failing to park the telescopes could have caused the dome to not be closed even if `retry_without_parking` was set to `True`.


## 1.1.1 - November 10, 2024

### 🚀 New

* [#38](https://github.com/sdss/lvmgort/pull/38) Add a post-observing daily task that runs 15 minutes after morning twilight and will do a few check (make sure the dome is closed, park the telescopes, etc.) and retry safe calibrations that failed during the normal sequence.

### ✨ Improved

* Add a comment to the night log when a calibration fails.
* Do not start exposure if we are within 10 minutes of twilight.

### 🔧 Fixed

* Prevent the Overwatcher observer from opening the dome while calibrations are ongoing.
* Fixed a bug in the twilight flats recipe related to the extra exposures.

### ⚙️ Engineering

* Use API to send notifications.


## 1.1.0 - November 7, 2024

### 🔥 Breaking change

* `GortObserver.observe_tile` now default to `async_readout=False`. This will block until the exposure is done, which is a more natural behaviour for an external user that is not trying to over-optimise things. The code that uses `observe_tile` in GORT (`Gort.observe()` and `ObserverOverwatcher.observe_loop_task()`) have been updated to explicitly use `async_readout=True`.

### 🚀 New

* [#37](https://github.com/sdss/lvmgort/pull/37) Basic implementation of the `Troubleshooter` class for the Overwatcher. Currently only very broad troubleshooting checks and recipes are implemented.

### 🏷️ Changed

* Removed morning twilight flats.

### 🔧 Fixed

* Temporary fix in the cleanup recipe for a bug in `lvmscp` caused by a quick reset after reading out a pending exposure.

### ⚙️ Engineering

* Added a very basic test to confirm the Overwatcher can be initialised.


## 1.0.2 - November 6, 2024

### 🚀 New

* [#34](https://github.com/sdss/lvmgort/pull/34) Adds a `safety` module to the Overwatcher that will monitor the alerts independently and close the dome at a very low level if the main task fails to do it after 5 minutes.

### ✨ Improved

* [#35](https://github.com/sdss/lvmgort/pull/35) Refactor dither observing to allow finer control of when to reacquire a tile and when to keep observing and adjust the science telescope dither position.
* [#36](https://github.com/sdss/lvmgort/pull/36) Modity the evening twilight recipe to continue cycling the standards mask and taking flats until the exposure time reaches 100s.
* GORT will fail to initialise if the Overwatcher is running. This can be overridden by passing `override_overwatcher=True` to the `Gort` constructor or `--override-overwatcher` in the CLI.
* Rearranged the Overwatcher helpers and make the pre- and post-observing scripts recipes.
* Added a framework for run daily tasks and a pre-observing task.
* Add option to disable the overwatcher to the shutdown recipe.


## 1.0.1 - October 30, 2024

### ✨ Improved

* Overwatcher: cancel the current cancellation if conditions are unsafe.
* Overwatcher: lock the dome for 30 minutes if it's closed due to unsafe conditions.
* Always reset the spectrographs before an exposure. Add some checks to ensure the shutters are closed during a cleanup.

### 🏷️ Changed

* Overwatcher: rename allow dome calibrations to allow calibrations, which enabled/disables all calibration (not only in-dome calibrations).

### 🔧 Fixes

* Overwatcher: fixes to daytime logic.
* Add the current GORT version to the Overwatcher actor.
* Set the correct dither position in `GortObserver`.


## 1.0.0 - October 28, 2024

### 🚀 New

* [#32](https://github.com/sdss/lvmgort/pull/32) Initial complete implementation of the overwatcher.

### 🔧 Fixed

* [#33](https://github.com/sdss/lvmgort/pull/33) Fixed an issue in which the first standard would not be reacquired during dithered observations. Also forces a rehoming of the fibre selector mask before each observation.


## 1.0.0b1 - July 9, 2024

### 🔥 Deprecated

* Removed the websocket code and CLI. All this functionality is now part of `lvmapi`.

### ✨ Improved

* Wait for previous exposure to finish reading out in `Observer.expose()`. While this was already happening when calling `Exposure.expose()`, we are now blocking until the exposure finishes a bit earlier which prevents the standard loop to begin too early.
* Upgraded to `polars` 1.0.0.


## 0.12.0 - July 8, 2024

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
