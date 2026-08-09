import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from .ai_provider import AIProviderInterface
from .ffmpeg_adapter import FFmpegAdapter
from .moneyprinter_adapter import MoneyPrinterTurboAdapter
from .recovery import TaskRecoveryManager
from .task_queue import TaskQueueClient, TaskQueueError
from .video_use_adapter import VideoUseAdapter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("worker.main")


class WorkerHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "status": "healthy",
                "service": "worker",
                "engine": "Aether Studio Worker",
                "uptime_seconds": int(time.time() - START_TIME)
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, _format, *_args):
        # Suppress logging every health-check access to keep logs clean
        pass


def create_health_server(
    host: str = "0.0.0.0",  # noqa: S104 - isolated container health endpoint
    port: int = 8001,
) -> HTTPServer:
    return HTTPServer((host, port), WorkerHealthHandler)


def start_health_server(port: int = 8001):
    server = create_health_server(port=port)
    logger.info(
        "Worker health check server listening on port %s",
        server.server_address[1],
    )
    server.serve_forever()


START_TIME = time.time()


@dataclass
class WorkerComponents:
    ffmpeg: FFmpegAdapter
    ai: AIProviderInterface
    recovery: TaskRecoveryManager
    moneyprinter: MoneyPrinterTurboAdapter
    video_use: VideoUseAdapter
    queue: TaskQueueClient | None = None


def initialize_worker() -> WorkerComponents:
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    queue = TaskQueueClient(backend_url=backend_url)
    # Enable degradation fallback by default in background worker
    moneyprinter_adapter = MoneyPrinterTurboAdapter(degrade_on_failure=True)
    return WorkerComponents(
        ffmpeg=FFmpegAdapter(),
        ai=AIProviderInterface(),
        recovery=TaskRecoveryManager(backend_url=backend_url, queue=queue),
        moneyprinter=moneyprinter_adapter,
        video_use=VideoUseAdapter(),
        queue=queue,
    )


def process_render_task(components: WorkerComponents, task: dict, poll_interval: float = 0.5) -> dict:
    if components.queue is None:
        raise TaskQueueError("Worker task queue is not configured")
    task_id = task["taskId"]
    upstream_job_id = task.get("upstreamJobId")
    try:
        if not upstream_job_id:
            submitted = components.video_use.submit_render(task["renderPayload"])
            upstream_job_id = submitted["jobId"]
            components.queue.update(
                task_id,
                status="processing",
                progress=int(submitted.get("progress", 0)),
                message="任务已提交至视频渲染服务",
                upstream_job_id=upstream_job_id,
            )

        deadline = time.monotonic() + float(os.environ.get("AETHER_RENDER_TIMEOUT_SECONDS", "3600"))
        while time.monotonic() < deadline:
            upstream = components.video_use.get_job_status(upstream_job_id)
            upstream_status = str(upstream.get("status", "processing"))
            progress = int(upstream.get("progress", 0))
            message = str(upstream.get("message", upstream_status))
            if upstream_status == "completed":
                return components.queue.update(
                    task_id, status="completed", progress=100, message=message,
                    upstream_job_id=upstream_job_id,
                )
            if upstream_status == "failed":
                return components.queue.update(
                    task_id, status="failed", progress=100, message=message,
                    upstream_job_id=upstream_job_id, error=message,
                    retryable=bool(upstream.get("retryable", False)),
                )
            components.queue.update(
                task_id, status="processing", progress=progress, message=message,
                upstream_job_id=upstream_job_id,
            )
            time.sleep(poll_interval)
        raise TimeoutError("Render exceeded the worker timeout")
    except Exception as exc:
        logger.exception("Render task %s failed in worker", task_id)
        return components.queue.update(
            task_id, status="failed", progress=int(task.get("progress", 0)),
            message="工作节点将在短暂故障后自动重试", upstream_job_id=upstream_job_id,
            error=str(exc), retryable=True,
        )


def process_m1_moneyprinter_task(components: WorkerComponents, task_data: dict) -> dict:
    """
    Clear, auditable M1-0 call path for a MoneyPrinterTurbo generation request.
    This demonstrates the end-to-end adapter pipeline (Contract Ready).
    Real video production is marked as unavailable/unknown at this stage as credentials are not configured.
    """
    logger.info("Processing task via MoneyPrinterTurbo Sidecar Adapter: %s", task_data)

    # 1. Check health & capabilities
    health = components.moneyprinter.check_health()
    if health.get("status") != "healthy":
        logger.error("MoneyPrinterTurbo sidecar is unhealthy or unreachable. Aborting task.")
        return {"status": "failed", "reason": "Sidecar unhealthy or unreachable"}

    # 2. Trigger Generation
    try:
        task_id = components.moneyprinter.generate_video(
            subject=task_data.get("subject", "AI Anime"),
            aspect=task_data.get("aspect", "9:16"),
            voice_name=task_data.get("voice_name", "en-US-JennyNeural"),
        )
        logger.info("Successfully triggered sidecar generation task: %s", task_id)

        # 3. Poll task status
        status = components.moneyprinter.get_task_status(task_id)
        logger.info("Fetched sidecar task status: %s", status)
        return status
    except Exception as exc:
        logger.error("Failed to process MoneyPrinterTurbo task: %s", exc)
        return {"status": "failed", "reason": str(exc)}


def run_worker(poll_interval: float = 10):
    logger.info("Initializing Aether Studio Background Worker...")

    components = initialize_worker()
    components.recovery.scan_and_recover_tasks()

    logger.info("Probing MoneyPrinterTurbo sidecar capabilities...")
    mpt_health = components.moneyprinter.check_health()
    logger.info("MoneyPrinterTurbo health: %s", mpt_health)
    mpt_caps = components.moneyprinter.get_capabilities()
    logger.info("MoneyPrinterTurbo capabilities: %s", mpt_caps)

    logger.info("Probing video-use sidecar capabilities...")
    video_use_health = components.video_use.check_health()
    logger.info("video-use health: %s", video_use_health)
    if video_use_health.get("status") == "healthy":
        logger.info("video-use capabilities: %s", components.video_use.get_capabilities())

    logger.info("Worker initialization complete. Starting task execution loop...")

    try:
        while True:
            task = components.queue.claim() if components.queue is not None else None
            if task is None:
                time.sleep(poll_interval)
                continue
            logger.info("Claimed render task %s", task["taskId"])
            process_render_task(components, task)
    except KeyboardInterrupt:
        logger.info("Worker shutting down gracefully.")


if __name__ == "__main__":
    worker_port = int(os.environ.get("WORKER_PORT", "8001"))
    t = threading.Thread(
        target=start_health_server,
        args=(worker_port,),
        daemon=True,
    )
    t.start()
    run_worker()
