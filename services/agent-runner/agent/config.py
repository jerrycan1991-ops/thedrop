"""Runner configuration, read from the environment.

Deliberately plain: no pydantic-settings, no .env discovery. This process runs on the
desktop under a supervisor, and config that silently comes from a file nobody
remembers is exactly how a runner ends up pointed at the wrong environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: The API's heartbeat grace is 90s (`_HEARTBEAT_GRACE_SECONDS` in
#: services/api/app/routers/worker.py). Beat well inside it: at 30s we can miss two in
#: a row and still be considered online, which absorbs a transient network blip without
#: the admin flapping between ONLINE and OFFLINE.
DEFAULT_HEARTBEAT_SECONDS = 30

#: Idle polling interval. ADR-0001 accepts polling latency because the pipeline is
#: measured in minutes; there is no server-side long poll, so this is a plain interval.
DEFAULT_IDLE_POLL_SECONDS = 10

#: How long a lease we ask for. The heartbeat extends leases we hold, so this only has
#: to outlast a single handler run plus one missed heartbeat.
DEFAULT_LEASE_SECONDS = 900


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    api_url: str
    token: str
    worker_name: str
    handlers: tuple[str, ...]
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    idle_poll_seconds: int = DEFAULT_IDLE_POLL_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    max_jobs: int = 1
    capabilities: dict[str, object] = field(default_factory=dict)

    @property
    def agent_version(self) -> str:
        from agent import __version__

        return __version__


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def load_config(handlers: tuple[str, ...]) -> RunnerConfig:
    """Build config from the environment, failing loudly on anything missing.

    `handlers` comes from the registry rather than the environment: advertising a
    handler this process cannot actually dispatch would have the API lease us work we
    would immediately fail.
    """
    api_url = _require("THEDROP_API_URL").rstrip("/")

    # An http:// endpoint would send the bearer token in clear text. The desktop
    # reaches the VPS across the public internet, so this is not a preference.
    is_local = "localhost" in api_url or "127.0.0.1" in api_url
    if not api_url.startswith("https://") and not is_local:
        raise ConfigError(f"THEDROP_API_URL must be https:// outside local testing (got {api_url})")

    return RunnerConfig(
        api_url=api_url,
        token=_require("WORKER_TOKEN"),
        worker_name=os.environ.get("WORKER_NAME", "desktop"),
        handlers=handlers,
        heartbeat_seconds=int(
            os.environ.get("RUNNER_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)
        ),
        idle_poll_seconds=int(
            os.environ.get("RUNNER_IDLE_POLL_SECONDS", DEFAULT_IDLE_POLL_SECONDS)
        ),
        lease_seconds=int(os.environ.get("RUNNER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)),
        max_jobs=int(os.environ.get("RUNNER_MAX_JOBS", 1)),
    )
