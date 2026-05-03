"""Tests for the ``users`` table and project ownership backfill.

Covers migration 0004: the new ``users`` table, the nullable
``projects.owner_user_id`` FK, and the backfill that maps each distinct
``admin_chat_id`` to a synthesised ``User`` row.

These tests require a live PostgreSQL DB (same convention as test_phase1).
"""

import os
import random
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

SERVER_ROOT = Path(__file__).parent.parent

_CONN_ERROR_KEYWORDS = (
    "password authentication",
    "connection refused",
    "could not connect",
    "no such host",
    "name or service not known",
)


def _run_alembic(cmd: list[str], db_url: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(cmd, cwd=SERVER_ROOT, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).lower()
        if any(kw in combined for kw in _CONN_ERROR_KEYWORDS):
            pytest.skip(f"DB not reachable — {result.stderr[:300]}")
        raise AssertionError(
            f"Command {cmd} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


# ── User model ────────────────────────────────────────────────────────────────


async def test_user_can_be_created(db_session: AsyncSession) -> None:
    """A User row can be inserted with only telegram_user_id."""
    from app.models.user import User

    user = User(telegram_user_id=42)
    db_session.add(user)
    await db_session.flush()

    assert user.id is not None
    assert isinstance(user.id, uuid.UUID)
    assert user.created_at is not None


async def test_user_telegram_user_id_unique(db_session: AsyncSession) -> None:
    """telegram_user_id enforces a UNIQUE constraint."""
    from app.models.user import User

    db_session.add(User(telegram_user_id=7777))
    await db_session.flush()

    db_session.add(User(telegram_user_id=7777))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ── Project ↔ User link ───────────────────────────────────────────────────────


async def test_project_owner_user_id_links_to_user(db_session: AsyncSession) -> None:
    """Project can be created with owner_user_id pointing at a User."""
    from app.models.project import Project
    from app.models.user import User

    user = User(telegram_user_id=111222)
    db_session.add(user)
    await db_session.flush()

    project = Project(
        name="linked.com",
        api_key_hash="link_hash_" + "a" * 54,
        admin_chat_id=111222,
        owner_user_id=user.id,
    )
    db_session.add(project)
    await db_session.flush()
    await db_session.refresh(project)

    assert project.owner_user_id == user.id


async def test_project_owner_user_id_is_not_null_post_0005(db_session: AsyncSession) -> None:
    """After migration 0005, owner_user_id is NOT NULL.

    Inserting a project without an owner must raise. Pre-0005 this test
    asserted the column accepted NULLs (rollout window). 0005 closes that
    window; the column is now mandatory.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.project import Project

    project = Project(
        name="nullowner.com",
        api_key_hash="null_hash_" + "d" * 54,
        admin_chat_id=333,
    )
    db_session.add(project)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_project_owner_cascade_on_user_delete(db_session: AsyncSession) -> None:
    """Deleting a User cascades to its projects (ondelete=CASCADE)."""
    from app.models.project import Project
    from app.models.user import User

    user = User(telegram_user_id=444555)
    db_session.add(user)
    await db_session.flush()

    project = Project(
        name="cascade.com",
        api_key_hash="casc_hash_" + "e" * 54,
        admin_chat_id=444555,
        owner_user_id=user.id,
    )
    db_session.add(project)
    await db_session.flush()

    project_id = project.id
    await db_session.delete(user)
    await db_session.flush()

    remaining = await db_session.execute(
        text("SELECT id FROM projects WHERE id = :pid"),
        {"pid": project_id},
    )
    assert remaining.first() is None


# ── Migration backfill ────────────────────────────────────────────────────────


def test_migration_backfills_owner_user_id(db_url: str) -> None:
    """End-to-end migration safety check.

    Procedure:
      1. downgrade to 0003 (users table + owner_user_id gone)
      2. seed three projects — 2 distinct admin_chat_ids, one shared
      3. upgrade to 0004 → backfill creates users + populates FK
      4. downgrade to 0003 → users table + owner_user_id cleanly removed,
         seeded projects survive (they pre-dated 0004)
      5. upgrade to 0004 → re-run succeeds idempotently

    Uses high-entropy ids and name-scoped cleanup so the test does not
    collide with other phases' data or prior runs of itself.
    """
    psycopg = pytest.importorskip("psycopg")

    rnd = random.Random()
    chat_a = rnd.randint(10_000_000, 99_999_999)
    chat_b = rnd.randint(10_000_000, 99_999_999)
    while chat_b == chat_a:
        chat_b = rnd.randint(10_000_000, 99_999_999)
    name_prefix = f"p7bf-{rnd.randint(10_000, 99_999)}-"
    names = [name_prefix + s for s in ("alpha", "beta", "gamma")]

    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    def _scoped_cleanup() -> None:
        """Idempotently remove just the rows this test created."""
        with psycopg.connect(sync_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM projects WHERE name = ANY(%s)",
                    (names,),
                )
                # users table only exists at/above 0004.
                cur.execute("SELECT to_regclass('public.users')")
                (exists,) = cur.fetchone()
                if exists is not None:
                    cur.execute(
                        "DELETE FROM users WHERE telegram_user_id = ANY(%s)",
                        ([chat_a, chat_b],),
                    )

    try:
        # ── 1. downgrade to pre-users state ──────────────────────────────
        _run_alembic(["alembic", "downgrade", "0003"], db_url)
        _scoped_cleanup()

        # ── 2. seed projects (scoped; no wholesale deletes) ──────────────
        with psycopg.connect(sync_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO projects (id, name, api_key_hash, admin_chat_id)
                    VALUES
                      (gen_random_uuid(), %s, %s, %s),
                      (gen_random_uuid(), %s, %s, %s),
                      (gen_random_uuid(), %s, %s, %s)
                    """,
                    (
                        names[0],
                        f"hash-{names[0]}",
                        chat_a,
                        names[1],
                        f"hash-{names[1]}",
                        chat_b,
                        names[2],
                        f"hash-{names[2]}",
                        chat_a,
                    ),
                )

        # ── 3. upgrade to 0004 (runs backfill) ───────────────────────────
        _run_alembic(["alembic", "upgrade", "0004"], db_url)

        with psycopg.connect(sync_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT telegram_user_id FROM users WHERE telegram_user_id = ANY(%s) ORDER BY telegram_user_id",
                ([chat_a, chat_b],),
            )
            rows = cur.fetchall()
            assert sorted(r[0] for r in rows) == sorted([chat_a, chat_b])

            cur.execute(
                """
                SELECT p.name, u.telegram_user_id
                FROM projects p
                JOIN users u ON u.id = p.owner_user_id
                WHERE p.name = ANY(%s)
                ORDER BY p.name
                """,
                (names,),
            )
            linked = cur.fetchall()
            assert linked == [
                (names[0], chat_a),
                (names[1], chat_b),
                (names[2], chat_a),
            ]

            cur.execute(
                "SELECT COUNT(*) FROM projects WHERE name = ANY(%s) AND owner_user_id IS NULL",
                (names,),
            )
            (unlinked,) = cur.fetchone()
            assert unlinked == 0

        # ── 4. downgrade-after-upgrade symmetry ──────────────────────────
        _run_alembic(["alembic", "downgrade", "0003"], db_url)

        with psycopg.connect(sync_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            (users_rel,) = cur.fetchone()
            assert users_rel is None, "users table should be dropped on downgrade"

            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'projects' AND column_name = 'owner_user_id'
                """
            )
            assert cur.fetchone() is None, "owner_user_id column should be dropped on downgrade"

            cur.execute(
                "SELECT COUNT(*) FROM projects WHERE name = ANY(%s)",
                (names,),
            )
            (surviving,) = cur.fetchone()
            assert surviving == 3, "pre-0004 projects must survive a downgrade"

        # ── 5. upgrade again must succeed (re-run safety) ────────────────
        _run_alembic(["alembic", "upgrade", "0004"], db_url)

    finally:
        _scoped_cleanup()
        # restore head so the rest of the suite sees the expected schema
        _run_alembic(["alembic", "upgrade", "head"], db_url)


def test_migration_0005_enforces_not_null(db_url: str) -> None:
    """Migration 0005 makes ``projects.owner_user_id`` NOT NULL.

    Round-trip:
      1. downgrade to 0004 (column nullable again)
      2. insert a project with NULL ``owner_user_id`` → succeeds
      3. upgrade to 0005 → fails with a self-explanatory RAISE EXCEPTION
      4. clean up the offending row, retry upgrade → succeeds
      5. downgrade to 0004 → constraint dropped, column nullable again
    """
    psycopg = pytest.importorskip("psycopg")

    rnd = random.Random()
    chat_id = rnd.randint(10_000_000, 99_999_999)
    name = f"p35nn-{rnd.randint(10_000, 99_999)}-orphan"
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    def _cleanup() -> None:
        with psycopg.connect(sync_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("DELETE FROM projects WHERE name = %s", (name,))

    try:
        _run_alembic(["alembic", "downgrade", "0004"], db_url)
        _cleanup()

        # Insert a row with NULL owner_user_id (allowed at 0004).
        with psycopg.connect(sync_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO projects (id, name, api_key_hash, admin_chat_id, owner_user_id)
                    VALUES (gen_random_uuid(), %s, %s, %s, NULL)
                    """,
                    (name, f"hash-{name}", chat_id),
                )

        # Upgrade should fail loudly via the RAISE EXCEPTION pre-check.
        env = {**os.environ, "DATABASE_URL": db_url}
        result = subprocess.run(
            ["alembic", "upgrade", "0005"],
            cwd=SERVER_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "upgrade must fail when NULLs remain"
        combined = result.stdout + result.stderr
        assert (
            "owner_user_id has NULLs" in combined
        ), f"expected RAISE EXCEPTION text in alembic output; got:\n{combined}"

        # Clean up the orphan and retry — should succeed.
        _cleanup()
        _run_alembic(["alembic", "upgrade", "0005"], db_url)

        # Constraint is in place — inserting a NULL now must fail.
        with psycopg.connect(sync_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        INSERT INTO projects (id, name, api_key_hash, admin_chat_id, owner_user_id)
                        VALUES (gen_random_uuid(), %s, %s, %s, NULL)
                        """,
                        (name, f"hash-{name}", chat_id),
                    )
                    raise AssertionError("INSERT with NULL owner_user_id must fail at 0005")
                except psycopg.errors.NotNullViolation:
                    pass

        # Symmetric downgrade: constraint dropped, NULLs allowed again.
        _run_alembic(["alembic", "downgrade", "0004"], db_url)
        with psycopg.connect(sync_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO projects (id, name, api_key_hash, admin_chat_id, owner_user_id)
                    VALUES (gen_random_uuid(), %s, %s, %s, NULL)
                    """,
                    (name, f"hash-{name}", chat_id),
                )

    finally:
        _cleanup()
        _run_alembic(["alembic", "upgrade", "head"], db_url)
