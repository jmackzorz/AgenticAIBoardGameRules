"""AWS Lambda handler for the BGG research agent."""

import json
import os
import time

import anthropic
import boto3

from bgg_lambda.agent import BggAgent
from bgg_shared.bgg import BggClient

# Initialized once per container — reused across warm invocations.
_anthropic_client = anthropic.Anthropic()
_bgg_client = BggClient()
_table = boto3.resource("dynamodb").Table(os.environ["SESSIONS_TABLE"])

_SESSION_TTL_SECONDS = 86_400  # 24 hours


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_API_SECRET = os.environ["API_SECRET"]

# ------------------------------------------------------------------
# DynamoDB helpers
# ------------------------------------------------------------------

def _load_history(session_id: str) -> list[dict]:
    response = _table.get_item(Key={"session_id": session_id})
    item = response.get("Item")
    if not item:
        return []
    return json.loads(item["history"])


def _save_history(session_id: str, history: list[dict]) -> None:
    _table.put_item(Item={
        "session_id": session_id,
        "history": json.dumps(_serialize_history(history)),
        "ttl": int(time.time()) + _SESSION_TTL_SECONDS,
    })


def _serialize_history(history: list[dict]) -> list[dict]:
    """Convert Anthropic SDK content blocks to plain dicts for JSON storage."""
    result = []
    for msg in history:
        content = msg["content"]
        if isinstance(content, str):
            result.append({"role": msg["role"], "content": content})
        elif isinstance(content, list):
            blocks = [
                block.model_dump() if hasattr(block, "model_dump") else block
                for block in content
            ]
            result.append({"role": msg["role"], "content": blocks})
        else:
            result.append(msg)
    return result


# ------------------------------------------------------------------
# Handler
# ------------------------------------------------------------------

def handler(event, _context):
    if event.get("headers", {}).get("x-api-secret") != _API_SECRET:
        return _error(403, "Forbidden")

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    session_id = body.get("session_id", "").strip()
    message = body.get("message", "").strip()

    if not session_id or not message:
        return _error(400, "session_id and message are required")

    try:
        history = _load_history(session_id)

        agent = BggAgent(
            anthropic_client=_anthropic_client,
            bgg_client=_bgg_client,
            initial_history=history,
        )

        reply = agent.chat(message)
        _save_history(session_id, agent.history)

    except Exception as exc:
        return _error(500, str(exc))

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"session_id": session_id, "reply": reply}),
    }


def _error(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }
