"""Simulated Docker boundary for disposable demos and tests, never a product mode."""

import asyncio
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from voxops.core.settings import Settings
from voxops.execution.docker_driver import CommandOutcome, DockerDriver


class SimulatedDocker(DockerDriver):
    """Deterministic Docker boundary for unit tests, never a product execution mode."""

    def __init__(self, settings=None, cancel=None):
        self.events = []
        self.removed = []
        self.created = {}
        self.image_volumes = {}
        self.clock_step = 0
        self.now = 0
        super().__init__(
            settings or Settings(),
            "run",
            "owner",
            lambda item: None,
            cancel or asyncio.Event(),
            lambda s: s,
        )

    async def command(self, args, timeout=30, **kwargs):
        self.events.append((args, timeout))
        self.now += self.clock_step
        if timeout <= 0:
            return CommandOutcome(None, "", 0, timed_out=True)
        output = ""
        if args[1] == "info":
            output = json.dumps({"NCPU": 8, "MemTotal": 8 * 1024**3, "CgroupVersion": "2"})
        elif args[1] == "version":
            output = "29.test"
        elif args[1] == "create":
            name = args[args.index("--name") + 1]
            output = f"{len(self.created) + 1:064x}"
            self.created[name] = (output, args)
        elif "inspect" in args:
            name = args[-1]
            item = next(
                (r for r in self.resources if r.name == name or r.resource_id == name), None
            )
            if item is not None and item.name in self.removed:
                return CommandOutcome(1, "No such resource", 0)
            if args[1] == "image" and item is None:
                data = {"Id": "sha256:stock", "Config": {"Volumes": self.image_volumes}}
            elif item is None or item.name in self.removed:
                return CommandOutcome(1, "No such resource", 0)
            elif item.kind == "container" and item.name not in self.created:
                return CommandOutcome(1, "No such resource", 0)
            elif item.kind == "volume":
                data = {"Labels": self._labels(item.task_id)}
            elif item.kind == "image":
                data = {"Id": "sha256:built", "Config": {"Labels": self._labels(item.task_id)}}
            else:
                resource_id, create = self.created[item.name]
                data = {
                    "Id": resource_id,
                    "Image": "sha256:stock",
                    "Config": {"Labels": self._labels(item.task_id)},
                    "State": {"Running": True, "ExitCode": 0, "OOMKilled": False},
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": arg.split("src=")[1].split(",")[0],
                            "Destination": arg.split("dst=")[1].split(",")[0],
                        }
                        for arg in create
                        if arg.startswith("type=volume,")
                    ],
                    "NetworkSettings": {
                        "Ports": {
                            f"{create[create.index('-p') + 1].rsplit(':', 1)[1]}/tcp": [
                                {"HostIp": "127.0.0.1", "HostPort": "12345"}
                            ]
                        }
                    }
                    if "-p" in create
                    else {"Ports": {}},
                    "HostConfig": {
                        "Privileged": False,
                        "Memory": int(next(a[9:-1] for a in create if a.startswith("--memory=")))
                        * 1024**2,
                        "NanoCpus": int(
                            float(next(a[7:] for a in create if a.startswith("--cpus="))) * 1e9
                        ),
                        "PidsLimit": int(
                            next(a[13:] for a in create if a.startswith("--pids-limit="))
                        ),
                    },
                }
            output = json.dumps(data)
        elif args[1] == "rm" or args[1:3] in (["volume", "rm"], ["image", "rm"]):
            self.removed.extend(
                r.name
                for r in self.resources
                if (
                    args[-1] in {r.name, r.resource_id}
                    or (r.name in self.created and self.created[r.name][0] == args[-1])
                    or (r.kind == "image" and args[-1] == "sha256:built")
                )
            )
        elif args[1] == "exec" and args[-1] == "--version":
            output = "buildctl test"
        return CommandOutcome(0, output, 0, _stdout=output)


class DemoDocker(SimulatedDocker):
    def __init__(self, settings, journal, cancel, session):
        super().__init__(settings, cancel)
        self.journal, self.redact = journal, session.redact
        self.http = None

    async def exec(self, runtime, argv, timeout, **kwargs):
        if argv[-1] == "--version":
            return CommandOutcome(0, "v22.0.0", 0, _stdout="v22.0.0")
        return await super().exec(runtime, argv, timeout, **kwargs)

    async def runtime(self, *args, **kwargs):
        handle = await super().runtime(*args, **kwargs)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(
            target=lambda: self.http.serve_forever(poll_interval=0.05), daemon=True
        )
        self.thread.start()
        return replace(handle, host_port=self.http.server_port)

    async def cleanup(self, resources=None, **kwargs):
        try:
            await super().cleanup(resources, **kwargs)
        finally:
            if self.http:
                self.http.shutdown()
                self.http.server_close()
                self.thread.join(timeout=1)
                self.http = None
