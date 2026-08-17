"""Error handling - wires up errors.py and recovery.py to Alfred."""

from .errors import get_error_classifier as _get_classifier
from .recovery import get_recovery_manager as _get_recovery


def get_error_classifier():
    """Return the actual ErrorClassifier instance."""
    return _get_classifier()


def get_recovery_manager():
    """Return the actual RecoveryManager instance."""
    return _get_recovery()


def get_retry_handler():
    """Return a RetryHandler instance."""
    from .recovery import RetryHandler
    return RetryHandler()
