"""AgentCore Runtime entrypoint — serves POST /invocations on port 8080."""

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from bgg_shared.bgg import BggClient
from .agent import BggAgent
from .sessions import MemorySessionStore

app = BedrockAgentCoreApp()

# Safe to build at import time — reads no environment and opens no connection.
_bgg_client = BggClient()

# The store is built lazily so this module stays importable without AWS config,
# which is what made the old Lambda handler untestable.
_store: MemorySessionStore | None = None


def _get_store() -> MemorySessionStore:
    global _store
    if _store is None:
        _store = MemorySessionStore(os.environ["MEMORY_ID"])
    return _store


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    """Run one conversational turn against the session's stored history."""
    message = (payload or {}).get("prompt", "")
    if not isinstance(message, str) or not message.strip():
        return {"error": "'prompt' must be a non-empty string"}

    session_id = context.session_id
    store = _get_store()

    history = store.load(session_id)
    agent = BggAgent(
        bgg_client=_bgg_client,
        initial_history=history,
    )

    reply = agent.chat(message.strip())

    # Everything the turn added: the user message, any tool_use/tool_result
    # exchanges, and the final assistant turn.
    store.append(session_id, agent.history[len(history):])

    return {"reply": reply, "session_id": session_id}


if __name__ == "__main__":
    app.run()
