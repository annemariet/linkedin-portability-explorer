"""Lightweight LLM client wrappers (no neo4j-graphrag dependency)."""

from __future__ import annotations

import os
import subprocess
import time
import warnings
from dataclasses import dataclass
from typing import Any, Literal, cast

MAMMOUTH_BASE_URL = "https://api.mammouth.ai/v1"
OLLAMA_DEFAULT_URL = "http://localhost:11434"
OPENAI_COMPAT_DEFAULT_MODEL = "gpt-5.4-nano"
_MAMMOUTH_UNSUPPORTED_CHAT_PREFIXES = ("gemini-2.5", "gemini-2.0-flash")
# Mammouth public catalog may still list these; chat/completions rejects them.
_MAMMOUTH_DEPRECATED_CHAT_MODELS: dict[str, str] = {
    "gpt-5-nano": OPENAI_COMPAT_DEFAULT_MODEL,
}

_KEYRING_SERVICES = ("lucys-foundry", "agent-fleet-rts")
_MAMMOUTH_KEYRING_ACCOUNTS = ("mammouth", "mammouth_api_key", "openai")
_ANTHROPIC_KEYRING_LOOKUPS = (
    ("agent-fleet-rts", "Anthropic"),
    ("agent-fleet-rts", "anthropic"),
    ("agent-fleet-rts", "anthropic_api_key"),
    ("Anthropic", "Anthropic"),
)

OLLAMA_DEFAULT_LLM_MODEL = "llama3.2:3b"


@dataclass
class LLMResponse:
    content: str


class LLMClient:
    """Minimal invoke() interface used by summarize_posts and pipeline_report."""

    def invoke(
        self, user_prompt: str, system_instruction: str | None = None
    ) -> LLMResponse:
        raise NotImplementedError


class OpenAICompatLLM(LLMClient):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        json_mode: bool,
    ) -> None:
        from openai import OpenAI

        self._model = model
        self._json_mode = json_mode
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def invoke(
        self, user_prompt: str, system_instruction: str | None = None
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": user_prompt})
        typed_messages = cast(Any, messages)
        if self._json_mode:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=typed_messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
        else:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=typed_messages,
                temperature=0,
            )
        content = response.choices[0].message.content or ""
        return LLMResponse(content=content)


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        max_tokens: int,
    ) -> None:
        from anthropic import Anthropic

        self._model = model
        self._max_tokens = max_tokens
        self._client = Anthropic(api_key=api_key)

    def invoke(
        self, user_prompt: str, system_instruction: str | None = None
    ) -> LLMResponse:
        if system_instruction:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_instruction,
                messages=[{"role": "user", "content": user_prompt}],
            )
        else:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": user_prompt}],
            )
        parts = [
            block.text
            for block in response.content
            if hasattr(block, "text") and block.text
        ]
        return LLMResponse(content="".join(parts))


