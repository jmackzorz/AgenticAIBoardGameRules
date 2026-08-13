"""JSON schema definitions for every tool Claude can call."""

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_games",
        "description": (
            "Search BoardGameGeek for board games by name or keyword. "
            "Returns a list of matching games with their BGG IDs. "
            "Use this first when the user mentions a game by name or asks about a type of game. "
            "Then use get_game_details to fetch full information for specific IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (game name or keywords).",
                },
                "exact": {
                    "type": "boolean",
                    "description": "If true, only return exact name matches. Default false.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_game_details",
        "description": (
            "Fetch full details for one or more board games by their BGG ID. "
            "Returns ratings, player counts, playing time, complexity, "
            "categories, mechanics, and designers. "
            "You can pass up to 20 IDs at once for efficient batch fetching. "
            "Always use this after search_games to get detailed info for specific games."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of BGG game IDs to fetch details for.",
                    "minItems": 1,
                    "maxItems": 20,
                },
            },
            "required": ["game_ids"],
        },
    },
    {
        "name": "get_hot_games",
        "description": (
            "Get the current BGG Hot List — the 50 board games that are trending "
            "right now based on page views and user activity. "
            "Use this when the user asks what is popular or trending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of hot games to return (1–50). Default 10.",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
        },
    },
]
