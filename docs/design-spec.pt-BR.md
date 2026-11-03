# Ovenlight: especificação de implementação

> Inventário, busca, acompanhamento ao vivo e reabertura de sessões do Claude Code, em qualquer pasta do seu computador.
> Alvo inicial: **macOS e Linux**. Windows fica para depois.

**Nome:** Ovenlight (a luz do forno que você acende para espiar se já assou: as sessões do Claude Code "assam", "cozinham" e "putter" enquanto você espera).
**Marca:** o nome não usa "Claude" nem "Anthropic". O Claude Code é citado só de forma descritiva ("session explorer for Claude Code"), sempre com o aviso: *projeto independente, não afiliado à Anthropic; Claude e Claude Code são marcas da Anthropic.*
**Comando:** `ovenlight`. **Dados:** `~/.ovenlight/`. **Prefixo de sessões tmux:** `ovenlight-`. **Variáveis:** `OVENLIGHT_*`.
**Disponibilidade (checada em 03/11/2026):** livre no PyPI, no npm e no crates.io; no GitHub há 3 repositórios pessoais pequenos com o mesmo nome (`snowy-ghost/ovenlight`, `mcsr-cards/ovenlight`, `wcomartin/ovenlight-delight`). **Não verificado:** Homebrew, domínio e marcas registradas. Reserve o nome cedo publicando um pacote vazio no PyPI e no npm.

---

## 1. Problema

Quem usa o Claude Code em muitos projetos perde o controle: não sabe em que pasta começou uma sessão, quantas existem, o que está rodando agora, nem como retomar uma conversa antiga. Hoje o seletor nativo (`claude --resume`, `Ctrl+A`) lista sessões de todos os projetos, mas não tem busca avançada no conteúdo, não mostra o que está vivo, não envia comandos e não guarda histórico além da retenção padrão.

## 2. Objetivos

1. **Inventário global:** todas as sessões de todas as pastas, com contagem, filtros e facetas.
2. **Busca avançada** no conteúdo (pedidos, respostas, comandos, erros, arquivos).
3. **Ao vivo:** ver o que está aberto agora e acompanhar o que cada sessão está fazendo.
4. **Enviar comandos** às sessões que o Claude Code permite controlar.
5. **Reabrir** qualquer sessão no diretório certo, com aviso de conflito.
6. **Preservar histórico:** backup dos transcritos e histórico pesquisável mesmo depois da limpeza de 30 dias.

### Fora de escopo da v1

- Windows (tmux e `ps`/`lsof` não existem lá; exige ConPTY e outra detecção).
- Editar ou apagar transcritos do Claude Code por conta própria.
- Busca semântica e consulta em linguagem natural (v2).
- Sessões do app desktop, do claude.ai/code e da extensão do VS Code (cada um mantém sua própria lista).

---

## 3. O que sabemos do Claude Code (verificado na documentação)

Estes fatos vêm da documentação oficial e de guias sobre agent view lidos durante o planejamento. Reconfirme antes de implementar, porque o produto muda rápido.

| Tema | Fato |
|---|---|
| Armazenamento | Transcritos em `~/.claude/projects/<projeto>/<session-id>.jsonl`. `<projeto>` é o diretório de trabalho com caracteres não alfanuméricos trocados por `-`; acima de 200 caracteres, é truncado e recebe um hash. |
| Configuração | `CLAUDE_CONFIG_DIR` muda o local; `cleanupPeriodDays` (padrão 30) controla a retenção; `CLAUDE_CODE_SKIP_PROMPT_HISTORY` suprime escrita. |
| Formato | **Interno e instável** entre versões. A própria documentação recomenda `/export` ou `claude -p --output-format json` para integrações. |
| Retomar | `claude --continue`, `claude --resume` (seletor), `--resume <nome>`, `--resume <id>`, `--resume <caminho.jsonl>`, `--from-pr <n>`. |
| Retomar de qualquer pasta | `claude --resume <id>` funciona de qualquer diretório: procura no projeto atual e nos worktrees, depois em todos os outros projetos da máquina (só resolve se exatamente um outro projeto tiver o transcrito). |
| Pasta de origem sumiu | Se o worktree da sessão não existe mais, o Claude Code retoma na pasta atual. |
| Seletor | `Ctrl+A` todos os projetos, `Ctrl+W` worktrees, `Ctrl+B` branch, `Space` prévia, `Ctrl+R` renomear. |
| Sessões ocultas | Sessões de `claude -p` e do Agent SDK não aparecem no seletor nem no `--continue` (mas retomam por ID). Sessões cujo primeiro prompt foi `/loop` também ficam ocultas. |
| Nomes | O nome padrão (`<pasta>-<2 caracteres>`) **não** serve para retomar. Nome definido pelo usuário e título gerado servem. |
| Fork | `--fork-session` / `/branch` criam cópia com novo ID. Retomar a mesma sessão em dois terminais sem fork **mistura os transcritos**. |
| Agent view | `claude agents` (v2.1.139+, research preview). Linhas = sessões em background. `Space` abre peek (última saída ou pergunta pendente) e permite responder; `Enter`/`→` conecta. Estados: working, needs input, idle, completed, failed, stopped. |
| Comandos de shell | `claude --bg "<prompt>"`, `claude attach <id>`, `claude logs <id>`, `claude stop <id>` (alias `kill`), `claude rm <id>`, `claude respawn --all`, `/bg` dentro de uma sessão. |
| JSON | `claude agents --json` (verificado em 03/11/2026, v2.1.317): lista de `{pid, cwd, kind, startedAt (ms), sessionId, name, status}`. **Inclui sessões interativas** (`kind: "interactive"`, `status: busy/idle`), não só background. O valor de `kind` para background ainda não foi observado. |
| Retomar sessão rodando | `claude --resume <sessão> "prompt"` entrega o prompt à sessão em background e **abre (attach)** no terminal. Não abre, e não envia nada, se houver entrada/saída redirecionada ou flags como `--permission-mode`, `--model`, `--settings`, `--output-format`, `--max-turns`. Exige v2.1.285 ou posterior. |
| Hooks | `SessionEnd` recebe `transcript_path` no input. |
| Custo | Cada sessão em background consome rate limit de forma independente. |
| Retomada de contexto | O que é restaurado: histórico, modelo, agente, modo de permissão. Não são restaurados `--mcp-config`, `--settings`, `--plugin-dir`, `--add-dir`. |

