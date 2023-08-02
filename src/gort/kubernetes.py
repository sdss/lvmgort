#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-29
# @Filename: kubernetes.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import datetime
import logging
import os
import pathlib
from time import sleep

import kubernetes
from kubernetes.utils import create_from_yaml

from gort import config
from gort.tools import is_notebook


class Kubernetes:
    """Interface with the Kubernetes cluster."""

    def __init__(self, log: logging.Logger | None = None):
        self.is_notebook = is_notebook()

        self.log = log or logging.getLogger("gort.kubernetes")

        if os.getenv("KUBERNETES_SERVICE_HOST"):
            self.is_pod = True
        else:
            self.is_pod = False

        # If we are in a notebook, we assume it's the one running in the Jupyter
        # Lab deployment, which is configured to have access to the cluster.
        if self.is_notebook or self.is_pod:
            kubernetes.config.load_incluster_config()
        else:
            kubernetes.config.load_config()

        self.v1 = kubernetes.client.CoreV1Api()
        self.apps_v1 = kubernetes.client.AppsV1Api()

    def list_namespaces(self):
        """Returns a list of namespaces."""

        namespace_info = self.v1.list_namespace()
        namespaces = [item.metadata.name for item in namespace_info.items]

        return namespaces

    def list_deployments(self):
        """Returns a list of deployments."""

        deployment_info = self.apps_v1.list_deployment_for_all_namespaces()
        deployments = [item.metadata.name for item in deployment_info.items]

        return deployments

    def get_deployment_namespace(self, deployment: str):
        """Returns the namespace of a deployment."""

        deployment_info = self.apps_v1.list_deployment_for_all_namespaces()

        for item in deployment_info.items:
            meta = item.metadata
            if meta.name == deployment:
                return meta.namespace

        return None

    def get_yaml_file(self, name: str):
        """Finds and returns the contents of a Kubernetes YAML file."""

        if self.is_notebook or self.is_pod:
            path = pathlib.Path(config["kubernetes"]["path"]["notebook"])
        else:
            path = pathlib.Path(config["kubernetes"]["path"]["default"])

        path = path.expanduser()

        files = list(path.glob(f"**/{name}*"))

        if files is None or len(files) == 0:
            raise ValueError(f"No YAML file found for {name!r}.")
        elif len(files) > 1:
            raise ValueError(f"Multiple YAML files found for {name!r}.")

        return files[0]

    def apply_from_file(self, name: str | pathlib.Path):
        """Applies a YAML file.

        Parameters
        ----------
        name
            The full path to the file to apply. If the path is relative,
            the file will be searched in the directory for YAML files
            defined in the configuration.

        """

        if isinstance(name, pathlib.Path) or os.path.isabs(name):
            path = pathlib.Path(name)
        else:
            path = self.get_yaml_file(name)

        deployments = create_from_yaml(
            kubernetes.client.ApiClient(),
            yaml_file=str(path),
        )

        return [dep[0].metadata.name for dep in deployments]

    def delete_deployment(self, deployment: str):
        """Deletes resources from a YAML file.

        Parameters
        ----------
        deployment
            The deployment to delete.

        """

        namespace = self.get_deployment_namespace(deployment)
        if namespace is None:
            raise ValueError(f"Deployment {deployment!r} not found.")

        self.apps_v1.delete_namespaced_deployment(deployment, namespace)

    def restart_deployment(self, deployment: str, from_file: bool = True):
        """Restarts a deployment.

        If the deployment is running, does a rollout restart. Otherwise looks
        for the deployment file and applies it.

        """

        if deployment in self.list_deployments() and not from_file:
            namespace = self.get_deployment_namespace(deployment)
            if namespace is None:
                raise ValueError(f"Namespace not found for deployment {deployment}.")

            # Create a patch for the current deployment saying that it was restarted
            # now, and it will.
            now = datetime.datetime.utcnow()
            now = str(now.isoformat("T") + "Z")
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            }

            self.log.debug(f"Rollout restarting deployment {deployment}.")
            self.apps_v1.patch_namespaced_deployment(
                deployment,
                namespace,
                body,
                pretty="true",
            )

        else:
            try:
                file_ = self.get_yaml_file(deployment)
            except ValueError as err:
                self.log.warning(
                    f"Failed restarting from file: {err} "
                    "Doing rollout restart instead."
                )
                return self.restart_deployment(deployment, from_file=False)

            if deployment in self.list_deployments():
                self.delete_deployment(deployment)
                sleep(3)  # Give some time for the pods to exit.
            else:
                self.log.warning(f"{deployment!r} is not running.")

            self.log.info(f"Starting deployment from YAML file {str(file_)}.")
            self.apply_from_file(file_)
