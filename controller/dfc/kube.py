"""Kubernetes backend (imported only in-cluster / with kubeconfig)."""
from __future__ import annotations


class KubeBackend:
    def __init__(self):
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.namespace = None
        self.deployment = None
        self.api_calls = 0

    def pods(self, ns, selector):
        self.api_calls += 1
        out = []
        for p in self.core.list_namespaced_pod(ns, label_selector=selector).items:
            if p.status.phase == "Running" and p.status.pod_ip and not p.metadata.deletion_timestamp:
                out.append((p.metadata.name, p.status.pod_ip))
        return out

    def replicas(self, ns, dep):
        self.api_calls += 1
        self.namespace, self.deployment = ns, dep
        d = self.apps.read_namespaced_deployment(dep, ns)
        return int(d.spec.replicas or 0), int(d.status.ready_replicas or 0)

    def set_replicas(self, n):
        self.api_calls += 1
        self.apps.patch_namespaced_deployment_scale(
            self.deployment, self.namespace, {"spec": {"replicas": int(n)}})