### O que NÃO foi verificado (tratar como hipótese e validar com dados reais)

1. Campos exatos das linhas do `.jsonl` (usei `type`, `message`, `cwd`, `gitBranch`, `timestamp`, `isSidechain`, `isMeta`, `usage` e entradas `summary`/`custom-title`/`ai-title`, vindos de conhecimento prévio).
2. Esquema do `claude agents --json` e formato da saída de `claude logs`.
3. Formato exato do JSON de hooks em `~/.claude/settings.json`.
4. Nome do processo em instalações nativas (`claude`, ou um binário com número de versão em `.../claude/versions/`).
5. Envio de prompt a sessão em background por pseudo-terminal (ver seção 8).
6. Se `tmux paste-buffer -p` + `Enter` funciona bem com a TUI real do Claude Code (foi validado apenas com `cat`).

**Primeira tarefa da implementação:** rodar o `ovenlight doctor` (seção 11) contra os seus arquivos reais e ajustar o parser.

---

## 4. Arquitetura

```
              ┌────────────────────────── ovenlight (CLI) ─────────────────────────────┐
              │ indexar buscar listar ver reabrir vivo enviar executar web doctor ...│
              └───────┬───────────────┬──────────────────┬──────────────┬────────────┘
                      │               │                  │              │
               ┌──────▼─────┐  ┌──────▼─────┐   ┌────────▼───────┐ ┌────▼─────┐
               │  indexer   │  │   search   │   │  live / actions│ │   web    │
               │ parse.jsonl│  │ query→FTS5 │   │ ps lsof tmux   │ │ http.srv │
               └──────┬─────┘  └──────┬─────┘   │ claude agents  │ │ + ui.html│
                      │               │         └────────┬───────┘ └────┬─────┘
                 ┌────▼───────────────▼───┐              │              │
                 │SQLite+FTS5 (~/.ovenlight)│◄─────────────┴──────────────┘
                 └────▲───────────────────┘
                      │ lê
        ~/.claude/projects/*/*.jsonl  (fonte de verdade, somente leitura)
        ~/.ovenlight/backup/projects/...   (cópias, nunca apaga)
```

**Princípios**

- **Somente leitura** sobre `~/.claude`. O Ovenlight nunca edita nem apaga transcritos do Claude Code.
- **O banco é descartável:** dá para reconstruir tudo com `ovenlight indexar --forcar`. Exceção: tags, favoritas e o histórico de sessões cujo transcrito foi apagado (por isso o backup).
- **Sem dependências externas na v1:** só biblioteca padrão do Python (≥ 3.9), SQLite com FTS5, `tmux` opcional.
- **Parser tolerante:** `.get()` em tudo, ignora o que não reconhece, conta o que ignorou (alimenta o `doctor`).

### Estrutura de arquivos sugerida

```
ovenlight/
  config.py     caminhos e constantes (APP_NAME, ~/.ovenlight, projects_dir, encode_cwd)
  parse.py      leitura do .jsonl → itens {kind, role, text, ts, file?, tool?}
  db.py         schema, conexão, store_session, find_session
  indexer.py    varredura incremental, backup, restauração
  query.py      sintaxe de busca → expressão FTS5 + filtros SQL
  search.py     execução, agrupamento por sessão, facetas, resumo
  live.py       processos, agent view, tmux, tail, enviar, parar
  actions.py    reabrir, abrir terminal (Mac/Linux), executar gerenciada
  web.py        servidor local, token, API JSON
  ui.html       interface (arquivo único, sem build)
  cli.py        argparse
tests/
```

---

## 5. Modelo de dados

