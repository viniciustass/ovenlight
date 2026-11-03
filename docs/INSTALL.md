# Installing Ovenlight, step by step

*Em português: [INSTALACAO.md](INSTALACAO.md).*

Ovenlight is a single Python program with no dependencies. It runs on **macOS** and **Linux**.

## 1. Check the requirements

| You need | How to check | If missing |
|---|---|---|
| Python 3.9 or newer | `python3 --version` | macOS: `brew install python` · Debian/Ubuntu: `sudo apt install python3` |
| SQLite with FTS5 (full-text search) | `python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('ok')"` | use Python from Homebrew, python.org or your distro (all include FTS5) |
| Claude Code | `claude --version` | <https://docs.claude.com/en/docs/claude-code> (search works without it; reopening and messaging need it) |
| *(optional)* tmux | `tmux -V` | `brew install tmux` / `sudo apt install tmux`, only for `ovenlight run` |

## 2. Install

### Option A: one line (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | bash
```

The installer:

1. checks your OS, Python 3.9+ and SQLite FTS5;
2. downloads `ovenlight.pyz` from the latest GitHub release and **verifies its SHA-256** (it refuses to install a
   file that does not match);
3. installs it as `~/.local/bin/ovenlight`;
4. adds `~/.local/bin` to your `PATH` in your shell profile if it is not there yet (with a comment).

Options, set before `bash`:

```bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | OVENLIGHT_VERSION=v0.1.0 bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | OVENLIGHT_INSTALL_DIR=~/bin bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | OVENLIGHT_NO_MODIFY_PATH=1 bash
```

Prefer to read it before running? Download it, look, then run it:
`curl -fsSLO https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh && less install.sh && bash install.sh`

### Option B: manual single file

```bash
mkdir -p ~/.local/bin
curl -fL -o ~/.local/bin/ovenlight https://github.com/viniciustass/ovenlight/releases/latest/download/ovenlight.pyz
chmod +x ~/.local/bin/ovenlight
```

If `ovenlight` is "command not found", add `~/.local/bin` to your `PATH`
(zsh: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`, then open a new terminal).

### Option C: pipx

```bash
pipx install git+https://github.com/viniciustass/ovenlight
```

### Option D: from source

```bash
git clone https://github.com/viniciustass/ovenlight
cd ovenlight
python3 -m ovenlight doctor                 # run without installing
python3 scripts/build_pyz.py                # or build your own dist/ovenlight.pyz
```

## 3. First run

```bash
ovenlight doctor
```

You should see `ok` for SQLite FTS5, your transcripts folder (`~/.claude/projects`) and `claude` on PATH, plus a
sample of your recent transcripts with **0 unreadable** lines.

```bash
ovenlight index --backup
```

Indexes every session (incremental: later runs only read what changed) and copies transcripts to
`~/.ovenlight/backup`, so they stay searchable after Claude Code's 30-day cleanup.

```bash
ovenlight web --open
```

Starts the local web UI on <http://127.0.0.1:7777> and opens your browser. **The first time, use the full address
printed in the terminal** (it ends in `?t=…`): it authorizes that browser with a cookie. Stop with `Ctrl+C`.
Use `--port 7788` for another port and `--no-terminal` to disable the in-browser terminal.

## 4. Optional setup

**Keep the index fresh automatically** (Claude Code `SessionEnd` hook). Print the snippet and merge it into
`~/.claude/settings.json` yourself; Ovenlight never edits your settings:

```bash
ovenlight hook --snippet
```

**iTerm instead of Terminal.app** (macOS): `export OVENLIGHT_TERMINAL=iterm`.
**Any other terminal**: `export OVENLIGHT_TERMINAL_CMD='wezterm start -- bash -lc {cmd}'`.

**Send prompts through tmux**: install tmux and start sessions with `ovenlight run ~/your/project`.

## 5. Update

Run the installer again (Option A), or repeat Option B, or `pipx upgrade ovenlight`. Your data in `~/.ovenlight`
is kept. Restart `ovenlight web` afterwards; the page reloads fresh assets automatically.

## 6. Uninstall

```bash
rm ~/.local/bin/ovenlight          # or: pipx uninstall ovenlight
rm -rf ~/.ovenlight                # index, backups, token and audit log (your ~/.claude is never touched)
```

Remove the `# added by the ovenlight installer` line from your shell profile if the installer added it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: ovenlight` | `~/.local/bin` is not on your `PATH`: open a new terminal, or add it (see Option B) |
| "Your SQLite has no FTS5" | use a Python from Homebrew/python.org/your distro; `pyenv` builds may need `sqlite3` dev headers |
| `Address already in use` | another `ovenlight web` is running; stop it or use `--port 7788` |
| Page says to open the address with `?t=` | the browser has no auth cookie yet: copy the full URL from the terminal |
| Old logo or layout after updating | restart `ovenlight web`, then reload (Cmd/Ctrl+Shift+R) |
| "reopen" does nothing on macOS | the first time, macOS asks to let Python control Terminal: allow it in the prompt (System Settings › Privacy › Automation) |
| A message to a session "waits for your approval" | that session runs with `--dangerously-skip-permissions`; approve it in its terminal (Claude Code safeguard, by design) |
| Live view misses a session | run `ovenlight live --json` and open an issue with the output (process detection is heuristic) |
| `ovenlight doctor` shows unreadable lines or unknown entry types | Claude Code changed its transcript format: please open an issue |

Issues: <https://github.com/viniciustass/ovenlight/issues>
