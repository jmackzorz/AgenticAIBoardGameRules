from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SearchResult:
    id: str
    name: str
    year_published: Optional[str] = None


@dataclass
class BoardGame:
    id: str
    name: str
    year_published: Optional[str] = None
    description: Optional[str] = None
    min_players: Optional[int] = None
    max_players: Optional[int] = None
    playing_time: Optional[int] = None
    min_age: Optional[int] = None
    average_rating: Optional[float] = None
    bayes_rating: Optional[float] = None
    average_weight: Optional[float] = None
    bgg_rank: Optional[int] = None
    num_ratings: Optional[int] = None
    categories: list[str] = field(default_factory=list)
    mechanics: list[str] = field(default_factory=list)
    designers: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)

    def to_summary(self) -> str:
        """Return a compact text summary for Claude's context."""
        lines = [f"**{self.name}** (BGG ID: {self.id})"]
        if self.year_published:
            lines.append(f"Year: {self.year_published}")
        if self.min_players and self.max_players:
            lines.append(f"Players: {self.min_players}–{self.max_players}")
        elif self.min_players:
            lines.append(f"Players: {self.min_players}+")
        if self.playing_time:
            lines.append(f"Playing time: {self.playing_time} min")
        if self.min_age:
            lines.append(f"Min age: {self.min_age}+")
        if self.average_rating:
            lines.append(f"Avg rating: {self.average_rating:.2f}/10")
        if self.bayes_rating:
            lines.append(f"Geek rating: {self.bayes_rating:.2f}/10")
        if self.bgg_rank:
            lines.append(f"BGG rank: #{self.bgg_rank}")
        if self.average_weight:
            lines.append(f"Complexity: {self.average_weight:.2f}/5")
        if self.num_ratings:
            lines.append(f"Ratings: {self.num_ratings:,}")
        if self.designers:
            lines.append(f"Designers: {', '.join(self.designers)}")
        if self.categories:
            lines.append(f"Categories: {', '.join(self.categories)}")
        if self.mechanics:
            lines.append(f"Mechanics: {', '.join(self.mechanics[:8])}")
        if self.description:
            desc = self.description[:500].rsplit(" ", 1)[0] + "..."
            lines.append(f"Description: {desc}")
        return "\n".join(lines)
