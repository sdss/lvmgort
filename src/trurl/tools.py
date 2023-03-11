#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: tools.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import re


def get_valid_variable_name(var_name: str):
    """Converts a string to a valid variable name."""

    return re.sub(r"\W|^(?=\d)", "_", var_name)
