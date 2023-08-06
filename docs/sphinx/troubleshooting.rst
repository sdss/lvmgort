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
     'kubernetes-dashboard',
     'coredns',
     'traefik',
     'metrics-server',
     'rabbitmq',
     'dashboard-metrics-scraper',
     'lvm-skyw-pwi',
     'lvm-sci-pwi',
     'lvm-spec-pwi',
     'lvmecp',
     'lvm-spec-pressure-sp2',
     'lvm-spec-pressure-sp3',
     'lvm-spec-pressure-sp1',
     'lvm-skyw-ag',
     'lvmtel',
     'lvm-skye-ag',
     'lvm-spec-ag',
     'lvm-sci-ag',
     'lvmieb',
     'lvmnps',
     'lvm-skye-pwi',
     'lvmtan',
     'lvm-jupyter',
     'cerebro',
     'lvmscp',
     'lvmguider']

.. warning::
    This feature requires running ``gort`` in a machine that has access to the Kubernetes cluster. While you can (but is not recommended) to run ``gort`` locally and access the RabbitMQ exchange by forwarding its access port, you won't be able to do the same to access the Kubernetes API.

Here is a list of deployments, what they do, and when it may be useful to restart them. Users should **not** try to restart deployments not listed in this table.

.. csv-table::
   :file: data/deployments.csv
   :widths: 20, 35, 45
   :header-rows: 1