class OllamaLLMClient(LLMClient):
    def __init__(self, *, model: str, host: str) -> None:
        import ollama

        self._model = model
        self._client = ollama.Client(host=host)

    def invoke(
        self, user_prompt: str, system_instruction: str | None = None
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": user_prompt})
        response = self._client.chat(model=self._model, messages=messages)
        content = response.message.content if response.message else ""
        return LLMResponse(content=content or "")


def resolve_mammouth_chat_model(model: str, *, quiet: bool = False) -> str:
    stripped = (model or "").strip()
    replacement = _MAMMOUTH_DEPRECATED_CHAT_MODELS.get(stripped)
    if replacement:
        if not quiet:
            warnings.warn(
                f"LLM model {stripped!r} is not available for Mammouth chat; "
                f"using {replacement}.",
                stacklevel=2,
            )
        return replacement
    if any(stripped.startswith(p) for p in _MAMMOUTH_UNSUPPORTED_CHAT_PREFIXES):
        if not quiet:
            warnings.warn(
                f"LLM_MODEL={stripped!r} is not supported for Mammouth chat; "
                f"using {OPENAI_COMPAT_DEFAULT_MODEL}.",
                stacklevel=2,
            )
        return OPENAI_COMPAT_DEFAULT_MODEL
    return stripped


def _resolve_api_key(quiet: bool = False) -> tuple[str | None, str | None]:
    key = os.getenv("MAMMOUTH_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from MAMMOUTH_API_KEY env var")
        return key, "MAMMOUTH_API_KEY env var"

    key = os.getenv("LLM_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from LLM_API_KEY env var")
        return key, "LLM_API_KEY env var"

    try:
        import keyring

        for service in _KEYRING_SERVICES:
            for account in _MAMMOUTH_KEYRING_ACCOUNTS:
                key = keyring.get_password(service, account)
                if key:
                    if not quiet:
                        print(
                            f"  Using API key from keyring "
                            f"(service={service!r}, account={account!r})"
                        )
                    return key, "macOS Keychain"
    except Exception as exc:
        if not quiet:
            warnings.warn(f"Keyring lookup failed: {exc}", stacklevel=3)

    key = os.getenv("OPENAI_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from OPENAI_API_KEY env var")
        return key, "OPENAI_API_KEY env var"

    return None, None


def _resolve_anthropic_api_key(quiet: bool = False) -> tuple[str | None, str | None]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from ANTHROPIC_API_KEY env var")
        return key, "ANTHROPIC_API_KEY env var"

    try:
        import keyring

        for service, account in _ANTHROPIC_KEYRING_LOOKUPS:
            key = keyring.get_password(service, account)
            if key:
                if not quiet:
                    print(
                        f"  Using Anthropic API key from keyring "
                        f"(service={service!r}, account={account!r})"
                    )
                return key, f"macOS Keychain ({service}/{account})"
    except Exception as exc:
        if not quiet:
            warnings.warn(f"Keyring lookup failed: {exc}", stacklevel=3)

    return None, None


def _ensure_ollama_running(base_url: str | None = None) -> bool:
    import urllib.error
    import urllib.request

    url = base_url or OLLAMA_DEFAULT_URL
    try:
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        pass

    print("  Starting Ollama server...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("  Ollama is not installed. Install it from https://ollama.com")
        return False

    for _ in range(10):
        time.sleep(1)
        try:
            req = urllib.request.Request(url, method="GET")
            urllib.request.urlopen(req, timeout=2)
            print("  Ollama server started successfully")
            return True
        except (urllib.error.URLError, OSError):
            continue

    print("  Ollama server did not start in time")
    return False


def _resolve_provider_model(
    stage: Literal["summary", "report"] | None,
) -> tuple[str, str]:
    prefix = f"LLM_{stage.upper()}_" if stage else "LLM_"
    provider = os.getenv(f"{prefix}PROVIDER") or os.getenv("LLM_PROVIDER") or "openai"
    defaults: dict[str, str] = {
        "ollama": OLLAMA_DEFAULT_LLM_MODEL,
        "anthropic": "claude-sonnet-4-5",
    }
    model = (
        os.getenv(f"{prefix}MODEL")
        or os.getenv("LLM_MODEL")
        or os.getenv("MAMMOUTH_MODEL")
        or defaults.get(provider)
        or OPENAI_COMPAT_DEFAULT_MODEL
    )
    if provider in ("openai", "mammouth"):
        model = resolve_mammouth_chat_model(model, quiet=True)
    return provider, model


def get_default_provider_model(stage: Literal["summary", "report"]) -> tuple[str, str]:
    provider, model = _resolve_provider_model(stage)
    if provider == "openai":
        provider = "mammouth"
    return provider, model


def get_summary_model_id(
    provider_override: str | None = None,
    model_override: str | None = None,
) -> str:
    if provider_override and model_override:
        provider = provider_override
        model = model_override
    else:
        provider, model = _resolve_provider_model("summary")
    if provider in ("openai", "mammouth"):
        model = resolve_mammouth_chat_model(model, quiet=True)
        provider = "mammouth"
    return f"{provider}:{model}"


def get_report_model_id(
    provider_override: str | None = None,
    model_override: str | None = None,
) -> str:
    if provider_override and model_override:
        provider = provider_override
        model = model_override
    else:
        provider, model = _resolve_provider_model("report")
    if provider == "mammouth":
        model = resolve_mammouth_chat_model(model, quiet=True)
    return f"{provider}:{model}"


def create_llm(
    quiet: bool = False,
    json_mode: bool = True,
    stage: Literal["summary", "report"] | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> LLMClient:
    if provider_override and model_override:
        provider = "openai" if provider_override == "mammouth" else provider_override
        model = model_override
    else:
        provider, model = _resolve_provider_model(stage)

    if provider == "openai" or provider == "mammouth":
        api_key, _ = _resolve_api_key(quiet=quiet)
        if not api_key:
            print("  No OpenAI-compatible API key found. Falling back to Ollama...")
            return _create_ollama_llm(
                quiet=quiet, is_fallback=True, json_mode=json_mode
            )

        use_mammouth = provider == "mammouth" or provider_override == "mammouth"
        base_url = (
            MAMMOUTH_BASE_URL
            if use_mammouth
            else os.getenv("LLM_BASE_URL", MAMMOUTH_BASE_URL)
        )
        if MAMMOUTH_BASE_URL in (base_url or "").split("?")[0]:
            model = resolve_mammouth_chat_model(model, quiet=quiet)

        if not quiet:
            label = "Mammouth" if use_mammouth else "OpenAI-compatible"
            print(f"  LLM: {label} ({model} via {base_url})")
        return OpenAICompatLLM(
            model=model,
            api_key=api_key,
            base_url=base_url,
            json_mode=json_mode,
        )

    if provider == "ollama":
        return _create_ollama_llm(
            quiet=quiet, model_override=model, json_mode=json_mode
        )

    if provider == "anthropic":
        api_key, _ = _resolve_anthropic_api_key(quiet=quiet)
        if not api_key:
            raise RuntimeError(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY or keyring entry."
            )
        try:
            max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192"))
        except ValueError:
            max_tokens = 8192
        for pattern, cap in {
            "claude-3-haiku-20240307": 4096,
            "claude-3-5-haiku": 8000,
        }.items():
            if pattern in model.lower():
                max_tokens = min(max_tokens, cap)
                break
        if not quiet:
            print(f"  LLM: Anthropic ({model}, max_tokens={max_tokens})")
        return AnthropicLLMClient(model=model, api_key=api_key, max_tokens=max_tokens)

    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")


def _create_ollama_llm(
    quiet: bool = False,
    is_fallback: bool = False,
    model_override: str | None = None,
    json_mode: bool = True,
) -> LLMClient:
    del json_mode  # Ollama path ignores structured output flag
    base_url = os.getenv("OLLAMA_BASE_URL", OLLAMA_DEFAULT_URL)
    model = (
        OLLAMA_DEFAULT_LLM_MODEL
        if is_fallback
        else (model_override or os.getenv("LLM_MODEL", OLLAMA_DEFAULT_LLM_MODEL))
    ) or OLLAMA_DEFAULT_LLM_MODEL

    if not _ensure_ollama_running(base_url):
        raise RuntimeError(
            "Cannot connect to Ollama. Install from https://ollama.com "
            "or set LLM_API_KEY / MAMMOUTH_API_KEY in keyring."
        )

    if not quiet:
        print(f"  LLM: Ollama ({model} at {base_url})")
    return OllamaLLMClient(model=model, host=base_url)
