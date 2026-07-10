# agentbox

Spin up LLM-agnostic AI agents in hardened, network-isolated Docker containers with one command.

```bash
uv tool install -e .
agentbox init          # walks through provider, model, API key, agent role + allowed websites
agentbox up
agentbox run worker "summarise the repo in /workspace"
```

## Why

Running an agent directly on your machine exposes everything it can reach. agentbox treats every agent as a stranger you handed a terminal: guardrails are enforced by the runtime and a proxy choke point, never by the agent's own logic.

## Architecture

```
┌────────────── agentbox_internal (internal: true — no egress) ──────────────┐
│  agent-a ──┐                                                                │
│  agent-b ──┼──►  llm-proxy (LiteLLM :4000)  ──►  Anthropic / OpenAI /       │
│            │                                     Ollama / Model Runner / …  │
│            └──►  mcp-gateway (:8811)        ──►  tool APIs                  │
└──────────────────────────────────────────────────────────────────────────────┘
          only the proxy and gateway are also attached to the egress network
```

**LLM-agnostic by construction.** Agents only ever see `OPENAI_BASE_URL` + a
virtual key pointing at a LiteLLM proxy sidecar. Swapping Anthropic → OpenAI →
Gemini → Groq → Ollama → Docker Model Runner is a 3-line change in
`agentbox.yaml`; agent code and images never change. The proxy also enforces
a hard **budget cap** (`budget_usd`) and **rate limits** (`rpm`/`tpm`) — cost
controls the agent cannot bypass or talk itself out of.

**Deny-by-default networking.** Agent containers sit on an `internal: true`
Docker network with no route to the internet. Their only exits are the LLM
proxy and (optionally) the MCP gateway, which double as audit points.

**Per-agent roles and website allowlists.** Give an agent standing
instructions (`role:`, injected as `$AGENT_ROLE`) and access to specific
sites (`egress: [arxiv.org, stratechery.com]`). The allowlist is enforced by
a squid sidecar on a private per-agent network — standard `HTTP(S)_PROXY`
env vars are injected, HTTPS is filtered by CONNECT host without TLS
interception, and every request is logged. Other agents can't reach it.

**Container hardening (on by default).** Non-root user, read-only rootfs with
capped tmpfs, `cap_drop: ALL`, `no-new-privileges`, CPU/memory/pids limits,
only the project workspace mounted. No `~/.ssh`, no `~/.aws`, no `.env`
parents, no docker.sock (the MCP gateway can opt in, with a loud warning —
socket access is effectively host root).

**Secrets as files, never env vars.** Provider keys are mounted via Docker
secrets and read inside the container; the manifest validator rejects
anything that looks like a secret in `env:` (env vars leak via
`docker inspect`, `ps`, and `/proc`).

**Ephemeral + bounded lifecycles.** `agentbox run` uses `--rm` with a hard
wall-clock TTL (default 30 min) — agents that should take 30 seconds
sometimes try to run for 30 hours. `agentbox reap` kills anything that
outlives its TTL label, suitable for a cron job.

## Commands

| Command | What it does |
|---|---|
| `agentbox init` | Scaffold `agentbox.yaml`, `workspace/`, gitignored secrets dir; asks for provider → model → agent role → allowed websites → API key (key skipped for local providers; `--provider/--model/--role/--egress/--api-key/--api-base` for non-interactive) |
| `agentbox build` | Validate manifest, regenerate compose + proxy config |
| `agentbox up` / `down` | Start / stop the whole stack |
| `agentbox run <agent> "task"` | Ephemeral run with TTL enforcement (`$AGENT_TASK`) |
| `agentbox ps` / `logs <agent>` | Inspect what's running |
| `agentbox reap` | Kill containers past their TTL |
| `agentbox nuke` | Tear down + delete generated state (keeps secrets) |

## Manifest reference

See the generated `agentbox.yaml` — every field is commented. Highlights:

```yaml
model:
  provider: anthropic            # or openai | gemini | groq | ollama | openai_compatible …
  model: claude-sonnet-4-6
  api_key_file: .agentbox/secrets/llm_api_key
  budget_usd: 25                 # hard cap at the proxy
  rpm: 60

tools:
  mcp_servers: [duckduckgo]      # brokered via docker/mcp-gateway
  allow_docker_socket: false     # keep this false unless you know why

agents:
  analyst:
    image: myorg/researcher:latest
    role: >-                     # standing instructions, injected as $AGENT_ROLE
      You are a research analyst. Summarise new posts from the allowed
      sources into /workspace/brief.md, with citations.
    egress: [arxiv.org, stratechery.com]   # ONLY these sites are reachable
    workspace: ./workspace       # the ONLY thing mounted
    limits: {cpus: 1.0, memory: 1g, pids: 256, ttl_minutes: 30}
```

### Local models

Point the proxy at a model running on your host — no API key, nothing else
changes (agents still just see `OPENAI_BASE_URL`). Picking `ollama` or
`openai_compatible` at the `agentbox init` prompt generates this for you:

```yaml
model:
  provider: ollama                # Ollama on the host
  model: llama3.1
  api_base: http://host.docker.internal:11434
  # api_key_file: not needed for local models
```

```yaml
model:
  provider: openai_compatible     # Docker Model Runner (also vLLM, LM Studio…)
  model: ai/smollm2
  api_base: http://model-runner.docker.internal/engines/v1
```

`host.docker.internal` resolves automatically on Docker Desktop (Mac/Windows);
on Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the
proxy service. Note `budget_usd` is meaningless for local models, but `rpm`/
`tpm` still apply.

Inside an agent container, any OpenAI-compatible SDK just works:

```python
from openai import OpenAI
import os
client = OpenAI()  # reads OPENAI_BASE_URL + OPENAI_API_KEY injected by agentbox
r = client.chat.completions.create(model=os.environ["AGENT_MODEL"],
                                   messages=[{"role": "user",
                                              "content": os.environ.get("AGENT_TASK", "hello")}])
```

## Roadmap ideas

- OTLP export from the proxy for token/latency/cost dashboards
- microVM backend (Docker Sandboxes `sbx`) as an alternative to containers
  for a harder isolation boundary
- Per-agent virtual keys with separate budgets (LiteLLM supports this)

## Known tradeoffs

- The egress allowlist is a forward proxy: tools inside the agent must honor
  the injected `HTTP(S)_PROXY` env vars (most HTTP clients do). Tools that
  ignore them don't get around the allowlist — they just have no route.

- Container isolation is weaker than microVMs; for untrusted-code execution
  consider Docker Sandboxes as the backend.
- The MCP gateway needs the docker socket to spawn per-server containers —
  that's a deliberate, opt-in trust decision.
- `deploy.resources.limits` requires a recent Docker Compose; on older
  versions swap for `mem_limit`/`cpus` keys.
