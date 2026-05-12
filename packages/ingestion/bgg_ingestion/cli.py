"""BGG rulebook indexing pipeline CLI.

Usage:
    python -m bgg_ingestion.cli index data/pdfs/ --output data/chunks/ --resume
    python -m bgg_ingestion.cli qa data/chunks/
    python -m bgg_ingestion.cli reembed 178900
    python -m bgg_ingestion.cli inspect 178900
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

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
    name = games[0].name if games else f"Game {bgg_id}"
    cache_file.write_text(
        json.dumps({"bgg_id": bgg_id, "name": name}, indent=2), encoding="utf-8"
    )
    return name


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
    min_pages: int = typer.Option(8, help="Flag PDFs with fewer than this many pages"),
    min_size_kb: int = typer.Option(500, help="Flag PDFs smaller than this many KB"),
    delete: bool = typer.Option(False, "--delete", help="Delete Tier -1 PDFs (wrong content confirmed by first-page text)"),
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

    confirmed: list[tuple[str, str]] = []   # first-page text hit — definitely wrong
    suspicious: list[tuple[str, str]] = []  # size/page only — needs manual review

    for pdf_path in pdf_files:
        try:
            bgg_id = pdf_path.stem
            int(bgg_id)  # validate it's a numeric BGG ID
        except ValueError:
            continue

        size_reasons: list[str] = []
        text_reason: str | None = None
        size_kb = pdf_path.stat().st_size // 1024

        try:
            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)

            if page_count < min_pages:
                size_reasons.append(f"only {page_count} pages")
            if size_kb < min_size_kb:
                size_reasons.append(f"only {size_kb} KB")

            if reader.pages:
                first_text = reader.pages[0].extract_text() or ""
                first_lower = first_text.lower()
                for term in _FIRST_PAGE_FLAGS:
                    if term in first_lower:
                        text_reason = f"first page: {term!r}"
                        break

        except PdfReadError as exc:
            size_reasons.append(f"unreadable PDF: {exc}")
        except Exception as exc:
            size_reasons.append(f"error: {exc}")

        all_reasons = ([text_reason] if text_reason else []) + size_reasons

        if text_reason:
            confirmed.append((bgg_id, "; ".join(all_reasons)))
            status = "CONFIRMED"
        elif size_reasons:
            suspicious.append((bgg_id, "; ".join(size_reasons)))
            status = "SUSPICIOUS"
        else:
            status = "ok"

        typer.echo(f"  {bgg_id:>8}  {size_kb:>6} KB  {status}  {'; '.join(all_reasons)}")

    typer.echo(f"\n{len(confirmed)} confirmed (Tier -1), {len(suspicious)} suspicious, out of {len(pdf_files)} PDFs.\n")

    if confirmed:
        typer.echo(f"CONFIRMED — wrong content (will be deleted with --delete):")
        typer.echo("  " + " ".join(bgg_id for bgg_id, _ in confirmed))

    if suspicious:
        typer.echo(f"\nSUSPICIOUS — review manually (not deleted by --delete):")
        typer.echo("  " + " ".join(bgg_id for bgg_id, _ in suspicious))

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


if __name__ == "__main__":
    app()
