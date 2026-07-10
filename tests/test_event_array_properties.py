"""Array-valued event properties: schema validation, write-time sort, and queries.

Covers:
* :class:`TrackEventRequest` accepts arrays of scalars and rejects nested
  shapes (objects, nested arrays, ``None`` is OK as a value but not as a
  whole property — handled by Pydantic's default).
* The ingestion endpoint sorts every homogeneous array at write time so
  ``["b", "a"]`` and ``["a", "b"]`` collapse into the same JSONB value
  (key naming is irrelevant — the rule applies uniformly).
* Heterogeneous arrays (mixed scalar types that ``<`` can't compare) fall
  back to insertion order instead of 400-ing.
* The two canonical dashboard queries (per-element count and most-common-
  combo) return the expected shapes against a small fixture dataset.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.models.event import Event
from app.schemas.event import TrackEventRequest

# ── Schema-level unit tests (no DB) ────────────────────────────────────────


class TestTrackEventRequestArrays:
    """Validation behaviour of :class:`TrackEventRequest.properties`."""

    def _make(self, properties: dict) -> TrackEventRequest:
        return TrackEventRequest(
            api_key="proj_x",
            event_name="e",
            session_id="s",
            properties=properties,
        )

    def test_accepts_string_array(self) -> None:
        m = self._make({"tags": ["a", "b", "c"]})
        assert m.properties["tags"] == ["a", "b", "c"]

    def test_accepts_number_array(self) -> None:
        m = self._make({"scores": [1, 2, 3.5]})
        assert m.properties["scores"] == [1, 2, 3.5]

    def test_accepts_boolean_array(self) -> None:
        # Booleans sort by their int value (False=0, True=1) so [True, False]
        # is reordered to [False, True] on write.
        m = self._make({"flags": [True, False]})
        assert m.properties["flags"] == [False, True]

    def test_accepts_array_containing_null(self) -> None:
        m = self._make({"vals": ["a", None, "b"]})
        assert m.properties["vals"] == ["a", None, "b"]

    def test_accepts_heterogeneous_scalar_array(self) -> None:
        m = self._make({"misc": ["a", 1, True, None]})
        assert m.properties["misc"] == ["a", 1, True, None]

    def test_accepts_empty_array(self) -> None:
        m = self._make({"tags": []})
        assert m.properties["tags"] == []

    def test_rejects_array_with_object(self) -> None:
        with pytest.raises(ValidationError) as exc:
            self._make({"tags": [{"x": 1}]})
        assert "tags" in str(exc.value)

    def test_rejects_array_with_nested_array(self) -> None:
        with pytest.raises(ValidationError) as exc:
            self._make({"tags": [[1, 2]]})
        assert "tags" in str(exc.value)

    def test_rejects_top_level_object_value(self) -> None:
        with pytest.raises(ValidationError) as exc:
            self._make({"nested": {"a": 1}})
        assert "nested" in str(exc.value)

    # ── Write-time sort (uniform, regardless of key name) ──────────────────

    def test_string_array_is_sorted_at_write_time(self) -> None:
        # Any key — no _set suffix or other convention needed.
        m = self._make({"interest": ["vertical_to_horizontal", "unsure"]})
        assert m.properties["interest"] == ["unsure", "vertical_to_horizontal"]

    def test_number_array_is_sorted(self) -> None:
        m = self._make({"scores": [3, 1, 2]})
        assert m.properties["scores"] == [1, 2, 3]

    def test_unsorted_string_array_becomes_sorted(self) -> None:
        m = self._make({"tags": ["b", "a", "c"]})
        assert m.properties["tags"] == ["a", "b", "c"]

    def test_heterogeneous_array_is_left_alone(self) -> None:
        """Mixed-type arrays can't be sorted comparably; leave them as-sent."""
        m = self._make({"mixed": [1, "a", True]})
        assert m.properties["mixed"] == [1, "a", True]

    def test_empty_array_stays_empty(self) -> None:
        m = self._make({"tags": []})
        assert m.properties["tags"] == []

    def test_array_with_nulls_falls_back_to_insertion_order(self) -> None:
        """``None`` may not sort against str/int in Python 3; we leave the
        array alone in that case so the caller never sees a 400."""
        m = self._make({"vals": ["a", None, "b"]})
        # Either sort-skipped (left as-sent) or sorted; both are acceptable.
        # The contract is: never raises, always returns a list of the same
        # elements.
        assert sorted(m.properties["vals"], key=lambda x: (x is None, x)) == sorted(
            ["a", None, "b"], key=lambda x: (x is None, x)
        )
        assert set(map(type, m.properties["vals"])) == set(map(type, ["a", None, "b"]))


# ── Property-value length cap (DoS guard) ──────────────────────────────────


def test_property_value_over_max_length_rejected():
    huge = "x" * 9000  # exceeds the 8 KB per-value cap
    with pytest.raises(ValidationError):
        TrackEventRequest(
            api_key="proj_" + "a" * 64,
            event_name="e",
            session_id="s",
            properties={"blob": huge},
        )


def test_property_list_element_over_max_length_rejected():
    huge = "x" * 9000  # exceeds the 8 KB per-value cap
    with pytest.raises(ValidationError):
        TrackEventRequest(
            api_key="proj_" + "a" * 64,
            event_name="e",
            session_id="s",
            properties={"blobs": ["ok", huge]},
        )


def test_property_value_at_max_length_accepted():
    at_cap = "x" * 8000  # under the 8192-char cap — accepted
    m = TrackEventRequest(
        api_key="proj_" + "a" * 64,
        event_name="e",
        session_id="s",
        properties={"blob": at_cap},
    )
    assert m.properties["blob"] == at_cap


