"""
Recovery strategies for error handling.

Implements the 4 recovery patterns based on error type.
"""

import asyncio
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass

from .errors import AlfredError, get_error_classifier


@dataclass
class RecoveryResult:
    """Result of a recovery attempt."""

    success: bool
    action: str
    message: str
    retry_needed: bool = False
    retry_delay: float = 0.0
    fallback_message: Optional[str] = None


class RecoveryManager:
    """
    Manages error recovery with retry logic and backoff.
    """

    def __init__(self):
        self.classifier = get_error_classifier()
        self.max_retries = 3

    async def recover(
        self, error: Exception, tool: str = None, context: Dict[str, Any] = None
    ) -> RecoveryResult:
        """
        Attempt to recover from an error.

        Args:
            error: The exception that occurred
            tool: The tool that failed
            context: Additional context for recovery

        Returns:
            RecoveryResult with action to take
        """
        error_message = str(error)
        alfred_error = self.classifier.classify(error_message, tool)

        # Get recovery strategy
        strategy = self.classifier.get_recovery_strategy(alfred_error)

        # Execute recovery
        result = await self._execute_recovery(alfred_error, strategy, context)

        return result

    async def _execute_recovery(
        self, error: AlfredError, strategy: Callable, context: Optional[Dict[str, Any]]
    ) -> RecoveryResult:
        """Execute the recovery strategy."""

        action = strategy(error)

        if action["action"] == "retry":
            # Check retry count
            if error.retries >= self.max_retries:
                return RecoveryResult(
                    success=False,
                    action="fail",
                    message=f"Max retries ({self.max_retries}) exceeded for {error.tool}",
                    fallback_message=f"I tried {self.max_retries} times but couldn't complete this task.",
                )

            # Wait before retry
            delay = action.get("delay", 1.0)
            await asyncio.sleep(delay)

            return RecoveryResult(
                success=False,
                action="retry",
                message=action.get("message", "Retrying..."),
                retry_needed=True,
                retry_delay=delay,
            )

        elif action["action"] == "notify":
            return RecoveryResult(
                success=False,
                action="notify",
                message=action.get("message", f"Configuration issue with {error.tool}"),
            )

        elif action["action"] == "search_memory":
            # TODO: Search T5 for solutions
            return RecoveryResult(
                success=False,
                action="fallback",
                message="Let me try a different approach...",
                fallback_message="I encountered an issue. Let me try a different approach.",
            )

        elif action["action"] == "decline":
            return RecoveryResult(
                success=False,
                action="decline",
                message=action.get("message", "I can't do that."),
                fallback_message="I don't have the capability to handle this request. It's been logged for future improvement.",
            )

        else:
            return RecoveryResult(
                success=False,
                action="fail",
                message="Unknown recovery action",
                fallback_message="I encountered an unknown error. Please try again.",
            )


class RetryHandler:
    """
    Handles retry logic with exponential backoff.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt using exponential backoff."""
        delay = self.base_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)

    async def execute_with_retry(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute a function with automatic retry on failure.

        Args:
            func: Async function to execute
            *args, **kwargs: Arguments to pass to func

        Returns:
            Result of func

        Raises:
            Last exception if all retries fail
        """
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                return result

            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self.calculate_delay(attempt)
                    await asyncio.sleep(delay)

        # All retries failed
        raise last_error


# Singleton instances
_recovery_manager: Optional[RecoveryManager] = None
_retry_handler: Optional[RetryHandler] = None


def get_recovery_manager() -> RecoveryManager:
    """Get singleton recovery manager."""
    global _recovery_manager
    if _recovery_manager is None:
        _recovery_manager = RecoveryManager()
    return _recovery_manager


def get_retry_handler() -> RetryHandler:
    """Get singleton retry handler."""
    global _retry_handler
    if _retry_handler is None:
        _retry_handler = RetryHandler()
    return _retry_handler
