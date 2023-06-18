# encoding: utf-8

import logging

from sdsstools import get_config, get_logger, get_package_version


# pip package name
NAME = "lvmtrurl"

# Loads config. config name is the package name.
config = get_config("lvmtrurl")

log = get_logger(NAME)
log.sh.setLevel(logging.INFO)
log.start_file_logger("/data/logs/trurl/trurl.log")


# package name should be pip package name
__version__ = get_package_version(path=__file__, package_name=NAME)


from .core import *
from .trurl import *
