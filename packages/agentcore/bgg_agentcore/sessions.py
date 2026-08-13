"""Conversation persistence backed by AgentCore Memory short-term events."""

from datetime import datetime, timezone

import boto3

# One actor per deployment. Sessions are the unit of isolation here; per-user
# actors only become useful once long-term memory strategies are enabled.
_ACTOR_ID = "bgg-research-agent"

# CreateEvent caps a single event at 100 payload items.
_MAX_PAYLOAD_ITEMS = 100

# Upper bound on turns replayed into a new agent instance.
_MAX_EVENTS = 100


class MemorySessionStore:
    """Loads and appends conversation turns for one AgentCore Memory resource.

    History is stored as `blob` payloads rather than `conversational` ones: a turn
    may contain tool_use and tool_result blocks, which do not survive being
    flattened into the {role, content-string} shape `conversational` expects.
    """

    def __init__(
        self,
        memory_id: str,
        *,
        client=None,
        actor_id: str = _ACTOR_ID,
    ) -> None:
        self._memory_id = memory_id
        self._client = client or boto3.client("bedrock-agentcore")
        self._actor_id = actor_id

    def load(self, session_id: str) -> list[dict]:
        """Return the stored message history for a session, oldest first."""
        response = self._client.list_events(
            memoryId=self._memory_id,
            actorId=self._actor_id,
            sessionId=session_id,
            includePayloads=True,
            maxResults=_MAX_EVENTS,
        )

        # ListEvents ordering is not contractual, so sort rather than assume it.
        events = sorted(
            response.get("events", []),
            key=lambda event: event.get("eventTimestamp") or 0,
        )

        history: list[dict] = []
        for event in events:
            for item in event.get("payload", []):
                message = item.get("blob")
                if message is not None:
                    history.append(message)
        return history

    def append(self, session_id: str, messages: list[dict]) -> None:
        """Persist newly produced messages as one event per batch of 100."""
        if not messages:
            return

        serialized = [_serialize_message(m) for m in messages]
        for start in range(0, len(serialized), _MAX_PAYLOAD_ITEMS):
            chunk = serialized[start : start + _MAX_PAYLOAD_ITEMS]
            self._client.create_event(
                memoryId=self._memory_id,
                actorId=self._actor_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=[{"blob": message} for message in chunk],
            )


def _serialize_message(message: dict) -> dict:
    """Convert Anthropic SDK content blocks into JSON-safe plain dicts."""
    content = message["content"]
    if isinstance(content, list):
        content = [
            block.model_dump() if hasattr(block, "model_dump") else block
            for block in content
        ]
    return {"role": message["role"], "content": content}
