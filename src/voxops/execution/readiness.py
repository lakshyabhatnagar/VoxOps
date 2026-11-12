"""Bounded observations of an owned process and its loopback HTTP endpoint."""

import asyncio
import time
from dataclasses import dataclass

import httpx

from voxops.core.errors import AppError
from voxops.domain.execution_models import ProbeSpec
from voxops.execution.docker_driver import DockerDriver, RuntimeHandle


# Hold the result of checking an app's local HTTP endpoint.
# Record the response status, elapsed time, and short descriptions of what was observed.
# The runner turns this into saved check evidence rather than saving a response body.
@dataclass(frozen=True)
class HTTPObservation:
    status: int
    duration_seconds: float
    evidence: tuple[str, ...]


# Wait briefly between checks while still responding to cancellation.
# Return normally when the delay expires.
# Raise cancellation immediately if the user stops the run before the delay finishes.
async def _pause(cancel: asyncio.Event, duration: float) -> None:
    if cancel.is_set():
        raise AppError("CANCELLED", "Execution was cancelled.", 409)
    try:
        await asyncio.wait_for(cancel.wait(), max(0, duration))
    except TimeoutError:
        return
    raise AppError("CANCELLED", "Execution was cancelled.", 409)


# Repeatedly ask Docker whether the app process is still running.
# Require it to remain alive for the requested short interval within the startup deadline.
# A process that starts and immediately exits must not count as a successful startup.
async def observe_process(
    driver: DockerDriver,
    runtime: RuntimeHandle,
    *,
    deadline: float,
    stability_seconds: float = 0.5,
) -> dict:
    started = time.monotonic()
    delay = 0.05
    while True:
        if driver.cancel.is_set():
            raise AppError("CANCELLED", "Execution was cancelled.", 409)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("TIMEOUT", "Process observation exceeded the startup deadline.", 409)
        state = await asyncio.wait_for(driver.inspect_runtime(runtime), remaining)
        if not state.get("Running"):
            raise AppError(
                "UNKNOWN_RUNTIME_FAILURE",
                "Owned process exited during observation"
                f" (exit={state.get('ExitCode')}, OOM={bool(state.get('OOMKilled'))}).",
                409,
            )
        elapsed = time.monotonic() - started
        if elapsed >= stability_seconds:
            return state
        await _pause(driver.cancel, min(delay, stability_seconds - elapsed, remaining))
        delay = min(0.2, delay * 1.5)


# Request only the app's allocated local HTTP address until its check passes or time runs out.
# Check that the process survives before and after the response, without following redirects.
# A general HTTP response proves reachability; an explicit health contract requires its configured
# success status.
async def probe_http(
    driver: DockerDriver,
    runtime: RuntimeHandle,
    spec: ProbeSpec,
    *,
    deadline: float,
    request_timeout: float,
    stability_seconds: float = 0.5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HTTPObservation:
    if runtime.host_port is None or not 1 <= runtime.host_port <= 65535:
        raise AppError("INVALID_ARTIFACT", "Owned runtime has no loopback port binding.", 409)
    target = f"http://127.0.0.1:{runtime.host_port}{spec.path}"
    started, delay, last_status = time.monotonic(), 0.05, None
    async with httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                message = "HTTP readiness did not meet its contract before the deadline."
                if last_status is not None:
                    message += f" Last HTTP status was {last_status}."
                raise AppError("UNKNOWN_RUNTIME_FAILURE", message, 409)
            await observe_process(driver, runtime, deadline=deadline, stability_seconds=0)
            status = None
            try:
                # Read headers only. Response bodies cannot consume unbounded time or memory.
                async with asyncio.timeout(min(remaining, request_timeout)):
                    async with client.stream(
                        "GET", target, timeout=min(remaining, request_timeout)
                    ) as response:
                        status = last_status = response.status_code
            except (httpx.HTTPError, TimeoutError):
                pass
            await observe_process(driver, runtime, deadline=deadline, stability_seconds=0)
            success = status is not None and (
                status in spec.success_statuses if spec.health_contract else 200 <= status < 500
            )
            if success:
                await observe_process(
                    driver, runtime, deadline=deadline, stability_seconds=stability_seconds
                )
                label = (
                    "Explicit application health contract met"
                    if spec.health_contract
                    else ("HTTP server reached; application health was not asserted")
                )
                return HTTPObservation(
                    status,
                    time.monotonic() - started,
                    (
                        f"{label} (status={status}, path={spec.path}).",
                        "Owned process remained running for at least "
                        f"{stability_seconds:g}s after response.",
                        "Redirects were not followed. "
                        "Only the owned loopback endpoint was requested.",
                    ),
                )
            await _pause(driver.cancel, min(delay, max(0, deadline - time.monotonic())))
            delay = min(0.5, delay * 1.6)
