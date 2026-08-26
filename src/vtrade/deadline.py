"""Shared monotonic deadline helpers for cancellable runtime boundaries."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from time import monotonic


class DeadlineExceeded(RuntimeError):
    """An operation did not finish before its caller-owned monotonic deadline."""


def deadline_remaining(
    deadline: float, *, clock: Callable[[], float] = monotonic
) -> float:
    return max(0.0, deadline - clock())


def check_deadline(
    deadline: float,
    label: str,
    *,
    clock: Callable[[], float] = monotonic,
) -> None:
    if clock() >= deadline:
        raise DeadlineExceeded(f"{label} deadline exceeded")


def run_with_deadline[T](
    operation: Callable[[], T],
    *,
    deadline: float,
    label: str,
    clock: Callable[[], float] = monotonic,
) -> T:
    """Run one blocking operation without waiting past its shared deadline.

    Python cannot safely interrupt an arbitrary blocking call.  The worker is
    therefore abandoned on timeout; callers must fence its side effects with
    their lease before publishing any durable result.
    """

    check_deadline(deadline, label, clock=clock)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vtrade-deadline")
    future = executor.submit(operation)
    try:
        result = future.result(timeout=deadline_remaining(deadline, clock=clock))
        check_deadline(deadline, label, clock=clock)
    except FutureTimeoutError as exc:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise DeadlineExceeded(f"{label} deadline exceeded") from exc
    except BaseException:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    if clock() >= deadline:
        executor.shutdown(wait=False, cancel_futures=True)
        raise DeadlineExceeded(f"{label} deadline exceeded")
    executor.shutdown(wait=True)
    return result


__all__ = ["DeadlineExceeded", "check_deadline", "deadline_remaining", "run_with_deadline"]
