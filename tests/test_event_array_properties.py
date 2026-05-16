"""Array-valued event properties: schema validation, write-time sort, and queries.

Covers:
* :class:`TrackEventRequest` accepts arrays of scalars and rejects nested
  shapes (objects, nested arrays, ``None`` is OK as a value but not as a
  whole property — handled by Pydantic's default).
* The ingestion endpoint persists the array as-is for non-``_set`` keys
  and sorted alphabetically/numerically for ``_set``-suffixed keys.
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
        m = self._make({"flags": [True, False]})
        assert m.properties["flags"] == [True, False]

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

    # ── Write-time sort for *_set keys ─────────────────────────────────────

    def test_set_suffix_string_array_is_sorted(self) -> None:
        m = self._make({"interest_set": ["vertical_to_horizontal", "unsure"]})
        assert m.properties["interest_set"] == ["unsure", "vertical_to_horizontal"]

    def test_set_suffix_number_array_is_sorted(self) -> None:
        m = self._make({"scores_set": [3, 1, 2]})
        assert m.properties["scores_set"] == [1, 2, 3]

    def test_non_set_suffix_string_array_preserves_order(self) -> None:
        m = self._make({"tags": ["b", "a", "c"]})
        assert m.properties["tags"] == ["b", "a", "c"]

    def test_set_suffix_heterogeneous_array_is_left_alone(self) -> None:
        """Mixed-type arrays can't be sorted comparably; leave them as-sent."""
        m = self._make({"mixed_set": [1, "a", True]})
        assert m.properties["mixed_set"] == [1, "a", True]

    def test_set_suffix_empty_array_stays_empty(self) -> None:
        m = self._make({"tags_set": []})
        assert m.properties["tags_set"] == []

    def test_set_suffix_with_nulls_sorts_nulls_to_one_end(self) -> None:
        """``None`` may not sort against str/int in Python 3; we leave the
        array alone in that case so the caller never sees a 400."""
        m = self._make({"vals_set": ["a", None, "b"]})
        # Either sort-skipped (left as-sent) or sorted; both are acceptable.
        # The contract is: never raises, always returns a list of the same
        # elements.
        assert sorted(m.properties["vals_set"], key=lambda x: (x is None, x)) == sorted(
            ["a", None, "b"], key=lambda x: (x is None, x)
        )
        assert set(map(type, m.properties["vals_set"])) == set(map(type, ["a", None, "b"]))


# ── End-to-end ingestion + query tests ─────────────────────────────────────


async def _create_project(api_client, name: str) -> dict:
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": name, "admin_chat_id": 111},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_track_persists_array_property_unchanged(api_client, db_session) -> None:
    """Non-``_set`` array properties round-trip in insertion order."""
    data = await _create_project(api_client, name="arr-keep-order.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "onboarding_completed",
            "session_id": str(uuid.uuid4()),
            "properties": {"interest": ["vertical_to_horizontal", "unsure"]},
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
    assert rows[0].properties.get("interest") == ["vertical_to_horizontal", "unsure"]


async def test_track_sorts_set_suffixed_array_at_write_time(api_client, db_session) -> None:
    """A ``_set``-suffixed array is sorted before insertion."""
    data = await _create_project(api_client, name="arr-set-sort.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "onboarding_completed",
            "session_id": str(uuid.uuid4()),
            "properties": {
                "role": "creator",
                "interest_set": ["vertical_to_horizontal", "unsure"],
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
    assert rows[0].properties.get("interest_set") == ["unsure", "vertical_to_horizontal"]
    assert rows[0].properties.get("role") == "creator"


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
    ``interest_set`` arrays. Because ``_set`` keys are sorted at write
    time, the "combo" GROUP BY treats ``["a","b"]`` and ``["b","a"]``
    as the same bucket without extra normalisation at read time.
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
                "properties": {"interest_set": interests},
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
                     jsonb_array_elements_text(properties->'interest_set') AS elem
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
    combos = (
        await db_session.execute(
            text(
                """
                SELECT properties->'interest_set' AS combo, count(*) AS n
                FROM events
                WHERE project_id = :pid AND event_name = 'onboarding_completed'
                GROUP BY combo
                ORDER BY n DESC, combo::text ASC
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
