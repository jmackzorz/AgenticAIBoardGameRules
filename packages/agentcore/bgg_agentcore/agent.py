"""Core agent: drives the Claude tool-use loop backed by BGG data."""

import os

from anthropic import AnthropicBedrockMantle

from bgg_shared.bgg import BggClient
from .tools import TOOL_DEFINITIONS, dispatch

# Bedrock serves the same Messages API but namespaces model IDs under `anthropic.`.
_MODEL = "anthropic.claude-sonnet-4-6"
_DEFAULT_REGION = "us-west-2"

_SYSTEM_PROMPT = """You are a knowledgeable board game research assistant powered by
live data from BoardGameGeek (BGG), the world's largest board game database.

You have access to three tools:
- search_games: search for games by name or keyword
- get_game_details: fetch full stats, ratings, and descriptions for specific games
- get_hot_games: retrieve the current BGG trending list

Guidelines:
- When a user asks about a specific game, search for it first then fetch its details.
- When comparing games, fetch details for all of them in a single get_game_details call.
- When recommending games, consider player count, complexity, playing time, and theme.
- Always ground your answers in data from the tools; do not invent statistics.
- BGG ratings are out of 10. Complexity (weight) is out of 5 (1 = simple, 5 = very complex).
- Be concise but thorough. Structure your answers clearly with relevant stats highlighted.
"""


class BggAgent:
    """Stateful agent that maintains conversation history across turns."""

    def __init__(
        self,
        *,
        region: str | None = None,
        anthropic_client: AnthropicBedrockMantle | None = None,
        bgg_client: BggClient | None = None,
        initial_history: list[dict] | None = None,
    ) -> None:
        # Credentials come from the ambient AWS chain (task role locally, execution
        # role in AgentCore) — there is no API key to pass or store.
        self._client = anthropic_client or AnthropicBedrockMantle(
            aws_region=region or os.environ.get("AWS_REGION") or _DEFAULT_REGION
        )
        self._bgg = bgg_client or BggClient()
        self._owns_bgg = bgg_client is None
        self._history: list[dict] = list(initial_history) if initial_history else []

    @property
    def history(self) -> list[dict]:
        return self._history

    def close(self) -> None:
        if self._owns_bgg:
            self._bgg.close()

    def __enter__(self) -> "BggAgent":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def chat(self, user_message: str) -> str:
        """Send a user message, run the agentic loop, and return Claude's reply."""
        self._history.append({"role": "user", "content": user_message})

        while True:
            response = self._client.messages.create(
                model=_MODEL,
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOL_DEFINITIONS,
                messages=self._history,
            )

            # Append the full assistant turn (may contain text + tool_use blocks)
            self._history.append(
                {"role": "assistant", "content": response.content}
            )

            if response.stop_reason == "end_turn":
                # Claude is done — extract the text reply
                return self._extract_text(response.content)

            if response.stop_reason == "tool_use":
                # Execute every tool Claude requested and collect results
                tool_results = self._run_tool_calls(response.content)
                self._history.append(
                    {"role": "user", "content": tool_results}
                )
                # Loop: send tool results back and let Claude continue
                continue

            # Unexpected stop reason — return whatever text exists
            return self._extract_text(response.content)

    def reset(self) -> None:
        """Clear conversation history to start a fresh session."""
        self._history.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_tool_calls(
        self, content: list
    ) -> list[dict]:
        """Execute all tool_use blocks and return tool_result blocks."""
        results = []
        for block in content:
            if block.type != "tool_use":
                continue
            result_text = dispatch(self._bgg, block.name, block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )
        return results

    @staticmethod
    def _extract_text(content: list) -> str:
        parts = [block.text for block in content if block.type == "text"]
        return "\n".join(parts).strip()
