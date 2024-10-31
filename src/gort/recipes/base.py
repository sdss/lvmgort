#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: base.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Type

from gort.enums import Event
from gort.pubsub import notify_event


if TYPE_CHECKING:
    from gort.gort import Gort


__all__ = ["recipes", "BaseRecipe"]


recipes: dict[str, Type[BaseRecipe]] = {}


class RegisterRecipe(type):
    """Metaclass to register recipes."""

    def __new__(cls, name, bases, class_dict):
        cls = type.__new__(cls, name, bases, class_dict)

        if name == "BaseRecipe":
            return cls

        assert issubclass(cls, BaseRecipe)

        if "name" not in class_dict:
            raise ValueError(f"name attribute not defined in {name}.")

        # This is a hacked version of abc.ABCMeta, but this way we
        # don't need to chain two metaclasses.
        if "recipe" not in class_dict:
            raise ValueError(f"recipe method not defined in {name}.")

        recipes[class_dict["name"]] = cls

        return cls


class BaseRecipe(object, metaclass=RegisterRecipe):
    """Base class for recipes."""

    name: str | None = None

    def __init__(self, gort: Gort):
        self.gort = gort

    async def recipe(self, *args, **kwargs):
        """The recipe. Must be overridden by the subclass."""

        return

    async def __call__(self, *args, **kwargs):
        """Executes the recipe and sends event notifications."""

        payload: dict[str, Any] = {
            "recipe_name": self.name,
            "args": list(args),
            "kwargs": kwargs,
        }

        await notify_event(Event.RECIPE_START, payload=payload)

        try:
            await self.recipe(*args, **kwargs)
        except Exception as err:
            error_payload = payload.copy()
            error_payload["error"] = str(err)
            await notify_event(Event.RECIPE_FAILED, payload=error_payload)
            raise
        else:
            await notify_event(Event.RECIPE_END, payload=payload)
