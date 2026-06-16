"""
Configurable LLM and embedder factory.

Supports OpenAI-compatible (including Mammouth), Ollama, VertexAI, and Anthropic
providers.

Per-stage model selection (LUC-51): use cheaper model for summarization/categorization,
stronger model for report generation. Set LLM_SUMMARY_PROVIDER/MODEL and
LLM_REPORT_PROVIDER/MODEL; fall back to LLM_PROVIDER/MODEL when unset.

API key resolution order (for OpenAI-compatible providers):
1. ``LLM_API_KEY`` environment variable
2. macOS Keychain (keyring)
3. ``OPENAI_API_KEY`` environment variable
4. If none found: automatic fallback to Ollama

API key resolution order (for Anthropic provider):
1. ``ANTHROPIC_API_KEY`` environment variable
2. macOS Keychain (tries common service/account pairs, including
   various service/account pairs)

Environment variables:

  LLM_PROVIDER         openai | ollama | vertexai | anthropic (default: openai)
  LLM_MODEL           Model name                   (default: gpt-5-nano for openai)
  LLM_SUMMARY_PROVIDER / LLM_SUMMARY_MODEL  Override for summarization stage (cheaper)
  LLM_REPORT_PROVIDER / LLM_REPORT_MODEL    Override for report stage (stronger)
  LLM_BASE_URL        Custom base URL               (default: https://api.mammouth.ai/v1)
  LLM_API_KEY         API key (for OpenAI-compatible providers)
  ANTHROPIC_API_KEY   API key for Anthropic provider
  ANTHROPIC_MAX_TOKENS  Max tokens for Anthropic responses (default: 8192; max 64000 for Sonnet 4.5)
  EMBEDDING_PROVIDER   openai | ollama | vertexai   (default: openai)
  EMBEDDING_MODEL      Embedding model name         (default: text-embedding-ada-002)
  OLLAMA_BASE_URL      Ollama server URL            (default: http://localhost:11434)
"""

import os
import subprocess
import time
import warnings
from typing import Literal

MAMMOUTH_BASE_URL = "https://api.mammouth.ai/v1"
OLLAMA_DEFAULT_URL = "http://localhost:11434"

# Default model for OpenAI-compatible API (Mammouth, etc.) when LLM_MODEL is unset.
OPENAI_COMPAT_DEFAULT_MODEL = "gpt-5-nano"

# Mammouth lists some Gemini ids on /public/models that fail on /v1/chat/completions.
_MAMMOUTH_UNSUPPORTED_CHAT_PREFIXES = ("gemini-2.5", "gemini-2.0-flash")


def resolve_mammouth_chat_model(model: str, *, quiet: bool = False) -> str:
    """Map public-catalog ids to a Mammouth chat-compatible model when needed."""
    if any(model.startswith(p) for p in _MAMMOUTH_UNSUPPORTED_CHAT_PREFIXES):
        if not quiet:
            warnings.warn(
                f"LLM_MODEL={model!r} is not supported for Mammouth chat; "
                f"using {OPENAI_COMPAT_DEFAULT_MODEL}. "
                "Pick another model from the Report dropdown.",
                stacklevel=2,
            )
        return OPENAI_COMPAT_DEFAULT_MODEL
    return model


# Keyring service/account for LLM API keys
_KEYRING_SERVICE = "agent-fleet-rts"
_KEYRING_ACCOUNT = "mammouth_api_key"
_ANTHROPIC_KEYRING_LOOKUPS = (
    ("agent-fleet-rts", "Anthropic"),
    ("agent-fleet-rts", "anthropic"),
    ("agent-fleet-rts", "anthropic_api_key"),
    ("Anthropic", "Anthropic"),
)


def _resolve_api_key(quiet=False):
    """Try to find an OpenAI-compatible API key.

    Returns (api_key, source_description) or (None, None).
    """
    # 1. Explicit env var
    key = os.getenv("LLM_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from LLM_API_KEY env var")
        return key, "LLM_API_KEY env var"

    # 2. macOS Keychain
    try:
        import keyring

        key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        if key:
            if not quiet:
                print(
                    f"  Using API key from keyring "
                    f"(service={_KEYRING_SERVICE!r}, account={_KEYRING_ACCOUNT!r})"
                )
            return key, "macOS Keychain"
    except Exception as exc:
        if not quiet:
            warnings.warn(f"Keyring lookup failed: {exc}", stacklevel=3)

    # 3. OPENAI_API_KEY env var (standard OpenAI SDK default)
    key = os.getenv("OPENAI_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from OPENAI_API_KEY env var")
        return key, "OPENAI_API_KEY env var"

    return None, None


