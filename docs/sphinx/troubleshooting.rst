.. _troubleshooting:

Troubleshooting
===============

Restarting a subsystem
----------------------

When a subsystem fails it is possible to restart the underlying software and controllers and reset all devices to a nominal state. This can be accomplished by calling `.GortDeviceSet.restart`, for example

.. code:: python

    >>> await g.ags.restart()
    03:45:03 [DEBUG]   Deleting deployment lvmagcam.
    03:45:06 [INFO]    Starting deployment from YAML file /home/sdss5/config/kube/actors/lvmagcam.yml.

This restart command is generally safe to use, but there may be simpler and faster troubleshooting that the user can try before resorting it.

Restarting lvmtan
-----------------

The Twice-As-Nice devices (K-mirrors, focusers, fibre selector) may hang up at times. In this case you can try restarting the telescope subsystem with ::

    await g.telescopes.restart()

Or do a more focused restart of the TAN system with ::

    await g.telescopes.restart_lvmtan()

If this does not work you may need to use the GUI. In `lvmweb <http://lvm-hub:8080/lvmweb/motan>` go to the Motor Controllers secion. There should be eight elements in the interface (three K-mirrors, four focusers, on fibre selector). Each one of them has a small circular "LED" that can be red (not working) or green (connected). Make sure all the devices have green circles. If some of them do now, try restarting the deployment ::

    g.kubernetes.restart_deployment('lvmtan')

then reload the controllers page and wait until all the LEDs are green. You'll also see some green checkmarks. If they are red crosses that means that the device is in a bad state. Try stopping and aborting the invalid devices and then home them. When you home them you should see the motor numbers/degrees change and a progress bar. The progress bar and numbers must at some point stop (K-mirrors will home at -135 degrees, fibre selector at 0, focusers at 40).

It may requires a few stop/abort/home and even various restarts of the controller to get things to work again.

Restarting a deployment
-----------------------

Most users will just want to restart a subsystem as shown above. For those wanting a finer control of what software is running, the `.Kubernetes` access point provices

LVM actors and services run in a Kubernetes cluster as `deployments <https://kubernetes.io/docs/concepts/workloads/controllers/deployment/>`__. To restart an actor or service we must restart the deployment. ``gort`` provides a simple object to access the cluster API and perform some usual tasks. For example, to restart the actor ``lvmguider``, for example

.. code:: python

    >>> g = await Gort(verbosity='debug').init()
    >>> g.kubernetes.restart_deployment('lvmguider')
    21:28:18 [DEBUG]:   Deleting deployment lvmguider.
    21:28:23 [INTO]:    Starting deployment from YAML file /home/sdss5/config/kube/actors/lvmguider.yml.

If the deployment was not running you may see a message indicating that the deployment is being recreated from a YAML file. You can see running deployments with

.. code:: python

    >>> g.kubernetes.list_deployments()
    ['local-path-provisioner',
     'rabbitmq',
     'lvmnps',
     'lvmieb',
     'lvmtelemetry',
     'restapi',
     'kubernetes-dashboard-metrics-scraper',
     'metrics-server',
     'kubernetes-dashboard-cert-manager-webhook',
     'kubernetes-dashboard-nginx-controller',
     'kubernetes-dashboard-metrics-server',
     'kubernetes-dashboard-api',
     'kubernetes-dashboard-web',
     'kubernetes-dashboard-cert-manager-cainjector',
     'kubernetes-dashboard-cert-manager',
     'coredns',
     'traefik',
     'lvm-spec-pressure-sp2',
     'lvm-spec-pressure-sp1',
     'lvm-spec-pressure-sp3',
     'lvm-jupyter',
     'lvmecp',
     'lvmscp',
     'lvmguider',
     'lvmagcam',
     'lvmpwi-sci',
     'lvmpwi-spec',
     'lvmpwi-skye',
     'lvmpwi-skyw',
     'lvmtan',
     'cerebro']

.. warning::
    This feature requires running ``gort`` in a machine that has access to the Kubernetes cluster. While you can (but is not recommended) to run ``gort`` locally and access the RabbitMQ exchange by forwarding its access port, you won't be able to do the same to access the Kubernetes API.

Here is a list of deployments, what they do, and when it may be useful to restart them. Users should **not** try to restart deployments not listed in this table.

.. csv-table::
   :file: data/deployments.csv
   :widths: 20, 35, 45
   :header-rows: 1
