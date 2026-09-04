"""HTTP client for the worker lease protocol.

Mirrors services/api/app/routers/worker.py exactly. Every method is safe to call when
the VPS is unreachable: transport failures raise `ApiUnavailableError`, which the caller
treats as "try again later" rather than a reason to exit. The desktop is expected to be
offline for hours (ARCHITECTURE.md §3), and so is the network between them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ApiUnavailableError(RuntimeError):
    """The VPS could not be reached, or answered 5xx. Retryable, always."""


class AuthRejectedError(RuntimeError):
    """The token was refused. Retrying will not help; a human must fix it."""


class PayloadRejectedError(RuntimeError):
    """The VPS refused the CONTENT of a request (400). Retrying cannot help.

    Distinct from ApiUnavailableError because the generic `_post` treats every 4xx as
    "try again later". For a rejected embedding batch that would be wrong in the worst
    way: a model or dimension mismatch would be retried forever, looking exactly like a
    network problem while never succeeding and never surfacing the real message.
    """


@dataclass(frozen=True)
class Job:
    id: str
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Job:
        return cls(
            id=data["id"],
            job_type=data["jobType"],
            payload=data.get("payload") or {},
            attempts=data.get("attempts", 0),
            max_attempts=data.get("maxAttempts", 0),
            idempotency_key=data.get("idempotencyKey"),
        )


class WorkerClient:
    def __init__(self, api_url: str, token: str, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=api_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WorkerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            # Includes DNS failure, connection refused, timeouts -- i.e. every way the
            # desktop's own connection drops. Not an error worth exiting over.
            raise ApiUnavailableError(str(exc)) from exc

        if response.status_code == 401:
            raise AuthRejectedError(
                "worker token rejected. Re-provision with "
                "`python -m thedrop_database.provision_worker --name <name> --rotate` "
                "on the VPS."
            )
        if response.status_code >= 500:
            raise ApiUnavailableError(f"{response.status_code} from {path}")
        # 404 and 409 on complete/fail are meaningful to the caller, not failures here.
        if response.status_code >= 400 and response.status_code not in (404, 409):
            raise ApiUnavailableError(f"{response.status_code} from {path}: {response.text[:200]}")

        try:
            return {"_status": response.status_code, **(response.json() or {})}
        except ValueError:
            return {"_status": response.status_code}

    def heartbeat(
        self,
        *,
        current_job_count: int,
        agent_version: str,
        capabilities: dict[str, Any],
        gpu_name: str | None = None,
        gpu_vram_free_mb: int | None = None,
    ) -> dict[str, Any]:
        """Also extends every lease this node holds -- see the API handler."""
        return self._post(
            "/api/v1/worker/heartbeat",
            {
                "status": "online",
                "current_job_count": current_job_count,
                "agent_version": agent_version,
                "capabilities": capabilities,
                "gpu_name": gpu_name,
                "gpu_vram_free_mb": gpu_vram_free_mb,
            },
        )

    def claim(self, handlers: list[str], max_jobs: int, lease_seconds: int) -> list[Job]:
        data = self._post(
            "/api/v1/worker/jobs/claim",
            {"handlers": handlers, "max_jobs": max_jobs, "lease_seconds": lease_seconds},
        )
        return [Job.from_api(j) for j in data.get("jobs", [])]

    def complete(self, job_id: str, result: dict[str, Any]) -> str:
        data = self._post(f"/api/v1/worker/jobs/{job_id}/complete", {"result": result})
        if data["_status"] == 409:
            # The lease expired and the job was reaped and re-leased while we worked.
            # Our result is stale by definition; dropping it is correct.
            return "lost_lease"
        if data["_status"] == 404:
            return "not_found"
        return str(data.get("status", "ok"))

    def fail(self, job_id: str, error: str, retryable: bool = True) -> str:
        data = self._post(
            f"/api/v1/worker/jobs/{job_id}/fail",
            {"error": error[:4000], "retryable": retryable},
        )
        if data["_status"] in (404, 409):
            return "lost_lease"
        return str(data.get("status", "unknown"))

    def store_embeddings(self, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Post vectors, before completing the job that produced them.

        Not part of `complete`: `jobs.result` is kept forever, so vectors sent that way
        would duplicate every embedding into the jobs table permanently.

        Ordering is deliberate. If this succeeds and the process dies before the job is
        completed, the lease expires, the job is requeued and the same vectors are
        written again -- identical values, so a retry costs GPU time and changes
        nothing. The reverse order could complete a job whose vectors were never
        stored, and nothing would ever look at that article again.
        """
        try:
            response = self._client.post(
                "/api/v1/worker/embeddings", json={"model": model, "items": items}
            )
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(str(exc)) from exc

        if response.status_code == 401:
            raise AuthRejectedError("worker token rejected")
        if response.status_code == 400:
            raise PayloadRejectedError(response.text[:400])
        if response.status_code != 200:
            raise ApiUnavailableError(f"{response.status_code} from /embeddings")
        return dict(response.json())

    def store_entities(self, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Post extracted entities, before completing the job that produced them.

        Same contract and same ordering as `store_embeddings`: deliver first, complete
        second, so a crash in between costs a re-extraction rather than an article
        marked processed whose entities were never stored.
        """
        try:
            response = self._client.post(
                "/api/v1/worker/entities", json={"model": model, "items": items}
            )
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(str(exc)) from exc

        if response.status_code == 401:
            raise AuthRejectedError("worker token rejected")
        if response.status_code == 400:
            raise PayloadRejectedError(response.text[:400])
        if response.status_code != 200:
            raise ApiUnavailableError(f"{response.status_code} from /entities")
        return dict(response.json())

    def store_claims(self, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Post extracted claims, before completing the job that produced them.

        Same contract and same ordering as `store_embeddings`/`store_entities`: deliver
        first, complete second, so a crash in between costs a re-extraction rather than
        a story marked processed whose claims were never stored.
        """
        try:
            response = self._client.post(
                "/api/v1/worker/claims", json={"model": model, "items": items}
            )
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(str(exc)) from exc

        if response.status_code == 401:
            raise AuthRejectedError("worker token rejected")
        if response.status_code == 400:
            raise PayloadRejectedError(response.text[:400])
        if response.status_code != 200:
            raise ApiUnavailableError(f"{response.status_code} from /claims")
        return dict(response.json())

    def store_contradictions(self, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Post contradiction-check results, before completing the job that produced
        them. Same contract and ordering as store_claims/store_embeddings/
        store_entities.
        """
        try:
            response = self._client.post(
                "/api/v1/worker/contradictions", json={"model": model, "items": items}
            )
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(str(exc)) from exc

        if response.status_code == 401:
            raise AuthRejectedError("worker token rejected")
        if response.status_code == 400:
            raise PayloadRejectedError(response.text[:400])
        if response.status_code != 200:
            raise ApiUnavailableError(f"{response.status_code} from /contradictions")
        return dict(response.json())

    def status(self) -> dict[str, Any]:
        try:
            response = self._client.get("/api/v1/worker/status")
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(str(exc)) from exc
        if response.status_code == 401:
            raise AuthRejectedError("worker token rejected")
        if response.status_code != 200:
            raise ApiUnavailableError(f"{response.status_code} from /status")
        return dict(response.json())
