import logging

from .task_queue import TaskQueueClient

logger = logging.getLogger("worker.recovery")

class TaskRecoveryManager:
    """
    TaskRecoveryManager identifies tasks interrupted due to worker crashes or network losses,
    and handles graceful resume capabilities.
    """
    def __init__(self, backend_url: str, queue: TaskQueueClient | None = None):
        self.backend_url = backend_url
        self.queue = queue
        logger.info(f"TaskRecoveryManager configured with backend {backend_url}")

    def scan_and_recover_tasks(self) -> list:
        """
        Scans database / backend for 'processing' or 'pending' tasks assigned to this worker,
        recovering them or resetting their state so they can be re-run.
        """
        logger.info("Scanning for interrupted tasks to recover...")
        recovered_tasks = self.queue.recover() if self.queue is not None else []
        logger.info(f"Scan complete. Recovered {len(recovered_tasks)} tasks.")
        return recovered_tasks
