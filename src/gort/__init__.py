# encoding: utf-8

from astropy.utils import iers
from astropy.utils.iers import conf

from sdsstools import get_config, get_package_version


# pip package name
NAME = "lvmgort"

# Loads config. config name is the package name.
config = get_config(NAME, config_envvar="GORT_CONFIG_FILE")

# package name should be pip package name
__version__ = get_package_version(path=__file__, package_name=NAME)

# Ensure that astropy does not connect to the internet.
conf.auto_download = False
conf.iers_degraded_accuracy = "ignore"

# See https://github.com/astropy/astropy/issues/15881
iers_a = iers.IERS_A.open(iers.IERS_A_FILE)
iers.earth_orientation_table.set(iers_a)

from .core import *
from .devices import *
from .exposure import *
from .gort import *
from .observer import *
from .tile import *
