import json
import socket
import threading
import time

from django.core.management.base import BaseCommand

BUFFER_SIZE = 65536


def parse_address(value):
    host, port = value.rsplit(":", 1)
    return host, int(port)


class FaultState:
    def __init__(self):
        self.lock = threading.RLock()
        self.connections = set()
        self.mode = "pass"
        self.direction = "upstream"
        self.cut_after_bytes = None
        self.latency_ms = 0

    def snapshot(self):
        with self.lock:
            return {
                "mode": self.mode,
                "direction": self.direction,
                "cut_after_bytes": self.cut_after_bytes,
                "latency_ms": self.latency_ms,
                "connections": len(self.connections),
            }

    def register(self, connection):
        with self.lock:
            self.connections.add(connection)

    def unregister(self, connection):
        with self.lock:
            self.connections.discard(connection)

    def close_all(self):
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            connection.close()

    def configure(self, command):
        action = command.get("action")
        with self.lock:
            if action == "restore":
                self.mode = "pass"
                self.direction = "upstream"
                self.cut_after_bytes = None
                self.latency_ms = 0
            elif action == "blackhole":
                self.mode = "blackhole"
                self.direction = command.get("direction", "upstream")
            elif action == "latency":
                self.mode = "latency"
                self.latency_ms = int(command.get("latency_ms", 0))
            elif action == "cut-after-bytes":
                self.mode = "cut-after-bytes"
                self.direction = command.get("direction", "upstream")
                self.cut_after_bytes = int(command["bytes"])
            elif action == "close":
                pass
            elif action == "status":
                pass
            else:
                raise ValueError("unknown action: %s" % action)
        if action == "close":
            self.close_all()
        return self.snapshot()


class ProxyConnection:
    def __init__(self, client, upstream_address, state, output):
        self.client = client
        self.upstream_address = upstream_address
        self.state = state
        self.output = output
        self.closed = threading.Event()
        self.forwarded = {"upstream": 0, "downstream": 0}
        self.upstream = socket.create_connection(upstream_address)

    def start(self):
        self.state.register(self)
        threading.Thread(
            target=self.pipe,
            args=(self.client, self.upstream, "upstream"),
            daemon=True,
        ).start()
        threading.Thread(
            target=self.pipe,
            args=(self.upstream, self.client, "downstream"),
            daemon=True,
        ).start()

    def pipe(self, source, target, direction):
        try:
            while not self.closed.is_set():
                data = source.recv(BUFFER_SIZE)
                if not data:
                    break
                snapshot = self.state.snapshot()
                mode = snapshot["mode"]
                fault_direction = snapshot["direction"]

                if mode == "blackhole" and fault_direction == direction:
                    continue

                if mode == "latency" and snapshot["latency_ms"] > 0:
                    time.sleep(snapshot["latency_ms"] / 1000)

                if mode == "cut-after-bytes" and fault_direction == direction:
                    remaining = snapshot["cut_after_bytes"] - self.forwarded[direction]
                    if remaining <= 0:
                        break
                    if len(data) >= remaining:
                        target.sendall(data[:remaining])
                        self.forwarded[direction] += remaining
                        break

                target.sendall(data)
                self.forwarded[direction] += len(data)
        except OSError:
            pass
        finally:
            self.close()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        for sock in (self.client, self.upstream):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.state.unregister(self)


class Command(BaseCommand):
    help = "Run a dev-only TCP fault proxy between judge-server and bridge."

    def add_arguments(self, parser):
        parser.add_argument("--listen", default="127.0.0.1:19999")
        parser.add_argument("--upstream", default="127.0.0.1:9999")
        parser.add_argument("--control", default="127.0.0.1:19998")

    def handle(self, *args, **options):
        listen_address = parse_address(options["listen"])
        upstream_address = parse_address(options["upstream"])
        control_address = parse_address(options["control"])
        state = FaultState()

        threading.Thread(
            target=self.control_server,
            args=(control_address, state),
            daemon=True,
        ).start()

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(listen_address)
        server.listen()

        self.stdout.write(
            "fault proxy listening on %s:%d -> %s:%d; control on %s:%d"
            % (*listen_address, *upstream_address, *control_address)
        )
        try:
            while True:
                client, address = server.accept()
                self.stdout.write("accepted judge connection from %s:%s" % address)
                try:
                    ProxyConnection(
                        client, upstream_address, state, self.stdout
                    ).start()
                except OSError as e:
                    self.stderr.write("failed to connect upstream: %s" % e)
                    client.close()
        finally:
            server.close()

    def control_server(self, control_address, state):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(control_address)
        server.listen()
        try:
            while True:
                client, _ = server.accept()
                with client:
                    raw = client.recv(BUFFER_SIZE)
                    try:
                        command = json.loads(raw.decode("utf-8"))
                        response = {"ok": True, "state": state.configure(command)}
                    except Exception as e:
                        response = {"ok": False, "error": str(e)}
                    client.sendall(json.dumps(response).encode("utf-8"))
        finally:
            server.close()