```sql
CREATE TABLE sessions(
  id TEXT PRIMARY KEY,            -- nome do arquivo (sem .jsonl)
  tool TEXT NOT NULL DEFAULT 'claude',
  project_key TEXT,               -- nome da pasta em ~/.claude/projects
  cwd TEXT, git_branch TEXT,
  title TEXT,                     -- título do transcrito (custom > gerado)
  first_prompt TEXT,
  started REAL, ended REAL,       -- epoch
  n_msgs INTEGER, n_prompts INTEGER,
  model TEXT, tok_in INTEGER, tok_out INTEGER, tok_cache INTEGER,
  size INTEGER, path TEXT, mtime REAL,
  cwd_exists INTEGER DEFAULT 1,   -- a pasta de origem ainda existe?
  gone INTEGER DEFAULT 0,         -- transcrito apagado (histórico indexado permanece)
  version TEXT,
  tags TEXT DEFAULT '', starred INTEGER DEFAULT 0   -- do usuário: nunca sobrescrever
);

CREATE TABLE files(session_id TEXT, path TEXT);            -- arquivos lidos/editados
CREATE TABLE tools(session_id TEXT, name TEXT, n INTEGER); -- ferramentas usadas

-- texto pesquisável: tabela comum + FTS5 externo (permite DELETE por sessão com índice)
CREATE TABLE doc(id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT, ts REAL, text TEXT);
CREATE INDEX doc_sid ON doc(session_id);
CREATE VIRTUAL TABLE docs_fts USING fts5(
  text, content='doc', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
-- triggers doc_ai (INSERT) e doc_ad (DELETE com comando 'delete' do FTS5)

CREATE TABLE state(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, session_id TEXT);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
```

**Por que `doc` + FTS5 externo:** apagar linhas de uma tabela FTS5 pela coluna `session_id` faz varredura completa. Com a tabela comum indexada, a reindexação de uma sessão é rápida.

**`remove_diacritics 2`:** buscar "configuracao" acha "configuração".

### Tipos (`kind`) de documento

| kind | Origem |
|---|---|
| `prompt` | mensagem do usuário (ignorar as que começam com `<command-`, `<local-command`, `Caveat:`, `<system-reminder`) |
| `reply` | texto do assistente |
| `bash` | comando da ferramenta Bash (+ descrição) |
| `file` | `Read/Edit/Write/MultiEdit/NotebookEdit` + caminho (também vai para a tabela `files`) |
| `tool` | demais ferramentas: nome + parâmetro principal |
| `error` | `tool_result` com `is_error` (truncar em 1.500 caracteres) |
| `saida` | saída de ferramenta, **opcional** (`OVENLIGHT_INDEX_RESULTS=1`); aumenta muito o banco |

---

## 6. Indexador

**Regras**

1. Varrer `projects_dir()/*/*.jsonl`. Ignorar arquivos `agent-*.jsonl` (subagentes em versões antigas).
2. **ID da sessão = nome do arquivo**, não o `sessionId` interno. Ao retomar, o Claude Code cria outro arquivo, e as entradas copiadas podem trazer o ID antigo.
3. **Incremental:** pular o arquivo se `(size, mtime)` bateu com a tabela `state`.
4. Ignorar entradas `isSidechain` e `isMeta` no texto.
5. **Tokens:** a mesma mensagem aparece em várias linhas (uma por bloco). Deduplicar por `message.id`, mantendo o último `usage`. Contar mensagens por `message.id` único.
6. `cwd`, branch e versão: usar o **último** valor visto (a sessão pode mudar de pasta com `/cd`).
7. **Transcrito sumiu** (limpeza de retenção): marcar `gone = 1`, **manter** o texto indexado. A sessão continua pesquisável e restaurável do backup.
8. `cwd_exists`: gravar se a pasta de origem ainda existe (alimenta o filtro `orfa:sim`).
9. Commit a cada ~25 arquivos. Um arquivo ruim não derruba a indexação (capturar exceção, registrar até 10 amostras em `meta.last_index_stats`).
10. `INSERT ... ON CONFLICT DO UPDATE` **sem** tocar em `tags` e `starred`.

**Backup:** copiar para `~/.ovenlight/backup/projects/<projeto>/<id>.jsonl` quando o tamanho/mtime mudou. Nunca apagar. `ovenlight restaurar <id>` copia de volta e reindexa.

**Hook `SessionEnd`:** `ovenlight hook` lê o JSON do stdin, pega `transcript_path`, indexa e copia para o backup. Gere o trecho de `settings.json` com `ovenlight hook --snippet` e **não edite o arquivo do usuário automaticamente**. Confira o formato atual na documentação de hooks.

---

## 7. Busca avançada

### Sintaxe

```
webhook stripe projeto:loja branch:feat/* depois:7d em:bash -docker "erro 500"
```

| Elemento | Significado |
|---|---|
| texto livre | AND implícito |
| `"frase exata"` | frase |
| `deplo*` | prefixo |
| `-palavra` | exclui |
| `OR` / `OU` | alternativa (maiúsculas; precedência do FTS5: NOT > AND > OR) |
| `projeto:x` (`p:`) | `cwd` ou pasta do projeto contém x |
| `branch:feat/*` (`b:`) | branch com curinga `*` |
| `depois:` / `antes:` | `ended >= t` / `started <= t`; aceita `2026-09-01`, `2026-09`, `12h`, `7d`, `2w`, `3m`, `ontem`, `hoje` |
| `modelo:opus` | modelo contém |
| `arquivo:auth.py` | sessão leu/editou o arquivo |
| `usou:Bash` | ferramenta usada (Bash, Edit, Task, `mcp__...`) |
| `em:bash` | escopo do texto: `prompt`, `resposta`, `bash`, `erro`, `arquivo`, `tool`, `saida` (repetível) |
| `msgs:>20` `tokens:>100000` `tam:>1mb` | comparações numéricas |
| `tag:x` `estrela:sim` | etiquetas e favoritas |
| `orfa:sim` | pasta de origem não existe mais |
| `removida:sim` | transcrito apagado (existe só no Ovenlight) |

### Regras de implementação

