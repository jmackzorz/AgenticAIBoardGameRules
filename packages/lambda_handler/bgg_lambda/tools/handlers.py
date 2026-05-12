"""Tool handler functions — each corresponds to a tool definition."""

import json

from bgg_shared.bgg import BggClient


def _handle_search_games(client: BggClient, input_data: dict) -> str:
    query = input_data["query"]
    exact = input_data.get("exact", False)
    results = client.search(query, exact=exact)

    if not results:
        return json.dumps({"results": [], "message": f"No games found for '{query}'."})

    items = [
        {"id": r.id, "name": r.name, "year": r.year_published}
        for r in results[:25]  # cap at 25 to keep context manageable
    ]
    return json.dumps({"results": items, "total_found": len(results)})


def _handle_get_game_details(client: BggClient, input_data: dict) -> str:
    game_ids: list[str] = [str(gid) for gid in input_data["game_ids"]]
    games = client.get_game_details(game_ids)

    if not games:
        return json.dumps({"games": [], "message": "No details found for those IDs."})

    return json.dumps({"games": [_game_to_dict(g) for g in games]})


def _handle_get_hot_games(client: BggClient, input_data: dict) -> str:
    limit = int(input_data.get("limit", 10))
    results = client.get_hot_games(limit=limit)

    items = [
        {"id": r.id, "name": r.name, "year": r.year_published}
        for r in results
    ]
    return json.dumps({"hot_games": items})


def _game_to_dict(game) -> dict:
    return {
        "id": game.id,
        "name": game.name,
        "year_published": game.year_published,
        "min_players": game.min_players,
        "max_players": game.max_players,
        "playing_time_minutes": game.playing_time,
        "min_age": game.min_age,
        "average_rating": round(game.average_rating, 2) if game.average_rating else None,
        "geek_rating": round(game.bayes_rating, 2) if game.bayes_rating else None,
        "complexity_weight": round(game.average_weight, 2) if game.average_weight else None,
        "bgg_rank": game.bgg_rank,
        "num_ratings": game.num_ratings,
        "designers": game.designers,
        "categories": game.categories,
        "mechanics": game.mechanics[:10],  # limit for context size
        "description_snippet": (
            game.description[:400] if game.description else None
        ),
    }


# Dispatch table maps tool name → handler
_HANDLERS = {
    "search_games": _handle_search_games,
    "get_game_details": _handle_get_game_details,
    "get_hot_games": _handle_get_hot_games,
}


def dispatch(client: BggClient, tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return its result as a JSON string."""
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        return handler(client, tool_input)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
