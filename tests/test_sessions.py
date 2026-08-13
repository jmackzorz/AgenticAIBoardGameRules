"""Tests for the AgentCore Memory session store."""

import pytest

from bgg_agentcore.sessions import MemorySessionStore


class _FakeMemoryClient:
    """Stand-in for the boto3 'bedrock-agentcore' data-plane client."""

    def __init__(self, events: list | None = None) -> None:
        self.events = events or []
        self.list_calls: list[dict] = []
        self.created_events: list[dict] = []

    def list_events(self, **kwargs):
        self.list_calls.append(kwargs)
        return {"events": self.events}

    def create_event(self, **kwargs):
        self.created_events.append(kwargs)
        return {"event": {"eventId": f"evt-{len(self.created_events)}"}}


class _FakeBlock:
    """Mimics an Anthropic SDK content block, which is a Pydantic model."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def _event(timestamp: int, *messages: dict) -> dict:
    return {
        "eventTimestamp": timestamp,
        "payload": [{"blob": m} for m in messages],
    }


def _store(client: _FakeMemoryClient) -> MemorySessionStore:
    return MemorySessionStore("mem-test-abc1234567", client=client)


# ----------------------------------------------------------------------
# load
# ----------------------------------------------------------------------

def test_load_returns_empty_history_for_new_session():
    # Arrange
    client = _FakeMemoryClient(events=[])

    # Act
    history = _store(client).load("session-1")

    # Assert
    assert history == []


def test_load_scopes_the_query_to_memory_actor_and_session():
    # Arrange
    client = _FakeMemoryClient()

    # Act
    _store(client).load("session-1")

    # Assert
    call = client.list_calls[0]
    assert call["memoryId"] == "mem-test-abc1234567"
    assert call["sessionId"] == "session-1"
    assert call["actorId"]
    assert call["includePayloads"] is True


def test_load_orders_messages_oldest_first_regardless_of_service_order():
    # Arrange — newest event returned first, as a paginated API often would
    client = _FakeMemoryClient(events=[
        _event(200, {"role": "assistant", "content": "second"}),
        _event(100, {"role": "user", "content": "first"}),
    ])

    # Act
    history = _store(client).load("session-1")

    # Assert
    assert [m["content"] for m in history] == ["first", "second"]


def test_load_preserves_payload_order_within_a_single_event():
    # Arrange — one turn writes several messages as one event
    client = _FakeMemoryClient(events=[
        _event(
            100,
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "tool_use"},
            {"role": "user", "content": "tool_result"},
        ),
    ])

    # Act
    history = _store(client).load("session-1")

    # Assert
    assert [m["content"] for m in history] == ["q", "tool_use", "tool_result"]


def test_load_ignores_non_blob_payload_items():
    # Arrange — a conversational item alongside our blobs must not corrupt history
    client = _FakeMemoryClient(events=[
        {
            "eventTimestamp": 100,
            "payload": [
                {"conversational": {"role": "user", "content": "ignored"}},
                {"blob": {"role": "user", "content": "kept"}},
            ],
        },
    ])

    # Act
    history = _store(client).load("session-1")

    # Assert
    assert history == [{"role": "user", "content": "kept"}]


# ----------------------------------------------------------------------
# append
# ----------------------------------------------------------------------

def test_append_writes_nothing_when_there_are_no_new_messages():
    # Arrange
    client = _FakeMemoryClient()

    # Act
    _store(client).append("session-1", [])

    # Assert
    assert client.created_events == []


def test_append_writes_one_event_containing_every_message():
    # Arrange
    client = _FakeMemoryClient()
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    # Act
    _store(client).append("session-1", messages)

    # Assert
    assert len(client.created_events) == 1
    payload = client.created_events[0]["payload"]
    assert payload == [
        {"blob": {"role": "user", "content": "hello"}},
        {"blob": {"role": "assistant", "content": "hi"}},
    ]


def test_append_serializes_pydantic_content_blocks():
    # Arrange — assistant turns hold SDK block objects, not plain dicts
    client = _FakeMemoryClient()
    messages = [{
        "role": "assistant",
        "content": [_FakeBlock({"type": "text", "text": "hi"})],
    }]

    # Act
    _store(client).append("session-1", messages)

    # Assert
    stored = client.created_events[0]["payload"][0]["blob"]
    assert stored == {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}


def test_append_splits_batches_larger_than_the_payload_limit():
    # Arrange — CreateEvent accepts at most 100 payload items per event
    client = _FakeMemoryClient()
    messages = [{"role": "user", "content": str(i)} for i in range(150)]

    # Act
    _store(client).append("session-1", messages)

    # Assert
    assert len(client.created_events) == 2
    assert len(client.created_events[0]["payload"]) == 100
    assert len(client.created_events[1]["payload"]) == 50


def test_append_round_trips_through_load():
    # Arrange
    client = _FakeMemoryClient()
    store = _store(client)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [_FakeBlock({"type": "text", "text": "hi"})]},
    ]

    # Act
    store.append("session-1", messages)
    client.events = [{
        "eventTimestamp": 100,
        "payload": client.created_events[0]["payload"],
    }]
    history = store.load("session-1")

    # Assert
    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]
