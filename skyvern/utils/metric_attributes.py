"""Attributes that identify the process emitting a per-process OTEL metric."""

import os


def task_metric_attributes() -> dict[str, str]:
    """Identify the emitting process so a per-process gauge does not collapse into one
    fleet-wide series. ``HOSTNAME`` is the fallback: it is unique per ECS ``awsvpc`` task
    and per Kubernetes pod."""
    container = os.getenv("ECS_CONTAINER_NAME")
    if not container:
        task_arn = os.getenv("ECS_TASK_ARN")
        if task_arn:
            container = task_arn.split("/")[-1]
    if not container:
        container = os.getenv("HOSTNAME")

    return {"container": container} if container else {}
