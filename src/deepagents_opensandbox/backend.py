"""Deep Agents sandbox backend for a locally-running OpenSandbox server.

Bridges OpenSandbox's async-only Python SDK to the `SandboxBackendProtocol`
that Deep Agents expects. `execute()`/`upload_files()`/`download_files()` are
implemented using one shared background event loop (see `_loop.py`) rather
than a per-call `asyncio.run()`, which can break OpenSandbox's async client
after the first call. Async tool calls (`aexecute()` etc.) intentionally rely
on `BaseSandbox`'s default behavior of thread-offloading these sync methods —
see the comment above `akill()` for why a hand-rolled async path was removed.
"""

from __future__ import annotations

import asyncio
import warnings
from datetime import timedelta

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models import WriteEntry
from opensandbox.models.execd import RunCommandOpts

from deepagents_opensandbox._loop import get_loop

DEFAULT_IMAGE = "opensandbox/code-interpreter:v1.1.0"
# If you change DEFAULT_IMAGE, check OpenSandbox's README for that tag's path —
# a mismatch here makes the sandbox container exit immediately (exit code
# 127, "command not found") and every health check will simply time out,
# which looks like a networking problem but isn't one.

DEFAULT_ENTRYPOINT = ["/opt/code-interpreter/code-interpreter.sh"]


class InsecureSandboxWarning(UserWarning):
    """Raised when connecting to an OpenSandbox server with no api_key set.

    See the "Security" section of the README for why this matters even for
    local/single-user setups.
    """


