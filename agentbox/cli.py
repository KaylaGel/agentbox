"""agentbox CLI — spin up LLM-agnostic agents in hardened containers.

Commands:
  init    Scaffold agentbox.yaml + .agentbox/ in the current directory
  build   Validate the manifest and (re)generate compose + proxy config
  up      Start the stack (proxy, gateway, agents)
  run     Run one agent ephemerally with a task string
  ps      List running agentbox containers
  logs    Tail an agent's logs
  reap    Kill containers that have exceeded their TTL
  down    Stop the stack
  nuke    Stop the stack and delete generated state (keeps secrets)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
import yaml

from .compose import write_all
from .manifest import DEFAULT_MANIFEST, Manifest, normalize_domain

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Hardened, LLM-agnostic agent containers.")

MANIFEST = Path("agentbox.yaml")
COMPOSE = Path(".agentbox/compose.yaml")


def _sh(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, **kw)


def _require_manifest() -> Manifest:
    if not MANIFEST.exists():
        typer.secho("No agentbox.yaml here — run `agentbox init` first.", fg="red")
        raise typer.Exit(1)
    try:
        return Manifest.load(MANIFEST)
    except Exception as e:
        typer.secho(f"Manifest invalid: {e}", fg="red")
        raise typer.Exit(1)


def _build(m: Manifest) -> None:
    compose_path, _ = write_all(m, Path("."))
    typer.secho(f"✓ wrote {compose_path}", fg="green")
    if m.tools.allow_docker_socket:
        typer.secho(
            "⚠ tools.allow_docker_socket is ON — the MCP gateway can control your "
            "Docker daemon. That is effectively root on this host. Only enable "
            "for gateways/servers you trust.", fg="yellow",
        )
    key_file = m.model.api_key_file
    if key_file and not Path(key_file).exists():
        typer.secho(f"⚠ secret file {key_file} does not exist yet — create it "
                    f"(chmod 600) before `agentbox up`.", fg="yellow")


def _write_secret(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip() + "\n")


# Providers that run on your own hardware: no API key, endpoint URL instead.
LOCAL_PROVIDERS = {"ollama", "openai_compatible"}
PROVIDERS = ["anthropic", "openai", "gemini", "groq", "bedrock", "azure",
             "ollama", "openai_compatible"]
PROVIDER_CHOICES = " | ".join(PROVIDERS)


def _select(title: str, options: list[str], default: int = 0) -> str:
    """Arrow-key option menu (↑/↓ or j/k, Enter to pick). Falls back to a
    plain text prompt where raw terminal mode isn't available (e.g. Windows)."""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        # No raw-mode terminal (Windows, pipes, test harnesses…).
        return typer.prompt(f"{title} ({' | '.join(options)})", default=options[default])

    idx = default

    def draw() -> None:
        for i, opt in enumerate(options):
            line = typer.style(f"> {opt}", fg="cyan") if i == idx else f"  {opt}"
            sys.stdout.write("\x1b[2K" + line + "\n")
        sys.stdout.flush()

    sys.stdout.write(f"{title} (↑/↓, Enter):\n")
    draw()
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x1b":
                ch = {"[A": "k", "[B": "j"}.get(sys.stdin.read(2), "")
            if ch == "k":
                idx = (idx - 1) % len(options)
            elif ch == "j":
                idx = (idx + 1) % len(options)
            sys.stdout.write(f"\x1b[{len(options)}A")
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    # Collapse the menu into a single confirmation line.
    sys.stdout.write(f"\x1b[{len(options) + 1}A\x1b[J{title}: {options[idx]}\n")
    sys.stdout.flush()
    return options[idx]
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.1",
    "gemini": "gemini-2.5-pro",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "llama3.1",
    "openai_compatible": "ai/smollm2",
}
DEFAULT_API_BASE = {
    "ollama": "http://host.docker.internal:11434",
    "openai_compatible": "http://model-runner.docker.internal/engines/v1",
}
_KEY_LINE = "  api_key_file: .agentbox/secrets/llm_api_key   # file, never an env var"
# Placeholder lines in DEFAULT_MANIFEST swapped out when init collects values.
_ROLE_LINE = "    # role: You are a research analyst…   # injected as $AGENT_ROLE"
_EGRESS_LINE = "    egress: []             # e.g. [arxiv.org, stratechery.com]"


