"""
Fetch available models from LLM providers for UI selection.

Supports Ollama (local), Anthropic API, Mammouth API (OpenAI-compatible).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from linkedin_api.llm_config import (
    MAMMOUTH_BASE_URL,
    OLLAMA_DEFAULT_URL,
    _ensure_ollama_running,
    _resolve_anthropic_api_key,
)

logger = logging.getLogger(__name__)


def fetch_ollama_models(base_url: str | None = None) -> list[tuple[str, str]]:
    """List models available in Ollama. Returns (label, model_id); label same as id. Starts Ollama if needed."""
    base = base_url or OLLAMA_DEFAULT_URL
    if not _ensure_ollama_running(base):
        return []
    url = base.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        models = data.get("models", [])
        names = [m.get("name", "") for m in models if m.get("name")]
        return [(n, n) for n in names]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return []


def fetch_anthropic_models() -> list[tuple[str, str]]:
    """List models from Anthropic API. Returns (label, model_id); label same as id. Requires ANTHROPIC_API_KEY."""
    api_key, _ = _resolve_anthropic_api_key(quiet=True)
    if not api_key:
        return []
    url = "https://api.anthropic.com/v1/models"
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "anthropic-version": "2023-06-01",
                "x-api-key": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("data", [])
        ids = [m.get("id", "") for m in items if m.get("id")]
        return [(i, i) for i in ids]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return []


def _mammouth_owner_display(m: dict, mid: str) -> str:
    """Display owner from owned_by; if API returns 'openai' for all, infer from model id."""
    raw = (m.get("owned_by") or "").strip()
    if raw and raw.lower() != "openai":
        return raw
    mid_lower = mid.lower()
    if mid_lower.startswith("claude"):
        return "Anthropic"
    if mid_lower.startswith("gemini"):
        return "Google"
    if mid_lower.startswith("gpt") or mid_lower.startswith("text-embedding"):
        return "OpenAI"
    if mid_lower.startswith("grok"):
        return "xAI"
    if mid_lower.startswith("llama"):
        return "Meta"
    if mid_lower.startswith("mistral") or mid_lower.startswith("codestral"):
        return "Mistral"
    if mid_lower.startswith("deepseek"):
        return "DeepSeek"
    if mid_lower.startswith("qwen"):
        return "Alibaba"
    if mid_lower.startswith("kimi"):
        return "Moonshot"
    if mid_lower.startswith("sonar"):
        return "Perplexity"
    if mid_lower.startswith("glm"):
        return "Zhipu"
    return raw or "—"


def fetch_mammouth_models() -> list[tuple[str, str]]:
    """List models from Mammouth API GET /public/models. Returns (label, id) with owner and $/M."""
    base = os.getenv("LLM_BASE_URL", MAMMOUTH_BASE_URL).rstrip("/")
    api_root = base.removesuffix("/v1") if base.endswith("/v1") else base
    url = f"{api_root}/public/models"
    logger.info("Mammouth models: GET %s", url)
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; linkedin-portability/1.0)"
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("data", data.get("models", data.get("list", [])))
        if not isinstance(items, list) or not items:
            logger.warning("Mammouth models: empty or invalid data")
            return []
        out: list[tuple[str, str, float]] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("model_id") or m.get("name")
            if not mid:
                continue
            mid = str(mid)
            owner = _mammouth_owner_display(m, mid)
            info = m.get("model_info") or {}
            try:
                inc = float(info.get("input_cost_per_token") or 0) * 1e6
                outc = float(info.get("output_cost_per_token") or 0) * 1e6
                cost = (
                    f"${inc:.2f} / ${outc:.2f} per M (in/out)" if (inc or outc) else ""
                )
            except (TypeError, ValueError):
                inc, outc, cost = 0.0, 0.0, ""
            label = f"{mid} · {owner}" + (f" · {cost}" if cost else "")
            out.append((label, mid, inc))
        out.sort(key=lambda x: x[2])
        logger.info("Mammouth models: got %d models", len(out))
        return [(label, mid) for label, mid, _ in out]
    except urllib.error.HTTPError as e:
        logger.warning("Mammouth models: HTTP %s %s", e.code, e.reason)
        return []
    except (
        urllib.error.URLError,
        OSError,
        json.JSONDecodeError,
        KeyError,
    ) as e:
        logger.warning("Mammouth models: %s: %s", type(e).__name__, e)
        return []


def fetch_models_for_provider(provider: str) -> list[tuple[str, str]]:
    """Fetch model list for the given provider. Returns (label, model_id) for all; Mammouth uses rich labels."""
    if provider == "ollama":
        return fetch_ollama_models()
    if provider == "anthropic":
        return fetch_anthropic_models()
    if provider == "mammouth":
        return fetch_mammouth_models()
    return []


def fetch_all_provider_models() -> dict[str, list[tuple[str, str]]]:
    """Fetch models for all providers in parallel. Returns {provider: [(label, model_id)]}."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    providers = ["ollama", "anthropic", "mammouth"]
    result: dict[str, list[tuple[str, str]]] = {}

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_models_for_provider, p): p for p in providers}
        for fut in as_completed(futures):
            provider = futures[fut]
            try:
                models = fut.result()
                result[provider] = models if models else []
            except Exception:
                result[provider] = []
    return result
