# Instalando o Ovenlight, passo a passo

*In English: [INSTALL.md](INSTALL.md).*

O Ovenlight é um único programa em Python, sem dependências. Roda no **macOS** e no **Linux**.

> O Ovenlight é um projeto independente, não afiliado nem endossado pela Anthropic.
> Claude e Claude Code são marcas da Anthropic.

## 1. Confira os pré-requisitos

| Você precisa de | Como conferir | Se não tiver |
|---|---|---|
| Python 3.9 ou mais novo | `python3 --version` | macOS: `brew install python` · Debian/Ubuntu: `sudo apt install python3` |
| SQLite com FTS5 (busca de texto) | `python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('ok')"` | use o Python do Homebrew, do python.org ou da sua distribuição (todos têm FTS5) |
| Claude Code | `claude --version` | <https://docs.claude.com/en/docs/claude-code> (a busca funciona sem ele; reabrir e mandar mensagens precisam dele) |
| *(opcional)* tmux | `tmux -V` | `brew install tmux` / `sudo apt install tmux`, só para o `ovenlight run` |

## 2. Instale

### Opção A: uma linha (recomendada)

```bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | bash
```

O instalador:

1. confere o sistema, o Python 3.9+ e o SQLite FTS5;
2. baixa o `ovenlight.pyz` da última release no GitHub e **confere o SHA-256** (se o arquivo não bater, ele não
   instala);
3. instala como `~/.local/bin/ovenlight`;
4. coloca `~/.local/bin` no seu `PATH`, no perfil do seu shell, se ainda não estiver lá (com um comentário).

Opções, antes do `bash`:

```bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | OVENLIGHT_VERSION=v0.1.0 bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | OVENLIGHT_INSTALL_DIR=~/bin bash
curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | OVENLIGHT_NO_MODIFY_PATH=1 bash
```

Prefere ler antes de rodar? Baixe, leia e depois execute:
`curl -fsSLO https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh && less install.sh && bash install.sh`

### Opção B: arquivo único, manual

```bash
mkdir -p ~/.local/bin
curl -fL -o ~/.local/bin/ovenlight https://github.com/viniciustass/ovenlight/releases/latest/download/ovenlight.pyz
chmod +x ~/.local/bin/ovenlight
```

Se aparecer "command not found: ovenlight", coloque `~/.local/bin` no `PATH`
(zsh: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc` e abra um terminal novo).

### Opção C: pipx

```bash
pipx install git+https://github.com/viniciustass/ovenlight
```

### Opção D: a partir do código

```bash
git clone https://github.com/viniciustass/ovenlight
cd ovenlight
python3 -m ovenlight doctor                 # roda sem instalar
python3 scripts/build_pyz.py                # ou gera o seu próprio dist/ovenlight.pyz
```

## 3. Primeiro uso

```bash
ovenlight doctor
```

Deve mostrar `ok` para o SQLite FTS5, para a pasta de transcritos (`~/.claude/projects`) e para o `claude` no
PATH, além de uma amostra dos seus transcritos recentes com **0 linhas ilegíveis** ("0 unreadable").

```bash
ovenlight index --backup
```

Indexa todas as sessões (de forma incremental: nas próximas vezes só lê o que mudou) e copia os transcritos para
`~/.ovenlight/backup`, para continuarem pesquisáveis depois da limpeza de 30 dias do Claude Code.

```bash
ovenlight web --open
```

Sobe a interface web local em <http://127.0.0.1:7777> e abre o navegador. **Na primeira vez, use o endereço completo
que aparece no terminal** (termina em `?t=…`): ele autoriza aquele navegador com um cookie. Para parar, `Ctrl+C`.
Use `--port 7788` para outra porta e `--no-terminal` para desligar o terminal dentro do navegador.

## 4. Configurações opcionais

**Manter o índice sempre atualizado** (hook `SessionEnd` do Claude Code). Mostre o trecho e cole você mesmo no
`~/.claude/settings.json`; o Ovenlight nunca edita as suas configurações:

```bash
ovenlight hook --snippet
```

**iTerm em vez do Terminal.app** (macOS): `export OVENLIGHT_TERMINAL=iterm`.
**Qualquer outro terminal**: `export OVENLIGHT_TERMINAL_CMD='wezterm start -- bash -lc {cmd}'`.

**Mandar prompts via tmux**: instale o tmux e inicie as sessões com `ovenlight run ~/seu/projeto`.

## 5. Atualizar

Rode o instalador de novo (Opção A), repita a Opção B, ou `pipx upgrade ovenlight`. Os seus dados em
`~/.ovenlight` são mantidos. Depois, reinicie o `ovenlight web`; a página recarrega os arquivos novos sozinha.

## 6. Desinstalar

```bash
rm ~/.local/bin/ovenlight          # ou: pipx uninstall ovenlight
rm -rf ~/.ovenlight                # índice, backups, token e audit log (o seu ~/.claude nunca é alterado)
```

Se o instalador adicionou a linha `# added by the ovenlight installer` ao perfil do seu shell, remova-a.

## Problemas comuns

| Sintoma | Solução |
|---|---|
| `command not found: ovenlight` | `~/.local/bin` não está no `PATH`: abra um terminal novo ou adicione (veja a Opção B) |
| "Your SQLite has no FTS5" | use um Python do Homebrew, do python.org ou da distribuição; builds do `pyenv` podem precisar dos headers do `sqlite3` |
| `Address already in use` | já há um `ovenlight web` rodando; pare-o ou use `--port 7788` |
| A página pede para abrir o endereço com `?t=` | o navegador ainda não tem o cookie: copie o endereço completo do terminal |
| Logo ou layout antigo depois de atualizar | reinicie o `ovenlight web` e recarregue (Cmd/Ctrl+Shift+R) |
| "reopen" não faz nada no macOS | na primeira vez o macOS pede permissão para o Python controlar o Terminal: aceite (Ajustes do Sistema › Privacidade › Automação) |
| A mensagem para uma sessão "aguarda sua aprovação" | essa sessão roda com `--dangerously-skip-permissions`; aprove no terminal dela (proteção do Claude Code, proposital) |
| Uma sessão não aparece no Live | rode `ovenlight live --json` e abra uma issue com a saída (a detecção de processos é heurística) |
| `ovenlight doctor` mostra linhas ilegíveis ou tipos desconhecidos | o Claude Code mudou o formato dos transcritos: abra uma issue |

Issues: <https://github.com/viniciustass/ovenlight/issues>
