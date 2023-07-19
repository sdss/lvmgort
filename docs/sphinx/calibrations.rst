
.. _calibrations:

Taking calibration data
=======================

Calibration data can be taken manually by commanding the individual components. For example, we would start by pointing all the telescopes to the flat field screen ::

    await g.telescopes.goto_named_position("calibration")

Then we can turn of the argon lamp ::

    await g.nps.calib.on('argon')

And finally expose the spectrographs ::

    await g.specs.expose(60, flavour='arc')

`gort` provides some tools to simplify this procedure and define and run long calibration sequences requiring minimal human supervision. To launch a standard calibration sequence ::

    await g.specs.calibrate(sequence="normal")

See `~.SpectrographSet.calibrate` for details. Pre-defined calibration sequences are defined in the :ref:`configuration file <configuration-file>` under ``specs.calibration.sequences``. Alternatively one can pass `~.SpectrographSet.calibrate` a dictionary with the calibration sequence details; such dictionary must match the model defined :ref:`below <calibration-schema>`.

An example of a very simple calibration sequence would be ::

    {
        "biases": {
            "count": 1
        },
        "darks": {
            "exposure_time": [90, 900]
        },
        "lamps": {
            "Quartz": {
                "warmup": 20,
                "exposure_time": 120,
                "flavour": "flat"
            }
        }
    }

which would take one bias, two darks with 90 and 900 second exposures, and then warm up the quartz lamp for 20 seconds before taking a 120 second flat exposure. Additional lamps can be added with the same format and multiple exposures can be taken for each lamp. Calibrations are always taken in order of bias, darks, then lamps.

In some cases one may want a series of fibres in the spectrophotometric mask to be exposed during a single exposure. We can define that with ::

    {
        "lamps":
            "LDLS": {
                "warmup": 300,
                "exposure_time": 270,
                "fibsel": {
                    "initial_position": "P1-2",
                    "positions": "P1-"
                    "time_per_position": 20,
                }
            }
    }

which will rotate the fibre mask to expose each fibre whose name begins with ``P1-`` for 10 seconds each, starting with ``P1-2``. ``"positions"`` can also be a explicit list of fibre mask positions. It's possible to use the fibre selector with default parameters by setting ``"fibsel": True`` ::

    {
        "lamps":
            "LDLS": {
                "warmup": 300,
                "exposure_time": [270, 300],
                "fibsel": True
            }
    }

In this case two LDLS exposures will be take with 270 and 300 second exposures, each one iterating over the default fibre mask positions.


.. _calibration-schema:

Schema
------

.. jsonschema:: ../../src/gort/etc/calibration_schema.json