@app.command()
def init(project: str = typer.Option("my-agents", help="Project name"),
         provider: str = typer.Option(None, "--provider",
                                      help=f"LLM provider ({PROVIDER_CHOICES}); prompted for if omitted"),
         model: str = typer.Option(None, "--model",
                                   help="Model name; sensible per-provider default if omitted"),
         api_key: str = typer.Option(None, "--api-key",
                                     help="Provider API key; prompted for if omitted"),
         api_base: str = typer.Option(None, "--api-base",
                                      help="Endpoint URL for local/self-hosted providers"),
         role: str = typer.Option(None, "--role",
                                  help="Agent role/personality, injected as $AGENT_ROLE"),
         egress: str = typer.Option(None, "--egress",
                                    help="Comma-separated websites the agent may reach")) -> None:
    """Scaffold agentbox.yaml, workspace/, and .agentbox/secrets/."""
    if MANIFEST.exists():
        typer.secho("agentbox.yaml already exists; not overwriting.", fg="yellow")
        raise typer.Exit(1)

    interactive = sys.stdin.isatty()
    if provider is None and interactive:
        provider = _select("Provider", PROVIDERS)
    provider = (provider or "anthropic").strip()
    local = provider in LOCAL_PROVIDERS
    if model is None and interactive:
        model = typer.prompt("Model", default=DEFAULT_MODELS.get(provider, ""))
    model = (model or "").strip() or DEFAULT_MODELS.get(provider, "claude-sonnet-4-6")
    if local and api_base is None:
        default_base = DEFAULT_API_BASE.get(provider, "")
        api_base = (typer.prompt("Model endpoint URL", default=default_base)
                    if interactive else default_base)
    if role is None and interactive:
        role = typer.prompt("Agent role / personality (Enter to skip)",
                            default="", show_default=False)
    role = (role or "").strip()
    if egress is None and interactive:
        egress = typer.prompt("Websites the agent may reach, comma-separated "
                              "(Enter for none)", default="", show_default=False)
    domains = [d for d in (normalize_domain(d)
                           for d in (egress or "").replace(" ", ",").split(",")) if d]

    text = DEFAULT_MANIFEST.replace("my-agents", project)
    text = text.replace("provider: anthropic", f"provider: {provider}")
    text = text.replace("model: claude-sonnet-4-6", f"model: {model}")
    if role:
        # yaml-dump for correct quoting/wrapping, then re-indent to agent depth.
        dumped = yaml.safe_dump({"role": role}, width=68, allow_unicode=True).rstrip()
        text = text.replace(_ROLE_LINE,
                            "\n".join("    " + l for l in dumped.splitlines()))
    if domains:
        text = text.replace(_EGRESS_LINE, f"    egress: [{', '.join(domains)}]")
    if local:
        # No key needed; point the proxy at the local endpoint instead.
        # (--api-key still honored for authed self-hosted endpoints.)
        key_lines = (["  api_key_file: .agentbox/secrets/llm_api_key"] if api_key else [])
        text = text.replace(_KEY_LINE, "\n".join(key_lines + [f"  api_base: {api_base}"]))
    MANIFEST.write_text(text)
    Path("workspace").mkdir(exist_ok=True)
    Path(".agentbox/secrets").mkdir(parents=True, exist_ok=True)
    gitignore = Path(".gitignore")
    line = ".agentbox/\n"
    if not gitignore.exists() or line not in gitignore.read_text():
        with open(gitignore, "a") as f:
            f.write(line)
    typer.secho("✓ scaffolded agentbox.yaml (secrets dir is .agentbox/secrets/, "
                "gitignored)", fg="green")
    key_path = Path(".agentbox/secrets/llm_api_key")
    if api_key is None and not local and not key_path.exists() and interactive:
        api_key = typer.prompt(f"{provider} API key (Enter to skip)", default="",
                               hide_input=True, show_default=False)
    if api_key:
        _write_secret(key_path, api_key)
        typer.secho(f"✓ saved API key to {key_path} (mode 600)", fg="green")
        typer.echo("Next: `agentbox up`.")
    elif local:
        typer.echo(f"Local provider '{provider}' at {api_base} — no API key "
                   "needed. Next: `agentbox up`.")
    elif key_path.exists():
        typer.echo(f"Using existing key at {key_path}. Next: `agentbox up`.")
    else:
        typer.echo(f"Next: put your {provider} key in {key_path} (chmod 600), "
                   "then `agentbox build && agentbox up`.")


