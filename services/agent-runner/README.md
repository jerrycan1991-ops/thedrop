# agent-runner — the desktop side

Runs on the RTX 4070 SUPER desktop, not the VPS. Claims leased jobs over authenticated
HTTPS, executes them locally, posts results back.

**Outbound only.** The VPS never dials the desktop (ADR-0001), so there is nothing to
port-forward, no dynamic DNS, and no tunnel to babysit. If the desktop is off, jobs stay
`QUEUED`, leases expire, the VPS reaper returns them, and the public site is unaffected.

This package deliberately does **not** depend on `thedrop-database`. The desktop is
classified semi-trusted in SECURITY.md because it executes model output; giving it
database credentials would put the most valuable secret on the least trusted machine.

## Provision a token — on the VPS

The token is generated server-side and shown once. Only its SHA-256 digest is stored.

```bash
cd ~/thedrop && set -a; . ~/.config/thedrop/thedrop.env; set +a; ~/thedrop/.venv/bin/python -m thedrop_database.provision_worker --name desktop-4070
```

To rotate later — the old token keeps working for 24 h, so this is not an outage:

```bash
python -m thedrop_database.provision_worker --name desktop-4070 --rotate
```

**If the terminal is shared or recorded**, write the token to a file instead of printing
it, then move it machine-to-machine without it ever appearing on a screen:

```bash
python -m thedrop_database.provision_worker --name desktop-4070 --rotate --write-to ~/.config/thedrop/worker-token
```

```bash
$env:WORKER_TOKEN = (ssh user@vps "cat ~/.config/thedrop/worker-token").Trim()
```

Delete the file once the runner has it. Retyping a 43-character token by hand across two
machines does not survive contact with reality.

## Run it — on the desktop

```bash
export THEDROP_API_URL=https://thedrop.channel
export WORKER_TOKEN=<the token printed above>
uv run --group desktop python -m agent --check
```

`--check` authenticates, prints what the server thinks this worker is, and exits. Do
this first: the two things that go wrong are the URL and the token, and finding out from
a one-shot command beats finding out from a runner that has already claimed work.

Then:

```bash
uv run --group desktop python -m agent
```

`/admin` shows `AI DESKTOP: ONLINE` within one heartbeat.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `THEDROP_API_URL` | *required* | Must be `https://` unless it is loopback — the bearer token crosses the public internet |
| `WORKER_TOKEN` | *required* | From `provision_worker` |
| `WORKER_NAME` | `desktop` | Cosmetic; the token identifies the node |
| `RUNNER_HEARTBEAT_SECONDS` | `30` | Must stay well under the API's 90 s grace |
| `RUNNER_IDLE_POLL_SECONDS` | `10` | There is no server-side long poll |
| `RUNNER_LEASE_SECONDS` | `900` | Heartbeats extend held leases, so this only needs to outlast one handler run |
| `RUNNER_MAX_JOBS` | `1` | Jobs claimed per poll |

## Proving the round trip

With the runner running, queue a no-op from the VPS and watch it flow:

```bash
python -m thedrop_database.enqueue_job --type noop --payload '{"sleep_seconds": 3}'
```

The runner logs `claimed job` → `noop handler ran` → `completed job` within one poll
interval. That exercises claim, dispatch, lease extension and completion without a
provider, a model or the GPU being involved.

## Running it as a service (Windows)

`python -m agent` in a terminal dies with the terminal. For anything beyond a test, run
it under Task Scheduler:

```bash
powershell -ExecutionPolicy Bypass -File infrastructure\desktop\install-task.ps1
```

It prompts for the token (hidden), stores config in the **user** environment, registers
a task that starts at logon, and starts it. Logs go to
`%LOCALAPPDATA%\thedrop\logs\agent-runner.log`.

Task Scheduler rather than a Windows Service or PM2: it is native, it is where a Windows
operator already looks, a real service would need a wrapper because Python is not a
service host, and PM2's Windows boot persistence needs a third-party helper and is the
flakiest part of PM2 on that platform.

Three settings that matter:

| Setting | Why |
|---|---|
| Runs as your user, not SYSTEM | Needs your `uv` and nvm PATH, and holds a credential that should not be available to every process |
| `ExecutionTimeLimit` = 0 | The default three-day limit would silently kill a runner meant to run forever |
| `MultipleInstances IgnoreNew` | Stops the *task* starting twice. It cannot stop a runner started any other way, so it is a convenience, not the guard — the runner's own lock is (ADR-0014) |

Restart is capped at 3 attempts, 5 minutes apart. The runner already survives an
unreachable VPS on its own, backing off to 120 s — if it has *exited* three times in
fifteen minutes, retrying is not the answer.

