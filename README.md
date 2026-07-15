# deepagents-opensandbox-Backend

A [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) sandbox
backend for [OpenSandbox](https://github.com/alibaba/OpenSandbox), running entirely
locally via Docker — no cloud sandbox provider, API key, or per-minute billing required.

## Description

`deepagents-opensandbox-backend` implements deep agents' `BaseSandbox` interface on top of a
locally-run OpenSandbox server. It gives an agent a real, isolated Docker sandbox to
execute code, read/write files, and search the filesystem — the same capability as
deep agents' cloud-hosted sandbox providers (Daytona, Modal, Runloop), but running
entirely on your own machine.

## Requirements

- Python 3.10+
- Docker (Docker Desktop on Windows/Mac, native Docker on Linux)
- [uv](https://docs.astral.sh/uv/) (recommended) or plain `pip`

**Platform notes:**
- **Linux** — works out of the box; the SDK can usually reach sandbox container ports directly.
- **Windows / Mac (Docker Desktop)** — direct container-port access is frequently *not*
  reachable from the host. Pass `use_server_proxy=True` to `OpenSandboxBackend.create()`
  if sandbox creation hangs for ~30s and then fails with a health-check timeout.

## Installation

```bash
uv venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
uv pip install deepagents-opensandbox-backend
```

## One-time local setup

```bash
# Docker image the sandbox will run
docker pull opensandbox/code-interpreter:v1.1.0

# The OpenSandbox control-plane server, run as an isolated uv tool
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
```

Before starting the server, read **Security** below and edit `~/.sandbox.toml` — the
generated default config is intentionally insecure for first-run convenience.

```bash
uvx opensandbox-server        # leave this running in its own terminal, serves :8080
```

## Security

The generated default config has **no API authentication and no host-path
restrictions**. The server will print a `SECURITY WARNING` and require you to type
`YES` to continue — that prompt exists because of a disclosed vulnerability
([alibaba/OpenSandbox#750](https://github.com/alibaba/OpenSandbox/issues/750)): with
the defaults, *any* HTTP request to the server (no login, no token) can ask it to
mount `/var/run/docker.sock` into a sandbox, which hands that request root on your
machine via the Docker API.

This isn't just "don't expose it to the internet" — it matters on a single dev
machine too:
- The server enables allow-all CORS by default, so a malicious script in **any
  browser tab** you have open can call the API directly from JavaScript.
- Any other local process or user account on the machine can reach it too.

Before running `uvx opensandbox-server` for real, edit `~/.sandbox.toml`:

```toml
[server]
api_key = "some-long-random-string-you-generate"

[storage]
# Empty list secretly means "allow every host path" — always set this explicitly.
allowed_host_paths = ["/tmp/opensandbox-data"]  # or wherever you actually need mounted
```

Then pass the matching key when creating a sandbox:

```python
backend = OpenSandboxBackend.create(api_key="some-long-random-string-you-generate")
```

If you skip `api_key`, `OpenSandboxBackend.create()` raises an `InsecureSandboxWarning`
as a reminder — fine for a few minutes of local testing, not fine to leave running
that way.

## How to use

### Basic agent usage

```python
from deepagents import create_deep_agent
from deepagents_opensandbox_backend import OpenSandboxBackend

backend = OpenSandboxBackend.create(api_key="some-long-random-string-you-generate")
agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    system_prompt="You are a Python coding assistant with sandbox access.",
    backend=backend,
)

try:
    result = agent.invoke({"messages": "Write and run a script that processes data.csv"})
    print(result["messages"][-1].content)
finally:
    backend.kill()
```

### Uploading a local file for the agent to use

The sandbox starts empty — to have the agent work on a file from your own machine,
upload it first with `upload_files()`. Always use a full path under `/workspace/`,
and tell the agent that exact path. Deep agents' filesystem tools (`ls`, `read_file`,
etc.) treat `/` as their own virtual root, which has no built-in awareness of where
`upload_files()` actually placed your file — using the real absolute path for both
avoids that mismatch entirely:

```python
from pathlib import Path

csv_bytes = Path("customers-1000.csv").read_bytes()
results = backend.upload_files([("/workspace/customers-1000.csv", csv_bytes)])

for r in results:
    if r.error:
        raise RuntimeError(f"Upload failed for {r.path}: {r.error}")

result = agent.invoke({
    "messages": "The file is at /workspace/customers-1000.csv. Analyze it and "
                "generate a summary with charts."
})
```

### Downloading a file the agent created

Once the agent finishes (e.g. it wrote a chart as a PNG), pull it back to your own
machine with `download_files()`:

```python
downloads = backend.download_files(["/workspace/customers_by_country.png"])
for d in downloads:
    if d.error:
        print(f"Could not download {d.path}: {d.error}")
    else:
        Path("customers_by_country.png").write_bytes(d.content)
```

`download_files()` is binary-safe (uses OpenSandbox's `read_bytes` under the hood),
so this works for images and other non-text files, not just plain text — a plain
text-mode read would corrupt them.

### Running a command directly (bypassing the agent)

Useful for debugging what's actually in the sandbox, independent of what the
agent's own tools report:

```python
result = backend.execute("ls -la /workspace")
print(result.output, result.exit_code)
```

### `OpenSandboxBackend.create()` parameters

| Parameter | Default | Description |
|---|---|---|
| `domain` | `"localhost:8080"` | Address of the running `opensandbox-server` |
| `image` | `opensandbox/code-interpreter:v1.1.0` | Sandbox Docker image |
| `entrypoint` | tied to `image` | Override if pinning a different image tag (see Troubleshooting) |
| `api_key` | `None` | Matches `[server] api_key` in `~/.sandbox.toml` — see Security |
| `use_server_proxy` | `False` | Set `True` on Windows/Mac Docker Desktop — see Requirements |
| `timeout` | 30 minutes | Sandbox idle timeout |

## Troubleshooting

**"Sandbox health check timed out" / container log shows `... not found` / exit code
127** — the `entrypoint` path doesn't match the image tag. OpenSandbox has changed
this path across image versions (e.g. `/opt/opensandbox/code-interpreter.sh` for
`v1.0.1`/`v1.0.2`, but `/opt/code-interpreter/code-interpreter.sh` for `v1.1.0`).
Check `docker logs <container>` — if you see "not found" for the entrypoint script,
the container exited immediately and no health check was ever going to pass.

**Health check keeps failing but the container itself looks fine (running, not
exited)** — see the Windows/Mac note under Requirements; pass `use_server_proxy=True`.

## Testing

```bash
uv pip install -e ".[test]"
uv run pytest tests/integration_tests/test_sandbox.py -v
```

Requires `opensandbox-server` running locally (see setup above) — these are real
integration tests against a live sandbox, not mocked unit tests.

## How to contribute

1. Fork and clone the repo.
2. `uv venv && source .venv/bin/activate && uv pip install -e ".[test]"`
3. Make your change.
4. `ruff check .` and `ruff format --check .` before committing — CI enforces both.
5. Run the test suite (needs `opensandbox-server` running locally, see Testing above).
6. Open a PR against `main`. CI runs lint and a Docker-free smoke test automatically
   on every push/PR; the full integration suite is triggered manually (see
   `.github/workflows/ci.yml`) since it depends on a live local Docker sandbox and
   isn't reliably reproducible on every hosted CI run — run it yourself before
   merging anything touching `backend.py`.

## License

MIT — see [LICENSE](LICENSE).
