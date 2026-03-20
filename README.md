# DocLens — AI Documentation Analyzer

An AI-powered tool that crawls documentation websites, extracts structured product modules and submodules, and generates clean JSON output. Built with multi-LLM support, crawl caching, diff mode, and competitor analysis.

![Python](https://img.shields.io/badge/Python-3.12+-blue) ![Streamlit](https://img.shields.io/badge/Streamlit-1.32+-red) ![Pydantic](https://img.shields.io/badge/Pydantic-v2-green)

---

## What it does

Point DocLens at any documentation website and it returns a structured tree of modules and submodules:
```json
[
  {
    "module": "Authentication",
    "Description": "Handles user login, registration, and session management.",
    "Submodules": {
      "SSO": "Single sign-on integration with OAuth providers.",
      "MFA": "Multi-factor authentication setup and management."
    }
  }
]
```

---

## Features

- **Intelligent crawling** — respects robots.txt, retries on rate limits, normalizes URLs to avoid duplicates
- **SQLite cache** — pages cached for 24h, second run is instant
- **Multi-LLM** — OpenAI, Anthropic Claude, Google Gemini
- **Pydantic validation** — every LLM response validated, no silent failures
- **Diff mode** — compare two extraction runs, see what changed
- **Competitor analysis** — crawl two sites, get AI-generated comparison
- **CLI + Streamlit UI** — use from terminal or browser

---

## Project structure
```
DocLens/
├── app/
│   └── app.py              # Streamlit UI — extract, diff, compare tabs
├── scripts/
│   └── cli.py              # CLI interface
├── utils/
│   ├── crawler.py          # Web crawler with cache + robots.txt
│   ├── extractor.py        # LLM extraction with Pydantic validation
│   └── diff.py             # Diff engine for comparing extractions
├── requirements.txt
└── .env.example
```

---

## Setup
```bash
git clone https://github.com/CHANDU-M05/DocLens
cd DocLens
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add your API key to .env
streamlit run app/app.py
```

---

## CLI usage
```bash
# OpenAI
python scripts/cli.py --urls https://docs.example.com --output results.json

# Gemini (free tier)
python scripts/cli.py --urls https://docs.example.com --provider gemini

# Deep crawl, no cache
python scripts/cli.py --urls https://docs.example.com --max-depth 3 --no-cache
```

---

## Engineering decisions

**Why SQLite cache?**
Without it, every dev iteration re-crawls 100 pages. With it, second run is instant and target sites arent hammered. TTL of 24h prevents stale results.

**Why Pydantic for LLM responses?**
GPT sometimes wraps JSON in markdown fences, returns lists instead of objects, or hallucinates extra fields. Pydantic catches all of this at the schema level. On failure: log and skip, never silently return empty.

**Why robots.txt + Retry-After?**
Without robots.txt compliance, you crawl paths site owners disallow and risk IP bans. Without Retry-After, you hammer a rate-limiting server. Both are table stakes for any production web crawler.

**Why a LLMProvider abstraction?**
Every project that hardcodes one provider is immediately obsolete. A thin base class costs 20 lines and means you can swap providers without touching extraction logic.

---

## Built by

Chandu — [GitHub](https://github.com/CHANDU-M05)
