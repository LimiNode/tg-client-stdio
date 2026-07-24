from __future__ import annotations

import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, TextIO

from .client import JsonlWorkerClient
from .protocol import DEFAULT_MAX_JSONL_RECORD_BYTES, MIN_MAX_JSONL_RECORD_BYTES


@dataclass
class WorkerProcessConfig:
    args: list[str] = field(default_factory=lambda: ["--mock"])
    executable: str = sys.executable
    command: list[str] | None = None
    max_jsonl_bytes: int = DEFAULT_MAX_JSONL_RECORD_BYTES
    startup_hello: bool = True
    startup_timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 5.0


class WorkerProcess:
    """Owns one worker subprocess and exposes a JsonlWorkerClient."""

    def __init__(self, config: WorkerProcessConfig | None = None) -> None:
        self._config = config or WorkerProcessConfig()
        if self._config.max_jsonl_bytes < MIN_MAX_JSONL_RECORD_BYTES:
            raise ValueError(
                f"max_jsonl_bytes must be at least {MIN_MAX_JSONL_RECORD_BYTES}")
        if self._config.startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if self._config.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._process: subprocess.Popen[bytes] | None = None
        self.client: JsonlWorkerClient | None = None

    def start(self, stderr: TextIO | int | None = None) -> JsonlWorkerClient:
        if self._process is not None:
            raise RuntimeError("worker process already started")

        process = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        try:
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("worker stdin/stdout pipes were not created")

            client = JsonlWorkerClient(
                input_stream=process.stdout,
                output_stream=process.stdin,
                max_jsonl_bytes=self._config.max_jsonl_bytes,
            )
            if self._config.startup_hello:
                _call_with_timeout(
                    lambda: client.hello({"client_name": "tg-client-stdio-process"}),
                    self._config.startup_timeout_seconds,
                    "worker startup hello timed out",
                )
        except BaseException:
            _cleanup_process(process, self._config.shutdown_timeout_seconds)
            raise

        self._process = process
        self.client = client
        return client

    def close(self, timeout_seconds: float | None = None) -> None:
        timeout = (
            self._config.shutdown_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        process = self._process
        client = self.client
        if process is None:
            self.client = None
            return

        try:
            if process.poll() is None and client is not None:
                try:
                    _call_with_timeout(
                        client.shutdown,
                        timeout,
                        "worker shutdown response timed out",
                    )
                except Exception:
                    pass

            _cleanup_process(process, timeout)
        finally:
            self.client = None
            self._process = None

    def __enter__(self) -> JsonlWorkerClient:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _command(self) -> list[str]:
        if self._config.command is not None:
            if not self._config.command:
                raise ValueError("worker command must not be empty")
            return list(self._config.command)
        return [
            self._config.executable,
            "-m",
            "tg_client_stdio_worker",
            *self._config.args,
            "--max-jsonl-bytes",
            str(self._config.max_jsonl_bytes),
        ]


def _call_with_timeout(
        callback: Callable[[], object],
        timeout_seconds: float,
        timeout_message: str) -> object:
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put((True, callback()))
        except BaseException as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(timeout_message)
    ok, value = result_queue.get_nowait()
    if ok:
        return value
    raise value


def _cleanup_process(process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout_seconds)
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
