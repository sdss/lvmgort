
.. _recipes:

Recipes
=======

Recipes are snippets of code that execute a series of tasks and commands, either for operational purposes or for troubleshooting.

Recipes are defined as subclasses of `.BaseRecipe` and must override the ``name`` attribute and the `~.BaseRecipe.recipe` method, the latter of which defines the tasks to execute. The recipe is instantiated with a `.Gort` object and can be executed by calling the instance. For example, we define the recipe ``test_recipe`` ::

    from gort.recipes import BaseRecipe

    class TestRecipe(BaseRecipe):
        name = 'test_recipe'

        async def recipe(self, do_task_1=True, do_task_2=False):
            if do_task_1:
                ...

            if do_task_2:
                ...

And now we can execute it as ::

    from gort import Gort

    g = await Gort().init()
    recipe = BaseRecipe(g)
    await recipe(do_task_2=True)

A list of available recipes can be retrieved as ::

    >>> from gort.recipes import recipes
    >>> recipes
    {'calibration': gort.recipes.calibration.CalibrationRecipe,
     'startup': gort.recipes.operations.StartupRecipe,
     'shutdown': gort.recipes.operations.ShutdownRecipe
     ...
    }

For simplicity, recipes can be executed using `.Gort.execute_recipe` ::

    await g.execute_recipe('shutdown')

A number of recipes are also accessible directly in `.Gort` for convenience, for example `~.Gort.startup` or `~.Gort.shutdown`.


Available recipes
-----------------

.. _recipes-startup:

``startup``
^^^^^^^^^^^

The `startup <.StartupRecipe>` executes a series of steps that must be run before observing. In particular it:

- Homes the telescopes, K-mirrors, focusers, and fibre selector.
- Takes AG dark frames.
- Runs the calibration sequence.
- Opens the dome.
- Focuses the telescopes.

To run it, you can access it directly from `.Gort` ::

    >>> from gort import Gort
    >>> g = await Gort().init()
    >>> await g.startup()
    ...

`startup <.StartupRecipe.recipe>` accepts arguments to determine whether to run the calibration sequence (and which one), open the dome, focus, and whether to ask for confirmation for opening the dome. To skip the calibration sequence and do not ask for confirmation for opening the dome ::

    await g.startup(calibration_sequence=False, confirm_open=False)

.. _recipes-shutdown:

``shutdown``
^^^^^^^^^^^^

To shutdown operations, close the dome, and park the telescopes for the night you can use the `shutdown <.ShutdownRecipe>` recipe or directly call `.Gort.shutdown`.

If you are closing the dome temporarily, you may not want to disable the telescopes; in this case call ::

    await g.shutdown(park_telescopes=False)

``calibration``
^^^^^^^^^^^^^^^

Runs spectrograph calibration sequences. See :ref:`calibrations`.
