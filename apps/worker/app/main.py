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


def initialize_worker() -> WorkerComponents:
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    return WorkerComponents(
        ffmpeg=FFmpegAdapter(),
        ai=AIProviderInterface(),
        recovery=TaskRecoveryManager(backend_url=backend_url),
    )


def run_worker(poll_interval: float = 10):
    logger.info("Initializing Aether Studio Background Worker...")

    components = initialize_worker()
    components.recovery.scan_and_recover_tasks()

    logger.info("Worker initialization complete. Starting task execution loop...")

    try:
        while True:
            logger.info("Daemon active: polling for pending video rendering and subtitle generation tasks...")
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