```bash
Get-ScheduledTask -TaskName "TheDrop Agent Runner" | Get-ScheduledTaskInfo
```

```bash
powershell -File infrastructure\desktop\install-task.ps1 -Uninstall
```

### Stopping it

**`Stop-ScheduledTask` does not stop the runner.** It kills the PowerShell wrapper; the
`uv run python -m agent` grandchild survives and keeps claiming. Observed directly: the
task reported `Ready` while four runner processes kept polling. That is how orphans
accumulate. Use:

```bash
. .\infrastructure\desktop\runner-control.ps1 ; Stop-Runner -Name desktop-4070
```

`Stop-Runner` goes by the single-instance lock, so it stops the process that actually
owns that worker name and nothing else — a second runner serving a *different*
`WORKER_NAME` is legitimate and is left alone. `Get-RunnerLock -Name <name>` reports the
holder without stopping it. `install-task.ps1` calls `Stop-Runner` itself, both before
re-registering and on `-Uninstall`.

## Embeddings (Phase 3, ADR-0005)

The VPS computes no embeddings. It queues `embed_articles` jobs whose payload carries
the article text; this runner encodes them on the GPU with `bge-small-en-v1.5` (384
dimensions, normalized) and posts the vectors to `POST /api/v1/worker/embeddings`.

Install the model stack — several GB, and separate from `desktop` so a runner that only
claims `noop` never downloads it:

```bash
uv sync --group desktop-ml
```

For CUDA wheels rather than the CPU default, install torch first:

```bash
uv pip install torch --index-url https://download.pytorch.org/whl/cu124
```

Three things worth knowing:

- **Without the stack, `embed_articles` is not advertised.** The handler unregisters
  itself, so the API never leases embedding work to a desktop that cannot do it and the
  batches simply wait. A warning is logged, because a broken install would otherwise
  look identical to an idle queue.
- **Vectors do not travel through `complete`.** `jobs.result` is kept forever, so they
  would be a second permanent copy of every embedding. The runner posts them, strips
  them, and completes with a summary.
- **Deliver, then complete.** If the process dies in between, the lease expires and the
  batch is re-embedded to identical values. The reverse order could mark a job done
  whose vectors were never stored, and nothing would revisit those articles.

A batch the server refuses — wrong model, wrong dimensions, a vector off the unit
sphere — fails permanently rather than retrying. Recomputing produces the same rejected
vectors, and ADR-0005's single vector space is exactly the invariant that must not
degrade quietly.

## Adding a handler

The registry is the single source of truth: whatever is registered is what gets
advertised when claiming, so the API can never lease work this build cannot dispatch.

```python
from agent.handlers import register


@register("embed")
def embed(payload: dict) -> dict:
    ...
    return {"vectors": n}
```

Raise `NonRetryableError` for anything retrying cannot fix — a malformed payload, a
missing field. Every other exception is treated as transient and the job is requeued
with backoff, because the common failures here are a model server not up yet or a
provider rate limit.

Re-provision the worker afterwards so its stored `capabilities` list matches.

## Behaviour worth knowing

- **The heartbeat runs on its own thread**, so it continues during a long job. Each
  heartbeat extends every lease this node holds. If the process dies, heartbeats stop,
  the lease expires, and the VPS requeues the job — that is the entire crash-safety
  story, and it needs no cleanup path here.
- **An unreachable VPS is not an error.** The runner backs off from 5 s to 120 s and
  keeps trying. Only a rejected token is fatal (exit `2`), because retrying that forever
  looks identical to being offline while never recovering.
- **A result that cannot be reported is dropped, not retried.** The lease expires, the
  reaper requeues, and `idempotencyKey` is what makes the second run safe.
- **SIGTERM finishes the current batch** before exiting. Those leases are already ours;
  abandoning them would mean waiting out the lease before anyone else could pick them up.
- **Only one runner per worker name, per machine.** A second one exits `3` immediately
  and names the pid holding the lock. This is not tidiness: worker identity is the
  *token*, so two runners sharing one are a single node, and the startup lease release
  would fail jobs the other is running — discarding finished work and spending
  `attempts` on a job that never failed. The guard is an OS lock, so it cannot go stale.
  It does **not** cover two machines sharing a token; see ADR-0014.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean stop (SIGTERM/SIGINT after finishing the batch) |
| `1` | `--check` could not reach the API |
| `2` | Configuration missing, or the token was rejected — needs a human |
| `3` | Another runner already holds this worker name on this machine |

`2` and `3` differ on purpose: `2` will never succeed on retry, `3` resolves the moment
the other process exits.

## Tests

```bash
uv run pytest tests/test_agent_runner.py tests/test_runner_single_instance.py
```

Runs against an in-process mock of the real lease protocol — no services, no network.
