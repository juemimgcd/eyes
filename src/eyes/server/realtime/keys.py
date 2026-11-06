def summary_key(namespace, epoch, project_id, experiment_id):
    """Shared cache address for summary reads and post-commit invalidation."""
    return f"{namespace}:summary:v3:{epoch}:{project_id}:{experiment_id}"
