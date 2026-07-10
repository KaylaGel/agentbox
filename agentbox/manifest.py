"""agentbox.yaml manifest schema.

Design principles (baked in as defaults, not opt-ins):
- Guardrails are enforced OUTSIDE the agent: network isolation, resource
  limits, cost caps, and TTLs live in the container runtime and the LLM
  proxy, never in the agent's own logic.
- LLM-agnostic via a single seam: every agent only ever sees an
  OpenAI-compatible endpoint (a LiteLLM proxy sidecar). Swapping
  Anthropic -> OpenAI -> Ollama -> Docker Model Runner is a config change.
- Least privilege: non-root, read-only rootfs, cap_drop ALL, internal-only
  networks, secrets mounted as files (never env vars).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class ModelConfig(BaseModel):
    """LLM provider config. Translated into a LiteLLM proxy config, so any
    provider LiteLLM supports works: anthropic, openai, gemini, groq,
    bedrock, azure, ollama, docker-model-runner (openai-compatible), etc."""

    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    # Path to a file containing the API key. Mounted as a Docker secret.
    # Never passed as an environment variable (visible in inspect/ps/proc).
    api_key_file: Optional[str] = None
    # For local/self-hosted providers (ollama, docker model runner, vllm)
    api_base: Optional[str] = None
    # Hard cost ceiling enforced by the proxy, not the agent.
    budget_usd: Optional[float] = 25.0
    budget_duration: str = "30d"
    rpm: Optional[int] = 60
    tpm: Optional[int] = None


class ToolsConfig(BaseModel):
    """MCP tool access, brokered through docker/mcp-gateway."""

    mcp_servers: list[str] = Field(default_factory=list)
    # The MCP gateway needs the docker socket to spawn per-server containers.
    # This is a real security tradeoff — off by default, warn loudly when on.
    allow_docker_socket: bool = False


class Limits(BaseModel):
    cpus: float = 1.0
    memory: str = "1g"
    pids: int = 256
    # Agents that should take 30 seconds sometimes try to run for 30 hours.
    ttl_minutes: int = 30


def normalize_domain(d: str) -> str:
    """Accept a URL or bare domain; return a bare lowercase domain."""
    return d.strip().lower().split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]


class AgentConfig(BaseModel):
    image: str
    command: Optional[list[str]] = None
    # Role / standing instructions for the agent ("You are a research
    # analyst…"). Injected as $AGENT_ROLE — it's a prompt, not a secret.
    role: Optional[str] = None
    # Only the workspace is mounted. Never ~/.ssh, ~/.aws, docker.sock,
    # or parent dirs that happen to contain a .env.
    workspace: str = "./workspace"
    workspace_read_only: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    # Extra egress domains beyond the LLM proxy / MCP gateway (e.g.
    # [arxiv.org, stratechery.com]). Enforced by a per-agent squid allowlist
    # sidecar; deny-by-default otherwise.
    egress: list[str] = Field(default_factory=list)

    @field_validator("egress")
    @classmethod
    def normalize_domains(cls, v: list[str]) -> list[str]:
        return [d for d in (normalize_domain(d) for d in v) if d]

    @field_validator("env")
    @classmethod
    def no_secret_looking_env(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
        if bad:
            raise ValueError(
                f"env vars {bad} look like secrets. Use api_key_file / file-mounted "
                "secrets instead — env vars leak via docker inspect, ps and /proc."
            )
        return v


class Hardening(BaseModel):
    non_root: bool = True
    read_only_rootfs: bool = True
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    tmpfs_size: str = "100m"


class Manifest(BaseModel):
    version: int = 1
    project: str = "agentbox"
    model: ModelConfig = Field(default_factory=ModelConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    hardening: Hardening = Field(default_factory=Hardening)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def dump(self) -> str:
        return yaml.safe_dump(self.model_dump(exclude_none=True), sort_keys=False)


DEFAULT_MANIFEST = """\
version: 1
project: my-agents

# ── LLM (swap providers freely — agents never know the difference) ──────────
# Every agent talks to an OpenAI-compatible LiteLLM proxy sidecar.
# provider can be: anthropic | openai | gemini | groq | bedrock | azure |
#                  ollama | openai_compatible (Docker Model Runner, vLLM, ...)
model:
  provider: anthropic
  model: claude-sonnet-4-6
  api_key_file: .agentbox/secrets/llm_api_key   # file, never an env var
  budget_usd: 25          # hard cap enforced at the proxy
  budget_duration: 30d
  rpm: 60

# ── Tools via MCP gateway (optional) ─────────────────────────────────────────
tools:
  mcp_servers: []          # e.g. [duckduckgo, fetch]
  allow_docker_socket: false

# ── Agents ───────────────────────────────────────────────────────────────────
agents:
  worker:
    image: python:3.12-slim
    command: ["python", "-c", "print('hello from an isolated agent')"]
    # role: You are a research analyst…   # injected as $AGENT_ROLE
    workspace: ./workspace
    limits:
      cpus: 1.0
      memory: 1g
      pids: 256
      ttl_minutes: 30
    # Allowed websites beyond the LLM proxy / MCP gateway — enforced by a
    # per-agent allowlist proxy sidecar. Empty = no direct egress at all.
    egress: []             # e.g. [arxiv.org, stratechery.com]

# ── Container hardening (sane defaults, override only if you must) ──────────
hardening:
  non_root: true
  read_only_rootfs: true
  drop_all_capabilities: true
  no_new_privileges: true
"""
