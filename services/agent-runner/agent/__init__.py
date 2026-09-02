"""THE DROP desktop agent-runner.

Claims leased jobs from the VPS over authenticated HTTPS and executes them locally on
the RTX 4070 SUPER. Outbound only: the VPS never dials the desktop (ADR-0001).

The package is `agent`, not `app`, unlike services/api and services/worker. Those two
both ship a top-level `app` and get away with it only because they run in separate
processes; a test importing both sees whichever landed on sys.path first. This one is
imported alongside them in the suite, so it takes a name of its own.
"""

__version__ = "0.1.0"
