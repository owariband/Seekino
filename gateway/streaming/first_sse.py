"""Bound startup of an owned HTTP/1 streaming request, without cancelling its task."""
from __future__ import annotations

import socket
from threading import Lock, Timer
from time import monotonic
from typing import Any

import httpx

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.model_diagnostics import record_model_event

FIRST_SSE_TIMEOUT_SECONDS = 100.0


class FirstSSEDeadline:
    """One request, one dedicated HTTP/1 client; never closes a shared HTTP/2 socket."""

    def __init__(self, request_timeout_seconds: float) -> None:
        self.limit = min(FIRST_SSE_TIMEOUT_SECONDS, request_timeout_seconds)
        self._lock = Lock()
        self._timer: Timer | None = None
        self._network: Any = None
        self._received = False
        self._expired = False
        self._started = 0.0
        self._original_timeouts: dict[str, Any] = {}
        self._has_headers = False

    def start(self) -> None:
        self._started = monotonic()
        self._timer = Timer(self.limit, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def on_request(self, request: httpx.Request) -> None:
        # Public HTTPX/httpcore trace extension exposes the connection before
        # response headers. Closing only the eventual response is too late.
        previous = request.extensions.get("trace")

        def trace(name: str, info: dict[str, Any]) -> None:
            if previous is not None:
                previous(name, info)
            if name.endswith(("connect_tcp.complete", "start_tls.complete")):
                network = info["return_value"]
                with self._lock:
                    self._network = network
                    expired = self._expired
                if expired:
                    self._shutdown(network)
                    self.raise_if_expired()

        request.extensions["trace"] = trace
        self._original_timeouts = dict(request.extensions.get("timeout", {}))
        remaining = max(0.001, self.limit - (monotonic() - self._started))
        request.extensions["timeout"] = {
            key: min(value, remaining) if value is not None else remaining
            for key, value in self._original_timeouts.items()
        }

    def attach_response(self, response: httpx.Response) -> None:
        self._has_headers = True
        # httpcore reads the body timeout when body iteration begins. Restore
        # it now; the absolute startup timer still guards the first SSE.
        response.request.extensions["timeout"] = self._original_timeouts
        network = response.extensions.get("network_stream")
        if network is not None:
            with self._lock:
                self._network = network
        self.raise_if_expired()

    def received(self) -> None:
        self.raise_if_expired()
        with self._lock:
            if not self._expired:
                self._received = True
        self.close()
        self.raise_if_expired()

    def raise_if_expired(self) -> None:
        with self._lock:
            self._expired = self._expired or (
                not self._received and monotonic() - self._started >= self.limit
            )
            expired = self._expired
        if expired:
            raise ModelFailureError(
                f"No first SSE event within {self.limit:g} seconds",
                reason=ModelFailureReason.MODEL_TIMEOUT,
            )

    def _expire(self) -> None:
        with self._lock:
            if self._received:
                return
            self._expired = True
            network = self._network
        self._shutdown(network)

    def interrupt(self) -> None:
        with self._lock:
            network = self._network
        self._shutdown(network)

    @staticmethod
    def _shutdown(network: Any) -> None:
        if network is None:
            return
        try:
            sock = network.get_extra_info("socket")
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass  # Already closed, or a mock transport without sockets.

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer.join()
            self._timer = None
            if self._expired:
                record_model_event("first_sse_timeout", {
                    "timeout_seconds": self.limit,
                    "elapsed_seconds": monotonic() - self._started,
                    "phase": "waiting_for_sse" if self._has_headers else "waiting_for_headers",
                })
