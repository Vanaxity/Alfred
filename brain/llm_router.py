import asyncio
import time
import random
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum

from groq import Groq
from google import genai
from google.genai import types
from openai import OpenAI


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class Provider:
    name: str
    model: str
    client: Any
    priority: int
    api_key: Optional[str] = None
    base_timeout: float = 20.0
    timeout: Optional[float] = None
    min_timeout: float = 5.0
    ema_latency_ms: float = 0.0

    def __post_init__(self):
        if self.timeout is None:
            self.timeout = self.base_timeout


@dataclass
class CircuitBreaker:
    failure_threshold: int = 2
    recovery_timeout: float = 30.0
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    success_count: int = 0
    last_error: Optional[str] = None

    def record_success(self):
        self.failure_count = 0
        self.success_count += 1
        self.state = CircuitState.CLOSED

    def record_failure(self, error: str = ""):
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.last_error = error or None
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True


@dataclass
class LLMResponse:
    text: Optional[str] = None
    provider: Optional[str] = None
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    error: Optional[str] = None


class LLMRouter:
    def __init__(self, groq_key: str, gemini_key: str, openrouter_key: str):
        self.providers: List[Provider] = []
        self.breakers: Dict[str, CircuitBreaker] = {}
        self._last_used: Optional[str] = None

        if openrouter_key:
            self.providers.append(Provider(
                "openrouter", "nvidia/nemotron-3-super-120b-a12b:free",
                OpenAI(api_key=openrouter_key, base_url="https://openrouter.ai/api/v1", timeout=30.0),
                2, openrouter_key, base_timeout=20.0
            ))
        if groq_key:
            self.providers.append(Provider(
                "groq", "llama-3.1-8b-instant",
                Groq(api_key=groq_key, timeout=15.0), 1, groq_key,
                base_timeout=10.0
            ))
        if gemini_key:
            self.providers.append(Provider(
                "gemini", "gemini-2.0-flash",
                genai.Client(api_key=gemini_key), 3, gemini_key,
                base_timeout=20.0
            ))

        self.providers.sort(key=lambda p: p.priority)
        for p in self.providers:
            self.breakers[p.name] = CircuitBreaker()

    def _is_rate_limit(self, error: Exception) -> bool:
        s = str(error).lower()
        return any(x in s for x in ["429", "rate limit", "too many requests"])

    def _is_terminal(self, error: Exception) -> bool:
        s = str(error).lower()
        return any(x in s for x in ["400", "401", "invalid api key", "unauthorized"])

    def _is_server_error(self, error: Exception) -> bool:
        s = str(error).lower()
        return any(x in s for x in ["500", "502", "503", "server error", "internal"])

    def _retry_delay(self, attempt: int) -> float:
        base, cap = 0.5, 32.0
        delay = min(base * (2 ** attempt), cap)
        return delay * random.random()

    async def _execute_provider(
        self, provider: Provider, system_prompt: str, user_message: str,
        messages: Optional[List[Dict[str, str]]], max_tokens: int, temperature: float,
        timeout: Optional[float] = None, record_latency: bool = True,
    ) -> str:
        if provider.name == "gemini":
            fn = self._call_gemini
        else:
            fn = self._call_openai_compatible
        effective = timeout or provider.timeout
        start = time.perf_counter()
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, provider, system_prompt, user_message, messages, max_tokens, temperature),
            timeout=effective,
        )
        if record_latency:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if provider.ema_latency_ms:
                provider.ema_latency_ms = 0.9 * provider.ema_latency_ms + 0.1 * elapsed_ms
            else:
                provider.ema_latency_ms = elapsed_ms
        return result

    async def call(
        self,
        system_prompt: str,
        user_message: str,
        messages: Optional[List[Dict[str, str]]] = None,
        max_tokens: int = 800,
        temperature: float = 0.3,
    ) -> LLMResponse:
        fallback_used = False
        fallback_reason = None
        stop_all = False

        for provider in self.providers:
            breaker = self.breakers[provider.name]
            if not breaker.can_execute():
                print(f"[LLMRouter] {provider.name} circuit breaker open, skipping", flush=True)
                if not fallback_used:
                    fallback_used, fallback_reason = True, f"circuit breaker open for {provider.name}"
                continue

            # Preflight probe when breaker is recovering (HALF_OPEN)
            if breaker.state == CircuitState.HALF_OPEN:
                probe_ok = await self._probe_provider(provider)
                if not probe_ok:
                    breaker.record_failure("probe failed")
                    reason = f"probe failed for {provider.name}"
                    print(f"[LLMRouter] {provider.name} probe failed, staying open", flush=True)
                    if not fallback_used:
                        fallback_used, fallback_reason = True, reason
                    continue
                breaker.record_success()
                print(f"[LLMRouter] {provider.name} probe passed, circuit closed", flush=True)

            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    response = await self._execute_provider(
                        provider, system_prompt, user_message, messages, max_tokens, temperature
                    )
                    breaker.record_success()
                    self._last_used = provider.name
                    provider.timeout = min(
                        provider.base_timeout,
                        provider.timeout + (provider.base_timeout - provider.timeout) * 0.25,
                    )
                    print(f"[LLMRouter] {provider.name} succeeded (fallback={fallback_used})", flush=True)
                    return LLMResponse(
                        text=response, provider=provider.name,
                        fallback_used=fallback_used, fallback_reason=fallback_reason,
                    )

                except asyncio.TimeoutError as e:
                    breaker.record_failure("timeout")
                    provider.timeout = max(provider.min_timeout, provider.timeout * 0.75)
                    reason = "timeout"
                    print(f"[LLMRouter] {provider.name} failed: {reason}, trying next...", flush=True)

                except Exception as e:
                    if self._is_rate_limit(e):
                        breaker.record_failure(f"rate limited (429): {str(e)[:80]}")
                        reason = f"rate limited (429): {str(e)[:80]}"
                        print(f"[LLMRouter] {provider.name} failed: {reason}, trying next...", flush=True)
                        if not fallback_used:
                            fallback_used, fallback_reason = True, reason
                        break
                    if self._is_terminal(e):
                        reason = f"terminal error: {str(e)[:100]}"
                        print(f"[LLMRouter] {provider.name} terminal error: {str(e)[:100]}", flush=True)
                        if not fallback_used:
                            fallback_used, fallback_reason = True, reason
                        stop_all = True
                        break
                    if self._is_server_error(e):
                        reason = f"server error: {str(e)[:100]}"
                        print(f"[LLMRouter] {provider.name} failed: {reason}", flush=True)
                        breaker.record_failure(reason)
                        if attempt < max_retries:
                            delay = self._retry_delay(attempt)
                            print(f"[LLMRouter] {provider.name} retrying in {delay:.1f}s...", flush=True)
                            await asyncio.sleep(delay)
                            continue
                    else:
                        breaker.record_failure(f"error: {str(e)[:100]}")
                        reason = f"error: {str(e)[:100]}"
                        print(f"[LLMRouter] {provider.name} failed: {reason}, trying next...", flush=True)
                        break

                if not fallback_used:
                    fallback_used, fallback_reason = True, reason
                break

            if stop_all:
                break

        return LLMResponse(
            text=None, provider=None, fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            error="All AI providers are currently unavailable. Please try again in a few minutes.",
        )

    async def _probe_provider(self, provider: Provider) -> bool:
        """Cheap connectivity check used only when a breaker is HALF_OPEN."""
        try:
            await self._execute_provider(
                provider, "ping", "ping", None, 1, 0.0,
                timeout=3.0, record_latency=False,
            )
            return True
        except Exception:
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Return per-provider health counters for observability."""
        return {
            "last_used": self._last_used,
            "providers": {
                p.name: {
                    "model": p.model,
                    "state": self.breakers[p.name].state.value,
                    "timeout": round(p.timeout, 2),
                    "success_count": self.breakers[p.name].success_count,
                    "failure_count": self.breakers[p.name].failure_count,
                    "last_error": self.breakers[p.name].last_error,
                    "ema_latency_ms": round(p.ema_latency_ms, 1),
                }
                for p in self.providers
            },
        }

    def _call_gemini(
        self, provider: Provider, system_prompt: str, user_message: str,
        messages: Optional[List[Dict[str, str]]], max_tokens: int, temperature: float,
    ) -> str:
        if messages:
            contents = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
            contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))
            response = provider.client.models.generate_content(
                model=provider.model, contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens, temperature=temperature,
                ),
            )
        else:
            response = provider.client.models.generate_content(
                model=provider.model, contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens, temperature=temperature,
                ),
            )
        if not response.text or not response.text.strip():
            raise ValueError("Gemini returned empty response")
        return response.text

    def _call_openai_compatible(
        self, provider: Provider, system_prompt: str, user_message: str,
        messages: Optional[List[Dict[str, str]]], max_tokens: int, temperature: float,
    ) -> str:
        msg_list = [{"role": "system", "content": system_prompt}]
        if messages:
            for msg in messages:
                msg_list.append({"role": msg["role"], "content": msg["content"]})
        msg_list.append({"role": "user", "content": user_message})

        response = provider.client.chat.completions.create(
            model=provider.model, messages=msg_list,
            max_tokens=max_tokens, temperature=temperature,
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ValueError("LLM returned empty response")
        return content


async def test_router():
    import os
    router = LLMRouter(
        groq_key=os.getenv("GROQ_API_KEY", ""),
        gemini_key=os.getenv("GEMINI_API_KEY", ""),
        openrouter_key=os.getenv("OPENROUTER_API_KEY", ""),
    )
    response = await router.call(
        system_prompt="You are a helpful assistant.",
        user_message="Say hello in one sentence.",
    )
    print(f"Response: {response.text}")
    print(f"Provider: {response.provider}")
    print(f"Fallback: {response.fallback_used} ({response.fallback_reason})")
    print(f"Error: {response.error}")


if __name__ == "__main__":
    asyncio.run(test_router())
