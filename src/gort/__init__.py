# encoding: utf-8

from sdsstools import get_config, get_package_version


# pip package name
NAME = "lvmgort"

# Loads config. config name is the package name.
config = get_config(NAME, config_envvar="GORT_CONFIG_FILE")

# package name should be pip package name
__version__ = get_package_version(path=__file__, package_name=NAME)


from .core import *
from .devices import *
from .exposure import *
from .gort import *
from .observer import *
from .tile import *
