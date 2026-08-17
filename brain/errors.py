"""
Error Taxonomy for Alfred Brain

Classifies errors into 4 types for appropriate recovery:
1. Transient - API timeout, rate limit (retry with backoff)
2. Configuration - Invalid API key, wrong permissions (notify once)
3. Semantic - Wrong email, bad data (search T5 for fix)
4. Missing - Unsupported feature (decline + log)
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any
from datetime import datetime


class ErrorType(Enum):
    """Error classification types."""

    TRANSIENT = "transient"  # Retry with backoff
    CONFIGURATION = "configuration"  # Notify user once
    SEMANTIC = "semantic"  # Search memory for fix
    MISSING = "missing"  # Decline + log


class ErrorSeverity(Enum):
    """How serious is the error."""

    LOW = "low"  # Minor, continue
    MEDIUM = "medium"  # Needs attention
    HIGH = "high"  # Must stop
    CRITICAL = "critical"  # Alert user


@dataclass
class AlfredError:
    """Structured error information."""

    error_type: ErrorType
    severity: ErrorSeverity
    message: str
    tool: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    original_error: Optional[str] = None
    timestamp: str = None
    retries: int = 0

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class ErrorClassifier:
    """
    Classifies errors into appropriate types.
    Uses pattern matching and heuristics.
    """

    # Transient error patterns
    TRANSIENT_PATTERNS = [
        "timeout",
        "rate limit",
        "too many requests",
        "503",
        "502",
        "429",
        "connection reset",
        "temporary failure",
        "try again",
        "server busy",
        "unavailable",
    ]

    # Configuration error patterns
    CONFIG_PATTERNS = [
        "invalid api key",
        "authentication failed",
        "unauthorized",
        "401",
        "403",
        "permission denied",
        "not configured",
        "missing credentials",
        "invalid token",
        "expired",
        "not in safe",
        "not in allowed",
        "must start with http",
    ]

    # Semantic error patterns
    SEMANTIC_PATTERNS = [
        "not found",
        "invalid email",
        "wrong format",
        "does not exist",
        "no such",
        "404",
        "malformed",
        "parse error",
        "invalid data",
    ]

    # Missing capability patterns
    MISSING_PATTERNS = [
        "not implemented",
        "not supported",
        "unsupported",
        "capability not available",
        "feature not found",
        "unknown command",
    ]

    def classify(self, error_message: str, tool: str = None) -> AlfredError:
        """
        Classify an error based on its message.

        Returns:
            AlfredError with type, severity, and details
        """
        error_lower = error_message.lower()

        # Check transient first
        for pattern in self.TRANSIENT_PATTERNS:
            if pattern in error_lower:
                return AlfredError(
                    error_type=ErrorType.TRANSIENT,
                    severity=ErrorSeverity.MEDIUM,
                    message=f"Transient error: {error_message}",
                    tool=tool,
                    original_error=error_message,
                )

        # Check configuration
        for pattern in self.CONFIG_PATTERNS:
            if pattern in error_lower:
                return AlfredError(
                    error_type=ErrorType.CONFIGURATION,
                    severity=ErrorSeverity.HIGH,
                    message=f"Configuration error: {error_message}",
                    tool=tool,
                    original_error=error_message,
                )

        # Check semantic
        for pattern in self.SEMANTIC_PATTERNS:
            if pattern in error_lower:
                return AlfredError(
                    error_type=ErrorType.SEMANTIC,
                    severity=ErrorSeverity.MEDIUM,
                    message=f"Semantic error: {error_message}",
                    tool=tool,
                    original_error=error_message,
                )

        # Check missing
        for pattern in self.MISSING_PATTERNS:
            if pattern in error_lower:
                return AlfredError(
                    error_type=ErrorType.MISSING,
                    severity=ErrorSeverity.LOW,
                    message=f"Missing capability: {error_message}",
                    tool=tool,
                    original_error=error_message,
                )

        # Default: treat as transient
        return AlfredError(
            error_type=ErrorType.TRANSIENT,
            severity=ErrorSeverity.MEDIUM,
            message=f"Unknown error: {error_message}",
            tool=tool,
            original_error=error_message,
        )

    def get_recovery_strategy(self, error: AlfredError) -> Callable:
        """
        Get the appropriate recovery strategy for an error type.

        Returns:
            Recovery function to call
        """
        strategies = {
            ErrorType.TRANSIENT: self._recover_transient,
            ErrorType.CONFIGURATION: self._recover_configuration,
            ErrorType.SEMANTIC: self._recover_semantic,
            ErrorType.MISSING: self._recover_missing,
        }
        return strategies.get(error.error_type, self._recover_unknown)

    def _recover_transient(self, error: AlfredError) -> Dict[str, Any]:
        """Recover from transient errors with exponential backoff."""
        base_delay = 1.0  # 1 second
        max_delay = 30.0  # 30 seconds max
        delay = min(base_delay * (2**error.retries), max_delay)

        return {
            "action": "retry",
            "delay": delay,
            "message": f"Retrying in {delay:.1f} seconds...",
        }

    def _recover_configuration(self, error: AlfredError) -> Dict[str, Any]:
        """Recover from configuration errors - notify user once."""
        return {
            "action": "notify",
            "message": f"Configuration issue with {error.tool}: {error.original_error}",
            "notify_once": True,
        }

    def _recover_semantic(self, error: AlfredError) -> Dict[str, Any]:
        """Recover from semantic errors - search memory for fix."""
        return {
            "action": "search_memory",
            "memory_tier": "T5",
            "query": error.original_error,
            "message": "Searching memory for solutions...",
        }

    def _recover_missing(self, error: AlfredError) -> Dict[str, Any]:
        """Recover from missing capabilities - decline and log."""
        return {
            "action": "decline",
            "log": True,
            "message": f"I don't have the capability to {error.original_error}. This has been logged.",
        }

    def _recover_unknown(self, error: AlfredError) -> Dict[str, Any]:
        """Default recovery for unknown errors."""
        return {
            "action": "retry",
            "delay": 2.0,
            "message": "Unknown error, retrying...",
        }


# Singleton instance
_classifier: Optional[ErrorClassifier] = None


def get_error_classifier() -> ErrorClassifier:
    """Get singleton error classifier instance."""
    global _classifier
    if _classifier is None:
        _classifier = ErrorClassifier()
    return _classifier