def _resolve_anthropic_api_key(quiet=False):
    """Try to find an Anthropic API key.

    Returns (api_key, source_description) or (None, None).
    """
    # 1. Explicit env var
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        if not quiet:
            print("  Using API key from ANTHROPIC_API_KEY env var")
        return key, "ANTHROPIC_API_KEY env var"

    # 2. macOS Keychain (try common service/account conventions)
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


def _ensure_ollama_running(base_url=None):
    """Start Ollama server if it's not already running. Returns True if reachable."""
    import urllib.request
    import urllib.error

    url = base_url or OLLAMA_DEFAULT_URL
    # Check if already running
    try:
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        pass

    # Try to start it
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

    # Wait for it to come up
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
    """Resolve provider and model for a pipeline stage. Fallback to global vars."""
    prefix = f"LLM_{stage.upper()}_" if stage else "LLM_"
    provider = os.getenv(f"{prefix}PROVIDER") or os.getenv("LLM_PROVIDER") or "openai"
    # Per-provider defaults when LLM_*_MODEL and LLM_MODEL are unset (openai → gpt-5-nano below).
    defaults: dict[str, str] = {
        "ollama": OLLAMA_DEFAULT_LLM_MODEL,
        "vertexai": "gemini-1.5-pro",
        "anthropic": "claude-sonnet-4-5",
    }
    model = (
        os.getenv(f"{prefix}MODEL")
        or os.getenv("LLM_MODEL")
        or defaults.get(provider)
        or OPENAI_COMPAT_DEFAULT_MODEL
    )
    return provider, model


def get_default_provider_model(stage: Literal["summary", "report"]) -> tuple[str, str]:
    """Default provider and model for UI dropdowns. Provider: ollama, anthropic, mammouth."""
    provider, model = _resolve_provider_model(stage)
    if provider == "openai":
        provider = "mammouth"
    return provider, model


def get_report_model_id(
    provider_override: str | None = None,
    model_override: str | None = None,
) -> str:
    """Report stage model identifier for cache invalidation. Format: provider:model."""
    if provider_override and model_override:
        return f"{provider_override}:{model_override}"
    provider, model = _resolve_provider_model("report")
    return f"{provider}:{model}"


def create_llm(
    quiet: bool = False,
    json_mode: bool = True,
    stage: Literal["summary", "report"] | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
):
    """Create LLM instance based on LLM_PROVIDER (or stage-specific) env var.

    If provider is ``openai`` but no API key is found, falls back to Ollama
    automatically (starting the server if needed). Anthropic provider requires
    ``ANTHROPIC_API_KEY`` or a configured keyring entry.

    Args:
        quiet: Suppress log output.
        json_mode: If True (default), set response_format to json_object for JSON
            output (e.g. summarize_posts). If False, no response_format so the LLM
            can return plain text (e.g. report generation). Some providers (e.g.
            Mammouth) require the prompt to mention "json" when json_mode is True.
        stage: "summary" for categorization/short summary (cheaper model),
            "report" for report generation (stronger model). Uses LLM_SUMMARY_* /
            LLM_REPORT_* env vars when set; else LLM_PROVIDER/MODEL.
        provider_override: Override provider (ollama, anthropic, mammouth).
        model_override: Override model name. Used when provider_override is set.
    """
    if provider_override and model_override:
        provider = "openai" if provider_override == "mammouth" else provider_override
        model = model_override
    else:
        provider, model = _resolve_provider_model(stage)

    if provider == "openai":
        api_key, _ = _resolve_api_key(quiet=quiet)

        if not api_key:
            print(
                "  No OpenAI-compatible API key found. Tried:\n"
                "    1. LLM_API_KEY env var\n"
                "    2. macOS Keychain (agent-fleet-rts / mammouth_api_key)\n"
                "    3. OPENAI_API_KEY env var\n"
                "  Falling back to Ollama..."
            )
            return _create_ollama_llm(quiet=quiet, is_fallback=True)

        base_url = (
            MAMMOUTH_BASE_URL
            if provider_override == "mammouth"
            else os.getenv("LLM_BASE_URL", MAMMOUTH_BASE_URL)
        )
        if (
            MAMMOUTH_BASE_URL in (base_url or "").split("?")[0]
            and provider_override == "mammouth"
        ):
            model = resolve_mammouth_chat_model(model, quiet=quiet)

        from neo4j_graphrag.llm import OpenAILLM

        if not quiet:
            print(f"  LLM: OpenAI-compatible ({model} via {base_url})")

        model_params: dict[str, object] = {"temperature": 0}
        if json_mode:
            model_params["response_format"] = {"type": "json_object"}
        return OpenAILLM(
            model_name=model,
            model_params=model_params,
            api_key=api_key,
            base_url=base_url,
        )
    elif provider == "ollama":
        return _create_ollama_llm(quiet=quiet, model_override=model)
    elif provider == "vertexai":
        from neo4j_graphrag.llm import VertexAILLM

        if not quiet:
            print(f"  LLM: VertexAI ({model})")
        return VertexAILLM(model_name=model)
    elif provider == "anthropic":
        api_key, _ = _resolve_anthropic_api_key(quiet=quiet)
        if not api_key:
            raise RuntimeError(
                "No Anthropic API key found. Tried:\n"
                "  1. ANTHROPIC_API_KEY env var\n"
                "  2. macOS Keychain (agent-fleet-rts/Anthropic, "
                "agent-fleet-rts/anthropic)\n"
                "Set ANTHROPIC_API_KEY or add keyring entry."
            )

        from neo4j_graphrag.llm import AnthropicLLM

        try:
            max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192"))
        except ValueError:
            max_tokens = 8192
        _HAIKU_MAX_TOKENS: dict[str, int] = {
            "claude-3-haiku-20240307": 4096,
            "claude-3-5-haiku": 8000,
        }
        for pattern, cap in _HAIKU_MAX_TOKENS.items():
            if pattern in model.lower():
                max_tokens = min(max_tokens, cap)
                break
        if not quiet:
            print(f"  LLM: Anthropic ({model}, max_tokens={max_tokens})")
        model_params: dict[str, object] = {"max_tokens": max_tokens}
        return AnthropicLLM(
            model_name=model,
            model_params=model_params,
            api_key=api_key,
        )
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")


