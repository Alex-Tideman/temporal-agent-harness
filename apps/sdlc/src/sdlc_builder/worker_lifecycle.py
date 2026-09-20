"""Close cached workflow coroutines after the worker stops polling.

Temporal Python 1.33.0 leaves workflow instances in its Python cache on shutdown.
Garbage collection then closes suspended SDK streams outside their original
context. Use Temporal's normal remove-from-cache activation to unwind them.
This compatibility shim uses private cache access; the live restart test guards
it when updating Temporal. Remove when the SDK itself drains this cache.
"""

import asyncio
from contextlib import asynccontextmanager

from temporalio.bridge.proto.workflow_activation import (
    RemoveFromCache,
    WorkflowActivation,
    WorkflowActivationJob,
)
from temporalio.worker import Worker


def _evict_stopped_workflows(worker: Worker) -> None:
    if not worker.is_shutdown:
        raise RuntimeError("Workflow cache cleanup requires a stopped worker")
    workflow_worker = worker._workflow_worker
    if workflow_worker is None:
        return
    cache = workflow_worker._running_workflows
    for run_id, instance in list(cache.items()):
        completion = instance.activate(
            WorkflowActivation(
                run_id=run_id,
                jobs=[
                    WorkflowActivationJob(
                        remove_from_cache=RemoveFromCache(
                            message="App worker shutdown",
                            reason=RemoveFromCache.EvictionReason.LANG_REQUESTED,
                        )
                    )
                ],
            )
        )
        if completion.HasField("failed"):
            raise RuntimeError(f"Could not close cached workflow {run_id}")
        # This activation only releases in-memory coroutines. Nothing is sent
        # to Temporal: persisted workflows remain resumable on the next launch.
        del cache[run_id]


@asynccontextmanager
async def managed_worker(worker: Worker):
    try:
        async with worker:
            yield worker
    finally:
        if worker.is_shutdown:
            # All pollers and activation threads have stopped. Run eviction on
            # a separate thread so the workflow can enter its own event loop.
            await asyncio.to_thread(_evict_stopped_workflows, worker)
