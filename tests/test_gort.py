#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: test_main.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from gort import Gort


def test_placeholder():
    g = Gort()
    assert g
