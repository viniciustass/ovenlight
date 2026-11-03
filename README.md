<p align="center"><img src="docs/logo.svg" width="96" alt="Ovenlight logo"></p>

<h1 align="center">Ovenlight</h1>

<p align="center"><b>A session explorer for Claude Code.</b><br>
Find, follow, reopen and talk to your Claude Code sessions, from any folder on your machine.</p>

<p align="center">
<a href="https://github.com/viniciustass/ovenlight/actions/workflows/tests.yml"><img src="https://github.com/viniciustass/ovenlight/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
<img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="python 3.9+">
<img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey" alt="macOS | Linux">
<img src="https://img.shields.io/badge/dependencies-none-brightgreen" alt="no dependencies">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-orange" alt="MIT"></a>
</p>

> *Ovenlight* is the little light you switch on to peek through the oven window and see whether it's done:
> Claude Code sessions "bake", "cook" and "putter" while you wait.
>
> Ovenlight is an independent project, **not affiliated with or endorsed by Anthropic**.
> Claude and Claude Code are trademarks of Anthropic.

---

## Why

If you use Claude Code across many projects, you lose track: which folder a session started in, how many there
are, what is running right now, and how to get back to an old conversation. Ovenlight indexes every session on
your machine, makes them searchable, shows what is live, and lets you reopen or message them, from a terminal or
a local web UI.

## Features

- **Global inventory and search** over every Claude Code transcript (`~/.claude/projects`): full-text search
  (SQLite FTS5) with filters like `project:` `after:7d` `in:bash` `file:auth.py` `used:Edit` `-docker` `"exact phrase"`.
- **Live view** of the sessions running right now (terminal, background, tmux and browser sessions), with
  state, folder and a live tail of each conversation. **Grid mode** shows them all side by side; drag to reorder.
- **Reopen anywhere**: in a new Terminal/iTerm window in the right folder, or **inside the browser** in a real
  terminal (xterm.js over a local WebSocket). Conflict warnings stop you from opening the same session twice.
- **Talk to sessions**: send prompts to background, tmux-managed and browser sessions; message interactive
  sessions through Claude Code's own session messaging; or ask a *copy* (fork) of a conversation.
- **History that survives cleanup**: optional backups of transcripts and an index that keeps sessions searchable
  after Claude Code's 30-day cleanup.
- **A console-style web UI**: dark theme, markdown rendering like the Claude Code terminal, keyboard shortcuts,
  resizable panes, and a short intro animation (skippable, off with `fx` or reduce-motion).
- **No dependencies**: Python standard library only. Runs from a single file.

## Install

