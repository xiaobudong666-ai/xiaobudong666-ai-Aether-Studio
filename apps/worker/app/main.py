import os
import time
import json
import logging
import threading
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler

from .ffmpeg_adapter import FFmpegAdapter
from .ai_provider import AIProviderInterface
from .recovery import TaskRecoveryManager
from .moneyprinter_adapter import MoneyPrinterTurboAdapter
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
    host: str = "0.0.0.0",
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


def initialize_worker() -> WorkerComponents:
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    # Enable degradation fallback by default in background worker
    moneyprinter_adapter = MoneyPrinterTurboAdapter(degrade_on_failure=True)
    return WorkerComponents(
        ffmpeg=FFmpegAdapter(),
        ai=AIProviderInterface(),
        recovery=TaskRecoveryManager(backend_url=backend_url),
        moneyprinter=moneyprinter_adapter,
        video_use=VideoUseAdapter(),
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
            logger.info("Daemon active: polling for pending video rendering and subtitle generation tasks...")

            # Demonstration of the M1 call path for local contract validation:
            # Under actual production runs, tasks matching a MoneyPrinter pattern would pass through process_m1_moneyprinter_task.
            time.sleep(poll_interval)
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
