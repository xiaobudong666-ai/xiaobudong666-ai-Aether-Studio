import time
import json
import logging
import threading
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

    def log_message(self, format, *args):
        # Suppress logging every health-check access to keep logs clean
        pass

def start_health_server(port=8001):
    server = HTTPServer(("0.0.0.0", port), WorkerHealthHandler)
    logger.info(f"Worker health check server listening on port {port}")
    server.serve_forever()

START_TIME = time.time()

def run_worker():
    logger.info("Initializing Aether Studio Background Worker...")

    # 1. Initialize Adapters and Interfaces
    ffmpeg = FFmpegAdapter()
    ai = AIProviderInterface()
    recovery = TaskRecoveryManager(backend_url="http://localhost:8000")

    # 2. Run Task Recovery scan
    recovery.scan_and_recover_tasks()

    logger.info("Worker initialization complete. Starting task execution loop...")

    # 3. Simulate processing daemon loop
    try:
        while True:
            # Demonstration Task execution logging
            logger.info("Daemon active: polling for pending video rendering and subtitle generation tasks...")

            # Simulate processing a demo task if needed, or just sleep
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Worker shutting down gracefully.")

if __name__ == "__main__":
    # Start Health server in a background thread
    t = threading.Thread(target=start_health_server, args=(8001,), daemon=True)
    t.start()

    # Run the main worker daemon loop
    run_worker()
