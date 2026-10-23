class LLMError(Exception):
    """An LLM invocation or structured response failed."""


class MemoryError(RuntimeError):
    """A memory operation failed; callers must report degraded operation."""
