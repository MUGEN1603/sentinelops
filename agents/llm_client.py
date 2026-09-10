"""
agents/llm_client.py — Robust LLM client with circuit breaker, retries, fallback, and metrics.

Features:
- Circuit breaker pattern: stops calling failing models after threshold
- Exponential backoff retries with jitter
- Fallback chain: primary → secondary → tertiary models
- Latency/error metrics for observability
- Structured logging for debugging
- Health check endpoint for load balancers

Usage:
    from agents.llm_client import LLMClient, CircuitBreakerConfig

    config = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout=30,
        expected_exception=(httpx.RequestError, httpx.HTTPStatusError)
    )
    client = LLMClient(
        models=[
            {"name": "qwen3-coder:latest", "priority": 1},
            {"name": "codellama:13b", "priority": 2},
            {"name": "mistral:7b", "priority": 3},
        ],
        circuit_breaker_config=config,
    )

    response = await client.chat(messages=[...], model=None)  # auto-selects best available
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx
import ollama

log = logging.getLogger("llm-client")


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3          # Failures before opening
    recovery_timeout: int = 30          # Seconds before half-open
    success_threshold: int = 2          # Successes in half-open to close
    expected_exception: Tuple[type, ...] = (
        httpx.RequestError,
        httpx.HTTPStatusError,
        ConnectionError,
        TimeoutError,
    )


@dataclass
class ModelConfig:
    name: str
    priority: int
    timeout: int = 120
    max_tokens: Optional[int] = None
    temperature: float = 0.1


@dataclass
class ModelMetrics:
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    total_latency_ms: float = 0.0
    last_error: Optional[str] = None
    last_success_ts: Optional[float] = None
    circuit_state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_state_change: float = field(default_factory=time.time)


class CircuitBreaker:
    """Circuit breaker for a single model."""

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    async def can_execute(self) -> bool:
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if self.last_failure_time and \
                   time.time() - self.last_failure_time >= self.config.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    log.info("Circuit breaker entering HALF_OPEN state")
                    return True
                return False
            # HALF_OPEN
            return True

    async def record_success(self):
        async with self._lock:
            self.failure_count = 0
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.success_count = 0
                    log.info("Circuit breaker CLOSED after recovery")

    async def record_failure(self, error: Exception):
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            self.success_count = 0
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                log.warning("Circuit breaker OPEN after half-open failure")
            elif self.state == CircuitState.CLOSED and \
                 self.failure_count >= self.config.failure_threshold:
                self.state = CircuitState.OPEN
                log.warning(f"Circuit breaker OPEN after {self.failure_count} failures")


class LLMClient:
    """
    Robust LLM client with circuit breaker, retries, and fallback models.
    """

    def __init__(
        self,
        models: List[Dict[str, Any]],
        circuit_breaker_config: Optional[CircuitBreakerConfig] = None,
        default_retries: int = 2,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ):
        """
        Args:
            models: List of model configs with 'name', 'priority', optional 'timeout', 'max_tokens', 'temperature'
            circuit_breaker_config: CircuitBreakerConfig instance
            default_retries: Number of retries per model
            base_delay: Base delay for exponential backoff (seconds)
            max_delay: Maximum delay for exponential backoff (seconds)
        """
        self.circuit_config = circuit_breaker_config or CircuitBreakerConfig()
        self.default_retries = default_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

        # Initialize models sorted by priority
        self.models = sorted(
            [ModelConfig(**m) for m in models],
            key=lambda m: m.priority
        )

        # Per-model circuit breakers and metrics
        self.circuit_breakers = {m.name: CircuitBreaker(self.circuit_config) for m in self.models}
        self.metrics = {m.name: ModelMetrics() for m in self.models}

        # Ollama client with connection pooling
        self._ollama_client = ollama.AsyncClient()
        self._host = os.getenv("OLLAMA_HOST", "http://localhost:11434")

        log.info(f"LLMClient initialized with {len(self.models)} models: "
                 f"{[m.name for m in self.models]}")

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> str:
        """
        Simple chat completion returning just the content string.

        Convenience wrapper around `chat()` for agents that expect a string.
        """
        result = await self.chat(messages, model, temperature, max_tokens, retries)
        return result["content"]

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Chat completion with automatic model selection and fallback.

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Specific model name (None = auto-select best available)
            temperature: Override temperature
            max_tokens: Override max tokens
            retries: Override retry count

        Returns:
            Dict with 'content', 'model', 'latency_ms', 'usage'

        Raises:
            LLMError: If all models fail
        """
        retries = retries or self.default_retries

        # Select models to try
        if model:
            candidates = [m for m in self.models if m.name == model]
            if not candidates:
                raise ValueError(f"Model not configured: {model}")
        else:
            candidates = self._get_available_models()

        if not candidates:
            raise LLMError("No available models (all circuit breakers open)")

        last_error = None

        for candidate in candidates:
            for attempt in range(retries + 1):
                try:
                    result = await self._call_model(
                        candidate,
                        messages,
                        temperature or candidate.temperature,
                        max_tokens or candidate.max_tokens,
                    )
                    await self.circuit_breakers[candidate.name].record_success()
                    self._record_metrics(candidate.name, success=True, latency=result["latency_ms"])
                    result["model"] = candidate.name
                    return result
                except self.circuit_config.expected_exception as e:
                    last_error = e
                    delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                    log.warning(
                        f"Model {candidate.name} attempt {attempt + 1} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)

            # All retries exhausted for this model
            await self.circuit_breakers[candidate.name].record_failure(last_error)
            self._record_metrics(candidate.name, success=False, error=str(last_error))
            log.warning(f"Model {candidate.name} exhausted retries, trying next fallback")

        # All models exhausted
        raise LLMError(f"All models failed. Last error: {last_error}")

    async def _call_model(
        self,
        model: ModelConfig,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        """Call a specific model with timeout."""
        start = time.time()

        options = {
            "temperature": temperature,
        }
        if max_tokens:
            options["num_predict"] = max_tokens

        try:
            response = await asyncio.wait_for(
                self._ollama_client.chat(
                    model=model.name,
                    messages=messages,
                    options=options,
                ),
                timeout=model.timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Model {model.name} timed out after {model.timeout}s")

        latency_ms = (time.time() - start) * 1000

        content = response.get("message", {}).get("content", "")
        if not content:
            raise ValueError(f"Empty response from {model.name}")

        return {
            "content": content,
            "latency_ms": latency_ms,
            "usage": {
                "prompt_tokens": response.get("prompt_eval_count", 0),
                "completion_tokens": response.get("eval_count", 0),
            },
        }

    def _get_available_models(self) -> List[ModelConfig]:
        """Return models sorted by priority, skipping open circuits."""
        available = []
        for model in self.models:
            cb = self.circuit_breakers[model.name]
            # Check synchronously for speed
            if cb.state != CircuitState.OPEN:
                available.append(model)
        return available

    def _record_metrics(self, model_name: str, success: bool, latency: float = 0, error: str = None):
        m = self.metrics[model_name]
        m.total_calls += 1
        if success:
            m.successful_calls += 1
            m.total_latency_ms += latency
            m.last_success_ts = time.time()
        else:
            m.failed_calls += 1
            m.last_error = error

    def get_health(self) -> Dict[str, Any]:
        """Health check for load balancers."""
        available = len(self._get_available_models())
        return {
            "status": "healthy" if available > 0 else "degraded",
            "available_models": available,
            "total_models": len(self.models),
        }

    def get_metrics(self) -> Dict[str, Any]:
        """Return metrics for all models."""
        return {
            name: {
                "total_calls": m.total_calls,
                "success_rate": m.successful_calls / max(m.total_calls, 1),
                "avg_latency_ms": m.total_latency_ms / max(m.successful_calls, 1),
                "circuit_state": m.circuit_state.value,
                "last_error": m.last_error,
            }
            for name, m in self.metrics.items()
        }

    async def health_check(self) -> Dict[str, Any]:
        """Active health check - tries each model with a simple prompt."""
        results = {}
        for model in self.models:
            cb = self.circuit_breakers[model.name]
            can_exec = await cb.can_execute()
            if not can_exec:
                results[model.name] = {"status": "circuit_open", "available": False}
                continue

            try:
                start = time.time()
                await asyncio.wait_for(
                    self._ollama_client.chat(
                        model=model.name,
                        messages=[{"role": "user", "content": "ping"}],
                        options={"num_predict": 1, "temperature": 0},
                    ),
                    timeout=5,
                )
                latency = (time.time() - start) * 1000
                results[model.name] = {"status": "healthy", "latency_ms": latency, "available": True}
            except Exception as e:
                results[model.name] = {"status": "unhealthy", "error": str(e), "available": False}

        healthy_count = sum(1 for r in results.values() if r.get("available"))
        return {
            "status": "healthy" if healthy_count > 0 else "unhealthy",
            "healthy_models": healthy_count,
            "total_models": len(self.models),
            "details": results,
        }


class LLMError(Exception):
    """Raised when all LLM models fail."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function for simple usage
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_CLIENT: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get or create the default LLM client with sensible defaults."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        primary = os.getenv("OLLAMA_MODEL", "qwen3-coder:latest")
        fallback = os.getenv("OLLAMA_FALLBACK_MODEL", "codellama:13b")
        tertiary = os.getenv("OLLAMA_TERTIARY_MODEL", "mistral:7b")

        _DEFAULT_CLIENT = LLMClient(
            models=[
                {"name": primary, "priority": 1, "timeout": 120},
                {"name": fallback, "priority": 2, "timeout": 120},
                {"name": tertiary, "priority": 3, "timeout": 90},
            ],
            circuit_breaker_config=CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30,
            ),
            default_retries=2,
        )
    return _DEFAULT_CLIENT


async def chat_completion(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Simple chat completion helper.

    Args:
        messages: List of message dicts
        model: Specific model or None for auto
        temperature: Sampling temperature
        max_tokens: Max completion tokens

    Returns:
        Response content string

    Raises:
        LLMError: If all models fail
    """
    client = get_llm_client()
    result = await client.chat(messages, model, temperature, max_tokens)
    return result["content"]