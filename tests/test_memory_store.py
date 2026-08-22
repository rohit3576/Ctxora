"""Memory store tests against the in-memory fake (protocol semantics)."""

import pytest

from memory.contracts import Session, TurnInsert
from memory.fake import InMemoryMemoryStore


def insert(session_id: str, question: str) -> TurnInsert:
    """Build one turn for the demo tenant."""
    return TurnInsert(
        tenant="demo",
        session_id=session_id,
        nl_query=question,
        sql="SELECT 1",
        data=({"avg": 1.5},),
        summary="avg was 1.5",
        token_usage=12,
    )


class TestSessions:
    def test_create_session_returns_uuid_backed_session(self) -> None:
        store = InMemoryMemoryStore()

        session = store.create_session(tenant="demo", title="Rpm Query")

        assert session.tenant == "demo"
        assert session.title == "Rpm Query"
        assert len(session.id) == 36

    def test_fetch_session_roundtrip(self) -> None:
        store = InMemoryMemoryStore()
        created = store.create_session(tenant="demo", title="T")

        fetched = store.fetch_session(created.id)

        assert fetched == created

    def test_fetch_unknown_session_returns_none(self) -> None:
        assert InMemoryMemoryStore().fetch_session("00000000-0000-0000-0000-000000000000") is None


class TestTurns:
    def test_append_turn_returns_monotonic_ids(self) -> None:
        store = InMemoryMemoryStore()
        session = store.create_session(tenant="demo", title="T")

        first = store.append_turn(insert(session.id, "q1"))
        second = store.append_turn(insert(session.id, "q2"))

        assert second > first > 0

    def test_history_groups_turns_under_sessions_newest_first(self) -> None:
        store = InMemoryMemoryStore()
        older = store.create_session(tenant="demo", title="Older")
        newer = store.create_session(tenant="demo", title="Newer")
        store.append_turn(insert(older.id, "q-old"))
        store.append_turn(insert(newer.id, "q-new"))

        page = store.list_history("demo")

        assert [item.session.title for item in page] == ["Newer", "Older"]
        assert len(page[0].turns) == 1
        assert page[0].turns[0].nl_query == "q-new"

    def test_history_is_tenant_scoped(self) -> None:
        store = InMemoryMemoryStore()
        mine = store.create_session(tenant="demo", title="mine")
        other = store.create_session(tenant="other", title="theirs")
        store.append_turn(insert(mine.id, "q"))
        store.append_turn(insert(other.id, "q"))

        page = store.list_history("demo")

        assert [item.session.id for item in page] == [mine.id]

    def test_turns_carry_payload(self) -> None:
        store = InMemoryMemoryStore()
        session = store.create_session(tenant="demo", title="T")
        store.append_turn(insert(session.id, "q1"))

        turn = store.list_history("demo")[0].turns[0]

        assert turn.sql == "SELECT 1"
        assert turn.data == ({"avg": 1.5},)
        assert turn.summary == "avg was 1.5"
        assert turn.token_usage == 12


class TestSessionEquality:
    def test_session_is_frozen(self) -> None:
        session = Session(id="x" * 36, tenant="demo", title="T")
        attribute = "title"

        with pytest.raises(AttributeError):
            setattr(session, attribute, "nope")