- **Tokenizador:** regex `(-?)(?:([^\s:"]+):)?(?:"([^"]*)"|(\S+))`. Se `chave:` não é uma chave conhecida (ex.: `http://x`), tratar o token inteiro como texto.
- **Montagem do FTS5:** cada termo vira `"termo"` (aspas duplas escapadas por `""`), prefixo vira `"termo"*`; termos sem caractere alfanumérico são descartados; expressão final `(positivos) NOT (negativos separados por OR)`. Só negativos: avisar e não buscar.
- **Filtros** viram `WHERE` parametrizado sobre `sessions s` (use `LIKE ... ESCAPE '\'` e escape `%` e `_` do valor).
- **Consulta com texto:**

```sql
SELECT d.session_id, d.kind, d.ts,
       snippet(docs_fts, 0, '«', '»', '…', 14) AS snip, bm25(docs_fts) AS r
FROM docs_fts JOIN doc d ON d.id = docs_fts.rowid
JOIN sessions s ON s.id = d.session_id
WHERE docs_fts MATCH ? [AND d.kind IN (...)] [AND filtros]
ORDER BY r LIMIT 4000
```

  Agrupar por sessão em Python (rank = melhor `bm25`, até 3 trechos por sessão, contar ocorrências).
- **Sem texto:** só filtros, ordenado por `ended DESC`.
- **Facetas:** contagem por projeto (top 15) e por mês (últimos 12), calculadas sobre o conjunto completo antes de paginar.
- **Avisos** em vez de erro para: data inválida, número inválido, escopo desconhecido, `em:` sem texto, só exclusões. Consulta inválida para o FTS5 devolve aviso, nunca exceção.

### v2 (não implementar agora)

- **Híbrida:** embeddings locais opcionais, ranking combinado com BM25.
- **Linguagem natural:** "a sessão onde corrigi o login mês passado" → `claude -p` gera a consulta, o Ovenlight mostra a consulta gerada para conferência.
- `NEAR`, regex.

---

## 8. Ao vivo

### Três níveis de controle

| Nível | Como a sessão nasceu | Ver ao vivo | Enviar comando |
|---|---|---|---|
| **A. Background** | `claude --bg`, `/bg`, agent view | sim (`claude agents --json`, `claude logs <id>`) | sim, **mecanismo experimental** (abaixo) |
| **B. Gerenciada** | `ovenlight executar` (claude dentro de um tmux `ovenlight-*`) | sim (transcrito + `tmux capture-pane`) | sim (`tmux paste-buffer`) |
| **C. Terminal comum** | usuário digitou `claude` | só leitura (processo + transcrito) | **não**; orientar a usar `/bg` |

O nível C é um limite do Claude Code: não há API para injetar texto numa sessão em primeiro plano de outro terminal. Não tente hacks de teclado do sistema operacional na v1.

### Detecção de processos (nível C e B)

1. `ps -axo pid=,ppid=,etime=,stat=,tty=,command=` (funciona em macOS e Linux).
2. É Claude se: `basename(argv[0]) == "claude"`, ou `"/claude/versions/"` está em `argv[0]`, ou `node`/`bun`/`deno` com `argv[1]` terminando em `claude` ou contendo `claude-code`. **Validar com instalações reais** (npm, nativa, Homebrew).
3. Modo `headless` se houver `-p` ou `--print` nos argumentos.
4. **cwd do processo:** Linux `readlink /proc/<pid>/cwd`; macOS `lsof -a -d cwd -Fpn -p <pids>` (em lote).
5. **Vincular ao transcrito:** pasta `projects/<encode_cwd(cwd)>/` (tentar também `realpath`, por causa de `/private/var` no macOS); pegar o `.jsonl` mais recente. Com mais de um processo na mesma pasta, atribuir pelos N mais recentes e marcar confiança `baixa`.
6. **Estado estimado:** `mtime` do transcrito há menos de 15 s = "trabalhando", senão "ociosa". Rotular na interface como **estimado**.
7. **Gerenciadas:** `tmux list-panes -a -F '#{session_name}\t#{pane_pid}\t#{pane_current_path}'` filtrando `ovenlight-*`; o processo claude pertence à sessão se tiver o `pane_pid` como ancestral (subir pela tabela de `ppid`).

### Agent view (nível A)

- Chamar `claude agents --json` com timeout (12 s). Tolerar esquema desconhecido: procurar `id/short_id/session_id`, `state/status`, `name/title`, `cwd/directory`, `summary/last_output`; guardar o objeto bruto (`raw`) e mostrá-lo na interface.
- Se falhar (comando ausente, versão antiga, plano sem agent view), mostrar o motivo e seguir com os demais níveis.
- Evitar duplicar: uma sessão background pode aparecer também como processo; ao conseguir mapear pelo ID, esconder a linha de processo.

### Enviar comando

**Nível B (tmux), validado só com `cat`:**

```bash
tmux load-buffer -b ovenlight-send -         # texto via stdin
tmux paste-buffer -p -d -b ovenlight-send -t <sessao>   # -p: bracketed paste (preserva quebras de linha)
sleep 0.25
tmux send-keys -t <sessao> Enter
```

Testar com a TUI real: se o Claude Code tratar o paste de forma diferente, ajustar (por exemplo, `send-keys -l` para uma linha só).