OLLAMA_DEFAULT_LLM_MODEL = "llama3.2:3b"
OLLAMA_DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


def _create_ollama_llm(
    quiet: bool = False,
    is_fallback: bool = False,
    model_override: str | None = None,
):
    """Create an Ollama LLM, starting the server if needed."""
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

    from neo4j_graphrag.llm import OllamaLLM

    if not quiet:
        print(f"  LLM: Ollama ({model} at {base_url})")

    # Use `host=` not `base_url=` — ollama.Client expects `host` and
    # passes **kwargs through to httpx.Client which already gets base_url.
    return OllamaLLM(model_name=model or OLLAMA_DEFAULT_LLM_MODEL, host=base_url)


def create_embedder(quiet=False):
    """Create embedder instance based on EMBEDDING_PROVIDER env var.

    Falls back to Ollama if no API key is available for OpenAI-compatible providers.
    """
    provider = os.getenv("EMBEDDING_PROVIDER", "openai")

    if provider == "openai":
        api_key, source = _resolve_api_key(quiet=True)

        if not api_key:
            if not quiet:
                print("  No API key for embeddings, falling back to Ollama embedder")
            return _create_ollama_embedder(quiet=quiet, is_fallback=True)

        base_url = os.getenv("LLM_BASE_URL", MAMMOUTH_BASE_URL)

        from neo4j_graphrag.embeddings.openai import OpenAIEmbeddings

        model = os.getenv("EMBEDDING_MODEL", "text-embedding-ada-002")
        if not quiet:
            print(f"  Embedder: OpenAI-compatible ({model} via {base_url})")

        return OpenAIEmbeddings(model=model, api_key=api_key, base_url=base_url)
    elif provider == "ollama":
        return _create_ollama_embedder(quiet=quiet)
    elif provider == "vertexai":
        from neo4j_graphrag.embeddings.vertexai import VertexAIEmbeddings

        model = os.getenv("EMBEDDING_MODEL", "textembedding-gecko@002")
        if not quiet:
            print(f"  Embedder: VertexAI ({model})")
        return VertexAIEmbeddings(model=model)
    else:
        raise ValueError(f"Unknown EMBEDDING_PROVIDER: {provider!r}")


def _create_ollama_embedder(quiet=False, is_fallback=False):
    """Create an Ollama embedder, starting the server if needed."""
    base_url = os.getenv("OLLAMA_BASE_URL", OLLAMA_DEFAULT_URL)
    # When falling back from OpenAI, ignore EMBEDDING_MODEL (e.g. "[REDACTED]")
    model = (
        OLLAMA_DEFAULT_EMBEDDING_MODEL
        if is_fallback
        else os.getenv("EMBEDDING_MODEL", OLLAMA_DEFAULT_EMBEDDING_MODEL)
    )

    if not _ensure_ollama_running(base_url):
        raise RuntimeError(
            "Cannot connect to Ollama for embeddings. Install from https://ollama.com "
            "or set LLM_API_KEY / MAMMOUTH_API_KEY in keyring."
        )

    from neo4j_graphrag.embeddings import OllamaEmbeddings

    if not quiet:
        print(f"  Embedder: Ollama ({model} at {base_url})")

    # Use `host=` not `base_url=` — same ollama.Client bug as LLM above.
    return OllamaEmbeddings(model=model, host=base_url)
