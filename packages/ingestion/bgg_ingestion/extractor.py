"""PDF → markdown via marker-pdf, with a per-document cache."""

import re
from pathlib import Path

# marker's MarkdownRenderer emits "{N}---..." before each page when paginate_output=True.
# The separator is exactly 48 dashes (MarkdownRenderer.page_separator default).
_PAGE_SEP = "-" * 48
_PAGE_MARKER_RE = re.compile(r"\{(\d+)\}" + re.escape(_PAGE_SEP))
_IMAGE_RE = re.compile(r"!\[\]\([^)]+\)\n?")

# Module-level singleton: marker models are expensive to load.
_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        try:
            # marker v1.x
            from marker.converters.pdf import PdfConverter
            from marker.models import create_model_dict
            _converter = PdfConverter(artifact_dict=create_model_dict())
        except ImportError:
            # marker v0.x fallback
            from marker.models import load_all_models  # type: ignore[no-redef]
            _converter = load_all_models()
    return _converter


def _run_marker(pdf_path: str) -> str:
    """Run marker with pagination enabled and return the raw paginated markdown.

    marker's paginate_output=True inserts "{N}---..." markers between pages so
    we can detect exact page boundaries without relying on image filenames.
    """
    converter = _get_converter()

    if callable(converter) and not isinstance(converter, list):
        from marker.renderers.markdown import MarkdownRenderer
        document = converter.build_document(str(pdf_path))
        renderer = MarkdownRenderer(config={"paginate_output": True})
        rendered = renderer(document)
        return rendered.markdown

    # v0.x fallback: no page metadata available
    from marker.convert import convert_single_pdf  # type: ignore
    full_text, _, _ = convert_single_pdf(pdf_path, converter)
    return full_text


def _process_paginated_markdown(raw_text: str) -> tuple[str, list[int]]:
    """Parse {N}--- page markers and strip image refs.

    Returns (clean_text, page_offsets) where page_offsets[i] is the character
    offset in clean_text at which page i begins. bisect_right(page_offsets, pos)
    yields a 1-based page number for any position pos in clean_text.
    """
    parts = _PAGE_MARKER_RE.split(raw_text)
    # After split: [pre_text, "0", page0_body, "1", page1_body, ...]

    result: list[str] = []
    page_offsets: list[int] = [0]  # page 0 always starts at clean-text offset 0
    pos = 0

    # Pre-marker text (usually just whitespace; discard)
    # parts[0] is anything before the first {0}--- marker

    i = 1
    while i + 1 < len(parts):
        page_id = int(parts[i])
        clean_body = _IMAGE_RE.sub("", parts[i + 1]).strip()

        if clean_body:
            if result:
                sep = "\n\n"
                result.append(sep)
                pos += len(sep)

            # Record start of this page in clean text (skip page 0; it's always at 0)
            if page_id > 0:
                page_offsets.append(pos)

            result.append(clean_body)
            pos += len(clean_body)

        i += 2

    return "".join(result), page_offsets


def _strip_images_and_find_pages_fallback(raw_text: str) -> tuple[str, list[int]]:
    """Fallback for non-paginated markdown (v0.x or pre-paginate cache).

    Infers page boundaries from _page_N_ image reference filenames, then strips
    all image refs.
    """
    _PAGE_NUM_RE = re.compile(r"_page_(\d+)_")
    img_spans = [(m.start(), m.end()) for m in _IMAGE_RE.finditer(raw_text)]

    seen_pages: set[int] = set()
    page_boundaries: list[tuple[int, int]] = []
    for m in _IMAGE_RE.finditer(raw_text):
        pm = _PAGE_NUM_RE.search(m.group(0))
        if pm:
            page_num = int(pm.group(1))
            if page_num not in seen_pages:
                seen_pages.add(page_num)
                page_boundaries.append((m.start(), page_num))

    clean_parts: list[str] = []
    prev = 0
    for start, end in img_spans:
        clean_parts.append(raw_text[prev:start])
        prev = end
    clean_parts.append(raw_text[prev:])
    clean_text = "".join(clean_parts)

    if not page_boundaries:
        return clean_text, []

    def _to_clean_pos(raw_pos: int) -> int:
        return raw_pos - sum(end - start for start, end in img_spans if end <= raw_pos)

    page_boundaries.sort(key=lambda x: x[1])
    offsets: list[int] = [0]
    for raw_pos, page_num in page_boundaries:
        if page_num > 0:
            offsets.append(_to_clean_pos(raw_pos))
    return clean_text, offsets


def extract(pdf_path: Path, markdown_cache_dir: Path) -> tuple[str, list[int]]:
    """Extract markdown from a PDF, caching the raw marker output.

    The cache stores marker's paginated raw output (with {N}--- page markers and
    image refs). On every call — cache hit or miss — the markers and images are
    stripped and page offsets are computed fresh (fast regex).

    If an existing cache is non-paginated (stale from a prior code version), it is
    automatically invalidated and marker is re-run.

    Returns:
        (clean_markdown, page_offsets) where page_offsets[i] is the char offset
        in clean_markdown at which page i begins (bisect_right gives 1-based page).
    """
    pdf_path = Path(pdf_path)
    markdown_cache_dir = Path(markdown_cache_dir)
    markdown_cache_dir.mkdir(parents=True, exist_ok=True)

    cache_file = markdown_cache_dir / f"{pdf_path.stem}.md"

    raw_text: str | None = None
    if cache_file.exists():
        candidate = cache_file.read_text(encoding="utf-8")
        if _PAGE_MARKER_RE.search(candidate):
            raw_text = candidate
        # else: stale non-paginated cache — fall through to re-run marker

    if raw_text is None:
        raw_text = _run_marker(str(pdf_path))
        cache_file.write_text(raw_text, encoding="utf-8")

    if _PAGE_MARKER_RE.search(raw_text):
        return _process_paginated_markdown(raw_text)

    # v0.x or any non-paginated output
    return _strip_images_and_find_pages_fallback(raw_text)
