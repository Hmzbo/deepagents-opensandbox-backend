"""Standard sandbox conformance tests.

Requires a local OpenSandbox server running (see README) — these are
integration tests, not unit tests, since they exercise a real sandbox.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_tests.integration_tests import SandboxIntegrationTests

from deepagents_opensandbox_backend import OpenSandboxBackend


class TestOpenSandboxStandard(SandboxIntegrationTests):
    @pytest.fixture(scope="class")
    @classmethod
    def sandbox(cls) -> Iterator[SandboxBackendProtocol]:
        backend = OpenSandboxBackend.create()
        try:
            yield backend
        finally:
            backend.kill()
