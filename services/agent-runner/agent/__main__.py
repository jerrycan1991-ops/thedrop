"""Entry point.

    THEDROP_API_URL=https://thedrop.channel WORKER_TOKEN=... python -m agent

    python -m agent --check      # verify credentials and exit, without claiming anything

`--check` exists because the first thing that goes wrong is always the token or the
URL, and finding that out from a runner that has already claimed a job is worse than
finding it out from a one-shot command. It deliberately does NOT take the single
instance lock, so a running worker can still be diagnosed.

Exit codes: 0 clean stop, 1 unreachable API on `--check`, 2 configuration or a rejected
token, 3 another runner already holds this worker name on this machine.
"""

from __future__ import annotations

import argparse
import logging
import sys

from agent.client import ApiUnavailableError, AuthRejectedError, WorkerClient
from agent.config import ConfigError, load_config
from agent.runner import advertised_handlers, build_runner
from agent.single_instance import AlreadyRunningError, single_instance


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which buries the runner's own output under a
    # poll every ten seconds.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-runner", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the token and connection, print server-side status, then exit.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("agent-runner")

    handlers = advertised_handlers()
    if not handlers:
        log.error("no handlers registered; nothing to advertise")
        return 2

    try:
        config = load_config(handlers)
    except ConfigError as exc:
        log.error("%s", exc)
        log.error("required: THEDROP_API_URL, WORKER_TOKEN")
        return 2

    if args.check:
        with WorkerClient(config.api_url, config.token) as client:
            try:
                status = client.status()
            except AuthRejectedError as exc:
                log.error("%s", exc)
                return 2
            except ApiUnavailableError as exc:
                log.error("cannot reach %s: %s", config.api_url, exc)
                return 1
        log.info("authenticated as worker %r (status %s)", status["name"], status["status"])
        log.info("leased jobs held right now: %s", status["leasedJobs"] or "none")
        log.info("advertising handlers: %s", ",".join(handlers))
        return 0

    # Held for the whole claim loop. A second runner on this machine under the same
    # worker name is not a duplicate of a process -- it is a second claimant on one
    # node's leases, and its startup lease release would pull work out from under this
    # one. See agent/single_instance.py.
    try:
        with single_instance(config.worker_name):
            runner = build_runner(config)
            runner.install_signal_handlers()
            try:
                return runner.run()
            finally:
                runner.client.close()
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        log.error("stop the other runner, or set WORKER_NAME to a different worker.")
        # 3, not 2: a rejected token needs a human with a new credential, while this
        # resolves itself the moment the other process exits. A supervisor should treat
        # them differently.
        return 3


if __name__ == "__main__":
    sys.exit(main())
