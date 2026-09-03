"""Celery application.

ONE worker process on the VPS, three named queues, embedded beat.

Hard rule: this process runs with ``-B`` (embedded beat) and must never be scaled
past a single replica, or every scheduled task fires twice. If a second worker is ever
needed, split beat into its own unit first. This is repeated in the systemd unit and
in DEPLOYMENT.md because it is the kind of thing that gets forgotten at 2 a.m.

Heavy AI and media work never runs here -- it is a ``jobs`` row leased by the desktop
(ADR-0003).
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from thedrop_config import get_settings

settings = get_settings()

celery_app = Celery(
    "thedrop",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.maintain",
        "app.tasks.ingest",
        "app.tasks.embed",
        "app.tasks.extract",
        "app.tasks.cluster",
        "app.tasks.publish",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Ack after the task finishes, so a worker killed mid-task requeues instead of
    # silently dropping the work.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Prefetch 1: tasks here vary wildly in duration and a greedy prefetch on a
    # 2-concurrency worker leaves one slot idle behind a long task.
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    task_time_limit=600,
    task_soft_time_limit=540,
    broker_connection_retry_on_startup=True,
    task_default_queue="maintain",
    task_routes={
        "app.tasks.ingest.*": {"queue": "ingest"},
        "app.tasks.maintain.*": {"queue": "maintain"},
        # Embedding DISPATCH is a cheap query that inserts job rows; the expensive part
        # happens on the desktop. It belongs with maintenance, and is routed explicitly
        # rather than left to `task_default_queue` -- it already lands there by default,
        # so changing that default would silently strand it on a queue nobody consumes,
        # with beat publishing every 120s and nothing ever running.
        "app.tasks.embed.*": {"queue": "maintain"},
        "app.tasks.extract.*": {"queue": "maintain"},
        "app.tasks.cluster.*": {"queue": "maintain"},
        "app.tasks.publish.*": {"queue": "publish"},
    },
)

celery_app.conf.beat_schedule = {
    # Ingestion's heartbeat. Beat stays a fixed 60s tick and the decision about which
    # providers are due lives in a query, so changing a feed's cadence is a row update
    # rather than a redeploy.
    "dispatch-due-providers": {
        "task": "app.tasks.ingest.dispatch_due_providers",
        "schedule": 60.0,
    },
    # Returns jobs abandoned by an offline desktop to the queue. The single most
    # important periodic task: without it, a power cut on the desktop strands work.
    "reap-expired-job-leases": {
        "task": "app.tasks.maintain.reap_expired_leases",
        "schedule": 60.0,
    },
    "mark-stale-workers-offline": {
        "task": "app.tasks.maintain.mark_stale_workers_offline",
        "schedule": 60.0,
    },
    # Embeddings are the desktop's work (ADR-0005); this only queues it. Slower than
    # ingestion's tick because nothing downstream is more urgent than the next poll,
    # and a shorter interval would just re-scan the same backlog.
    "dispatch-embedding-batches": {
        "task": "app.tasks.embed.dispatch_embedding_batches",
        "schedule": 120.0,
    },
    # Entity extraction feeds the clustering guard. Slower than embedding's tick
    # because nothing downstream is waiting on it yet and it re-scans the same backlog.
    "dispatch-extraction-batches": {
        "task": "app.tasks.extract.dispatch_extraction_batches",
        "schedule": 180.0,
    },
    # Clustering is pure SQL and arithmetic on the VPS (ADR-0015), so it can run more
    # often than the desktop-bound stages. It only acts on articles that are both
    # embedded and extracted, so it naturally waits for them.
    "cluster-ready-articles": {
        "task": "app.tasks.cluster.cluster_ready_articles",
        "schedule": 90.0,
    },
    "reset-provider-quotas": {
        "task": "app.tasks.maintain.reset_provider_quotas",
        "schedule": crontab(hour=0, minute=5),
    },
}
