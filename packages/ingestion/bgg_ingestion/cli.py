"""BGG rulebook indexing pipeline CLI.

Usage:
    python -m bgg_ingestion.cli index data/pdfs/ --output data/chunks/ --resume
    python -m bgg_ingestion.cli qa data/chunks/
    python -m bgg_ingestion.cli reembed 178900
    python -m bgg_ingestion.cli inspect 178900
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure game names with non-ASCII characters (e.g. ō, é) don't crash on
# Windows consoles that default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import typer
from dotenv import load_dotenv
from tqdm import tqdm

app = typer.Typer(help="BGG rulebook indexing pipeline", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bgg_cache_dir() -> Path:
    return Path("data/bgg_cache")


def _markdown_dir() -> Path:
    return Path("data/markdown")


def _embeddings_dir() -> Path:
    return Path("data/embeddings")


def _get_game_name(bgg_id: int, bgg_client, cache_dir: Path) -> str:
    """Return the BGG game name, reading from or writing to a JSON cache."""
    cache_file = cache_dir / f"{bgg_id}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))["name"]

    games = bgg_client.get_game_details([str(bgg_id)])
    game = games[0] if games else None
    name = game.name if game else f"Game {bgg_id}"
    publishers = game.publishers if game else []
    cache_file.write_text(
        json.dumps({
            "bgg_id":         bgg_id,
            "name":           name,
            "year_published": game.year_published if game else None,
            "min_players":    game.min_players if game else None,
            "max_players":    game.max_players if game else None,
            "playing_time":   game.playing_time if game else None,
            "min_age":        game.min_age if game else None,
            "weight":         game.average_weight if game else None,
            "avg_rating":     game.average_rating if game else None,
            "bayes_rating":   game.bayes_rating if game else None,
            "bgg_rank":       game.bgg_rank if game else None,
            "num_ratings":    game.num_ratings if game else None,
            "categories":     game.categories if game else [],
            "mechanics":      game.mechanics if game else [],
            "designers":      game.designers if game else [],
            "artists":        [],  # not in BoardGame model; populated by --fetch-bgg
            "publishers":     publishers,
            "publisher":      publishers[0] if publishers else None,
        }, indent=2), encoding="utf-8"
    )
    return name


def _load_game_meta(bgg_id: int, cache_dir: Path) -> dict:
    """Return cached BGG metadata dict (name, weight, playtime). Empty dict if not cached."""
    cache_file = cache_dir / f"{bgg_id}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    return {}


def _adjusted_min_pages(base: int, weight: float | None) -> int:
    """Scale the minimum-pages threshold by game complexity.

    Uses BGG average weight (1–5) relative to a 2.5 midpoint.
    A light game (weight=1) gets a threshold of ~40% of base;
    a heavy game (weight=4) gets ~160% of base.
    """
    if weight is None:
        return base
    return max(2, round(base * weight / 2.5))


def _fetch_bgg_meta_batch(session, bgg_ids: list[str], api_key: str = "") -> list[dict]:
    """Fetch comprehensive BGG metadata for a batch of IDs via curl_cffi session.

    Uses the BGG XML API v2 with stats=1. Requires a Bearer token (BGG_API_KEY).
    BGG is known to return transient 401/429/202 errors — retries up to 10x.
    Caches everything useful from the response so we never need to re-fetch.
    """
    import xml.etree.ElementTree as ET
    from html import unescape

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    _MAX = 10
    _BASE_DELAY = 3.0
    resp = None
    for attempt in range(_MAX):
        resp = session.get(
            "https://boardgamegeek.com/xmlapi2/thing",
            params={"id": ",".join(bgg_ids), "stats": "1"},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            break
        if resp.status_code in (202, 401, 429):
            time.sleep(_BASE_DELAY * (attempt + 1))
            continue
        resp.raise_for_status()
    else:
        raise RuntimeError(f"BGG returned {resp.status_code} after {_MAX} retries")

    root = ET.fromstring(resp.text)

    def _ival(el):
        if el is None:
            return None
        try:
            return int(el.get("value") or 0) or None
        except (TypeError, ValueError):
            return None

    def _fval(el):
        if el is None:
            return None
        try:
            return float(el.get("value") or 0) or None
        except (TypeError, ValueError):
            return None

    def _links(item, link_type, exclude=("(Uncredited)",)):
        return [
            el.get("value", "")
            for el in item.findall(f"link[@type='{link_type}']")
            if el.get("value") not in ("", *exclude)
        ]

    results = []
    for item in root.findall("item"):
        game_id = item.get("id", "")

        name_el = item.find("name[@type='primary']")
        name = unescape(name_el.get("value", "Unknown")) if name_el is not None else "Unknown"
        alt_names = [
            unescape(el.get("value", ""))
            for el in item.findall("name[@type='alternate']")
        ]

        thumbnail = item.findtext("thumbnail") or None
        image = item.findtext("image") or None

        desc_el = item.find("description")
        description = unescape(desc_el.text or "").strip() if desc_el is not None else None

        year_published  = _ival(item.find("yearpublished"))
        min_players     = _ival(item.find("minplayers"))
        max_players     = _ival(item.find("maxplayers"))
        playing_time    = _ival(item.find("playingtime"))
        min_playtime    = _ival(item.find("minplaytime"))
        max_playtime    = _ival(item.find("maxplaytime"))
        min_age         = _ival(item.find("minage"))

        # Poll summary: best player count
        ps = item.find("poll-summary[@name='suggested_numplayers']")
        best_players     = ps.find("result[@name='bestwith']").get("value") if ps is not None else None
        rec_players      = ps.find("result[@name='recommmendedwith']").get("value") if ps is not None else None

        publishers   = _links(item, "boardgamepublisher")
        designers    = _links(item, "boardgamedesigner")
        artists      = _links(item, "boardgameartist")
        categories   = _links(item, "boardgamecategory", exclude=())
        mechanics    = _links(item, "boardgamemechanic", exclude=())
        families     = _links(item, "boardgamefamily",   exclude=())
        expansions   = _links(item, "boardgameexpansion", exclude=())
        integrations = _links(item, "boardgameintegration", exclude=())
        implementations = _links(item, "boardgameimplementation", exclude=())

        ratings = item.find("statistics/ratings")
        avg_rating = bayes_rating = weight = num_ratings = bgg_rank = None
        num_owned = num_trading = num_wanting = num_wishing = None
        num_comments = num_weights = rating_stddev = None

        if ratings is not None:
            num_ratings   = _ival(ratings.find("usersrated"))
            avg_rating    = _fval(ratings.find("average"))
            bayes_rating  = _fval(ratings.find("bayesaverage"))
            rating_stddev = _fval(ratings.find("stddev"))
            num_owned     = _ival(ratings.find("owned"))
            num_trading   = _ival(ratings.find("trading"))
            num_wanting   = _ival(ratings.find("wanting"))
            num_wishing   = _ival(ratings.find("wishing"))
            num_comments  = _ival(ratings.find("numcomments"))
            num_weights   = _ival(ratings.find("numweights"))
            weight        = _fval(ratings.find("averageweight"))
            rank_el = ratings.find("ranks/rank[@type='subtype'][@name='boardgame']")
            if rank_el is not None:
                try:
                    bgg_rank = int(rank_el.get("value") or 0) or None
                except (TypeError, ValueError):
                    bgg_rank = None

        results.append({
            "id":               game_id,
            "name":             name,
            "alt_names":        alt_names,
            "thumbnail":        thumbnail,
            "image":            image,
            "description":      description,
            "year_published":   year_published,
            "min_players":      min_players,
            "max_players":      max_players,
            "playing_time":     playing_time,
            "min_playtime":     min_playtime,
            "max_playtime":     max_playtime,
            "min_age":          min_age,
            "best_players":     best_players,
            "rec_players":      rec_players,
            "publishers":       publishers,
            "publisher":        publishers[0] if publishers else None,
            "designers":        designers,
            "artists":          artists,
            "categories":       categories,
            "mechanics":        mechanics,
            "families":         families,
            "expansions":       expansions,
            "integrations":     integrations,
            "implementations":  implementations,
            "weight":           weight,
            "num_weights":      num_weights,
            "avg_rating":       avg_rating,
            "bayes_rating":     bayes_rating,
            "rating_stddev":    rating_stddev,
            "bgg_rank":         bgg_rank,
            "num_ratings":      num_ratings,
            "num_owned":        num_owned,
            "num_trading":      num_trading,
            "num_wanting":      num_wanting,
            "num_wishing":      num_wishing,
            "num_comments":     num_comments,
        })
    return results


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def index(
    pdfs_dir: Path = typer.Argument(..., help="Directory containing <bggId>.pdf files"),
    output: Path = typer.Option(Path("data/chunks"), help="Output directory"),
    resume: bool = typer.Option(False, help="Skip PDFs that already have output JSON"),
    force: bool = typer.Option(False, help="Force re-index even when --resume is set"),
    embed: bool = typer.Option(True, help="Run Voyage embeddings; pass --no-embed to skip"),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Log enricher calls"),
) -> None:
    """Index all PDFs in PDFS_DIR, writing one <bggId>.json per game."""
    import os
    load_dotenv()

    pdfs_dir = pdfs_dir.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(pdfs_dir.glob("*.pdf"))
    if not pdf_files:
        typer.echo(f"No PDFs found in {pdfs_dir}", err=True)
        raise typer.Exit(1)

    # Lazy imports: keep CLI startup fast and avoid hard-failing when
    # heavy deps (marker, voyageai) are not installed yet.
    import anthropic
    from bgg_shared.bgg import BggClient
    from bgg_shared.schema import GameIndex
    from bgg_ingestion.chunker import chunk
    from bgg_ingestion.enricher import embed_chunks, tag_chunks
    from bgg_ingestion.extractor import extract
    from bgg_ingestion.qa import run_qa

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        typer.echo("ERROR: ANTHROPIC_API_KEY is not set. Check your .env file.", err=True)
        raise typer.Exit(1)
    if verbose:
        masked = f"{api_key[:8]}...{api_key[-4:]}"
        typer.echo(f"  ANTHROPIC_API_KEY loaded: {masked}")

    anthropic_client = anthropic.Anthropic()
    bgg_client = BggClient()

    embedder = None
    if embed:
        from bgg_shared.embedder import Embedder
        embedder = Embedder()

    cache_dir = _bgg_cache_dir()
    md_dir = _markdown_dir()
    emb_dir = _embeddings_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    emb_dir.mkdir(parents=True, exist_ok=True)

    for pdf_path in tqdm(pdf_files, desc="PDFs", unit="pdf"):
        try:
            bgg_id = int(pdf_path.stem)
        except ValueError:
            typer.echo(f"Skipping {pdf_path.name}: stem is not a BGG ID", err=True)
            continue

        out_file = output / f"{bgg_id}.json"
        if resume and out_file.exists() and not force:
            typer.echo(f"  skip {bgg_id} (already indexed)")
            continue

        game_name = _get_game_name(bgg_id, bgg_client, cache_dir)
        typer.echo(f"\n-> {game_name} ({bgg_id})")

        markdown, page_offsets = extract(pdf_path, md_dir)
        chunks = chunk(markdown, bgg_id, game_name, page_offsets)
        typer.echo(f"  {len(chunks)} chunks")

        log = typer.echo if verbose else None
        tag_chunks(chunks, anthropic_client, log=log)
        if embedder is not None:
            embed_chunks(chunks, embedder, embeddings_path=emb_dir / f"{bgg_id}.npy")
        else:
            typer.echo("  embeddings skipped (--no-embed)")

        game_index = GameIndex(
            bgg_id=bgg_id,
            game_name=game_name,
            source_pdf=str(pdf_path),
            indexed_at=datetime.now(timezone.utc),
            chunks=chunks,
            qa_flags=[],
        )
        game_index.qa_flags = run_qa(game_index)

        if game_index.qa_flags:
            typer.echo(f"  QA: {', '.join(game_index.qa_flags)}")

        out_file.write_text(game_index.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"  -> {out_file}")

    typer.echo("\nDone.")


@app.command("qa")
def qa_command(
    chunks_dir: Path = typer.Argument(Path("data/chunks"), help="Directory with <bggId>.json files"),
) -> None:
    """Print QA flag summary for all indexed games."""
    from bgg_shared.schema import GameIndex

    json_files = sorted(Path(chunks_dir).glob("*.json"))
    if not json_files:
        typer.echo(f"No indexed games found in {chunks_dir}", err=True)
        raise typer.Exit(1)

    for f in json_files:
        idx = GameIndex.model_validate_json(f.read_text(encoding="utf-8"))
        ok = not idx.qa_flags
        badge = "✓" if ok else f"✗ ({len(idx.qa_flags)})"
        flags = ", ".join(idx.qa_flags) if idx.qa_flags else "none"
        typer.echo(f"{badge}  {idx.game_name} ({idx.bgg_id})  —  {flags}")


@app.command()
def reembed(
    bgg_id: int = typer.Argument(..., help="BGG ID to re-embed"),
    chunks_dir: Path = typer.Option(Path("data/chunks"), help="Directory with indexed JSON files"),
) -> None:
    """Re-run Voyage embeddings for an already-indexed game."""
    load_dotenv()

    from bgg_shared.embedder import Embedder
    from bgg_shared.schema import GameIndex
    from bgg_ingestion.enricher import embed_chunks

    out_file = Path(chunks_dir) / f"{bgg_id}.json"
    if not out_file.exists():
        typer.echo(f"No index found for BGG ID {bgg_id}", err=True)
        raise typer.Exit(1)

    idx = GameIndex.model_validate_json(out_file.read_text(encoding="utf-8"))
    typer.echo(f"Re-embedding {idx.game_name} ({len(idx.chunks)} chunks)…")

    emb_dir = _embeddings_dir()
    emb_dir.mkdir(parents=True, exist_ok=True)
    embed_chunks(idx.chunks, Embedder(), embeddings_path=emb_dir / f"{bgg_id}.npy")

    typer.echo("Done.")


@app.command()
def inspect(
    bgg_id: int = typer.Argument(..., help="BGG ID to inspect"),
    chunks_dir: Path = typer.Option(Path("data/chunks"), help="Directory with indexed JSON files"),
    limit: int = typer.Option(0, help="Max chunks to print (0 = all)"),
) -> None:
    """Print chunk details for an indexed game."""
    from bgg_shared.schema import GameIndex

    out_file = Path(chunks_dir) / f"{bgg_id}.json"
    if not out_file.exists():
        typer.echo(f"No index found for BGG ID {bgg_id}", err=True)
        raise typer.Exit(1)

    idx = GameIndex.model_validate_json(out_file.read_text(encoding="utf-8"))

    typer.echo(f"\n{idx.game_name}  (BGG ID: {idx.bgg_id})")
    typer.echo(f"Source:   {idx.source_pdf}")
    typer.echo(f"Indexed:  {idx.indexed_at.strftime('%Y-%m-%d %H:%M UTC')}")
    typer.echo(f"Chunks:   {len(idx.chunks)}")
    typer.echo(f"QA flags: {', '.join(idx.qa_flags) if idx.qa_flags else 'none'}")
    typer.echo("")

    display = idx.chunks if not limit else idx.chunks[:limit]
    for c in display:
        breadcrumb = " › ".join(c.section_path) if c.section_path else "(no heading)"
        topics = ", ".join(c.topics) if c.topics else "—"
        snippet = c.text[:180].replace("\n", " ")
        typer.echo(
            f"[{c.chunk_id}]  page={c.page}  tokens={c.token_count}  type={c.chunk_type}\n"
            f"  {breadcrumb}\n"
            f"  topics: {topics}\n"
            f"  {snippet}…\n"
        )


@app.command()
def download(
    bgg_ids: list[int] = typer.Argument(..., help="One or more BGG game IDs"),
    output: Path = typer.Option(Path("data/pdfs"), help="Output directory for PDFs"),
    resume: bool = typer.Option(False, help="Skip games that already have a PDF"),
    delay: float = typer.Option(1.0, help="Seconds to wait between downloads"),
) -> None:
    """Download the English rulebook PDF for one or more BGG games.

    Requires BGG_USERNAME and BGG_PASSWORD in .env (free BGG account).
    Saves each file as <output>/<bggId>.pdf, ready for the index command.

        python -m bgg_ingestion.cli download 178900 266192 --resume
    """
    import os
    from bgg_ingestion.downloader import download_file, get_files, login, make_session, pick_rulebook

    load_dotenv()
    username = os.environ.get("BGG_USERNAME", "")
    password = os.environ.get("BGG_PASSWORD", "")
    if not username or not password:
        typer.echo("ERROR: BGG_USERNAME and BGG_PASSWORD must be set in .env", err=True)
        raise typer.Exit(1)

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    session = make_session()

    typer.echo(f"Logging in as {username!r}…")
    try:
        login(session, username, password)
        typer.echo("  logged in.")
    except Exception as exc:
        typer.echo(f"ERROR: BGG login failed — {exc}", err=True)
        raise typer.Exit(1)

    for bgg_id in bgg_ids:
        dest = output / f"{bgg_id}.pdf"
        if resume and dest.exists():
            typer.echo(f"  skip {bgg_id} (already downloaded)")
            continue

        typer.echo(f"\n-> BGG {bgg_id}")
        try:
            files = get_files(session, bgg_id)
            typer.echo(f"  {len(files)} files found")

            entry = pick_rulebook(files)
            if entry is None:
                typer.echo(f"  WARNING: no PDF found for {bgg_id}", err=True)
                continue

            typer.echo(f"  picking: {entry['title']!r}  ({entry['filename']}, votes={entry['numpositive']})")
            typer.echo("  launching browser...")
            download_file(session, entry, dest, username=username, password=password)
            size_kb = dest.stat().st_size // 1024
            typer.echo(f"  saved: {dest}  ({size_kb} KB)")

        except Exception as exc:
            typer.echo(f"  ERROR: {exc}", err=True)

        time.sleep(delay)

    typer.echo("\nDone.")


@app.command("download-top")
def download_top(
    count: int = typer.Argument(500, help="Number of top-ranked BGG games to download"),
    output: Path = typer.Option(Path("data/pdfs"), help="Output directory for PDFs"),
    resume: bool = typer.Option(True, help="Skip games that already have a PDF (default: on)"),
    delay: float = typer.Option(5.0, help="Seconds to wait between downloads"),
) -> None:
    """Download rulebooks for the top-ranked BGG board games by Geek Rating.

    Fetches the current BGG rankings, then downloads the English rulebook PDF
    for each game in rank order. Saves each file as <output>/<bggId>.pdf.

        python -m bgg_ingestion.cli download-top 100 --resume
    """
    import os
    from bgg_ingestion.downloader import download_file, get_files, get_top_rankings, login, make_session, pick_rulebook

    load_dotenv()
    username = os.environ.get("BGG_USERNAME", "")
    password = os.environ.get("BGG_PASSWORD", "")
    if not username or not password:
        typer.echo("ERROR: BGG_USERNAME and BGG_PASSWORD must be set in .env", err=True)
        raise typer.Exit(1)

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    # Rankings page is Cloudflare-protected — must use the curl_cffi session.
    session = make_session()
    typer.echo(f"Fetching top {count} games from BGG rankings…")
    top_games = get_top_rankings(session, count)
    if not top_games:
        typer.echo("ERROR: No games returned from BGG rankings.", err=True)
        raise typer.Exit(1)
    typer.echo(f"  {len(top_games)} games retrieved.")

    typer.echo(f"Logging in as {username!r}…")
    try:
        login(session, username, password)
        typer.echo("  logged in.")
    except Exception as exc:
        typer.echo(f"ERROR: BGG login failed — {exc}", err=True)
        raise typer.Exit(1)

    total = len(top_games)
    for i, game in enumerate(top_games, 1):
        game_id = game["id"]
        game_name = game["name"]
        dest = output / f"{game_id}.pdf"
        if resume and dest.exists():
            typer.echo(f"  [{i}/{total}] skip {game_id} ({game_name}) — already downloaded")
            continue

        typer.echo(f"\n[{i}/{total}] BGG {game_id} — {game_name}")
        try:
            files = get_files(session, int(game_id))
            typer.echo(f"  {len(files)} files found")

            entry = pick_rulebook(files)
            if entry is None:
                typer.echo(f"  WARNING: no English PDF found for {game_id}", err=True)
            else:
                typer.echo(f"  picking: {entry['title']!r}  ({entry['filename']}, votes={entry['numpositive']})")
                typer.echo("  launching browser...")
                download_file(session, entry, dest, username=username, password=password)
                size_kb = dest.stat().st_size // 1024
                typer.echo(f"  saved: {dest}  ({size_kb} KB)")

        except Exception as exc:
            typer.echo(f"  ERROR: {exc}", err=True)

        if i < total:
            typer.echo(f"  waiting {delay}s…")
            time.sleep(delay)

    typer.echo("\nDone.")


@app.command()
def scan(
    pdfs_dir: Path = typer.Argument(Path("data/pdfs"), help="Directory containing <bggId>.pdf files"),
    min_pages: int = typer.Option(8, help="Base page-count threshold (scaled by BGG weight when --fetch-bgg is used)"),
    min_size_kb: int = typer.Option(500, help="Flag PDFs smaller than this many KB"),
    delete: bool = typer.Option(False, "--delete", help="Delete Tier -1 PDFs (wrong content confirmed by first-page text)"),
    fetch_bgg: bool = typer.Option(False, "--fetch-bgg", help="Fetch BGG weight/playtime for all games and adjust thresholds by complexity"),
) -> None:
    """Audit downloaded PDFs and flag likely non-rulebooks.

    PDFs are split into two categories:

      CONFIRMED (Tier -1): first-page text contains avoidance terms like
        "solo mode", "variant rules", "quick reference", "reference card", etc.
        These are deleted by --delete.

      SUSPICIOUS: only short page count or small file size. Review manually
        before deciding to re-download.

    After --delete, re-run: python -m bgg_ingestion.cli download-top --resume
    """
    import re
    import warnings
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError, PdfReadWarning
    except ImportError:
        typer.echo("ERROR: pypdf is not installed. Run: uv add pypdf", err=True)
        raise typer.Exit(1)

    warnings.filterwarnings("ignore", category=PdfReadWarning)

    _FIRST_PAGE_FLAGS = {
        "solo mode", "solo rules", "variant rules", "variant rule",
        "quick reference", "reference card", "player aid",
    }

    pdf_files = sorted(Path(pdfs_dir).glob("*.pdf"))
    if not pdf_files:
        typer.echo(f"No PDFs found in {pdfs_dir}", err=True)
        raise typer.Exit(1)

    cache_dir = _bgg_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if fetch_bgg:
        from bgg_shared.bgg import BggClient
        ids_to_fetch = []
        for p in pdf_files:
            try:
                bgg_id_int = int(p.stem)
            except ValueError:
                continue
            meta = _load_game_meta(bgg_id_int, cache_dir)
            if "artists" not in meta:  # sentinel for comprehensive cache entry
                ids_to_fetch.append(str(bgg_id_int))

        if ids_to_fetch:
            import os as _os
            load_dotenv()
            bgg_api_key = _os.environ.get("BGG_API_KEY", "")
            if not bgg_api_key:
                typer.echo("ERROR: BGG_API_KEY not set in .env — required for --fetch-bgg", err=True)
                raise typer.Exit(1)
            typer.echo(f"Fetching BGG metadata for {len(ids_to_fetch)} games (batches of 20)…")
            from bgg_ingestion.downloader import make_session
            session = make_session()
            for i in range(0, len(ids_to_fetch), 20):
                batch = ids_to_fetch[i:i + 20]
                games = _fetch_bgg_meta_batch(session, batch, api_key=bgg_api_key)
                for game in games:
                    cf = cache_dir / f"{game['id']}.json"
                    data = {k: v for k, v in game.items() if k != "id"}
                    data["bgg_id"] = int(game["id"])
                    cf.write_text(json.dumps(data, indent=2), encoding="utf-8")
                typer.echo(f"  fetched {min(i + 20, len(ids_to_fetch))}/{len(ids_to_fetch)}")
                time.sleep(1.5)
            typer.echo("  BGG metadata cached.\n")
        else:
            typer.echo("BGG metadata already cached for all games.\n")

    confirmed: list[tuple[str, str]] = []   # first-page text hit — definitely wrong
    suspicious: list[tuple[str, str]] = []  # size/page only — needs manual review

    for pdf_path in pdf_files:
        try:
            bgg_id = pdf_path.stem
            bgg_id_int = int(bgg_id)
        except ValueError:
            continue

        meta = _load_game_meta(bgg_id_int, cache_dir)
        game_name = meta.get("name", "")
        weight = meta.get("weight")
        playtime = meta.get("playtime")
        adj_min = _adjusted_min_pages(min_pages, weight)

        size_reasons: list[str] = []
        text_reason: str | None = None
        page_count_low = False
        size_kb = pdf_path.stat().st_size // 1024

        try:
            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)

            if page_count < adj_min:
                page_reason = f"only {page_count} pages"
                if weight is not None:
                    parts = [f"w={weight:.1f}"]
                    if playtime:
                        parts.append(f"{playtime}min")
                    parts.append(f"adj.min={adj_min}")
                    page_reason += f" ({', '.join(parts)})"
                size_reasons.append(page_reason)
                page_count_low = True
            if size_kb < min_size_kb:
                size_reasons.append(f"only {size_kb} KB")

            if reader.pages:
                first_text = reader.pages[0].extract_text() or ""
                # Only check the title zone (first 150 chars) to avoid false
                # positives from terms appearing in component lists or TOCs.
                # Also skip matches preceded by a digit (e.g. "1 player aid")
                # which indicate a component count, not the document type.
                title_zone = first_text[:150].lower()
                for term in _FIRST_PAGE_FLAGS:
                    if term in title_zone and not re.search(r'\d\s*' + re.escape(term), title_zone):
                        text_reason = f"title zone: {term!r}"
                        break

        except PdfReadError as exc:
            size_reasons.append(f"unreadable PDF: {exc}")
        except Exception as exc:
            size_reasons.append(f"error: {exc}")

        all_reasons = ([text_reason] if text_reason else []) + size_reasons

        # Confirmed requires BOTH a title-zone text match AND a short page count.
        # Either condition alone is only suspicious.
        if text_reason and page_count_low:
            confirmed.append((bgg_id, "; ".join(all_reasons)))
            status = "CONFIRMED"
        elif text_reason or size_reasons:
            suspicious.append((bgg_id, "; ".join(all_reasons)))
            status = "SUSPICIOUS"
        else:
            status = "ok"

        name_suffix = f"  {game_name}" if game_name else ""
        typer.echo(f"  {bgg_id:>8}  {size_kb:>6} KB  {status:<12}  {'; '.join(all_reasons)}{name_suffix}")

    typer.echo(f"\n{len(confirmed)} confirmed (Tier -1), {len(suspicious)} suspicious, out of {len(pdf_files)} PDFs.\n")

    if confirmed:
        typer.echo("CONFIRMED — wrong content (will be deleted with --delete):")
        for bgg_id, reason in confirmed:
            meta = _load_game_meta(int(bgg_id), cache_dir)
            name = meta.get("name", "")
            label = f"{bgg_id}  {name}" if name else bgg_id
            typer.echo(f"  {label}  —  {reason}")

    if suspicious:
        from urllib.parse import quote_plus
        typer.echo("\nSUSPICIOUS — review manually (not deleted by --delete):")
        for bgg_id, reason in suspicious:
            meta = _load_game_meta(int(bgg_id), cache_dir)
            name = meta.get("name", "")
            publisher = meta.get("publisher", "")
            label = f"{bgg_id}  {name}" if name else bgg_id
            typer.echo(f"  {label}  —  {reason}")
            query = " ".join(filter(None, [name, publisher, "rulebook PDF"]))
            typer.echo(f"    https://www.google.com/search?q={quote_plus(query)}")

    if delete:
        if not confirmed:
            typer.echo("\nNothing to delete.")
        else:
            typer.echo(f"\nDeleting {len(confirmed)} confirmed PDFs…")
            for bgg_id, reason in confirmed:
                pdf_path = Path(pdfs_dir) / f"{bgg_id}.pdf"
                pdf_path.unlink()
                typer.echo(f"  deleted {bgg_id}.pdf  ({reason})")
            typer.echo(f"\nDone. Re-run: python -m bgg_ingestion.cli download-top --resume")
    elif confirmed:
        typer.echo(f"\nRe-run with --delete to remove confirmed files, then: python -m bgg_ingestion.cli download-top --resume")


@app.command()
def refetch(
    bgg_ids: list[int] = typer.Argument(..., help="BGG IDs to try fetching from alternative sources"),
    output: Path = typer.Option(Path("data/pdfs"), help="Directory to save PDFs"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be downloaded without saving"),
) -> None:
    """Try to find and download rulebooks for games from alternative sources.

    Searches cdn.1j1ju.com (a public rulebook repository) via DuckDuckGo for
    each BGG ID, then downloads the PDF if found. Replaces any existing file.

        python -m bgg_ingestion.cli refetch 96848 97207
        python -m bgg_ingestion.cli refetch 96848 --dry-run
    """
    import httpx
    from ddgs import DDGS

    cache_dir = _bgg_cache_dir()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    for bgg_id in bgg_ids:
        meta = _load_game_meta(bgg_id, cache_dir)
        game_name = meta.get("name") or f"BGG {bgg_id}"
        typer.echo(f"\n-> {game_name} ({bgg_id})")

        # Non-English filename indicators — prefer "rulebook" over these
        _NON_ENGLISH = ("regle", "regel", "reglas", "regels", "regola", "regler", "szabaly")

        def _url_score(url: str) -> int:
            """Higher = better. Prefer English 'rulebook' URLs."""
            u = url.lower()
            if "rulebook" in u:
                return 2
            if any(term in u for term in _NON_ENGLISH):
                return 0
            return 1

        import re as _re
        _STOP = {"the", "and", "for", "with", "game", "board", "card", "rule", "rules", "book"}

        def _name_matches_url(name: str, url: str) -> bool:
            """Return True if at least one significant word from name appears in the URL slug.

            Prevents false positives where DDG returns a popular URL (e.g. bc-878-vikings)
            for searches about completely unrelated games.
            """
            filename = url.rsplit("/", 1)[-1].lower()
            url_tokens = set(_re.split(r"[^a-z0-9]+", filename))
            name_words = [
                w.lower() for w in _re.split(r"[^a-zA-Z0-9]+", name)
                if len(w) >= 3 and w.lower() not in _STOP
            ]
            if not name_words:
                return True  # nothing to validate, allow
            return any(w in url_tokens for w in name_words)

        queries = [
            f"{game_name} english rulebook PDF 1j1ju",
            f"{game_name} rulebook PDF 1j1ju",
        ]

        candidates: list[str] = []
        for query in queries:
            typer.echo(f"   searching: {query}")
            try:
                with DDGS() as ddgs:
                    for result in ddgs.text(query, max_results=8):
                        url = result.get("href", "")
                        if "cdn.1j1ju.com" in url and url.lower().endswith(".pdf"):
                            if _name_matches_url(game_name, url):
                                candidates.append(url)
                            else:
                                typer.echo(f"   skipped (name mismatch): {url.rsplit('/', 1)[-1]}")
            except Exception as exc:
                typer.echo(f"   search error: {exc}", err=True)
            if candidates:
                break

        # Pick the highest-scoring candidate; skip non-English if better exists
        pdf_url = max(candidates, key=_url_score) if candidates else None

        if not pdf_url:
            typer.echo("   not found on 1j1ju.com")
            continue

        score = _url_score(pdf_url)
        lang_note = " (non-English — verify manually)" if score == 0 else ""
        typer.echo(f"   found: {pdf_url}{lang_note}")

        if dry_run:
            typer.echo("   (dry run — skipping download)")
            continue

        dest = output / f"{bgg_id}.pdf"
        try:
            with httpx.stream("GET", pdf_url, timeout=60, follow_redirects=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            size_kb = dest.stat().st_size // 1024
            typer.echo(f"   saved: {dest}  ({size_kb} KB)")
        except Exception as exc:
            typer.echo(f"   download error: {exc}", err=True)

    typer.echo("\nDone.")


if __name__ == "__main__":
    app()
