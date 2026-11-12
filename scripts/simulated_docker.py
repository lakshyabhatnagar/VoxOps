"""Simulated Docker boundary for disposable demos and tests, never a product mode."""

import asyncio
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from voxops.core.settings import Settings
from voxops.execution.docker_driver import CommandOutcome, DockerDriver


# Replace the Docker command boundary with deterministic in-memory responses for tests.
# Remember created resources, labels, limits, removals, and command arguments.
# The real driver logic can then be tested without building images or claiming that a real engine
# ran.
class SimulatedDocker(DockerDriver):
    """Deterministic Docker boundary for unit tests, never a product execution mode."""

    # Create empty simulated resource and command records.
    # Pass normal settings, ownership callbacks, and cancellation into the real driver
    # constructor.
    # Tests can inspect those records to confirm which operations the driver attempted.
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

    # Interpret the supported Docker command arguments and return a simulated response.
    # Generate resource IDs and inspection metadata from the recorded creates and ownership
    # records.
    # This avoids starting Docker processes while exercising the real driver's validation and
    # cleanup paths.
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


# Extend the simulated Docker boundary with a tiny real local HTTP responder.
# Use it when testing the service's readiness and publication flow without real containers.
# This is only a demo and test tool, not a product execution mode.
class DemoDocker(SimulatedDocker):
    # Initialize the simulated driver and attach the run's real journal and redactor.
    # Remember the cancellation signal supplied by the service.
    # Start without an HTTP server; the runtime step creates one when needed.
    def __init__(self, settings, journal, cancel, session):
        super().__init__(settings, cancel)
        self.journal, self.redact = journal, session.redact
        self.http = None

    # Return a supported Node version for tool-version checks.
    # Delegate other commands to the simulated driver's command implementation.
    # This lets native and static verification tests record consistent tool information.
    async def exec(self, runtime, argv, timeout, **kwargs):
        if argv[-1] == "--version":
            return CommandOutcome(0, "v22.0.0", 0, _stdout="v22.0.0")
        return await super().exec(runtime, argv, timeout, **kwargs)

    # Serve both container and native starts through their common runtime method.
    # Replace the returned handle's host port with a real local HTTP responder's port.
    # The service can then exercise readiness checks without claiming a real app is running.
    async def _runtime(self, *args, **kwargs):
        handle = await super()._runtime(*args, **kwargs)

        # Serve the minimal HTTP response used by the demo runtime.
        # Return no application data or real backend behavior.
        # The handler proves that the readiness request path works, not that a production
        # application is correct.
        class Handler(BaseHTTPRequestHandler):
            # Reply to a GET request with HTTP 200 and an empty body.
            # Send a zero content length so the client can finish immediately.
            # That is enough for the demo's basic server-reachability check.
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            # Suppress the standard HTTP server's request log output.
            # The simulated endpoint does not need console access logs for these tests.
            # This keeps demo output focused on VoxOps results and assertions.
            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(
            target=lambda: self.http.serve_forever(poll_interval=0.05), daemon=True
        )
        self.thread.start()
        return replace(handle, host_port=self.http.server_port)

    # Run the ordinary simulated Docker cleanup first.
    # Always stop and close the local HTTP server afterward, even when cleanup raises.
    # Join its thread and clear the reference so tests do not leave a server running.
    async def cleanup(self, resources=None, **kwargs):
        try:
            await super().cleanup(resources, **kwargs)
        finally:
            if self.http:
                self.http.shutdown()
                self.http.server_close()
                self.thread.join(timeout=1)
                self.http = None