class OpenSandboxBackend(BaseSandbox):
    """`BaseSandbox` implementation backed by a local OpenSandbox server.

    Example:
        ```python
        from deepagents import create_deep_agent
        from deepagents_opensandbox import OpenSandboxBackend

        backend = OpenSandboxBackend.create()  # talks to localhost:8080
        agent = create_deep_agent(model="anthropic:claude-sonnet-4-6", backend=backend)
        try:
            result = agent.invoke({"messages": "Write and run a hello-world script"})
        finally:
            backend.kill()
        ```
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._loop = get_loop()

    @classmethod
    def create(
        cls,
        domain: str = "localhost:8080",
        image: str = DEFAULT_IMAGE,
        entrypoint: list[str] | None = None,
        api_key: str | None = None,
        use_server_proxy: bool = False,
        timeout: timedelta = timedelta(minutes=30),
    ) -> "OpenSandboxBackend":
        """Create a fresh sandbox on a local OpenSandbox server and wrap it.

        Requires `opensandbox-server` already running (see package README).

        `use_server_proxy`: on native Linux Docker, the SDK can usually reach
        the sandbox container's ports directly. On Docker Desktop (Windows/
        Mac), direct container-port access is frequently *not* reachable from
        the host, and sandbox creation hangs for ~30s before failing with a
        health-check timeout that looks like a generic networking error. If
        you hit that on Windows/Mac, set this to True (it routes traffic
        through opensandbox-server instead of hitting the container
        directly — slightly higher latency, much more reliable locally).
        """
        loop = get_loop()

        if not api_key:
            warnings.warn(
                "OpenSandboxBackend.create() called with no api_key. The "
                "OpenSandbox server's API is unauthenticated by default, "
                "meaning ANY process on this machine (including malicious "
                "browser JavaScript, due to its allow-all CORS policy) can "
                "control your sandboxes - including mounting host paths if "
                "allowed_host_paths is misconfigured. See "
                "https://github.com/alibaba/OpenSandbox/issues/750.\n\n"
                "To fix: add to ~/.sandbox.toml:\n"
                "    [server]\n"
                '    api_key = "some-long-random-string-you-generate"\n\n'
                "Then pass the same value here: OpenSandboxBackend.create(api_key=...)",
                InsecureSandboxWarning,
                stacklevel=2,
            )

        config = ConnectionConfig(domain=domain, api_key=api_key, use_server_proxy=use_server_proxy)
        sandbox = loop.run(
            Sandbox.create(
                image,
                entrypoint=entrypoint or DEFAULT_ENTRYPOINT,
                connection_config=config,
                timeout=timeout,
            )
        )
        return cls(sandbox)

    @property
    def id(self) -> str:
        return self._sandbox.id

    # -- sync path -----------------------------------------------------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        opts = _run_opts(timeout)
        # No client-side `future.result(timeout=...)` here: the *server* enforces
        # the timeout via `opts.timeout` and terminates the command itself. A
        # separate client-side wait timeout would just raise locally while the
        # sandboxed process kept running orphaned.
        execution = self._loop.run(self._sandbox.commands.run(command, opts=opts))
        return _to_execute_response(execution)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        invalid = [FileUploadResponse(path=p, error="invalid_path") for p, _ in files if not p.startswith("/")]
        if invalid:
            # deepagents' contract treats invalid_path as a caller mistake an
            # LLM can retry/fix - reject client-side before any network call
            # rather than letting the SDK silently write it somewhere
            # unexpected (it resolves relative paths against its own default
            # working directory, which is rarely what the caller intended).
            return invalid

        entries = [WriteEntry(path=path, data=data, mode=644) for path, data in files]
        try:
            self._loop.run(self._sandbox.files.write_files(entries))
            return [FileUploadResponse(path=p) for p, _ in files]
        except Exception as exc:  # noqa: BLE001 - protocol requires per-file errors, not raises
            return [FileUploadResponse(path=p, error=str(exc)) for p, _ in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results = []
        for path in paths:
            if not path.startswith("/"):
                results.append(FileDownloadResponse(path=path, error="invalid_path"))
                continue
            try:
                # read_bytes (not read_file) - read_file is text-mode/UTF-8
                # only and corrupts arbitrary binary content (confirmed via
                # test_upload_binary_content_roundtrip).
                content = self._loop.run(self._sandbox.files.read_bytes(path))
                results.append(FileDownloadResponse(path=path, content=content))
            except Exception as exc:  # noqa: BLE001 - partial success required
                results.append(FileDownloadResponse(path=path, error=self._classify_download_error(path, exc)))
        return results

    def _classify_download_error(self, path: str, exc: Exception) -> str:
        """Best-effort mapping to deepagents' canonical error codes.

        OpenSandbox's own error responses for these cases are inconsistent
        (confirmed: directory downloads can close the connection mid-response
        rather than returning a clean error; permission-denied files report
        as a plain 404 same as a missing file) - so this proactively inspects
        the path via get_file_info() rather than trusting the original
        exception's wording. Falls back to the raw exception message if
        get_file_info() can't clarify things either.
        """
        try:
            info = self._loop.run(self._sandbox.files.get_file_info([path]))
        except Exception:  # noqa: BLE001 - fall through to generic classification below
            info = None

        entry = info.get(path) if info else None
        if entry is None:
            return "file_not_found"
        if entry.entry_type and "dir" in entry.entry_type.lower():
            return "is_directory"
        if entry.mode == 0:
            # Only catches the "no permission bits set for anyone" case
            # (e.g. chmod 000, as used by the standard test suite) - a full
            # permission check would need the sandbox process's effective
            # uid/gid, which isn't available client-side.
            return "permission_denied"

        text = str(exc)
        if "FILE_NOT_FOUND" in text or "404" in text:
            return "file_not_found"
        return text

    # -- lifecycle ---------------------------------------------------------

    def kill(self) -> None:
        """Tear down the underlying OpenSandbox sandbox."""
        self._loop.run(self._sandbox.kill())

    # -- async path -------------------------------------------------------
    # Deliberately NOT overriding aexecute/aupload_files/adownload_files here.
    #
    # An earlier version of this class implemented "true async" versions using
    # asyncio.wrap_future() to reschedule onto the shared background loop.
    # That produced a real, confirmed bug: LangGraph's ToolNode prefers the
    # async tool variant even under a synchronous agent.invoke() call (deep
    # agents registers both `func` and `coroutine` on every filesystem tool),
    # and the hand-rolled async bridge silently returned empty output for
    # commands that worked perfectly through the sync `execute()` path -
    # verified via `backend.execute(...)` directly. Since `_to_execute_response`
    # falls back to the string "(no output)" on empty logs, and deepagents'
    # `_parse_ls_output` silently treats non-JSON lines as no-ops, the failure
    # surfaced as a clean, error-free empty list (`[]`) - not an exception -
    # which made it very hard to notice.
    #
    # BaseSandbox's inherited defaults just run the proven-good sync methods
    # below inside a thread (`asyncio.to_thread`), which is slightly less
    # efficient but has already been verified to actually work end-to-end.

    async def akill(self) -> None:
        await asyncio.to_thread(self.kill)


def _run_opts(timeout: int | None) -> RunCommandOpts | None:
    if timeout is None:
        return None
    return RunCommandOpts(timeout=timedelta(seconds=timeout))


def _to_execute_response(execution) -> ExecuteResponse:  # noqa: ANN001
    # OpenSandbox streams stdout/stderr one line per chunk, with the
    # trailing newline already stripped from each chunk's `.text`. Joining
    # with "" (as an earlier version of this function did) silently
    # destroys every line boundary, which breaks any caller that parses
    # output line-by-line - notably deepagents' `ls`/`glob`/`grep`, which
    # JSON-decode one line at a time and silently swallow a decode error
    # into an empty result rather than raising. Joining with "\n" instead
    # restores the original line structure.
    stdout = "\n".join(chunk.text for chunk in execution.logs.stdout)
    stderr = "\n".join(chunk.text for chunk in execution.logs.stderr)
    combined = "\n".join(part for part in (stdout, stderr) if part)
    # NOTE: previously fell back to the literal string "(no output)" here.
    # deepagents' own parsers (_parse_grep_output, etc.) check `if not output`
    # to detect "nothing happened" - a non-empty placeholder string defeats
    # that check and gets misreported as literal command output/error detail
    # (confirmed via test_grep_no_matches). A genuinely empty string lets
    # each caller's own "no output" handling work as designed.
    return ExecuteResponse(
        output=combined,
        exit_code=getattr(execution, "exit_code", 0),
    )
