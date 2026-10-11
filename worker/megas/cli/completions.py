"""Generate shell completions for bash, zsh, fish, and PowerShell."""

from __future__ import annotations

from typing import Iterable

# Static completion sketches — keep aligned with build_parser command tree.
TOP_LEVEL = [
    "auth",
    "config",
    "status",
    "doctor",
    "version",
    "workers",
    "runs",
    "monitor",
    "operate",
    "security",
    "notifications",
    "connect",
    "pair",
    "start",
    "stop",
    "logs",
    "allow",
    "update",
    "worker",
    "completion",
]

NESTED: dict[str, list[str]] = {
    "auth": ["login", "logout", "status"],
    "config": ["path", "list", "get", "set", "unset"],
    "workers": ["list", "inspect", "pair", "start", "stop", "restart", "capabilities", "logs", "cancel"],
    "runs": ["list", "inspect", "watch", "cancel", "retry", "evidence"],
    "monitor": ["status", "events", "tail", "incidents", "incident", "alerts", "rules", "health"],
    "monitor incident": ["show", "acknowledge", "assign", "mute", "resolve"],
    "operate": ["browser", "windows", "stop"],
    "operate browser": ["start", "sessions", "inspect", "stop"],
    "operate windows": ["start", "sessions"],
    "security": ["findings", "finding", "audit", "remediation"],
    "security finding": ["show"],
    "security audit": ["start", "status"],
    "security remediation": ["review", "approve", "reject", "rollback"],
    "notifications": ["list", "watch", "acknowledge"],
    "completion": ["bash", "zsh", "fish", "powershell"],
}


def bash_script() -> str:
    commands = " ".join(TOP_LEVEL)
    nested_cases = []
    for key, values in NESTED.items():
        words = key.split()
        if len(words) == 1:
            nested_cases.append(
                f"""    {words[0]})
      COMPREPLY=( $(compgen -W "{' '.join(values)}" -- "$cur") )
      return 0
      ;;"""
            )
    body = "\n".join(nested_cases)
    return f"""# megas bash completion
_megas_completion() {{
  local cur prev
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  if [[ ${{COMP_CWORD}} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "{commands}" -- "$cur") )
    return 0
  fi
  case "${{COMP_WORDS[1]}}" in
{body}
  esac
}}
complete -F _megas_completion megas
"""


def zsh_script() -> str:
    top = " ".join(f"'{c}'" for c in TOP_LEVEL)
    return f"""#compdef megas
_megas() {{
  local -a commands
  commands=({top})
  _arguments '1:command:->cmds' '*::arg:->args'
  case $state in
    cmds) _describe 'command' commands ;;
  esac
}}
compdef _megas megas
"""


def fish_script() -> str:
    lines = ["# megas fish completion"]
    for cmd in TOP_LEVEL:
        lines.append(f"complete -c megas -f -n '__fish_use_subcommand' -a {cmd}")
    for key, values in NESTED.items():
        parts = key.split()
        if len(parts) != 1:
            continue
        for value in values:
            lines.append(
                f"complete -c megas -f -n '__fish_seen_subcommand_from {parts[0]}' -a {value}"
            )
    return "\n".join(lines) + "\n"


def powershell_script() -> str:
    commands = ", ".join(f"'{c}'" for c in TOP_LEVEL)
    return f"""Register-ArgumentCompleter -CommandName megas -ScriptBlock {{
  param($wordToComplete, $commandAst, $cursorPosition)
  $commands = @({commands})
  $commands | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
    [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
  }}
}}
"""


def render(shell: str) -> str:
    shell = shell.lower()
    if shell == "bash":
        return bash_script()
    if shell == "zsh":
        return zsh_script()
    if shell == "fish":
        return fish_script()
    if shell in {"powershell", "pwsh"}:
        return powershell_script()
    raise ValueError(f"Unsupported shell: {shell}")


__all__ = ["render", "TOP_LEVEL", "NESTED"]
