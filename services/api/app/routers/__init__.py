"""API routers.

Three surfaces, one process, three different auth models:

  * ``public``  - unauthenticated, read-only, cacheable
  * ``admin``   - session cookie + RBAC
  * ``worker``  - bearer token, the desktop's only interface
"""

from app.routers import admin, health, public, worker

__all__ = ["admin", "health", "public", "worker"]