Requirements: **macOS or Linux**, **Python 3.9+** with SQLite FTS5 (standard on macOS and most distros),
and [Claude Code](https://docs.claude.com/en/docs/claude-code) installed. `tmux` is optional.

**One line (recommended):**

```bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | bash
```

It checks Python 3.9+ and SQLite FTS5, downloads `ovenlight.pyz` from the latest release, **verifies its SHA-256**,
installs it as `~/.local/bin/ovenlight` and adds that folder to your `PATH` if needed. Read it first if you like:
[install.sh](install.sh).

**Manual single file:**

```bash
mkdir -p ~/.local/bin
curl -fL -o ~/.local/bin/ovenlight https://github.com/viniciustass/ovenlight/releases/latest/download/ovenlight.pyz
chmod +x ~/.local/bin/ovenlight
```

**With pipx:** `pipx install git+https://github.com/viniciustass/ovenlight`

**From source:** `git clone https://github.com/viniciustass/ovenlight && cd ovenlight && python3 -m ovenlight doctor`

Step-by-step guide, updating, uninstalling and troubleshooting: **[docs/INSTALL.md](docs/INSTALL.md)**
· em português: **[docs/INSTALACAO.md](docs/INSTALACAO.md)**.

## Quick start

```bash
ovenlight doctor                 # check the environment and a sample of your transcripts
ovenlight index --backup         # index ~/.claude/projects (and back up transcripts)
ovenlight web --open             # open the web UI (127.0.0.1:7777)

ovenlight search webhook stripe project:shop after:2w in:bash -docker
ovenlight live                   # sessions running now
ovenlight open a1b2c3d4          # reopen a session in a new terminal window, in its folder
```

The first time, open the full address printed by `ovenlight web` (it contains `?t=…`) to authorize the browser.

## Commands

| Command | What it does |
|---|---|
| `index [--force] [--backup] [--outputs] [-v]` | index transcripts (incremental) |
| `search <query> [-n 20] [--order recent] [--json]` | advanced search (alias `s`); syntax: `ovenlight search-help` |
| `list [filters] [-n 30]` | list sessions with the same filters (alias `ls`) |
| `show <id> [--tail N] [--full] [--outputs]` | show a session (an ID prefix is enough) |
| `open <id> [--shell] [--no-claude] [--bg] [--fork] [--managed] [--here] [--this-terminal] [--print] [--force]` | reopen a session, or a shell (with a new `claude`) in its folder (alias `resume`) |
| `live [--follow] [--json]` | sessions running now (alias `ps`) |
| `send <target> <text...>` | send a prompt / message to a live session |
| `ask <id> <text...> [--no-fork]` | ask a copy (fork) of a session |
| `stop <target>` | stop a background or tmux-managed session |
| `run [folder] [--detached] [-- claude args]` | start `claude` inside an Ovenlight tmux session |
| `web [--port 7777] [--open] [--no-auto] [--no-terminal]` | local web UI (alias `serve`) |
| `stats`, `tag <id> <tag> [--remove]`, `star <id>` | totals; organize |
| `backup`, `restore <id>` | copy transcripts aside; restore a deleted one |
| `hook [--snippet]` | `SessionEnd` hook integration (prints the JSON, never edits your settings) |
| `doctor` | diagnostics |

## Sending prompts: what works where

| Session | How it started | Ovenlight can… |
|---|---|---|
| Browser terminal | **▸ resume here** / **▸ shell + claude** in the web UI | type into it directly |
| tmux-managed | `ovenlight run [folder]` | type into it directly (`tmux paste-buffer`) |
| Interactive, plain terminal | you typed `claude` | **message** it via Claude Code session messaging (a tiny `claude -p` courier, ~5 s, under a cent) |
| Background | `claude --bg`, `/bg` | experimental pty send (not validated yet) |
| any | | **ask a copy** (fork) without touching the original |

Sessions started with `--dangerously-skip-permissions` are in a different permission class from the courier, so
Claude Code **holds** those messages until you approve them in that session's terminal. That is a deliberate
Claude Code safeguard; Ovenlight shows a notice and does not bypass it.

## Security and privacy

- Everything stays on your machine. The web server listens on **127.0.0.1 only**, requires a random token
  (`~/.ovenlight/token`, mode 0600, `HttpOnly; SameSite=Strict` cookie), validates `Host`, and only accepts
  requests and WebSockets whose `Origin` is Ovenlight itself (same host **and port**).
- The browser terminal only runs fixed programs (`claude --resume <id>` or your shell), never a command sent by
  the page. Disable it with `ovenlight web --no-terminal`.
- Transcripts are treated as untrusted content: everything is escaped before rendering (markdown included).
- `~/.ovenlight` is mode 0700; the database and backups are 0600. Every command Ovenlight runs or sends is
  appended to `~/.ovenlight/audit.log`.
- Ovenlight reads `~/.claude` and never writes to it, except `ovenlight restore` (copies a backed-up transcript
  back). Programs it starts do not inherit the markers of a parent Claude Code session.
- No telemetry, no network calls except what `claude` itself does. xterm.js is bundled, nothing loads from a CDN.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OVENLIGHT_HOME` | `~/.ovenlight` | data folder (index, backups, token, audit log) |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where Claude Code keeps transcripts |
| `OVENLIGHT_TERMINAL` | Terminal.app | `iterm` to open iTerm on macOS |
| `OVENLIGHT_TERMINAL_CMD` | auto | any terminal, e.g. `wezterm start -- bash -lc {cmd}` |
| `OVENLIGHT_COURIER_MODEL` | `claude-haiku-5-5` | model used to deliver session messages |
| `OVENLIGHT_INDEX_RESULTS` | off | `1` also indexes tool output (much larger database) |

## Status and limitations

Prototype (v0.1). Validated on macOS against real transcripts and live sessions, and in Chrome. Not yet validated:
sending through tmux to the real Claude Code TUI, the experimental background pty send, and Linux desktop
terminals. Claude Code's transcript format and agent view are internal and may change between versions;
`ovenlight doctor` reports unknown entry types. Windows is not supported.

The original design document (Portuguese) is in [docs/design-spec.pt-BR.md](docs/design-spec.pt-BR.md).

## Development

```bash
python3 -m unittest discover -s tests -v   # unit tests, synthetic data, no network
python3 scripts/build_pyz.py              # builds dist/ovenlight.pyz
```

## License

[MIT](LICENSE). Bundles xterm.js (MIT).
