"""V2 retains task snapshots and page variants for missing-output repairs."""


def cleanup(job):
    return {"generated_cache_removed": False, "cache_retained_for_repair": True}
