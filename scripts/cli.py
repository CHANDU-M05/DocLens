#!/usr/bin/env python3
"""
DocLens CLI — extract modules from documentation websites.

Usage:
    python scripts/cli.py --urls https://docs.example.com --output results.json
    python scripts/cli.py --urls https://docs.example.com --provider gemini
    python scripts/cli.py --urls https://docs.example.com --provider anthropic --model claude-3-haiku-20240307
"""

import argparse
import json
import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.crawler import Crawler
from utils.extractor import ModuleExtractor, get_provider

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('doclens.log')]
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Extract modules from documentation websites")
    parser.add_argument("--urls", nargs="+", required=True)
    parser.add_argument("--output", default="extracted_modules.json")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--provider", choices=["openai", "anthropic", "gemini"], default="openai")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--api-key", type=str)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--save-structure", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()

    valid_urls = [u.strip() for u in args.urls if u.strip().startswith(("http://","https://"))]
    invalid_urls = [u for u in args.urls if not u.strip().startswith(("http://","https://"))]

    if invalid_urls:
        logger.error(f"Invalid URLs: {invalid_urls}")
        sys.exit(1)
    if not valid_urls:
        logger.error("No valid URLs.")
        sys.exit(1)

    env_keys = {"openai":"OPENAI_API_KEY","anthropic":"ANTHROPIC_API_KEY","gemini":"GEMINI_API_KEY"}
    api_key = args.api_key or os.getenv(env_keys[args.provider])
    if not api_key:
        logger.error(f"API key not found. Set {env_keys[args.provider]} or use --api-key.")
        sys.exit(1)

    try:
        provider = get_provider(args.provider, api_key=api_key, model=args.model)
        logger.info(f"Provider: {provider.name} | Depth: {args.max_depth} | Cache: {'off' if args.no_cache else 'on'}")

        crawler = Crawler(max_pages=args.max_pages, delay=args.delay,
                         max_depth=args.max_depth, use_cache=not args.no_cache)
        extractor = ModuleExtractor(provider=provider)

        all_results = {"content":{},"hierarchy":{},"titles":{},"depths":{},"metadata":{},"structure":{}}
        for url in valid_urls:
            logger.info(f"Crawling: {url}")
            result = crawler.crawl(url)
            for key in all_results:
                all_results[key].update(result[key])

        if args.save_structure:
            base = Path(args.output).stem
            with open(f"{base}_structure.json", 'w') as f:
                json.dump({k: all_results[k] for k in ["hierarchy","titles","depths"]}, f, indent=2)

        logger.info("Extracting modules...")
        modules = extractor.extract_modules(all_results)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(modules, f, indent=2)
        logger.info(f"Saved to {output_path}")

        if modules:
            total_sub = sum(len(m.get('Submodules',{})) for m in modules)
            logger.info(f"{len(modules)} modules, {total_sub} submodules total.")
            for m in modules:
                logger.info(f"  - {m['module']} ({len(m.get('Submodules',{}))} submodules)")
        else:
            logger.warning("No modules extracted.")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
