"""
Retry utilities for backend server requests with exponential backoff.
"""

import asyncio
import logging
import time
from typing import Callable, TypeVar, Optional, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 5
    total_timeout_seconds: float = 2.0
    base_delay_ms: int = 50

    def __post_init__(self):
        """Validate configuration."""
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        if self.base_delay_ms <= 0:
            raise ValueError("base_delay_ms must be positive")


@dataclass
class RetryResult:
    """Result of a retry operation."""
    success: bool
    result: Optional[any] = None
    attempts: int = 0
    total_duration_ms: float = 0.0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


async def retry_with_backoff(
    func: Callable,
    *args,
    config: RetryConfig,
    retry_on_exceptions: tuple = (Exception,),
    operation_name: str = "operation",
    **kwargs
) -> RetryResult:
    """
    Executes a function with exponential backoff retry logic.

    Args:
        func: Async function to execute
        *args: Positional arguments to pass to func
        config: Retry configuration
        retry_on_exceptions: Tuple of exception types to retry on
        operation_name: Name of the operation for logging
        **kwargs: Keyword arguments to pass to func

    Returns:
        RetryResult with success status and result or errors

    The retry strategy:
    - Uses exponential backoff: delay = base_delay * (2 ^ attempt)
    - Respects total timeout budget
    - Logs each retry attempt for debugging
    """
    start_time = time.time()
    errors = []
    attempt = 0

    for attempt in range(config.max_retries + 1):
        # Check if we've exceeded the total timeout budget
        elapsed = time.time() - start_time
        if elapsed >= config.total_timeout_seconds:
            logger.warning(
                f"{operation_name}: Exceeded total timeout of {config.total_timeout_seconds}s "
                f"after {attempt} attempts"
            )
            break

        try:
            # Attempt the operation
            if attempt > 0:
                logger.debug(
                    f"{operation_name}: Retry attempt {attempt}/{config.max_retries}"
                )

            result = await func(*args, **kwargs)

            # Success!
            total_duration_ms = (time.time() - start_time) * 1000
            if attempt > 0:
                logger.info(
                    f"{operation_name}: Succeeded on attempt {attempt + 1} "
                    f"after {total_duration_ms:.1f}ms"
                )

            return RetryResult(
                success=True,
                result=result,
                attempts=attempt + 1,
                total_duration_ms=total_duration_ms,
                errors=errors
            )

        except retry_on_exceptions as e:
            error_msg = f"Attempt {attempt + 1}: {type(e).__name__}: {str(e)}"
            errors.append(error_msg)

            # Don't log on last attempt (we'll log the failure below)
            if attempt < config.max_retries:
                logger.debug(f"{operation_name}: {error_msg}")
            else:
                logger.warning(f"{operation_name}: Final attempt failed: {error_msg}")

            # Calculate backoff delay with exponential growth
            if attempt < config.max_retries:
                # Exponential backoff: base_delay * (2 ^ attempt)
                delay_ms = config.base_delay_ms * (2 ** attempt)
                delay_seconds = delay_ms / 1000.0

                # Don't sleep if it would exceed the total timeout
                elapsed = time.time() - start_time
                remaining_time = config.total_timeout_seconds - elapsed

                if remaining_time <= 0:
                    logger.debug(
                        f"{operation_name}: No time remaining for retry delay"
                    )
                    break

                # Cap the delay to remaining time
                actual_delay = min(delay_seconds, remaining_time)

                logger.debug(
                    f"{operation_name}: Waiting {actual_delay * 1000:.1f}ms before retry "
                    f"({remaining_time:.2f}s remaining of {config.total_timeout_seconds}s budget)"
                )

                await asyncio.sleep(actual_delay)

    # All retries exhausted
    total_duration_ms = (time.time() - start_time) * 1000
    logger.error(
        f"{operation_name}: Failed after {attempt + 1} attempts "
        f"in {total_duration_ms:.1f}ms. Errors: {errors[-3:]}"  # Show last 3 errors
    )

    return RetryResult(
        success=False,
        result=None,
        attempts=attempt + 1,
        total_duration_ms=total_duration_ms,
        errors=errors
    )


async def retry_async_generator(
    func: Callable,
    *args,
    config: RetryConfig,
    retry_on_exceptions: tuple = (Exception,),
    operation_name: str = "operation",
    **kwargs
):
    """
    Wrapper for async generator functions with retry logic.

    This is used for streaming responses where we want to retry
    the initial connection but stream once connected.

    Yields items from the async generator, retrying the initial
    connection if it fails.
    """
    result = await retry_with_backoff(
        func,
        *args,
        config=config,
        retry_on_exceptions=retry_on_exceptions,
        operation_name=operation_name,
        **kwargs
    )

    if not result.success:
        # Re-raise the last error
        raise Exception(f"Failed after {result.attempts} attempts: {result.errors[-1] if result.errors else 'Unknown error'}")

    # If successful, the result should be an async generator
    async for item in result.result:
        yield item