**Nível A (background), experimental:** a documentação diz que `claude --resume <id> "prompt"` entrega o prompt e depois abre a sessão no terminal, e que **não faz nada com saída redirecionada**. A ideia testável é rodar o comando dentro de um **pty**, esperar a mensagem `Sent your prompt to the background session`, e encerrar o cliente com `SIGHUP` (a sessão continua rodando). Pontos a validar: se `SIGHUP` realmente apenas desconecta; se a mensagem existe nesse formato; tamanho de janela do pty (usar `TIOCSWINSZ` 40×120). Use `subprocess.Popen` com `pty.openpty()` e `start_new_session=True`, **não** `pty.fork()` (o servidor web é multithread). Se não funcionar, a alternativa 100% suportada é só mostrar o comando `claude attach <id>`.

**Perguntar a uma sessão fora do ar:** `claude -p --resume <id> --output-format json --fork-session "<pergunta>"`. O `--fork-session` por padrão evita alterar a sessão original; ofereça `--sem-fork` explícito.

### Higiene de subprocessos (aprendido no protótipo)

Todo `subprocess.run` **sem TTY** deve usar `stdin=subprocess.DEVNULL` e `timeout`. Sem isso, um comando que lê stdin trava a chamada até o timeout.

### Notificações (v1.1)

Aviso de desktop quando uma sessão passa para "precisa de você", conclui ou falha. macOS: `osascript -e 'display notification ...'`; Linux: `notify-send`.

---

## 9. Reabrir

| Ação | Comando executado |
|---|---|
| Reabrir | `cd <cwd> && claude --resume <id>` em nova janela de terminal |
| Em segundo plano | `claude --resume <id> --bg` (despacho em background) |
| Bifurcar | `claude --resume <id> --fork-session` |
| Gerenciada | cria tmux `ovenlight-xxxxx` rodando `claude --resume <id>` e abre uma janela com `tmux attach` |
| Só imprimir | mostra o comando (para colar onde quiser) |

**Regras**

- Transcrito apagado (`gone`): recusar e sugerir `ovenlight restaurar <id>` se existir backup.
- Pasta de origem sumiu: avisar e usar a pasta pessoal (ou `--aqui` para a pasta atual). O Claude Code resolve o ID mesmo assim.
- **Conflito:** se algum processo ao vivo estiver vinculado ao mesmo ID, **não abrir** sem `--forcar` (dois terminais na mesma sessão misturam os transcritos). Sugerir `--fork`.
- Passar de novo as flags que **não** são restauradas (`--mcp-config`, `--settings`, `--plugin-dir`, `--add-dir`), se o usuário quiser: aceitar `-- <args do claude>`.

### Abrir terminal

**macOS:** AppleScript via `osascript`.
- Terminal.app: `tell application "Terminal" to do script "<cmd>"` + `activate`.
- iTerm2 (`OVENLIGHT_TERMINAL=iterm`): criar janela com perfil padrão e `write text`.
- Escapar `\` e `"` no AppleScript; usar `shlex.quote` no comando.

**Linux:** exige `DISPLAY` ou `WAYLAND_DISPLAY`; tentar nesta ordem: `gnome-terminal -- bash -lc`, `kgx`, `konsole -e`, `xfce4-terminal -e`, `kitty`, `alacritty -e`, `wezterm start --`, `foot`, `x-terminal-emulator -e`, `xterm -e`. Terminar o comando com `; exec ${SHELL:-bash}` para a janela não fechar.

**Override:** `OVENLIGHT_TERMINAL_CMD='wezterm start -- bash -lc {cmd}'`.

---

## 10. Interface web

**Servidor:** `http.server.ThreadingHTTPServer` na biblioteca padrão, **somente `127.0.0.1`** (porta padrão 7777). Reindexação automática a cada 60 s em thread (desligável com `--sem-auto`). Primeira execução indexa antes de servir.

### API

| Rota | Método | Função |
|---|---|---|
| `/api/search?q=&limit=&offset=&order=` | GET | busca ou lista (com facetas e avisos) |
| `/api/session?id=` | GET | metadados, arquivos, ferramentas e mensagens (primeiras 40 + últimas 120) |
| `/api/resumo` | GET | totais, projetos |
| `/api/live` | GET | sessões ao vivo, de todos os níveis |
| `/api/tail?key=&n=` | GET | últimas mensagens (ou texto bruto) de uma sessão ao vivo |
| `/api/index` | POST | indexar agora |
| `/api/reopen` | POST | `{id, mode, bg, fork, force}`; devolve 409 em caso de conflito |
| `/api/send` | POST | `{key, text}` |
| `/api/stop` | POST | `{key}` |
| `/api/ask` | POST | `{id, text, no_fork}` (pergunta headless) |
| `/api/tag`, `/api/star` | POST | etiquetas e favoritas |

Chave de item ao vivo: `bg:<id>`, `tmux:<nome>`, `pid:<n>`, `sid:<id>` (só leitura de transcrito).

### Telas

1. **Sessões:** campo de busca grande no topo; facetas à esquerda (projetos, meses); resultados em lista com trechos destacados, agrupados por mês quando não há texto; gaveta lateral com detalhe (conversa, arquivos, ferramentas, tags, ações, "perguntar a esta sessão").
2. **Ao vivo:** lista com estado, tipo (background, gerenciada, terminal comum, headless), pasta e última atividade; painel com acompanhamento a cada 2,5 s e caixa de envio quando permitido. Para nível C, explicar `/bg` e `ovenlight executar`.
3. **Ajuda de sintaxe** recolhível.

