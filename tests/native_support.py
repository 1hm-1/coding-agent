"""Shared capability gate for tests that prove the native sandbox boundary."""

from __future__ import annotations

import unittest

from coding_agent.sandbox import LinuxNamespaceExecutor


_CAPABILITIES = LinuxNamespaceExecutor().capabilities()
NATIVE_SANDBOX_AVAILABLE = _CAPABILITIES.available
NATIVE_SANDBOX_SKIP_REASON = (
    "native Linux sandbox capability unavailable; fail-closed path is covered separately"
)


def require_native_sandbox(test_case):
    """Skip a native-boundary test without treating fail-closed as a pass."""

    return unittest.skipUnless(NATIVE_SANDBOX_AVAILABLE, NATIVE_SANDBOX_SKIP_REASON)(test_case)
