Troubleshooting
===============

Restarting an actor
-------------------

LVM actors run in a Kubernetes cluster as `deployments <https://kubernetes.io/docs/concepts/workloads/controllers/deployment/>`__. To restart an actor we must restart the deployment. ``gort`` provides a simple object to access the cluster API and perform some usual tasks. To restart the actor ``lvmguider``, for example ::

    >>> g = await Gort(verbosity='debug').init()
    >>> g.kubernetes.restart_deployment('lvmguider')
    21:28:18 [DEBUG]: Rollout restarting deployment lvmguider.

If the deployment was not running you may see a message indicating that the deployment is being recreated from a YAML file. You can see running deployments with ::

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