### Direção visual

- **Tema:** "mar à noite". Escuro por padrão (azul-petróleo `#0e2a33`, não preto), claro via `prefers-color-scheme`.
- **Paleta:** mar `#0e2a33`, casco `#143742`, linha `#26566a`, espuma `#dbe9e6`, sal `#8fb0b2`, **luz `#ffc94d`** (único acento), sinal `#ff6a61` (só para "precisa de você" e erros).
- **Tipografia:** sans humanista do sistema (Avenir Next, Seravek, Cantarell, Ubuntu) para a interface; serifa (Palatino/Iowan) só para o nome e títulos; monoespaçada só para caminhos, IDs e comandos. Sem fontes externas.
- **Elemento memorável:** uma **vela** (triângulo com casco) no topo, ao lado do nome, que **balança suavemente somente quando há sessão trabalhando**. O resto fica discreto. Respeitar `prefers-reduced-motion`.
- **Texto:** frases curtas, verbos claros ("Reabrir", "Bifurcar", "Enviar", "Indexar agora"); estados vazios dizem o que fazer.
- Acessibilidade: foco visível, navegação por teclado nas linhas, responsivo até celular, `Esc` fecha a gaveta.

### Segurança (obrigatório, o servidor executa comandos)

1. Escutar apenas em `127.0.0.1`.
2. **Token** aleatório em `~/.ovenlight/token` (permissão 0600). A primeira visita usa `?t=<token>`, que vira cookie `HttpOnly; SameSite=Strict` e redireciona.
3. **Validar o cabeçalho `Host`** (aceitar só `127.0.0.1`, `localhost`, `[::1]`) contra DNS rebinding.
4. POST exige cookie/cabeçalho de token, cabeçalho `X-Ovenlight: 1` e, se houver `Origin`, que seja local (contra CSRF).
5. Comparar tokens com `secrets.compare_digest`.
6. Nunca montar comandos por concatenação em shell: usar listas de argumentos; `shlex.quote` onde o shell for inevitável (AppleScript, `bash -lc`).
7. Confirmar na interface antes de enviar para sessões com permissões amplas e antes de parar sessões.
8. Registrar em `~/.ovenlight/audit.log` tudo que foi enviado (data, alvo, texto).
9. Escapar HTML em tudo que vem de transcritos (prompt injection e XSS: transcritos são conteúdo não confiável). Os trechos de busca usam `«»` e só então viram `<mark>`.

---

## 11. CLI

| Comando | Função |
|---|---|
| `ovenlight indexar [--forcar] [--backup] [--saidas] [-v]` | indexa (incremental) |
| `ovenlight buscar <consulta> [-n 20] [--ordem recente] [--json]` | busca avançada |
| `ovenlight listar [filtros] [-n 30]` | lista com os mesmos filtros |
| `ovenlight ver <id> [--tail N] [--completo] [--saidas]` | mostra uma sessão |
| `ovenlight reabrir <id> [--bg] [--fork] [--gerenciada] [--aqui] [--aqui-terminal] [--imprimir] [--forcar]` | reabre |
| `ovenlight vivo [--seguir] [--json]` | sessões rodando agora |
| `ovenlight enviar <alvo> <texto...>` | envia a sessão ao vivo (bg ou gerenciada) |
| `ovenlight perguntar <id> <texto...> [--sem-fork]` | pergunta numa cópia da sessão |
| `ovenlight parar <alvo>` | para sessão bg ou gerenciada |
| `ovenlight executar [pasta] [--destacada] [-- args do claude]` | claude dentro de tmux gerenciado |
| `ovenlight web [--porta 7777] [--abrir] [--sem-auto]` | interface web |
| `ovenlight resumo` | totais e projetos |
| `ovenlight tag <id> <tag> [--remover]`, `ovenlight estrela <id>` | organização |
| `ovenlight backup`, `ovenlight restaurar <id>` | cópias de segurança |
| `ovenlight hook [--snippet]` | integração com `SessionEnd` |
| `ovenlight doctor` | diagnóstico |
| `ovenlight ajuda-busca` | sintaxe |

IDs podem ser **prefixos únicos** (ambíguo = erro claro). Cores só quando stdout é TTY e `NO_COLOR` não está definido.

### `ovenlight doctor`

Deve verificar: versão do Python e do SQLite, **FTS5 disponível**, pasta de transcritos e quantidade de arquivos, `claude` no PATH e versão, `tmux`, `ps`/`lsof`, resultado da última indexação (com amostras de erro) e uma **amostra dos 20 transcritos mais recentes** (linhas lidas, linhas ilegíveis, tipos de entrada vistos). É a ferramenta para descobrir quando uma nova versão do Claude Code mudou o formato.

---

## 12. Testes

Use `unittest` (sem dependências) com `HOME` e `OVENLIGHT_HOME` apontando para um diretório temporário e **transcritos sintéticos**.

**Fixture mínima:** 3 sessões (uma com Bash/Edit/Read/erro e título, uma antiga com `/clear`, uma com pasta de origem inexistente e linhas quebradas), mais um `agent-xyz.jsonl` que deve ser ignorado.

**Casos que precisam existir**

