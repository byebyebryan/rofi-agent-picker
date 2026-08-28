# Rofi Agent Picker

`rofi-agent-picker` is a standalone Rofi script-mode picker for Codex CLI,
Claude Code, and OpenCode sessions.  It ports the discovery and tmux-opening
engine from DMS Agent Picker while keeping Rofi responsible only for the flat,
searchable presentation.

Version `0.1.0` supports Python 3.11+ and has no runtime package dependencies.
The current core contract requires Python 3, the Codex CLI, SSH, tmux, and a
terminal with `-e` support.  Claude Code and OpenCode are optional provider
tools: their sessions are included only on hosts where the corresponding
binary is installed.  Remote hosts need Python 3, tmux, and whichever provider
tools they expose, but do not need this repository installed.

## Install and run

The executable can be run directly from a checkout:

```sh
./bin/rofi-agent-picker list | jq
rofi -show agent -modes "agent:$(pwd)/bin/rofi-agent-picker" \
  -kb-custom-1 Alt+r -eh 2
```

The normal Rofi invocation is configured as a script mode.  `Mod+A` or a
similar Niri binding can invoke it with `rofi -show agent`.  The prompt is
`Agents`; Rofi's normal arrow-key navigation, filtering, and Enter selection
remain available.  `Alt+R` performs a bounded foreground refresh.  Custom
input and deletion are disabled in this flat v0.1 UI.  Rofi must be launched
with `-eh 2` so each list element reserves height for both display lines.

Rows use a two-line layout: the session title is primary, while a smaller
secondary line shows the display host, shortened working directory, age, and
active/idle state.  The provider is represented by a bundled icon; provider
names and aliases remain in the row's filter text and invisible Rofi metadata,
so searching for `codex`, `claude`, `Claude Code`, or `opencode` still works.
The complete session identity is carried in Rofi's `info` metadata, not parsed
from visible text.  Active rows are marked with Rofi's active-row metadata.  A
successful selection focuses an existing Niri terminal when possible or
resumes/creates the compatible DMS tmux session and launches the terminal
detached.  Icon provenance and trademark notes are in [`ASSETS.md`](ASSETS.md).

## Configuration

Configuration is optional and lives at
`$XDG_CONFIG_HOME/rofi-agent-picker/config.toml`, or
`~/.config/rofi-agent-picker/config.toml`.  The accepted keys and an example
are in [`examples/config.toml`](examples/config.toml):

```toml
hosts = ["laptop.lan"]
host_routes = ["workstation=workstation-vpn.example|workstation.example"]
aliases = ["legacy-host=workstation"]
terminal = "ghostty"
max_sessions = 40
refresh_seconds = 30
ssh_connect_timeout = 2
ssh_connection_attempts = 1
```

Host routes take precedence over `hosts`.  Routes use
`logical=preferred|fallback`; the logical name is displayed and the selected
route is retained for opening.  Malformed TOML, unknown keys, wrong types, and
out-of-range values are reported visibly in Rofi.  Diagnostic CLI values such
as `--route`, `--host`, `--alias`, `--timeout`, and the SSH policy options
override the file for side-by-side testing.

## Cache visibility

The picker stores a private, versioned snapshot under
`$XDG_CACHE_HOME/rofi-agent-picker/`, or `~/.cache/rofi-agent-picker/`.  The
directory is mode 0700 and cache/lock files are mode 0600.  Writes use a
temporary file, fsync, and atomic replacement.  The snapshot fingerprint
includes host routes, hosts, aliases, session limit, and SSH policy, so a
discovery-affecting configuration change causes a synchronous refresh.

On a cache miss, the first invocation refreshes synchronously.  A fresh cache
renders immediately.  A stale cache renders immediately with a short
`Refreshing in background` message and starts at most one detached refresh.
While that worker is running, the open dialog polls the marker about once per
second and replaces the cached rows as soon as the fresh snapshot is written;
the status then clears and polling stops.  A failed or stalled worker also
stops polling and clears the transient status while leaving the cached rows
usable.  Current refresh/provider errors are shown for about three seconds and
then cleared automatically; the rows remain available throughout.  The new
result is also visible the next time the picker opens or after `Alt+R`.
Per-host snapshots and rows from failed provider stages are retained while a
host is unavailable, and current errors are summarized in the message area.
There is intentionally no resident process or push-update channel.

## Diagnostic CLI

The same executable has a JSON CLI when called without `ROFI_RETV`:

```sh
./bin/rofi-agent-picker list --limit 40
./bin/rofi-agent-picker list --route 'workstation=workstation-vpn.example|workstation.example' --stream
./bin/rofi-agent-picker active
./bin/rofi-agent-picker open --host local --id UUID --name project --cwd "$PWD" --detach
./bin/rofi-agent-picker open-claude --host local --id UUID --detach
./bin/rofi-agent-picker open-opencode --host local --id ses_... --detach
./bin/rofi-agent-picker refresh
```

The provider engine preserves DMS-created tmux option names and opening
behavior, including Codex `@codex_thread_id`, Claude
`@claude_session_id`, OpenCode `@opencode_session_id`, waiting-session reuse,
Niri window focus, and remote SSH attach behavior.  OpenCode discovery keeps
the root-only `parent_id IS NULL` filter and all-project scope.

## Deployment and ownership

This repository is the canonical implementation of the deployed Agent Picker.
Chezmoi pins the release archive and owns the Rofi mode, its host configuration,
and the Niri binding.  DMS remains responsible for the bar, notifications,
idle handling, lock screen, polkit, and the general Spotlight launcher.

The former DMS Agent Picker repository is retained for compatibility and
history, but is deprecated; new picker behavior belongs here.  The flat Rofi
presentation intentionally keeps layered navigation, project scoping, and
synthetic-session cleanup out of scope until they have a separately reviewed
design.