# ── End-to-end ingestion + query tests ─────────────────────────────────────


async def _create_project(api_client, name: str) -> dict:
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": name, "admin_chat_id": 111},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_track_sorts_array_property_at_write_time(api_client, db_session) -> None:
    """Any array property is sorted before insertion — no key convention needed."""
    data = await _create_project(api_client, name="arr-always-sort.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "onboarding_completed",
            "session_id": str(uuid.uuid4()),
            "properties": {
                "role": "creator",
                # Plain "interest" — no _set suffix. Server still sorts at write
                # time so combo queries collapse equivalent combinations.
                "interest": ["vertical_to_horizontal", "unsure"],
            },
        },
    )
    assert resp.status_code == 202, resp.text

    await db_session.invalidate()
    rows = (
        (
            await db_session.execute(
                select(Event).where(
                    Event.project_id == project_id,
                    Event.event_name == "onboarding_completed",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].properties.get("interest") == ["unsure", "vertical_to_horizontal"]
    assert rows[0].properties.get("role") == "creator"


async def test_track_preserves_order_for_heterogeneous_array(api_client, db_session) -> None:
    """Mixed-type arrays (can't be ``<`` compared) fall back to insertion order."""
    data = await _create_project(api_client, name="arr-mixed.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "e",
            "session_id": str(uuid.uuid4()),
            "properties": {"misc": ["a", 1, True]},
        },
    )
    assert resp.status_code == 202, resp.text

    await db_session.invalidate()
    rows = (
        (
            await db_session.execute(
                select(Event).where(Event.project_id == project_id, Event.event_name == "e")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].properties.get("misc") == ["a", 1, True]


async def test_track_rejects_nested_object_inside_array(api_client) -> None:
    data = await _create_project(api_client, name="arr-reject-obj.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "bad",
            "session_id": str(uuid.uuid4()),
            "properties": {"tags": [{"x": 1}]},
        },
    )
    assert resp.status_code == 422, resp.text


async def test_track_rejects_nested_array(api_client) -> None:
    data = await _create_project(api_client, name="arr-reject-nest.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "bad",
            "session_id": str(uuid.uuid4()),
            "properties": {"matrix": [[1, 2]]},
        },
    )
    assert resp.status_code == 422, resp.text


# ── Canonical dashboard queries against a small fixture dataset ────────────


async def test_per_element_count_and_most_common_combos(api_client, db_session) -> None:
    """The two queries from the README work on a small ingested dataset.

    Fixture: four ``onboarding_completed`` events with overlapping
    ``interest`` arrays. Note the plain ``interest`` key — no ``_set``
    suffix, no special convention. Because the server sorts every
    array at write time, the "combo" GROUP BY still treats
    ``["a","b"]`` and ``["b","a"]`` as the same bucket without
    extra normalisation at read time.
    """
    data = await _create_project(api_client, name="arr-queries.com")
    project_id = uuid.UUID(data["id"])
    api_key = data["api_key"]

    payloads = [
        # Two events with the same set, sent in different orders — should
        # collapse into one combo bucket thanks to write-time sort.
        ["vertical_to_horizontal", "unsure"],
        ["unsure", "vertical_to_horizontal"],
        # One event with a different set.
        ["vertical_to_horizontal"],
        # One event with another set sharing one element.
        ["unsure", "audio_only"],
    ]
    for interests in payloads:
        resp = await api_client.post(
            "/api/v1/track",
            json={
                "api_key": api_key,
                "event_name": "onboarding_completed",
                "session_id": str(uuid.uuid4()),
                "properties": {"interest": interests},
            },
        )
        assert resp.status_code == 202, resp.text

    await db_session.invalidate()

    # ── Query 1: per-element count ─────────────────────────────────────────
    per_element = (
        await db_session.execute(
            text(
                """
                SELECT elem, count(*) AS n
                FROM events,
                     jsonb_array_elements_text(properties->'interest') AS elem
                WHERE project_id = :pid AND event_name = 'onboarding_completed'
                GROUP BY elem
                ORDER BY n DESC, elem ASC
                """
            ),
            {"pid": project_id},
        )
    ).all()
    by_elem = {row.elem: row.n for row in per_element}
    # vertical_to_horizontal appears in 3 events; unsure in 3; audio_only in 1.
    assert by_elem["vertical_to_horizontal"] == 3
    assert by_elem["unsure"] == 3
    assert by_elem["audio_only"] == 1

    # ── Query 2: most common combos ────────────────────────────────────────
    # NOTE: we repeat the ``properties->'interest'`` expression in
    # ``GROUP BY`` and ``ORDER BY`` instead of using the ``combo`` alias.
    # Postgres rejects ``combo::text`` in ``ORDER BY`` with "column combo
    # does not exist" because the typecast forces ``combo`` to be parsed
    # as an input-column reference rather than the SELECT-list alias. The
    # canonical README query has no cast and is shorter (``GROUP BY
    # combo`` alone), so this only affects the deterministic tie-breaker
    # we need for the assertion below.
    combos = (
        await db_session.execute(
            text(
                """
                SELECT properties->'interest' AS combo, count(*) AS n
                FROM events
                WHERE project_id = :pid AND event_name = 'onboarding_completed'
                GROUP BY properties->'interest'
                ORDER BY n DESC, (properties->'interest')::text ASC
                """
            ),
            {"pid": project_id},
        )
    ).all()
    # Three distinct combos: [unsure, vertical_to_horizontal] x2,
    # [vertical_to_horizontal] x1, [audio_only, unsure] x1.
    assert len(combos) == 3
    top = combos[0]
    assert top.combo == ["unsure", "vertical_to_horizontal"]
    assert top.n == 2