- Parser: contagem de mensagens por `message.id`, tokens sem duplicar, ignorar `<command-*>`, linhas inválidas contadas.
- Indexador: incremental e idempotente, `gone` após apagar o arquivo (texto continua pesquisável), `restaurar` do backup, **tags e favoritas sobrevivem ao reindex**.
- Busca: acentos, frase, prefixo, exclusão, `OR`/`OU`, todos os escopos `em:`, cada filtro, combinação texto + filtro, avisos, entrada com caracteres especiais do FTS5 (`foo" AND (bar`) sem exceção, `http://x` tratado como texto.
- Reabrir: comando para cada modo, aviso de pasta órfã, recusa de sessão apagada, prefixo ambíguo.
- Ao vivo: `_is_claude` com várias formas de invocação; recusa de envio ao nível C com a orientação sobre `/bg`; ida e volta real com tmux usando `cat` no lugar do claude (pular se tmux ausente).
- Web: 401 sem token, 403 com `Host` estranho, POST recusado sem `X-Ovenlight` ou com `Origin` externo, cookie `HttpOnly`, busca/detalhe/ações.
- CLI: cada subcomando principal.

**Testes manuais obrigatórios com o Claude Code real** (nada disso foi validado ainda):

1. `ovenlight doctor` e `ovenlight indexar` nos seus transcritos reais.
2. Detecção de processo no macOS **e** no Linux, com a instalação que você usa.
3. `claude agents --json`: capturar a saída real e fixar o esquema.
4. Envio por tmux para a TUI real (uma linha, várias linhas, caracteres especiais, texto longo).
5. Envio ao background por pty (se falhar, remover e manter só `claude attach`).
6. Interface web em Safari/Chrome/Firefox, claro e escuro, tela estreita.

---

## 13. Plano de entrega

### v0.1: inventário e busca (o mais valioso, sem risco)
- [ ] `config`, `parse`, `db`, `indexer`
- [ ] `query` + `search` com todos os filtros
- [ ] CLI: `indexar buscar listar ver resumo doctor ajuda-busca`
- [ ] Testes de parser, indexador e busca
- **Pronto quando:** `ovenlight buscar` acha, em menos de 1 s, uma frase dita numa sessão antiga de qualquer pasta, e `ovenlight doctor` roda limpo nos seus arquivos reais.

### v0.2: reabrir e histórico
- [ ] `actions` (macOS e Linux), conflito, órfãs
- [ ] `backup`, `restaurar`, `hook`
- **Pronto quando:** reabrir uma sessão antiga abre o terminal na pasta certa e retoma a conversa.

### v0.3: ao vivo
- [ ] `live`: processos, agent view, tail
- [ ] `executar` (tmux) e `enviar`
- [ ] `vivo`, `parar`, `perguntar`
- **Pronto quando:** `ovenlight vivo` lista todas as suas sessões abertas e `ovenlight enviar` funciona numa sessão gerenciada.

### v0.4: interface web
- [ ] servidor, token, API, `ui.html`
- [ ] notificações de desktop
- **Pronto quando:** as três telas funcionam e a suíte de segurança da web passa.

### v1.0
- Endurecimento com dados reais, documentação, empacotamento (`pipx install`, Homebrew tap, ou um único arquivo `.pyz` via `zipapp`).

### Depois
- Busca semântica local e consulta em linguagem natural.
- Briefing de transferência entre LLMs/ferramentas e integração com sistemas de memória de longo prazo (por exemplo, o ai-memory, de Fábio Akita, que mantém uma wiki em Markdown sobre sessões de agentes). Possível arquitetura: o Ovenlight cuida do inventário e do controle das sessões; a camada de memória consome os mesmos transcritos.
- Adaptadores para Codex, opencode e outras ferramentas (a coluna `tool` já existe).
- Custos por projeto e por semana.
- Windows.

---

## 14. Riscos e decisões em aberto

| Risco | Mitigação |
|---|---|
| Formato do `.jsonl` muda | parser tolerante, `doctor` com amostra, testes com fixtures reais anonimizadas |
| `claude agents --json` tem esquema desconhecido | normalização defensiva + `raw` visível; fixar após capturar a saída real |
| Envio ao background por pty não funciona | rebaixar para mostrar `claude attach <id>`; documentar |
| Wrappers, aliases ou scripts que chamam o `claude` escondem o nome do processo | validar com a sua instalação real; permitir lista de padrões em configuração; nível B (`ovenlight executar`) não depende de detecção |
| Detecção de processo erra o vínculo | marcar confiança, mostrar "estimado", nunca agir sem confirmação |
| Servidor local executando comandos | seção 10 inteira; confirmações; log de auditoria |
| Banco cresce (texto duplicado no FTS) | `saida` desligado por padrão; `VACUUM` e `INSERT INTO docs_fts(docs_fts) VALUES('optimize')` periódicos |
| Dados sensíveis nos transcritos (chaves, segredos) | banco e backup com permissão 0600 em pasta 0700; opção de lista de exclusão por projeto; nunca enviar o conteúdo para a rede |
| Mudanças de política de produto (agent view é research preview) | camadas independentes: inventário e busca não dependem do agent view |

**Decisões que ficam com você**

1. Persistir o texto das **respostas do assistente** no banco (mais útil, mais volume) ou só pedidos e comandos?
2. Política de retenção do backup do Ovenlight (infinita, ou limite por idade/tamanho)?
3. O Ovenlight deve **editar** `~/.claude/settings.json` para instalar o hook, ou só imprimir o trecho (recomendado: só imprimir)?
4. Distribuição: `pipx`, Homebrew ou arquivo único?

