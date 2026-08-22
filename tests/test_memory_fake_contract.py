"""The in-memory fake must satisfy the same protocol it stands in for."""

from memory.contracts import MemoryStore
from memory.fake import InMemoryMemoryStore


class TestInMemoryStoreMatchesProtocol:
    def test_fake_is_a_memory_store(self) -> None:
        assert isinstance(InMemoryMemoryStore(), MemoryStore)
