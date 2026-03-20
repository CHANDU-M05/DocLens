"""
DocLens — AI Documentation Analyzer
"""

import streamlit as st
import json
import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.crawler import Crawler
from utils.extractor import ModuleExtractor, get_provider
from utils.diff import diff_extractions, diff_to_dict, ChangeType

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

st.set_page_config(
    page_title="DocLens — AI Doc Analyzer",
    page_icon="D",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ──
with st.sidebar:
    st.markdown("## DocLens")
    st.caption("AI-powered documentation analyzer")

    provider_choice = st.selectbox("LLM Provider", ["openai", "anthropic", "gemini"])

    model_options = {
        "openai": ["gpt-3.5-turbo", "gpt-4", "gpt-4o"],
        "anthropic": ["claude-3-haiku-20240307", "claude-3-sonnet-20240229"],
        "gemini": ["gemini-2.0-flash", "gemini-1.5-flash-latest"],
    }
    model_choice = st.selectbox("Model", model_options[provider_choice])

    env_keys = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
    env_key = env_keys[provider_choice]
    api_key_input = st.text_input(f"API Key", value=os.getenv(env_key, ""), type="password")
    api_key = api_key_input or os.getenv(env_key)

    st.markdown("---")
    max_pages = st.slider("Max pages per URL", 10, 300, 100)
    delay = st.slider("Delay between requests (s)", 0.1, 2.0, 0.5, 0.1)
    max_depth = st.slider("Crawl depth", 1, 4, 2)
    use_cache = st.toggle("Use crawl cache", value=True)
    st.caption("Cache stores pages in `.crawl_cache.db` for 24h.")

# ── Tabs ──
tab_extract, tab_diff, tab_compare = st.tabs(["Extract", "Diff", "Compare"])


# ── Tab 1: Extract ──
with tab_extract:
    st.markdown("### Extract modules from documentation")
    url_input = st.text_area("Documentation URLs (one per line)", placeholder="https://docs.example.com/", height=100)

    if st.button("Extract modules", type="primary"):
        if not api_key:
            st.error(f"API key required. Set {env_key} in sidebar or .env file.")
            st.stop()

        urls = [u.strip() for u in url_input.strip().split("\n") if u.strip()]
        invalid = [u for u in urls if not u.startswith(("http://","https://"))]
        if invalid:
            st.error(f"Invalid URLs: {invalid}")
            st.stop()
        if not urls:
            st.error("Enter at least one URL.")
            st.stop()

        try:
            provider = get_provider(provider_choice, api_key=api_key, model=model_choice)
        except ImportError as e:
            st.error(str(e))
            st.stop()

        crawler = Crawler(max_pages=max_pages, delay=delay, max_depth=max_depth, use_cache=use_cache)
        extractor = ModuleExtractor(provider=provider)
        status = st.empty()
        prog = st.progress(0)
        all_results = {"content":{},"hierarchy":{},"titles":{},"depths":{},"metadata":{},"structure":{}}

        for i, url in enumerate(urls):
            status.info(f"Crawling {url}...")
            result = crawler.crawl(url)
            for key in all_results:
                all_results[key].update(result[key])
            prog.progress((i+1)/len(urls)*0.5)

        status.info(f"Analyzing with {provider.name}...")
        modules = extractor.extract_modules(all_results)
        prog.progress(1.0)
        status.success(f"Done. {len(modules)} modules extracted.")
        st.session_state["last_extraction"] = modules

        if not modules:
            st.warning("No modules found.")
            st.stop()

        v1, v2, v3 = st.tabs(["Module tree", "JSON output", "Site structure"])

        with v1:
            for module in modules:
                with st.expander(f"**{module['module']}**"):
                    st.markdown(f"**Description:** {module['Description']}")
                    subs = module.get('Submodules', {})
                    if subs:
                        st.markdown("**Submodules:**")
                        for name, desc in subs.items():
                            st.markdown(f"- **{name}:** {desc}")
                    else:
                        st.caption("No submodules identified.")

        with v2:
            json_str = json.dumps(modules, indent=2)
            st.download_button("Download JSON", data=json_str, file_name="extracted_modules.json", mime="application/json")
            st.code(json_str, language="json")

        with v3:
            pages_by_depth = {}
            for url, depth in all_results["depths"].items():
                pages_by_depth.setdefault(depth, []).append(url)
            for depth in sorted(pages_by_depth):
                st.markdown(f"**Depth {depth}** ({len(pages_by_depth[depth])} pages)")
                for url in pages_by_depth[depth][:10]:
                    title = all_results["titles"].get(url, url)
                    st.caption(f"  {title} — {url}")


# ── Tab 2: Diff ──
with tab_diff:
    st.markdown("### Diff two extraction runs")
    st.caption("Upload two JSON files to see what changed between runs.")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Baseline** (older)")
        baseline_file = st.file_uploader("Baseline JSON", type="json", key="baseline")
    with col2:
        st.markdown("**Current** (newer)")
        current_file = st.file_uploader("Current JSON", type="json", key="current")

    if baseline_file and current_file:
        tmp_dir = Path("/tmp/doclens_diff")
        tmp_dir.mkdir(exist_ok=True)
        baseline_path = tmp_dir / "baseline.json"
        current_path = tmp_dir / "current.json"
        baseline_path.write_bytes(baseline_file.read())
        current_path.write_bytes(current_file.read())

        try:
            diff = diff_extractions(str(baseline_path), str(current_path))
        except Exception as e:
            st.error(f"Diff failed: {e}")
            st.stop()

        summary = diff.summary
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Added", summary["modules_added"])
        m2.metric("Removed", summary["modules_removed"])
        m3.metric("Modified", summary["modules_modified"])
        m4.metric("Unchanged", summary["modules_unchanged"])

        if not diff.has_changes:
            st.success("No changes detected.")
        else:
            if diff.added_modules:
                st.markdown("#### Added modules")
                for m in diff.added_modules:
                    with st.expander(f"+ {m.module}", expanded=True):
                        st.success(m.new_description)
                        for sub in m.submodule_diffs:
                            st.markdown(f"  + **{sub.name}:** {sub.new_description}")

            if diff.removed_modules:
                st.markdown("#### Removed modules")
                for m in diff.removed_modules:
                    with st.expander(f"- {m.module}"):
                        st.error(m.old_description)

            if diff.modified_modules:
                st.markdown("#### Modified modules")
                for m in diff.modified_modules:
                    with st.expander(f"~ {m.module} (similarity: {m.description_similarity:.0%})"):
                        if m.description_similarity < 0.85:
                            col_a, col_b = st.columns(2)
                            col_a.markdown("**Before**"); col_a.info(m.old_description)
                            col_b.markdown("**After**"); col_b.success(m.new_description)
                        if m.added_submodules:
                            st.markdown("**New submodules:**")
                            for s in m.added_submodules:
                                st.markdown(f"  + **{s.name}:** {s.new_description}")
                        if m.removed_submodules:
                            st.markdown("**Removed submodules:**")
                            for s in m.removed_submodules:
                                st.markdown(f"  - **{s.name}**")
                        if m.modified_submodules:
                            st.markdown("**Modified submodules:**")
                            for s in m.modified_submodules:
                                st.markdown(f"  ~ **{s.name}** ({s.similarity:.0%})")

        st.download_button("Download diff JSON", data=json.dumps(diff_to_dict(diff), indent=2),
                           file_name="diff.json", mime="application/json")


# ── Tab 3: Compare ──
with tab_compare:
    st.markdown("### Competitor analysis")
    st.caption("Crawl two documentation sites and get an AI-generated comparison.")

    col_a, col_b = st.columns(2)
    with col_a:
        url_a = st.text_input("Product A URL", placeholder="https://docs.producta.com/")
        name_a = st.text_input("Product A name", placeholder="ProductA")
    with col_b:
        url_b = st.text_input("Product B URL", placeholder="https://docs.productb.com/")
        name_b = st.text_input("Product B name", placeholder="ProductB")

    if st.button("Run analysis", type="primary"):
        if not api_key:
            st.error("API key required.")
            st.stop()
        if not url_a or not url_b:
            st.error("Both URLs required.")
            st.stop()

        name_a = name_a or "Product A"
        name_b = name_b or "Product B"

        try:
            provider = get_provider(provider_choice, api_key=api_key, model=model_choice)
        except ImportError as e:
            st.error(str(e))
            st.stop()

        crawler = Crawler(max_pages=max_pages, delay=delay, max_depth=max_depth, use_cache=use_cache)
        extractor = ModuleExtractor(provider=provider)
        status = st.empty()
        prog = st.progress(0)

        status.info(f"Crawling {name_a}...")
        results_a = crawler.crawl(url_a)
        prog.progress(0.25)

        status.info(f"Extracting modules from {name_a}...")
        modules_a = extractor.extract_modules(results_a)
        prog.progress(0.50)

        status.info(f"Crawling {name_b}...")
        results_b = crawler.crawl(url_b)
        prog.progress(0.75)

        status.info(f"Extracting modules from {name_b}...")
        modules_b = extractor.extract_modules(results_b)
        prog.progress(0.90)

        status.info("Generating comparison...")
        comparison_prompt = f"""Compare these two products based on their documentation.

{name_a} modules:
{json.dumps([m['module'] for m in modules_a], indent=2)}

{name_a} full extraction:
{json.dumps(modules_a, indent=2)[:3000]}

{name_b} modules:
{json.dumps([m['module'] for m in modules_b], indent=2)}

{name_b} full extraction:
{json.dumps(modules_b, indent=2)[:3000]}

Provide:
1. Features {name_a} has that {name_b} lacks
2. Features {name_b} has that {name_a} lacks
3. Areas where both compete directly
4. Overall assessment: which appears more feature-complete?

Be specific. Reference actual module names."""

        comparison_text = provider.complete(
            "You are a senior product analyst. Be specific, structured, and honest.",
            comparison_prompt, max_tokens=2000
        )
        prog.progress(1.0)
        status.success("Analysis complete.")

        col_left, col_right = st.columns(2)
        with col_left:
            st.markdown(f"#### {name_a} ({len(modules_a)} modules)")
            for m in modules_a:
                st.markdown(f"- **{m['module']}** ({len(m.get('Submodules',{}))} submodules)")
        with col_right:
            st.markdown(f"#### {name_b} ({len(modules_b)} modules)")
            for m in modules_b:
                st.markdown(f"- **{m['module']}** ({len(m.get('Submodules',{}))} submodules)")

        st.markdown("---")
        st.markdown("#### AI comparison")
        st.markdown(comparison_text)

        st.download_button(
            "Download comparison JSON",
            data=json.dumps({f"{name_a}_modules": modules_a, f"{name_b}_modules": modules_b, "comparison": comparison_text}, indent=2),
            file_name="comparison.json", mime="application/json"
        )
