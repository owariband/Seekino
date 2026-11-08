"""Shared model transports; adapters pass the stream setting resolved at creation."""
import socket
from typing import Any


def cancel_http_stream(stream: Any) -> None:
    """Wake a blocked HTTP/1 read before closing the response and its client.

    Closing a socket from another thread alone may leave recv blocked. HTTP/2
    connections can be multiplexed, so never shut down their shared socket.
    """
    try:
        response = getattr(stream, "response", None)
        extensions = getattr(response, "extensions", {})
        if (not getattr(response, "is_closed", False)
                and extensions.get("http_version") in {b"HTTP/1.0", b"HTTP/1.1"}):
            network = extensions.get("network_stream")
            sock = network.get_extra_info("socket") if network is not None else None
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        # SDK/test transports may not expose a socket, or it may already be closed.
        pass
    finally:
        stream.close()
