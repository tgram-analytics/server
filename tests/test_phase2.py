"""Phase 2 tests — project management API and API key security.

Unit tests (no DB) cover the security primitives.
Integration tests use the real app via ``api_client`` and need DATABASE_URL.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# ── Unit: API key generation and hashing ──────────────────────────────────────


def test_api_key_format() -> None:
    """generate_api_key returns a 'proj_' prefixed string of correct length."""
    from app.core.security import generate_api_key

    key = generate_api_key()
    assert key.startswith("proj_")
    # proj_ (5) + 64 hex chars (32 bytes as hex) = 69 total
    assert len(key) == 69
    assert key[5:].isalnum()


def test_api_key_is_random() -> None:
    """Two calls produce different keys."""
    from app.core.security import generate_api_key

    assert generate_api_key() != generate_api_key()


def test_hash_api_key_is_deterministic() -> None:
    """hash_api_key returns the same value for the same input."""
    from app.core.security import hash_api_key

    key = "proj_" + "a" * 64
    assert hash_api_key(key) == hash_api_key(key)


def test_hash_differs_from_plaintext() -> None:
    """The stored hash must not equal the plaintext key."""
    from app.core.security import generate_api_key, hash_api_key

    key = generate_api_key()
    assert hash_api_key(key) != key


def test_hash_is_sha256_length() -> None:
    """SHA-256 hex digest is always 64 characters."""
    from app.core.security import hash_api_key

    assert len(hash_api_key("any_key")) == 64


# ── Integration: validate_api_key service ─────────────────────────────────────


async def test_validate_api_key_returns_correct_project(db_session: AsyncSession) -> None:
    """validate_api_key(plaintext) returns the matching Project."""
    from app.core.security import validate_api_key
    from app.services.projects import create_project

    project, api_key = await create_project(
        db_session, name="validate-test.com", admin_chat_id=999_001
    )

    found = await validate_api_key(api_key, db_session)
    assert found is not None
    assert found.id == project.id


async def test_validate_api_key_wrong_key_returns_none(db_session: AsyncSession) -> None:
    """validate_api_key with an unknown key returns None."""
    from app.core.security import validate_api_key

    result = await validate_api_key("proj_" + "z" * 64, db_session)
    assert result is None


# ── Integration: REST API ──────────────────────────────────────────────────────


async def test_create_project_returns_201_with_api_key(api_client: AsyncClient) -> None:
    """POST /api/v1/internal/projects returns 201 and a proj_-prefixed api_key."""
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": "myapp.com"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "myapp.com"
    assert body["api_key"].startswith("proj_")
    assert len(body["api_key"]) == 69
    assert "api_key_hash" not in body  # hash must never be exposed


async def test_api_key_not_in_list_response(api_client: AsyncClient) -> None:
    """GET /api/v1/internal/projects does NOT include api_key in any item."""
    # Create a project first so the list is non-empty.
    await api_client.post("/api/v1/internal/projects", json={"name": "list-test.com"})

    resp = await api_client.get("/api/v1/internal/projects")
    assert resp.status_code == 200
    for item in resp.json():
        assert "api_key" not in item
        assert "api_key_hash" not in item


async def test_duplicate_project_name_is_allowed(api_client: AsyncClient) -> None:
    """Two projects can have the same name — names are not unique."""
    payload = {"name": "duplicate.com"}
    r1 = await api_client.post("/api/v1/internal/projects", json=payload)
    r2 = await api_client.post("/api/v1/internal/projects", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    # They get different API keys.
    assert r1.json()["api_key"] != r2.json()["api_key"]


async def test_get_project_by_id(api_client: AsyncClient) -> None:
    """GET /api/v1/internal/projects/{id} returns the project."""
    create_resp = await api_client.post("/api/v1/internal/projects", json={"name": "get-by-id.com"})
    project_id = create_resp.json()["id"]

    resp = await api_client.get(f"/api/v1/internal/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == project_id


async def test_get_project_not_found(api_client: AsyncClient) -> None:
    """GET with an unknown UUID returns 404."""
    resp = await api_client.get(f"/api/v1/internal/projects/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_delete_project(api_client: AsyncClient) -> None:
    """DELETE removes the project; subsequent GET returns 404."""
    create_resp = await api_client.post("/api/v1/internal/projects", json={"name": "delete-me.com"})
    project_id = create_resp.json()["id"]

    del_resp = await api_client.delete(f"/api/v1/internal/projects/{project_id}")
    assert del_resp.status_code == 204

    get_resp = await api_client.get(f"/api/v1/internal/projects/{project_id}")
    assert get_resp.status_code == 404


async def test_delete_nonexistent_project_returns_404(api_client: AsyncClient) -> None:
    """DELETE on an unknown UUID returns 404."""
    resp = await api_client.delete(f"/api/v1/internal/projects/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_missing_internal_key_returns_401(api_client: AsyncClient) -> None:
    """Requests without X-Internal-Key are rejected with 401."""
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": "unauth.com"},
        headers={"X-Internal-Key": ""},  # override the fixture default
    )
    assert resp.status_code == 401


async def test_wrong_internal_key_returns_401(api_client: AsyncClient) -> None:
    """Wrong X-Internal-Key is rejected with 401."""
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": "unauth.com"},
        headers={"X-Internal-Key": "wrong-key"},
    )
    assert resp.status_code == 401


async def test_create_project_with_domain_allowlist(api_client: AsyncClient) -> None:
    """domain_allowlist is stored and returned correctly."""
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": "allowlist.com", "domain_allowlist": ["https://myapp.com"]},
    )
    assert resp.status_code == 201
    assert resp.json()["domain_allowlist"] == ["https://myapp.com"]


async def test_list_projects_includes_created(api_client: AsyncClient) -> None:
    """A project created via POST shows up in GET list."""
    unique_name = f"list-check-{uuid.uuid4().hex[:8]}.com"
    await api_client.post("/api/v1/internal/projects", json={"name": unique_name})

    resp = await api_client.get("/api/v1/internal/projects")
    names = [p["name"] for p in resp.json()]
    assert unique_name in names
