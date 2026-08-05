import os
import time
import logging
import httpx

logger = logging.getLogger("api.moneyprinter_adapter")

class MoneyPrinterError(Exception):
    """Base exception for MoneyPrinterTurbo Adapter errors."""
    pass

class MoneyPrinterTimeoutError(MoneyPrinterError):
    """Exception raised when requests to MoneyPrinterTurbo timeout."""
    pass

class MoneyPrinterConnectionError(MoneyPrinterError):
    """Exception raised on connection errors to MoneyPrinterTurbo."""
    pass

class MoneyPrinterTaskFailedError(MoneyPrinterError):
    """Exception raised when a MoneyPrinterTurbo task fails."""
    pass


class MoneyPrinterTurboAdapter:
    """
    Adapter for communicating with the MoneyPrinterTurbo Sidecar API.
    Provides health checking, capability detection, video generation,
    status polling, error mapping, timeouts, and exponential backoff retries.
    """

    def __init__(
        self,
        api_url: str = None,
        timeout: float = None,
        max_retries: int = None,
        backoff_factor: float = None,
        degrade_on_failure: bool = False,
    ):
        self.api_url = (
            api_url
            or os.environ.get("MONEYPRINTER_API_URL", "http://localhost:8080")
        ).rstrip("/")

        self.timeout = float(
            timeout
            or os.environ.get("MONEYPRINTER_TIMEOUT", "10.0")
        )
        self.max_retries = int(
            max_retries
            or os.environ.get("MONEYPRINTER_MAX_RETRIES", "3")
        )
        self.backoff_factor = float(
            backoff_factor
            or os.environ.get("MONEYPRINTER_RETRY_BACKOFF", "2.0")
        )
        self.degrade_on_failure = degrade_on_failure or (
            os.environ.get("MONEYPRINTER_DEGRADE_ON_FAILURE", "false").lower() == "true"
        )

        logger.info(
            "MoneyPrinterTurboAdapter initialized. API URL: %s, Timeout: %ss, Max Retries: %s",
            self.api_url, self.timeout, self.max_retries
        )

    def _request_with_retry(
        self,
        method: str,
        path: str,
        json_data: dict = None,
        params: dict = None,
    ) -> httpx.Response:
        """
        Executes an HTTP request with exponential backoff retries.
        Maps generic HTTP exceptions to specific custom exceptions.
        """
        url = f"{self.api_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info(
                    "Sending %s request to %s (Attempt %s/%s)",
                    method, url, attempt, self.max_retries
                )
                with httpx.Client(trust_env=False, timeout=self.timeout) as client:
                    if method.upper() == "POST":
                        resp = client.post(url, json=json_data, params=params)
                    else:
                        resp = client.get(url, params=params)

                # Check status and raise if not 2xx
                resp.raise_for_status()
                return resp

            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                logger.warning(
                    "Connection error to %s on attempt %s: %s", url, attempt, exc
                )
                if attempt >= self.max_retries:
                    raise MoneyPrinterConnectionError(
                        f"Failed to connect to MoneyPrinterTurbo at {self.api_url}: {exc}"
                    ) from exc

            except httpx.ReadTimeout as exc:
                logger.warning(
                    "Timeout reading from %s on attempt %s: %s", url, attempt, exc
                )
                if attempt >= self.max_retries:
                    raise MoneyPrinterTimeoutError(
                        f"Request to MoneyPrinterTurbo timed out after {self.timeout}s: {exc}"
                    ) from exc

            except httpx.HTTPStatusError as exc:
                logger.error(
                    "HTTP Error %s response from %s: %s",
                    exc.response.status_code, url, exc.response.text
                )
                # Client or Server-side errors that shouldn't be retried if 4xx (except maybe 429)
                status_code = exc.response.status_code
                if status_code == 429 or status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(self.backoff_factor ** attempt)
                        continue
                raise MoneyPrinterError(
                    f"MoneyPrinterTurbo API error (HTTP {status_code}): {exc.response.text}"
                ) from exc

            except Exception as exc:
                logger.error("Unexpected error calling MoneyPrinterTurbo: %s", exc)
                raise MoneyPrinterError(f"Unexpected error: {exc}") from exc

            # Sleep with exponential backoff
            sleep_time = self.backoff_factor ** attempt
            time.sleep(sleep_time)

    def check_health(self) -> dict:
        """
        Probes the health of the MoneyPrinterTurbo sidecar.
        """
        try:
            # Probe root / to verify server is listening and responding
            # Using "/" is a reliable health check endpoint instead of "/docs"
            _ = self._request_with_retry("GET", "/")
            return {
                "status": "healthy",
                "service": "moneyprinter-sidecar",
                "url": self.api_url,
                "responsive": True,
            }
        except (MoneyPrinterConnectionError, MoneyPrinterTimeoutError) as exc:
            logger.warning("MoneyPrinterTurbo sidecar health probe failed (unreachable/timeout): %s", exc)
            if self.degrade_on_failure:
                return self.degrade(str(exc))
            return {
                "status": "unhealthy",
                "service": "moneyprinter-sidecar",
                "url": self.api_url,
                "responsive": False,
                "error": str(exc),
            }
        except Exception as exc:
            # If the sidecar responds but returns some status code (like 404), the server itself is still responsive
            logger.info("MoneyPrinterTurbo sidecar responded with HTTP status, but it is alive: %s", exc)
            return {
                "status": "healthy",
                "service": "moneyprinter-sidecar",
                "url": self.api_url,
                "responsive": True,
            }

    def get_capabilities(self) -> dict:
        """
        Probes/declares sidecar integration capabilities.
        Only reports actually verified and successful capabilities.
        Since this stage integrates the decoupled Adapter/Contract without full production credentials,
        advanced features are marked as unavailable or unknown.
        """
        try:
            health = self.check_health()
            is_active = health.get("status") == "healthy"
            return {
                "status": "active" if is_active else "degraded",
                "capabilities": {
                    "video_generation": "unknown (adapter integrated, credentials not configured)" if is_active else "unavailable",
                    "subtitles_sync": "unavailable",
                    "tts_voiceover": "unavailable",
                    "supported_aspect_ratios": ["9:16", "16:9", "1:1"] if is_active else [],
                },
                "pinned_upstream": {
                    "version": "v1.2.7",
                    "commit": "b09b0b6bc7fa05e60d3d5f3dfd68377e68e4de80",
                    "license": "MIT"
                }
            }
        except Exception as exc:
            logger.error("Failed to fetch capabilities: %s", exc)
            return self.degrade(str(exc))

    def generate_video(
        self,
        subject: str,
        aspect: str = "9:16",
        voice_name: str = "en-US-JennyNeural",
        video_concat_mode: str = "random",
        video_clip_duration: int = 5,
    ) -> str:
        """
        Submits a video generation task to MoneyPrinterTurbo.
        Returns the task ID.
        """
        payload = {
            "video_subject": subject,
            "video_aspect": aspect,
            "voice_name": voice_name,
            "video_concat_mode": video_concat_mode,
            "video_clip_duration": video_clip_duration,
        }
        try:
            resp = self._request_with_retry("POST", "/api/v1/video/generate", json_data=payload)
            data = resp.json()
            task_id = data.get("task_id") or data.get("taskId")
            if not task_id:
                raise MoneyPrinterError(f"No task_id found in response: {data}")
            return str(task_id)
        except Exception as exc:
            logger.error("Failed to generate video via MoneyPrinterTurbo: %s", exc)
            # Sidecar is unavailable; strictly raise error and do not forge fake success task IDs.
            raise

    def get_task_status(self, task_id: str) -> dict:
        """
        Queries the status of a specific MoneyPrinterTurbo task.
        """
        try:
            resp = self._request_with_retry("GET", f"/api/v1/video/status/{task_id}")
            data = resp.json()
            status = data.get("status")
            if status == "failed":
                raise MoneyPrinterTaskFailedError(
                    f"MoneyPrinterTurbo task {task_id} failed: {data.get('message', 'No details available')}"
                )
            return data
        except Exception as exc:
            logger.error("Failed to get task status for %s: %s", task_id, exc)
            # Sidecar is unavailable; strictly return degraded/failed status, no progress or URL.
            if self.degrade_on_failure:
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "progress": 0,
                    "message": f"Degraded fallback status query due to error: {exc}",
                    "degraded": True
                }
            raise

    def degrade(self, exception_msg: str) -> dict:
        """
        Graceful degradation boundary when the sidecar is unreachable or erroring out.
        """
        logger.warning("Triggering fallback degradation: %s", exception_msg)
        return {
            "status": "degraded",
            "service": "moneyprinter-sidecar",
            "url": self.api_url,
            "responsive": False,
            "fallback_active": True,
            "reason": exception_msg,
            "capabilities": {
                "video_generation": "unavailable",
                "subtitles_sync": "unavailable",
                "tts_voiceover": "unavailable",
                "supported_aspect_ratios": []
            }
        }
