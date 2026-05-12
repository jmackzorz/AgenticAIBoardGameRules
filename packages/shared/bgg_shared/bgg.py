import os
import time
import xml.etree.ElementTree as ET
from html import unescape
from typing import Optional

import httpx

from .models import BoardGame, SearchResult

_BASE_URL = "https://boardgamegeek.com/xmlapi2"
_TIMEOUT = 30.0
_RETRY_DELAY = 3.0   # base delay; multiplied by attempt number on each retry
_MAX_RETRIES = 5


class BggClient:
    """Thin wrapper around the BGG XML API v2."""

    def __init__(self, api_token: str | None = None) -> None:
        token = api_token or os.environ.get("BGG_API_KEY") or os.environ.get("BGG_API_TOKEN", "")
        headers: dict = {"User-Agent": "bgg-research-agent/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            timeout=_TIMEOUT,
            headers=headers,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BggClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_xml(self, endpoint: str, params: dict) -> ET.Element:
        """Fetch an endpoint, retrying if BGG returns 202 (queued)."""
        url = f"{_BASE_URL}/{endpoint}"
        for attempt in range(_MAX_RETRIES):
            response = self._http.get(url, params=params)
            if response.status_code == 200:
                return ET.fromstring(response.text)
            if response.status_code in (202, 401, 429):
                # 202: BGG queued the request (result not cached yet)
                # 401/429: BGG rate-limit signal; back off and retry
                time.sleep(_RETRY_DELAY * (attempt + 1))
                continue
            response.raise_for_status()
        raise RuntimeError(
            f"BGG returned {response.status_code} after {_MAX_RETRIES} retries for {url}"
        )

    @staticmethod
    def _attr(element: Optional[ET.Element], attr: str) -> Optional[str]:
        if element is None:
            return None
        return element.get(attr)

    @staticmethod
    def _int(value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def _float(value: Optional[str]) -> Optional[float]:
        try:
            return float(value) if value else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, exact: bool = False) -> list[SearchResult]:
        """Search BGG for board games matching *query*.

        Args:
            query: The search string.
            exact: If True, only return exact name matches.
        """
        params: dict = {"query": query, "type": "boardgame"}
        if exact:
            params["exact"] = 1

        root = self._get_xml("search", params)
        results: list[SearchResult] = []

        for item in root.findall("item"):
            game_id = item.get("id", "")
            primary = item.find("name[@type='primary']")
            name = self._attr(primary, "value") or "Unknown"
            year_el = item.find("yearpublished")
            year = self._attr(year_el, "value")
            results.append(SearchResult(id=game_id, name=name, year_published=year))

        return results

    def get_game_details(self, game_ids: list[str] | str) -> list[BoardGame]:
        """Fetch full details for one or more games by BGG ID.

        Accepts a single ID string or a list of IDs. BGG supports fetching
        up to 20 IDs in a single request via comma-separated values.
        """
        if isinstance(game_ids, str):
            game_ids = [game_ids]

        id_str = ",".join(game_ids)
        root = self._get_xml("thing", {"id": id_str, "stats": 1})
        games: list[BoardGame] = []

        for item in root.findall("item"):
            games.append(self._parse_game(item))

        return games

    def get_hot_games(self, limit: int = 20) -> list[SearchResult]:
        """Return the current BGG hot list (trending games).

        Args:
            limit: Number of results to return (max 50).
        """
        root = self._get_xml("hot", {"type": "boardgame"})
        results: list[SearchResult] = []

        for item in root.findall("item"):
            if len(results) >= limit:
                break
            game_id = item.get("id", "")
            name_el = item.find("name")
            name = self._attr(name_el, "value") or "Unknown"
            year_el = item.find("yearpublished")
            year = self._attr(year_el, "value")
            rank = item.get("rank")
            results.append(
                SearchResult(id=game_id, name=f"#{rank} {name}", year_published=year)
            )

        return results

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    def _parse_game(self, item: ET.Element) -> BoardGame:
        game_id = item.get("id", "")

        # Primary name
        primary_name_el = item.find("name[@type='primary']")
        name = self._attr(primary_name_el, "value") or "Unknown"

        # Basic attributes
        year = self._attr(item.find("yearpublished"), "value")
        min_players = self._int(self._attr(item.find("minplayers"), "value"))
        max_players = self._int(self._attr(item.find("maxplayers"), "value"))
        playing_time = self._int(self._attr(item.find("playingtime"), "value"))
        min_age = self._int(self._attr(item.find("minage"), "value"))

        # Description (HTML entities unescaped)
        desc_el = item.find("description")
        description = unescape(desc_el.text or "") if desc_el is not None else None

        # Links (categories, mechanics, designers, publishers)
        categories = [
            el.get("value", "")
            for el in item.findall("link[@type='boardgamecategory']")
        ]
        mechanics = [
            el.get("value", "")
            for el in item.findall("link[@type='boardgamemechanic']")
        ]
        designers = [
            el.get("value", "")
            for el in item.findall("link[@type='boardgamedesigner']")
            if el.get("value") != "(Uncredited)"
        ]
        publishers = [
            el.get("value", "")
            for el in item.findall("link[@type='boardgamepublisher']")
        ]

        # Statistics
        ratings_el = item.find("statistics/ratings")
        avg_rating = bgg_rank = bayes = weight = num_ratings = None

        if ratings_el is not None:
            avg_rating = self._float(
                self._attr(ratings_el.find("average"), "value")
            )
            bayes = self._float(
                self._attr(ratings_el.find("bayesaverage"), "value")
            )
            weight = self._float(
                self._attr(ratings_el.find("averageweight"), "value")
            )
            num_ratings = self._int(
                self._attr(ratings_el.find("usersrated"), "value")
            )
            rank_el = ratings_el.find(
                "ranks/rank[@type='subtype'][@name='boardgame']"
            )
            if rank_el is not None:
                bgg_rank = self._int(rank_el.get("value"))

        return BoardGame(
            id=game_id,
            name=name,
            year_published=year,
            description=description,
            min_players=min_players,
            max_players=max_players,
            playing_time=playing_time,
            min_age=min_age,
            average_rating=avg_rating,
            bayes_rating=bayes,
            average_weight=weight,
            bgg_rank=bgg_rank,
            num_ratings=num_ratings,
            categories=categories,
            mechanics=mechanics,
            designers=designers,
            publishers=publishers,
        )
