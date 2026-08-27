import json
import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from .ai_provider import AIProviderInterface
from .ffmpeg_adapter import FFmpegAdapter
from .generation_queue import GenerationQueueClient, GenerationQueueError
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
    generation_queue: GenerationQueueClient | None = None


def initialize_worker() -> WorkerComponents:
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    queue = TaskQueueClient(backend_url=backend_url)
    generation_queue = GenerationQueueClient(backend_url=backend_url)
    # Enable degradation fallback by default in background worker
    moneyprinter_adapter = MoneyPrinterTurboAdapter(degrade_on_failure=True)
    return WorkerComponents(
        ffmpeg=FFmpegAdapter(),
        ai=AIProviderInterface(),
        recovery=TaskRecoveryManager(backend_url=backend_url, queue=queue),
        moneyprinter=moneyprinter_adapter,
        video_use=VideoUseAdapter(),
        queue=queue,
        generation_queue=generation_queue,
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


def process_generation_task(
    components: WorkerComponents,
    task: dict,
    poll_interval: float = 0.5,
) -> dict:
    queue = components.generation_queue
    if queue is None:
        raise GenerationQueueError("Generation queue is not configured")
    task_id = str(task["taskId"])
    if task.get("providerMode") != "deterministic-fake":
        return queue.transition(
            task_id, status="FAILED", progress=0,
            message="运行时生成 Provider 未启用",
            error_code="PROVIDER_DISABLED", error_message="Provider disabled",
            retryable=False,
        )
    request = task.get("request") or {}
    upstream_job_id = task.get("upstreamJobId")
    try:
        if not upstream_job_id:
            try:
                upstream_job_id = components.moneyprinter.generate_video(
                    subject=request["videoSubject"],
                    aspect=request["videoAspect"],
                    voice_name=request["voiceName"],
                    video_concat_mode=request["videoConcatMode"],
                    video_clip_duration=request["videoClipDuration"],
                )
            except TimeoutError as exc:
                return queue.transition(
                    task_id, status="UNKNOWN", progress=0,
                    message="上游提交结果不明确，已停止自动重投",
                    error_code="AMBIGUOUS_SUBMISSION", error_message=str(exc),
                    retryable=False,
                )
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                retryable = not isinstance(status_code, int) or not 400 <= status_code < 500
                return queue.transition(
                    task_id, status="FAILED", progress=0,
                    message="Provider 拒绝生成请求" if not retryable else "Provider 提交暂时失败",
                    error_code="PROVIDER_4XX" if not retryable else "PROVIDER_SUBMIT_FAILED",
                    error_message=str(exc), retryable=retryable,
                )
            queue.transition(
                task_id, status="RUNNING", progress=5,
                message="生成任务已提交至确定性测试 Provider",
                upstream_job_id=upstream_job_id,
            )

        deadline = time.monotonic() + float(os.environ.get("AETHER_GENERATION_TIMEOUT_SECONDS", "3600"))
        while time.monotonic() < deadline:
            queue.heartbeat(task_id)
            upstream = components.moneyprinter.get_task_status(upstream_job_id)
            status = str(upstream.get("status", "processing")).lower()
            progress = max(5, min(95, int(upstream.get("progress", 50))))
            if status in {"completed", "succeeded"}:
                provider_artifact_id = str(
                    upstream.get("providerArtifactId") or upstream.get("artifact_id") or ""
                )
                if not provider_artifact_id:
                    return queue.transition(
                        task_id, status="FAILED", progress=progress,
                        message="Provider 未返回受信任产物编号",
                        upstream_job_id=upstream_job_id,
                        error_code="ARTIFACT_ID_MISSING", error_message="Artifact identifier missing",
                        retryable=False,
                    )
                queue.transition(
                    task_id, status="INGESTING", progress=95,
                    message="正在流式接收并校验生成产物",
                    upstream_job_id=upstream_job_id,
                    provider_artifact_id=provider_artifact_id,
                )
                artifact_stream = components.moneyprinter.stream_artifact(provider_artifact_id)
                if isinstance(artifact_stream, bytes):
                    artifact_stream = io.BytesIO(artifact_stream)
                return queue.artifact_intake(task_id, provider_artifact_id, artifact_stream)
            if status in {"failed", "error"}:
                retryable = bool(upstream.get("retryable", False))
                return queue.transition(
                    task_id, status="FAILED", progress=progress,
                    message="Provider 报告生成失败",
                    upstream_job_id=upstream_job_id,
                    error_code=str(upstream.get("errorCode") or "PROVIDER_FAILED"),
                    error_message=str(upstream.get("message") or "Provider failed"),
                    retryable=retryable,
                )
            if status in {"canceled", "cancelled"}:
                return queue.transition(
                    task_id, status="CANCELED", progress=progress,
                    message="Provider 已取消生成任务", upstream_job_id=upstream_job_id,
                )
            queue.transition(
                task_id, status="RUNNING", progress=progress,
                message="确定性测试 Provider 正在生成",
                upstream_job_id=upstream_job_id,
            )
            time.sleep(poll_interval)
        return queue.transition(
            task_id, status="UNKNOWN", progress=95,
            message="生成状态查询超时，已停止自动重投",
            upstream_job_id=upstream_job_id,
            error_code="STATUS_TIMEOUT", error_message="Generation status timed out",
            retryable=False,
        )
    except Exception as exc:
        logger.exception("Governed generation task %s failed", task_id)
        return queue.transition(
            task_id, status="FAILED", progress=0,
            message="生成 Worker 发生可恢复错误",
            upstream_job_id=upstream_job_id,
            error_code="WORKER_TRANSIENT_FAILURE", error_message=str(exc),
            retryable=True,
        )


def run_worker(poll_interval: float = 10):
    logger.info("Initializing Aether Studio Background Worker...")

    components = initialize_worker()
    components.recovery.scan_and_recover_tasks()

    logger.info("Governed generation provider runtime mode: disabled unless claimed task is deterministic-fake")

    logger.info("Probing video-use sidecar capabilities...")
    video_use_health = components.video_use.check_health()
    logger.info("video-use health: %s", video_use_health)
    if video_use_health.get("status") == "healthy":
        logger.info("video-use capabilities: %s", components.video_use.get_capabilities())

    logger.info("Worker initialization complete. Starting task execution loop...")

    try:
        while True:
            generation_task = (
                components.generation_queue.claim()
                if components.generation_queue is not None else None
            )
            if generation_task is not None:
                logger.info("Claimed governed generation task %s", generation_task["taskId"])
                process_generation_task(components, generation_task)
                continue
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
