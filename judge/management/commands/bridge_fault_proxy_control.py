import json
import socket

from django.core.management.base import BaseCommand, CommandError


def parse_address(value):
    host, port = value.rsplit(":", 1)
    return host, int(port)


class Command(BaseCommand):
    help = "Control a running bridge_fault_proxy process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--control",
            default="127.0.0.1:19998",
            help="Control socket address for bridge_fault_proxy.",
        )
        subparsers = parser.add_subparsers(dest="action", required=True)
        subparsers.add_parser("status")
        subparsers.add_parser("restore")
        subparsers.add_parser("close")

        blackhole = subparsers.add_parser("blackhole")
        blackhole.add_argument(
            "direction",
            choices=("upstream", "downstream"),
            nargs="?",
            default="upstream",
        )

        latency = subparsers.add_parser("latency")
        latency.add_argument("latency_ms", type=int)

        cut = subparsers.add_parser("cut-after-bytes")
        cut.add_argument("bytes", type=int)
        cut.add_argument(
            "direction",
            choices=("upstream", "downstream"),
            nargs="?",
            default="upstream",
        )

    def handle(self, *args, **options):
        command = {"action": options["action"]}
        if options["action"] == "blackhole":
            command["direction"] = options["direction"]
        elif options["action"] == "latency":
            command["latency_ms"] = options["latency_ms"]
        elif options["action"] == "cut-after-bytes":
            command["bytes"] = options["bytes"]
            command["direction"] = options["direction"]

        try:
            response = self._send(options["control"], command)
        except OSError as e:
            raise CommandError("fault proxy control unavailable: %s" % e)

        if not response.get("ok"):
            raise CommandError(response.get("error", "unknown fault proxy error"))
        self.stdout.write(json.dumps(response["state"], sort_keys=True))

    def _send(self, address, command):
        sock = socket.create_connection(parse_address(address), timeout=5)
        try:
            sock.sendall(json.dumps(command).encode("utf-8"))
            raw = sock.recv(65536)
            if not raw:
                raise ValueError("empty response")
            return json.loads(raw.decode("utf-8"))
        finally:
            sock.close()
