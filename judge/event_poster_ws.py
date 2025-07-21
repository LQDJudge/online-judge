import socket
import threading
import socketio

from django.conf import settings

__all__ = ["EventPostingError", "EventPoster", "post", "last"]
_local = threading.local()


class EventPostingError(RuntimeError):
    pass


class EventPoster(object):
    def __init__(self):
        self._connect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _connect(self):
        # Create Socket.IO client
        self._conn = socketio.Client(reconnection=False)

        # Define event handlers
        @self._conn.event
        def connect():
            pass

        @self._conn.event
        def connect_error(data):
            raise EventPostingError(f"Connection error: {data}")

        @self._conn.event
        def disconnect():
            pass

        # Connect to the Socket.IO server with authentication token
        try:
            self._conn.connect(
                settings.EVENT_DAEMON_URL,
                auth={"role": "sender", "token": settings.EVENT_DAEMON_KEY},
                namespaces=["/"],
                wait_timeout=5,
            )
        except Exception as e:
            raise EventPostingError(f"Failed to connect to WebSocket server: {e}")

    def _emit_with_callback(self, event, data=None, timeout=5.0):
        """Generic method to emit events with callback pattern"""
        response = None
        callback_called = threading.Event()

        def callback(data):
            nonlocal response
            response = data
            callback_called.set()

        # Emit with callback
        if data is not None:
            self._conn.emit(event, data, callback=callback)
        else:
            self._conn.emit(event, callback=callback)

        # Wait for callback to be called
        if not callback_called.wait(timeout=timeout):
            raise EventPostingError("Timeout waiting for server response")

        # Check response
        if not response:
            raise EventPostingError("No response received")

        if response.get("status") == "error":
            raise EventPostingError(response.get("code", "Unknown error"))
        else:
            return response.get("id", 0)

    def post(self, channel, message, tries=0):
        try:
            return self._emit_with_callback(
                "post", {"channel": channel, "message": message}
            )

        except (socketio.exceptions.ConnectionError, socket.error) as e:
            if tries > 10:
                raise EventPostingError(
                    f"Failed to post message after {tries} retries: {e}"
                )

            # Try to reconnect and retry
            self._reconnect()
            return self.post(channel, message, tries + 1)

    def last(self, tries=0):
        try:
            return self._emit_with_callback("last-msg")

        except (socketio.exceptions.ConnectionError, socket.error) as e:
            if tries > 10:
                raise EventPostingError(
                    f"Failed to get last message after {tries} retries: {e}"
                )

            # Try to reconnect and retry
            self._reconnect()
            return self.last(tries + 1)

    def _reconnect(self):
        """Helper method to handle reconnection logic"""
        try:
            self._conn.disconnect()
        except Exception:
            # Ignore disconnect errors as connection might already be closed
            pass

        self._connect()

    def close(self):
        """Properly close the connection"""
        try:
            self._conn.disconnect()
        except Exception:
            # Ignore disconnect errors as connection might already be closed
            pass


def _get_poster():
    if "poster" not in _local.__dict__:
        _local.poster = EventPoster()
    return _local.poster


def post(channel, message):
    try:
        return _get_poster().post(channel, message)
    except Exception:
        # Clean up connection on error
        _cleanup_poster()
    return 0


def last():
    try:
        return _get_poster().last()
    except Exception:
        # Clean up connection on error
        _cleanup_poster()
    return 0


def _cleanup_poster():
    """Helper function to clean up poster connection"""
    try:
        if hasattr(_local, "poster"):
            _local.poster.close()
            del _local.poster
    except AttributeError:
        # Poster was never created or already cleaned up
        pass
