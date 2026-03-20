import os
import json
import logging
import time
import re
from typing import Optional
from collections import defaultdict
from pydantic import BaseModel, ValidationError, field_validator

logger = logging.getLogger(__name__)


class ModuleSchema(BaseModel):
    module: str
    Description: str
    Submodules: dict = {}

    @field_validator('Submodules', mode='before')
    @classmethod
    def coerce_submodules(cls, v):
        if isinstance(v, list):
            result = {}
            for item in v:
                if isinstance(item, dict):
                    name = item.get('name') or item.get('title') or str(item)
                    desc = item.get('description') or item.get('Description') or ''
                    result[name] = desc
            return result
        return v or {}


def _extract_json_from_text(text):
    text = re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`').strip()
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            candidate = text[start:end+1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
    return text


def _parse_module_response(text, module_title):
    json_str = _extract_json_from_text(text)
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            data = data[0] if data else {}
        if 'module' not in data:
            data['module'] = module_title
        return ModuleSchema.model_validate(data)
    except (json.JSONDecodeError, ValidationError, IndexError) as e:
        logger.error(f"Parse failed for '{module_title}': {e}")
        return None


def _parse_modules_list_response(text):
    json_str = _extract_json_from_text(text)
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            data = [data]
        modules = []
        for item in data:
            try:
                modules.append(ModuleSchema.model_validate(item))
            except ValidationError as e:
                logger.warning(f"Skipping invalid module: {e}")
        return modules
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode failed: {e}")
        return []


class LLMProvider:
    def complete(self, system, user, max_tokens=4000):
        raise NotImplementedError

    @property
    def name(self):
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key=None, model="gpt-3.5-turbo"):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model

    @property
    def name(self): return f"openai/{self.model}"

    def complete(self, system, user, max_tokens=4000):
        for attempt in range(3):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role":"system","content":system},{"role":"user","content":user}],
                    temperature=0.2,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                if "rate_limit" in str(e).lower():
                    wait = 2**attempt*5
                    logger.warning(f"Rate limit. Waiting {wait}s.")
                    time.sleep(wait)
                else:
                    logger.error(f"OpenAI error (attempt {attempt+1}/3): {e}")
                    time.sleep(2**attempt)
        return ""


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key=None, model="claude-3-haiku-20240307"):
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
            self.model = model
        except ImportError:
            raise ImportError("pip install anthropic to use AnthropicProvider")

    @property
    def name(self): return f"anthropic/{self.model}"

    def complete(self, system, user, max_tokens=4000):
        for attempt in range(3):
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=max_tokens,
                    system=system, messages=[{"role":"user","content":user}],
                )
                return resp.content[0].text.strip()
            except Exception as e:
                logger.error(f"Anthropic error (attempt {attempt+1}/3): {e}")
                time.sleep(2**attempt)
        return ""


class GeminiProvider(LLMProvider):
    def __init__(self, api_key=None, model="gemini-2.0-flash"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key or os.getenv("GEMINI_API_KEY"))
            self.model_obj = genai.GenerativeModel(model)
            self.model = model
        except ImportError:
            raise ImportError("pip install google-generativeai to use GeminiProvider")

    @property
    def name(self): return f"gemini/{self.model}"

    def complete(self, system, user, max_tokens=4000):
        for attempt in range(3):
            try:
                prompt = f"{system}\n\n{user}"
                resp = self.model_obj.generate_content(prompt)
                return resp.text.strip()
            except Exception as e:
                logger.error(f"Gemini error (attempt {attempt+1}/3): {e}")
                time.sleep(2**attempt)
        return ""


def get_provider(provider_name, api_key=None, model=None):
    if provider_name == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or "claude-3-haiku-20240307")
    if provider_name == "gemini":
        return GeminiProvider(api_key=api_key, model=model or "gemini-2.0-flash")
    return OpenAIProvider(api_key=api_key, model=model or "gpt-3.5-turbo")


class ModuleExtractor:
    def __init__(self, provider=None, api_key=None, model="gpt-3.5-turbo"):
        self.provider = provider or OpenAIProvider(api_key=api_key, model=model)
        logger.info(f"ModuleExtractor using: {self.provider.name}")

    SYSTEM_PROMPT = (
        "You are an expert at analyzing software documentation and extracting structured product information. "
        "Always respond with valid JSON only. No preamble, no markdown fences, no explanation."
    )

    def _chunk_text(self, text, max_tokens=6000):
        words = text.split()
        chunks, current, length = [], [], 0
        for word in words:
            wt = len(word)/0.75
            if length+wt > max_tokens and current:
                chunks.append(' '.join(current))
                current, length = [word], wt
            else:
                current.append(word)
                length += wt
        if current: chunks.append(' '.join(current))
        return chunks

    def _identify_potential_modules(self, hierarchy, titles, structure):
        potential_modules = {}
        entry_points = set(hierarchy.keys())
        all_children = set(c for children in hierarchy.values() for c in children)
        root_urls = entry_points - all_children
        url_depth = {}
        for root in root_urls:
            url_depth[root] = 0
            self._calculate_depth(root, hierarchy, url_depth, 0)
        urls_by_depth = defaultdict(list)
        for url, depth in url_depth.items():
            urls_by_depth[depth].append(url)
        for depth in [1, 0]:
            if urls_by_depth[depth]:
                for url in urls_by_depth[depth]:
                    if url in titles:
                        potential_modules[url] = {"title": titles[url], "child_urls": hierarchy.get(url, []), "source": "hierarchy"}
                break
        for url, page_structure in structure.items():
            if "headings" in page_structure and page_structure["headings"]:
                headings_by_level = defaultdict(list)
                for h in page_structure["headings"]:
                    headings_by_level[h["level"]].append(h)
                for level in range(1, 4):
                    level_headings = headings_by_level[level]
                    if len(level_headings) >= 2:
                        for heading in level_headings:
                            module_id = f"{url}#{heading['id']}" if heading['id'] else f"{url}#{heading['text']}"
                            potential_modules[module_id] = {"title": heading["text"], "url": url, "heading_level": level, "source": "heading"}
                        break
        return potential_modules

    def _calculate_depth(self, url, hierarchy, url_depth, depth):
        for child in hierarchy.get(url, []):
            if child not in url_depth or depth+1 < url_depth[child]:
                url_depth[child] = depth+1
                self._calculate_depth(child, hierarchy, url_depth, depth+1)

    def _extract_module_with_submodules(self, module_title, content):
        prompt = f"""Analyze this documentation for the module '{module_title}'.

CONTENT:
{content}

Return ONLY this JSON:
{{
  "module": "{module_title}",
  "Description": "detailed description",
  "Submodules": {{
    "Submodule Name": "description"
  }}
}}"""
        raw = self.provider.complete(self.SYSTEM_PROMPT, prompt)
        if not raw:
            return None
        return _parse_module_response(raw, module_title)

    def _extract_from_chunk(self, content):
        prompt = f"""Analyze this documentation. Identify key modules and submodules.

CONTENT:
{content}

Return ONLY a JSON array:
[
  {{
    "module": "Module Name",
    "Description": "what this module does",
    "Submodules": {{
      "Feature": "description"
    }}
  }}
]"""
        raw = self.provider.complete(self.SYSTEM_PROMPT, prompt)
        if not raw:
            return []
        return _parse_modules_list_response(raw)

    def _merge_module_results(self, results):
        if not results: return None
        merged = results[0].model_copy(deep=True)
        for r in results[1:]:
            if len(r.Description) > len(merged.Description):
                merged.Description = r.Description
            for name, desc in r.Submodules.items():
                if name not in merged.Submodules or len(desc) > len(merged.Submodules[name]):
                    merged.Submodules[name] = desc
        return merged

    def _merge_modules(self, all_modules):
        merged = {}
        for m in all_modules:
            if m.module not in merged:
                merged[m.module] = m.model_copy(deep=True)
            else:
                existing = merged[m.module]
                if len(m.Description) > len(existing.Description):
                    existing.Description = m.Description
                for name, desc in m.Submodules.items():
                    if name not in existing.Submodules or len(desc) > len(existing.Submodules[name]):
                        existing.Submodules[name] = desc
        return list(merged.values())

    def extract_modules(self, crawl_results):
        content_map = crawl_results["content"]
        hierarchy = crawl_results["hierarchy"]
        titles = crawl_results["titles"]
        structure = crawl_results["structure"]

        potential_modules = self._identify_potential_modules(hierarchy, titles, structure)
        if not potential_modules:
            logger.info("No structure found. Processing all content.")
            return self._extract_from_unstructured(content_map)

        all_modules = []
        for module_id, module_info in potential_modules.items():
            module_title = module_info["title"]
            source_type = module_info.get("source", "unknown")
            logger.info(f"Processing: {module_title} ({source_type})")

            if source_type == "hierarchy":
                main_content = content_map.get(module_id, "")
                child_content = "\n\n".join(
                    f"--- {url.split('/')[-1]} ---\n{content_map[url]}"
                    for url in module_info.get("child_urls", []) if url in content_map
                )
                combined = f"MODULE: {module_title}\n\nMAIN:\n{main_content}\n\nSUBMODULES:\n{child_content}"
            else:
                combined = f"MODULE: {module_title}\n\nCONTENT:\n{module_info.get('main_content','')}"

            chunks = self._chunk_text(combined)
            chunk_results = []
            for i, chunk in enumerate(chunks):
                result = self._extract_module_with_submodules(module_title, chunk)
                if result: chunk_results.append(result)
                if i < len(chunks)-1: time.sleep(1)

            merged = self._merge_module_results(chunk_results)
            if merged: all_modules.append(merged)

        return [m.model_dump() for m in all_modules]

    def _extract_from_unstructured(self, content_map):
        all_content = "\n\n".join(f"URL: {url}\nCONTENT:\n{content}" for url, content in content_map.items())
        chunks = self._chunk_text(all_content)
        all_modules = []
        for i, chunk in enumerate(chunks):
            logger.info(f"Processing chunk {i+1}/{len(chunks)}")
            results = self._extract_from_chunk(chunk)
            all_modules.extend(results)
            if i < len(chunks)-1: time.sleep(1)
        return [m.model_dump() for m in self._merge_modules(all_modules)]
