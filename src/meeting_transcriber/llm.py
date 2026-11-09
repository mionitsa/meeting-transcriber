"""One function, `complete()`, that sends a prompt to whichever LLM the config names.

Most gateways speak the OpenAI chat-completions protocol, so they share one code
path and differ only in base URL and key variable. Anthropic has its own API, and
Vertex AI goes through Google's SDK because it authenticates with a service
account rather than a key.

    llm:
      provider: openrouter          # any key of PROVIDERS, or anthropic / vertex
      model: google/gemini-2.5-flash
      # base_url: https://...       # override, or required for openai-compatible
      # api_key_env: MY_KEY_VAR     # read the key from a different variable
      # max_tokens: 8192            # raise if long summaries come back cut off

Used as a library, `cfg["call"]` may instead be any `call(prompt, model) -> str`,
so a host application sends the prompts through its own client.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .common import Error, log

# provider -> (base URL, env var holding the key, default max_tokens or None)
PROVIDERS = {
    "openai":     ("https://api.openai.com/v1", "OPENAI_API_KEY", None),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", None),
    "fireworks":  ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", 8192),
    "together":   ("https://api.together.xyz/v1", "TOGETHER_API_KEY", 8192),
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY", None),
    "deepseek":   ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", 8192),
    "mistral":    ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", None),
    "xai":        ("https://api.x.ai/v1", "XAI_API_KEY", None),
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai",
                   "GEMINI_API_KEY", None),
    "ollama":     ("http://localhost:11434/v1", None, None),
    # LiteLLM, vLLM, LM Studio, a company gateway: anything OpenAI-compatible
    "openai-compatible": (None, "LLM_API_KEY", None),
}
NATIVE = ("anthropic", "vertex")
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
TIMEOUT = 600  # seconds; summarising a long meeting can take minutes


def complete(cfg: dict, prompt: str, model: str | None = None) -> str:
    """Send one user message, return the reply text."""
    model = model or cfg.get("model")
    if callable(cfg.get("call")):
        return cfg["call"](prompt, model)
    provider = str(cfg.get("provider") or "").lower()
    if not provider or not model:
        raise Error("set llm.provider and llm.model in config.yaml (see `transcribe init`)")
    log.debug("LLM request: %s / %s, %d chars", provider, model, len(prompt))
    started = time.monotonic()
    if provider == "anthropic":
        text = _anthropic(cfg, model, prompt)
    elif provider == "vertex":
        text = _vertex(cfg, model, prompt)
    elif provider in PROVIDERS:
        text = _openai_compatible(cfg, provider, model, prompt)
    else:
        raise Error(f"unknown llm.provider {provider!r}. "
                    f"Use one of: {', '.join([*PROVIDERS, *NATIVE])}")
    log.debug("LLM reply: %d chars in %.1fs", len(text), time.monotonic() - started)
    return text


def key_env(provider: str | None) -> str | None:
    """The env var holding this provider's key (None: no key, or not a key)."""
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    return PROVIDERS.get(provider or "", (None, None, None))[1]


def _key(cfg: dict, default_env: str | None) -> str | None:
    env = cfg.get("api_key_env") or default_env
    if not env:
        return None
    key = os.environ.get(env)
    if not key:
        raise Error(f"{env} is not set. Put it in the .env file (see `transcribe init`).")
    return key


def _post(url: str, headers: dict, body: dict) -> dict:
    """POST JSON with retries on rate limits, overload and network errors."""
    import httpx

    for attempt in range(4):
        wait = 5 * 2 ** attempt
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=TIMEOUT)
        except httpx.TransportError as e:
            if attempt == 3:
                raise Error(f"could not reach {url}: {e}") from None
            log.warning("could not reach the LLM (%s), retrying in %ds", e, wait)
        else:
            if r.is_success:
                return r.json()
            if r.status_code not in RETRY_STATUS or attempt == 3:
                raise Error(f"LLM request failed: HTTP {r.status_code} from {url}\n"
                            f"{r.text[:800]}")
            log.warning("LLM returned HTTP %d, retrying in %ds", r.status_code, wait)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _openai_compatible(cfg: dict, provider: str, model: str, prompt: str) -> str:
    base, key_env, max_tokens = PROVIDERS[provider]
    base = (cfg.get("base_url") or base or "").rstrip("/")
    if not base:
        raise Error(f"llm.provider {provider} needs llm.base_url in config.yaml")
    headers = {"X-Title": "meeting-transcriber"}
    if key := _key(cfg, key_env):
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if mt := cfg.get("max_tokens") or max_tokens:
        body["max_tokens"] = int(mt)
    data = _post(f"{base}/chat/completions", headers, body)
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        raise Error(f"unexpected LLM response: {json.dumps(data)[:500]}") from None
    if choice.get("finish_reason") == "length":
        log.warning("the LLM reply was cut off at its token limit; "
                    "raise llm.max_tokens in config.yaml")
    return choice.get("message", {}).get("content") or ""


def _anthropic(cfg: dict, model: str, prompt: str) -> str:
    base = (cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")
    headers = {"x-api-key": _key(cfg, "ANTHROPIC_API_KEY"),
               "anthropic-version": "2023-06-01"}
    body = {"model": model, "max_tokens": int(cfg.get("max_tokens") or 16000),
            "messages": [{"role": "user", "content": prompt}]}
    data = _post(f"{base}/v1/messages", headers, body)
    if data.get("stop_reason") == "max_tokens":
        log.warning("the LLM reply was cut off at its token limit; "
                    "raise llm.max_tokens in config.yaml")
    return "".join(b.get("text", "") for b in data.get("content") or []
                   if b.get("type") == "text")


def _vertex(cfg: dict, model: str, prompt: str) -> str:
    """Vertex AI. Credentials come from GOOGLE_APPLICATION_CREDENTIALS (a service
    account key file) or, failing that, gcloud's application default credentials."""
    try:
        from google import genai
    except ImportError:
        raise Error("the vertex provider needs the vertex extra: "
                    "uv tool install 'meeting-transcriber[vertex]'") from None

    project = cfg.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    keyfile = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not project and keyfile and Path(keyfile).is_file():
        project = json.loads(Path(keyfile).read_text()).get("project_id")
    if not project:
        raise Error("vertex needs a project: set llm.project in config.yaml or "
                    "GOOGLE_CLOUD_PROJECT in .env")
    location = cfg.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"
    client = genai.Client(vertexai=True, project=project, location=location)
    for attempt in range(4):
        try:
            return client.models.generate_content(model=model, contents=prompt).text or ""
        except Exception as e:
            code = getattr(e, "code", None)
            if attempt < 3 and (code is None or code in RETRY_STATUS):
                wait = 5 * 2 ** attempt
                log.warning("Vertex AI request failed (%s), retrying in %ds", e, wait)
                time.sleep(wait)
                continue
            hint = (" A 403 usually means the credentials belong to an account without "
                    "access to this project, not that the model is missing."
                    if code == 403 or "403" in str(e) else "")
            raise Error(f"Vertex AI request failed: {e}{hint}") from None
    raise AssertionError("unreachable")
