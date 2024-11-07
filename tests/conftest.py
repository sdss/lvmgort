#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: conftest.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import pytest
from pytest_mock import MockerFixture

from clu.testing import setup_test_actor

from gort.gort import Gort
from gort.overwatcher.actor import OverwatcherActor
from gort.overwatcher.calibration import CalibrationSchedule
from gort.overwatcher.helpers.tasks import DailyTaskBase


@pytest.fixture()
def mock_overwatcher(mocker: MockerFixture):
    """Mocks several things for the overwatcher tests."""

    mocker.patch.object(CalibrationSchedule, "update_schedule", autospec=True)
    mocker.patch.object(DailyTaskBase, "update_status", autospec=True)

    mocker.patch.object(Gort, "init", autospec=True)

    yield


@pytest.fixture()
async def overwatcher_actor(mock_overwatcher):
    _actor = OverwatcherActor()
    _actor = await setup_test_actor(_actor)  # type: ignore

    # setup_test_actor mocks OverwatcherActor.start() but we want to run the overwatcher
    # await _actor.overwatcher.run()

    yield _actor

    _actor.mock_replies.clear()

    await _actor.stop()
    _actor.overwatcher.cancel()