---

## 15a. Revisão de 03/11/2026 (macOS, dados reais)

- **Validado no macOS** com 139 transcritos reais (2,5 GB): `doctor` limpo (0 linhas ilegíveis), `indexar` em 18 s sem erros, `buscar` em < 0,2 s, `vivo`, `reabrir --imprimir`, detecção de conflito e segurança básica do `web`.
- **Corrigido:** `vivo` tratava todas as entradas do `claude agents --json` como background (duplicava linhas e permitia tentar envio por pty a sessões interativas). Agora entradas `interactive` enriquecem a linha do processo (ID exato da sessão e estado real, não estimado) e o envio a elas é recusado com a orientação sobre `/bg`.
- **Corrigido:** `~/.ovenlight` agora é `0700`; banco (`-wal`/`-shm`), backup e token são `0600`. Pastas antigas com `0755` são ajustadas na próxima execução.
- **Corrigido:** o `.pyz` descartava o código de saída (sempre 0). O `__main__.py` da raiz agora usa `sys.exit(main())`.
- 36 testes (6 novos de regressão). Originais preservados em `*.orig`.
- **CLI, sintaxe de busca, mensagens e interface web passaram para inglês.** Comandos: `index`, `search` (`s`), `list` (`ls`), `show`, `open` (`resume`), `live` (`ps`), `send`, `ask`, `stop`, `run`, `web`, `stats`, `tag`, `star`, `backup`, `restore`, `hook`, `doctor`, `search-help`. Filtros: `project:` `branch:` `after:` `before:` `model:` `file:` `used:` `in:` `msgs:` `tokens:` `size:` `tag:` `starred:yes` `orphan:yes` `deleted:yes`; escopos de `in:`: prompt, reply, bash, error, file, tool, output. As seções 7 e 11 abaixo ainda usam os nomes antigos em português.
- **Pendente:** `audit.log` (seção 10, item 8) não está implementado; subagentes em `<sessão>/subagents/*.jsonl` não são indexados; tmux, envio por pty e interface no navegador continuam sem teste.

## 15. Estado do protótipo (entregue junto: `ovenlight-0.1.0.tar.gz` e `ovenlight.pyz`)

Protótipo em Python (só biblioteca padrão) que cobre as versões v0.1 a v0.4 deste plano.

**O que foi verificado no ambiente de desenvolvimento (Linux)**

- **30 testes automatizados passando**, com **dados sintéticos**: parser, indexador incremental, busca com todos os filtros e escopos, reabrir (incluindo pasta órfã e transcrito apagado/restaurado), segurança da web (401, 403 por `Host`, POST sem `X-Ovenlight` ou com `Origin` externo, cookie `HttpOnly`) e envio real por tmux.
- **Teste ponta a ponta com um `claude` falso:** detecção de processo comum, vínculo com o transcrito (confiança "alta"), `ovenlight executar` → `ovenlight vivo` → `ovenlight enviar` → `ovenlight parar` com tmux real, e recusa de envio ao terminal comum com a orientação sobre `/bg`. `ovenlight vivo` responde em ~0,1 s.
- **Servidor web real** (`ovenlight web`) rodando a partir do `.pyz`: token, cookie, redirecionamento, UI servida de dentro do zip, busca e detalhe via API, permissão 0600 do token.
- **JavaScript da interface** com sintaxe validada por `node --check`.

**Defeitos encontrados no teste e já corrigidos (com teste de regressão)**

1. `ovenlight executar <pasta> --destacada`: o `argparse.REMAINDER` engolia as opções depois da pasta. Agora os argumentos do `claude` vão depois de um `--` explícito.
2. `subprocess.run` sem `stdin=DEVNULL` travava quando o comando lia o stdin. Agora toda chamada sem entrada usa `DEVNULL`.
3. `ovenlight web --sem-auto` pulava também a **primeira indexação**, abrindo a interface vazia. Agora a primeira indexação sempre acontece; `--sem-auto` só desliga a periódica.

**O que NÃO foi validado (faça antes de confiar)**

- Nenhuma execução contra **transcritos reais** nem contra o **Claude Code real** (o formato do `.jsonl`, o JSON de `claude agents --json`, o nome do processo em instalações nativas e o formato do hook são hipóteses; veja a seção 3).
- Envio ao **background por pty**: implementado, **não testado**. Se não funcionar, remova e mantenha só a orientação `claude attach <id>`.
- Envio por tmux foi testado com `cat`/um script falso, **não** com a TUI real do Claude Code.
- A **interface web nunca foi aberta em um navegador** (nem em macOS): o visual, o layout responsivo e os modos claro/escuro estão sem verificação visual.
- Comportamento **no macOS** (`lsof` para o cwd, AppleScript para abrir o Terminal/iTerm) foi escrito, mas só o Linux foi exercitado.
- Detecção ao vivo é heurística: estado ("trabalhando"/"ociosa") vem do horário de modificação do transcrito e é rotulado como estimado.

**Como começar**

1. `ovenlight doctor` e `ovenlight indexar` nos seus arquivos reais; corrija o parser onde o `doctor` apontar linhas ilegíveis ou tipos de entrada desconhecidos.
2. Capture a saída real de `claude agents --json` e fixe o esquema em `live.background_sessions`.
3. Teste o envio por tmux e por pty com a TUI real.
4. Abra `ovenlight web --abrir` no navegador e ajuste o visual.
