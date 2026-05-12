"""Streamlit review UI for indexed BGG rulebook data.

Run from repo root:
    streamlit run packages/ingestion/viewer/app.py
"""

from pathlib import Path

import streamlit as st

from bgg_shared.schema import Chunk, GameIndex

CHUNKS_DIR = Path("data/chunks")
PDFS_DIR = Path("data/pdfs")

_TYPE_COLOR = {
    "rules": "#1f77b4",
    "setup": "#2ca02c",
    "variant": "#ff7f0e",
    "reference": "#9467bd",
}


def _load_games() -> dict[int, GameIndex]:
    return {
        (idx := GameIndex.model_validate_json(f.read_text(encoding="utf-8"))).bgg_id: idx
        for f in sorted(CHUNKS_DIR.glob("*.json"))
    }


def _qa_badge(flags: list[str]) -> str:
    if not flags:
        return '<span style="background:#2ca02c;color:white;padding:2px 8px;border-radius:4px;font-size:0.85em;">✓ No QA flags</span>'
    label = ", ".join(flags)
    return f'<span style="background:#d62728;color:white;padding:2px 8px;border-radius:4px;font-size:0.85em;">✗ {len(flags)} flag(s): {label}</span>'


def _topic_pills(topics: list[str]) -> str:
    if not topics:
        return "—"
    return " ".join(
        f'<span style="background:#e8e8e8;padding:1px 6px;border-radius:10px;font-size:0.8em;">{t}</span>'
        for t in topics
    )


def _render_chunk(chunk: Chunk) -> None:
    breadcrumb = " › ".join(chunk.section_path) if chunk.section_path else "(no heading)"
    page_label = f"p.{chunk.page}" if chunk.page else "p.?"
    type_color = _TYPE_COLOR.get(chunk.chunk_type, "#888")

    header = (
        f'<span style="font-weight:600;">{chunk.chunk_id}</span> '
        f'<span style="color:#555;">{breadcrumb}</span> '
        f'<span style="color:#888;font-size:0.85em;">{page_label} · {chunk.token_count} tok</span> '
        f'<span style="background:{type_color};color:white;padding:1px 5px;border-radius:3px;font-size:0.8em;">{chunk.chunk_type}</span>'
    )

    with st.expander(breadcrumb, expanded=False):
        st.markdown(header, unsafe_allow_html=True)
        st.markdown(_topic_pills(chunk.topics), unsafe_allow_html=True)
        if chunk.summary:
            st.caption(chunk.summary)
        st.code(chunk.text, language=None)
        if chunk.page:
            if st.button(f"Jump to page {chunk.page}", key=f"jump_{chunk.chunk_id}"):
                st.session_state.current_page = chunk.page
                st.session_state.pdf_key = chunk.chunk_id  # force re-render
                st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="BGG Rulebook Viewer",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("BGG Rulebook Viewer")

    if not CHUNKS_DIR.exists() or not any(CHUNKS_DIR.glob("*.json")):
        st.error(f"No indexed games found in `{CHUNKS_DIR}`. Run the ingestion pipeline first.")
        st.code("python -m bgg_ingestion.cli index data/pdfs/ --output data/chunks/")
        return

    games = _load_games()

    # --- Top bar ---
    options = {f"{g.game_name} ({g.bgg_id})": g for g in games.values()}
    selected = st.selectbox("Game", list(options.keys()), label_visibility="collapsed")
    game = options[selected]

    col_badge, col_link, col_stats = st.columns([3, 2, 2])
    with col_badge:
        st.markdown(_qa_badge(game.qa_flags), unsafe_allow_html=True)
    with col_link:
        pdf_path = PDFS_DIR / f"{game.bgg_id}.pdf"
        if pdf_path.exists():
            st.markdown(f"[Open original PDF]({pdf_path.resolve().as_uri()})")
    with col_stats:
        st.caption(
            f"{len(game.chunks)} chunks · indexed {game.indexed_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )

    st.divider()

    # --- Sidebar filters ---
    with st.sidebar:
        st.header("Filters")

        all_sections = sorted({c.section_path[0] for c in game.chunks if c.section_path})
        section_filter = st.multiselect("Top-level section", all_sections)

        all_topics = sorted({t for c in game.chunks for t in c.topics})
        topic_filter = st.multiselect("Topic", all_topics)

        all_types = sorted({c.chunk_type for c in game.chunks})
        type_filter = st.multiselect("Chunk type", all_types)

        if game.qa_flags:
            qa_filter: list[str] = []  # QA flags are per-game, not per-chunk in this schema
        search_text = st.text_input("Search chunk text", "")

        st.caption(f"{len(game.chunks)} total chunks")

    # --- Apply filters ---
    visible: list[Chunk] = game.chunks
    if section_filter:
        visible = [c for c in visible if c.section_path and c.section_path[0] in section_filter]
    if topic_filter:
        visible = [c for c in visible if any(t in topic_filter for t in c.topics)]
    if type_filter:
        visible = [c for c in visible if c.chunk_type in type_filter]
    if search_text:
        q = search_text.lower()
        visible = [c for c in visible if q in c.text.lower()]

    # --- Two-pane layout ---
    left_col, right_col = st.columns([1, 1])

    with left_col:
        st.subheader("PDF")
        current_page = st.session_state.get("current_page", 1)

        if pdf_path.exists():
            try:
                from streamlit_pdf_viewer import pdf_viewer  # type: ignore

                pdf_viewer(
                    str(pdf_path),
                    width=680,
                    pages_to_render=[current_page],
                    key=f"pdf_{game.bgg_id}_{st.session_state.get('pdf_key', 'init')}",
                )
            except ImportError:
                st.warning("`streamlit-pdf-viewer` not installed. Run: `pip install streamlit-pdf-viewer`")
            except Exception as exc:
                st.warning(f"PDF viewer error: {exc}")
        else:
            st.warning(f"PDF not found at `{pdf_path}`.")
            st.info("Place the PDF at the path above to enable the viewer.")

        if current_page != 1:
            if st.button("← Back to page 1"):
                st.session_state.current_page = 1
                st.session_state.pdf_key = "reset"
                st.rerun()

    with right_col:
        st.subheader(f"Chunks ({len(visible)} shown)")
        for chunk in visible:
            _render_chunk(chunk)


if __name__ == "__main__":
    main()