@app.command()
def build() -> None:
    """Validate manifest and regenerate compose + LiteLLM configs."""
    _build(_require_manifest())


@app.command()
def up(detach: bool = typer.Option(True, "--detach/--attach")) -> None:
    """Start proxy, gateway, and agents."""
    m = _require_manifest()
    _build(m)
    args = ["docker", "compose", "-f", str(COMPOSE), "up"]
    if detach:
        args.append("-d")
    raise typer.Exit(_sh(args).returncode)


@app.command()
def run(agent: str, task: str = typer.Argument("", help="Task passed as $AGENT_TASK")) -> None:
    """Run a single agent ephemerally (--rm) with an optional task string."""
    m = _require_manifest()
    _build(m)
    if agent not in m.agents:
        typer.secho(f"No agent '{agent}' in manifest. Have: {list(m.agents)}", fg="red")
        raise typer.Exit(1)
    ttl = m.agents[agent].limits.ttl_minutes
    args = ["docker", "compose", "-f", str(COMPOSE), "run", "--rm",
            "-e", f"AGENT_TASK={task}", f"agent-{agent}"]
    try:
        proc = _sh(args, timeout=ttl * 60)
        raise typer.Exit(proc.returncode)
    except subprocess.TimeoutExpired:
        typer.secho(f"⏱ agent '{agent}' exceeded its {ttl}m TTL — killed.", fg="red")
        _sh(["docker", "compose", "-f", str(COMPOSE), "kill", f"agent-{agent}"])
        raise typer.Exit(124)


@app.command()
def ps() -> None:
    """List running agentbox containers with their TTLs."""
    out = _sh(["docker", "ps", "--filter", "label=agentbox.project",
               "--format", "{{json .}}"], capture_output=True, text=True)
    rows = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
    if not rows:
        typer.echo("Nothing running.")
        return
    for r in rows:
        typer.echo(f"{r['Names']:<32} {r['Status']:<20} {r['Image']}")


@app.command()
def logs(agent: str, follow: bool = typer.Option(True, "--follow/--no-follow")) -> None:
    """Tail one agent's logs."""
    args = ["docker", "compose", "-f", str(COMPOSE), "logs", f"agent-{agent}"]
    if follow:
        args.append("-f")
    raise typer.Exit(_sh(args).returncode)


@app.command()
def reap() -> None:
    """Kill any agent container that has outlived its TTL label."""
    out = _sh(["docker", "ps", "--filter", "label=agentbox.agent",
               "--format", "{{.ID}}"], capture_output=True, text=True)
    killed = 0
    for cid in out.stdout.split():
        insp = _sh(["docker", "inspect", cid], capture_output=True, text=True)
        info = json.loads(insp.stdout)[0]
        ttl_min = int(info["Config"]["Labels"].get("agentbox.ttl_minutes", "0") or 0)
        if not ttl_min:
            continue
        started = datetime.fromisoformat(
            info["State"]["StartedAt"].split(".")[0] + "+00:00")
        age_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
        if age_min > ttl_min:
            _sh(["docker", "kill", cid], capture_output=True)
            typer.secho(f"✗ killed {cid[:12]} "
                        f"({info['Config']['Labels'].get('agentbox.agent')}, "
                        f"{age_min:.0f}m > {ttl_min}m TTL)", fg="red")
            killed += 1
    typer.echo(f"Reaped {killed} container(s)." if killed else "All within TTL.")


@app.command()
def down() -> None:
    """Stop the stack."""
    raise typer.Exit(_sh(["docker", "compose", "-f", str(COMPOSE), "down"]).returncode)


@app.command()
def nuke(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Stop everything and delete generated configs (keeps your secrets)."""
    if not yes and not typer.confirm("Stop stack and delete .agentbox generated files?"):
        raise typer.Exit(0)
    if COMPOSE.exists():
        _sh(["docker", "compose", "-f", str(COMPOSE), "down", "-v"])
    for f in ("compose.yaml", "litellm.yaml", "virtual_key"):
        p = Path(".agentbox") / f
        if p.exists():
            p.unlink()
    for p in Path(".agentbox").glob("egress-*.conf"):
        p.unlink()
    typer.secho("✓ nuked (secrets kept).", fg="green")


def main() -> None:  # console_scripts entry point
    app()


if __name__ == "__main__":
    sys.exit(main())
