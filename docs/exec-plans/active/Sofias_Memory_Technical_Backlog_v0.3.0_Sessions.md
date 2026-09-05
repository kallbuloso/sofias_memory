# Sofias Memory — Backlog Técnico Executável v0.3.0 Sessions

**Release:** v0.3.0  
**Feature:** First-class Durable Sessions  
**Status:** Proposed  
**Sequência:** SM-601..SM-607  
**Regra de execução:** executar uma task por vez; não antecipar dependências ou escopo de tickets posteriores.

## 1. Objetivo

O v0.3.0 transforma `session_id` de correlação metadata-only em uma capacidade first-class e durável de contexto temporal.

Ao final deste backlog, Sofias Memory deverá possuir:

- Sessions persistentes em PostgreSQL;
- SessionEntries append-only;
- lifecycle `active <-> archived`;
- criação explícita e lazy creation concurrency-safe;
- associação de Query e PipelineRun com Session;
- Session Context opt-in em Recall RAG;
- provenance exata das SessionEntries utilizadas;
- integração de Remember e manual retry;
- preservação de compatibilidade com callers e dados anteriores ao v0.3.0.

Este backlog não implementa Skills, Agents, runtime agêntico, TTL, cache ou promoção automática para memória permanente.

---

# 2. Fontes normativas

Ordem de precedência específica deste release:

1. instrução explícita da task em execução;
2. `AGENTS.md`;
3. ADR-0012 — First-Class Durable Sessions;
4. Feature Contract v0.3.0 — Sessions;
5. ADRs anteriores aplicáveis, especialmente ADR-0002, ADR-0003, ADR-0007 e ADR-0009;
6. contratos e testes existentes;
7. PRD original, exceto onde explicitamente amended pelo ADR-0012.

O Feature Contract define a semântica pública detalhada.

ADR-0012 define a mudança arquitetural e suas fronteiras.

Este backlog define somente a ordem executável de implementação e os gates.

---

# 3. Invariantes de release

Durante SM-601..SM-607:

- PostgreSQL continua authoritative;
- nenhuma Session ou SessionEntry é projetada para Neo4j;
- Session nunca é authorization boundary;
- Session não pertence obrigatoriamente a Dataset;
- `session_id` externo permanece textual, case-sensitive e imutável;
- `session_uuid` é a identidade estrutural UUID;
- nenhuma Session histórica é inferida por backfill;
- SessionEntry não é semantic memory;
- Recall não cria SessionEntry automaticamente;
- Session Context é opt-in;
- Session Context não altera retrieval;
- somente SessionEntries completas entram em RAG;
- archive bloqueia nova atividade, não cancela atividade admitida;
- manual retry preserva a Session original;
- Forget e Dataset Delete não removem Session/history;
- nenhuma transação PostgreSQL longa permanece aberta durante LLM, embeddings, HTTP ou Neo4j;
- não introduzir Redis, queue externa, TTL, Session embeddings, auto-summary ou background sync.

---

# 4. Sequência

| Ticket | Entrega principal | Depende de |
|---|---|---|
| SM-601 | Schema, domínio e persistence foundation | — |
| SM-602 | Session Management API | SM-601 |
| SM-603 | SessionEntry API + admission barrier | SM-602 |
| SM-604 | Recall + Query + Session Context provenance | SM-603 |
| SM-605 | Remember + PipelineRun + retry integration | SM-601, SM-602 |
| SM-606 | Cross-feature hardening e compatibility | SM-603, SM-604, SM-605 |
| SM-607 | Docs, smoke e release gate v0.3.0 | SM-606 |

SM-604 e SM-605 podem ser implementadas em qualquer ordem depois de suas dependências, mas não devem ser misturadas na mesma task.

---

# SM-601 — Sessions persistence foundation

## Objetivo

Criar a fundação persistente e de domínio de first-class Sessions sem ainda expor a API pública de management.

## Escopo

Implementar:

### Session

Modelo PostgreSQL com, no mínimo:

```text
id
key
name
status
metadata
created_at
updated_at
archived_at
```

Regras:

```text
id  = UUID PK
key = TEXT UNIQUE NOT NULL
```

`status` possui somente:

```text
active
archived
```

Criar Python domain enum correspondente e PostgreSQL enum conforme política do ADR-0007.

### SessionEntry

Modelo com:

```text
id
session_id
role
content
metadata
created_at
```

FK:

```text
session_entries.session_id
    → sessions.id
    ON DELETE CASCADE
```

`role` permanece `TEXT`.

### Query

Adicionar:

```text
session_id UUID NULL
session_context_entry_ids UUID[] NOT NULL DEFAULT '{}'
```

FK:

```text
queries.session_id
    → sessions.id
    ON DELETE SET NULL
```

### PipelineRun

Adicionar:

```text
session_id UUID NULL
```

FK:

```text
pipeline_runs.session_id
    → sessions.id
    ON DELETE SET NULL
```

Adicionar índice apropriado para list/filter por Session.

### Migration

Criar a próxima Alembic revision após o head atual.

A migration é aditiva.

Não executar qualquer backfill de Session a partir de dados legados.

### Persistence layer

Adicionar repositories/UoW necessários para:

- get por UUID;
- get por external key;
- create;
- concurrency-safe get-or-create;
- list paginado;
- update administrativo;
- row lock para admission decisions;
- SessionEntry append/list;
- Query list by Session;
- PipelineRun list/filter by Session quando necessário.

### Normalização compartilhada

Criar uma única primitive para `session_id`:

```text
None       → None
" abc "    → "abc"
""         → invalid
"   "      → invalid
>255 chars → invalid
```

Case deve ser preservado.

A mesma primitive será reutilizada pelos tickets posteriores.

### Error contract

Adicionar `SESSION_ARCHIVED` ao catálogo estável de erros.

Não criar um catálogo excessivo de Session-specific error codes quando `INVALID_REQUEST` já representar adequadamente validação/not-found/conflict genérico.

## Não fazer

- routes `/sessions`;
- Recall context;
- alteração comportamental de Remember;
- backfill;
- Session hard delete;
- Neo4j projection.

## Gate SM-601

A task só encerra quando:

- migration upgrade/downgrade é válida;
- schema guards refletem conscientemente as novas tabelas/colunas/FKs;
- uniqueness de `Session.key` é comprovada;
- `ON DELETE` policies estão cobertas por testes;
- get-or-create concorrente converge para uma única Session;
- normalização compartilhada possui testes de boundary;
- nenhuma row histórica é convertida automaticamente em Session;
- suite existente permanece verde após atualização deliberada dos schema tests.

---

# SM-602 — Session Management API

## Objetivo

Expor lifecycle e management first-class de Session sobre a fundação do SM-601.

## Endpoints

Implementar:

```text
POST  /api/v1/sessions
GET   /api/v1/sessions
GET   /api/v1/sessions/{session_uuid}
PATCH /api/v1/sessions/{session_uuid}
POST  /api/v1/sessions/{session_uuid}/archive
POST  /api/v1/sessions/{session_uuid}/restore
```

## Create

`POST /sessions` aceita:

```text
session_id optional
name optional
metadata optional
```

Se `session_id` for omitido:

- gerar UUID;
- usar representação textual desse UUID como external `session_id`.

External key existente:

```text
409
```

Não realizar upsert implícito.

## List

Suportar:

```text
limit
offset
status
session_id
```

Paginação segue default/max já estabelecidos pela API.

`session_id` é exact match.

## PATCH

Pode alterar:

```text
name
metadata
```

Não pode alterar:

```text
session_id
status
```

`metadata` possui replacement semantics, não deep merge.

## Archive/restore

Ambos são idempotentes.

```text
active   + archive → archived
archived + archive → archived

archived + restore → active
active   + restore → active
```

Archive/restore atualizam a metadata temporal administrativa apropriada.

Não criar `last_active_at`.

## Gate SM-602

Comprovar:

- envelope padrão da API;
- autenticação existente permanece aplicada;
- create explícito;
- server-generated key;
- duplicate key conflict;
- list/filter/pagination;
- get;
- PATCH;
- archive idempotente;
- restore idempotente;
- `session_id` imutável;
- não existe DELETE público;
- OpenAPI reflete exatamente o contrato aprovado.

---

# SM-603 — SessionEntry API and admission barrier

## Objetivo

Adicionar histórico contextual explícito e congelar a semântica operacional de archive como admission barrier.

## Endpoints

Implementar:

```text
POST /api/v1/sessions/{session_uuid}/entries
GET  /api/v1/sessions/{session_uuid}/entries
GET  /api/v1/sessions/{session_uuid}/queries
```

## Append

Payload mínimo:

```text
external_id  optional
role
content
metadata
```

SessionEntry é append-only.

Não adicionar PATCH ou DELETE.

`role` permanece open-ended.

`content` deve possuir limite público explícito e coberto por teste.

`external_id`, quando presente, usa a normalização já congelada no Feature Contract
(trim, 1..255 chars, case-sensitive, imutável, unique por Session) — não reimplementar
essa validação de forma independente.

## external_id safe replay

Sem `external_id`: append normal, sempre cria uma nova SessionEntry, `201`.

Com `external_id` inexistente na Session: cria SessionEntry, `201`.

Com `external_id` já existente na Session:

- payload semântico idêntico (`role` + `content` + `metadata`) → resolve a SessionEntry
  existente, não cria segunda row, responde `201` com a mesma SessionEntry (mesmo
  `entry_id`). Não introduzir `200` para distinguir replay de criação nova;
- payload semântico diferente → `409 IDEMPOTENCY_CONFLICT`, sem mutar a SessionEntry
  existente. Não criar novo ErrorCode; reutilizar `IDEMPOTENCY_CONFLICT`.

Concorrência:

- `same session_uuid` + `same external_id` + `same payload semântico` → duas requisições
  concorrentes devem convergir para uma única SessionEntry;
- `same session_uuid` + `same external_id` + `different payload semântico` → uma
  requisição vence a criação, a outra observa `409 IDEMPOTENCY_CONFLICT`.

A partial unique index `uq_session_entries_session_id_external_id` (SM-601) é a defesa
authoritative no PostgreSQL para ambos os casos acima; a lógica de resolução/conflito no
endpoint é responsabilidade do SM-603.

Ausência de `external_id` continua sendo append não idempotente (comportamento normal,
sem safe replay).

## Replay depois de archive

Decisão congelada (sem novo ADR): archive bloqueia nova atividade, não a
observação/replay de atividade já admitida.

Precedência de admissão, nesta ordem:

```text
1. existing external_id + same payload      → 201 replay
2. existing external_id + different payload → 409 IDEMPOTENCY_CONFLICT
3. missing external_id (ou sem external_id) + Session archived → 409 SESSION_ARCHIVED
4. caso contrário (Session active)          → 201, nova SessionEntry
```

Ou seja: `Session archived + existing external_id + same payload` continua `201` com a
mesma SessionEntry; `Session archived + existing external_id + different payload`
continua `409 IDEMPOTENCY_CONFLICT` (precedência sobre `SESSION_ARCHIVED`); somente
`Session archived + external_id inexistente/ausente` retorna `409 SESSION_ARCHIVED`.

## Admission barrier

Append deve tomar uma decisão atomicamente consistente com archive.

Conceitualmente:

```text
lock Session
verify ACTIVE
insert SessionEntry
commit
```

Se archived:

```text
409 SESSION_ARCHIVED
```

Uma corrida append/archive deve produzir somente um dos resultados válidos:

```text
append admitido antes do archive
```

ou:

```text
archive confirmado antes do append → append rejeitado
```

Nenhuma entry pode aparecer depois de um archive já observado como concluído por aquela ordem transacional.

## Entry listing

Suportar:

```text
limit
offset
order=asc|desc
```

Default:

```text
asc
```

Ordering determinístico:

```text
created_at, id
```

## Session Queries

`GET /sessions/{session_uuid}/queries` retorna projeção resumida de Query.

Não duplicar Query Provenance completa nesse endpoint.

## Gate SM-603

Comprovar:

- append ativo;
- append archived rejeitado;
- archive/append concurrency;
- restore volta a admitir append;
- ordering asc/desc determinístico;
- paginação;
- role não é enum;
- nenhuma interpretação de role como security/LLM privilege;
- Session Queries lista somente Queries associadas;
- SessionEntry continua ausente do Neo4j/outbox;
- append sem `external_id` continua não idempotente;
- append com `external_id` novo cria normalmente;
- replay com `external_id` + payload semântico idêntico resolve a mesma SessionEntry
  (mesmo `entry_id`, sem segunda row, `201`);
- `external_id` + payload semântico diferente retorna `409 IDEMPOTENCY_CONFLICT` sem
  mutar a SessionEntry existente;
- concorrência same-key/same-payload converge para uma única SessionEntry;
- concorrência same-key/different-payload produz exatamente um vencedor e um
  `409 IDEMPOTENCY_CONFLICT`;
- replay (`external_id` + payload idêntico) contra Session archived continua `201`
  com a mesma SessionEntry;
- `external_id` + payload diferente contra Session archived retorna
  `409 IDEMPOTENCY_CONFLICT`, não `SESSION_ARCHIVED`;
- novo append (sem `external_id`, ou `external_id` inexistente) contra Session
  archived retorna `409 SESSION_ARCHIVED`.

---

# SM-604 — Recall Session Context and provenance

## Objetivo

Transformar o `session_id` já aceito pelo Recall em uma associação first-class e adicionar Session Context RAG opt-in com provenance exata.

## Public contract

Recall mantém `session_id` e passa a aceitar:

```text
include_session_context: bool = false
```

Usar obrigatoriamente a primitive compartilhada de normalização criada em SM-601.

## Session resolution

### Sem `session_id`

Comportamento existente permanece.

```text
Query.session_id = NULL
```

### Com `session_id`

Nova atividade:

- resolve Session existente;
- lazy-create se ausente;
- rejeita Session archived;
- persiste Query associada à Session.

Lazy creation deve ser concurrency-safe.

## Context eligibility

`include_session_context=true` somente é válido quando:

```text
session_id != null
mode == rag
only_context == false
```

Combinações inválidas retornam `400 INVALID_REQUEST`.

Nunca ignorar o parâmetro silenciosamente.

## Transaction boundary

Não manter transação PostgreSQL aberta durante:

- embedding;
- Neo4j;
- LLM.

Usar short transaction para:

```text
resolve/create Session
verify ACTIVE
snapshot SessionEntries
```

e outra short transaction para persistir Query/resultados.

Archive depois da admission não cancela o Recall em andamento.

## Context selection

Introduzir Settings imutáveis de startup, no mínimo:

```text
SESSION_CONTEXT_MAX_ENTRIES
SESSION_CONTEXT_MAX_CHARS
```

Selecionar somente SessionEntries completas.

Algoritmo normativo:

1. considerar entries da mais recente para a mais antiga;
2. construir o maior sufixo cronológico contíguo que respeite ambos os limites;
3. não truncar nenhuma entry;
4. ao encontrar a primeira entry que não cabe, parar;
5. fornecer as selecionadas ao RAG em ordem oldest → newest;
6. se a entry mais recente não couber, utilizar contexto vazio.

Persistir em:

```text
Query.session_context_entry_ids
```

os IDs exatos e na ordem exata fornecida ao RAG.

## Retrieval

Não alterar:

- query usada no retrieval;
- vector retrieval;
- graph retrieval;
- hybrid ranking;
- coreference resolution.

Session Context afeta somente a geração RAG.

## Prompt safety

Renderizar Session Context como bloco contextual não confiável.

Nunca mapear `SessionEntry.role` diretamente para roles privilegiadas do provider.

## Query Provenance

Expandir:

```text
GET /api/v1/provenance/query/{query_id}
```

para expor:

```text
session_uuid
session_context
```

Ao reidratar cada entry:

```text
entry.session_id == query.session_id
```

deve ser validado.

Nunca retornar conteúdo de outra Session por confiar apenas no UUID armazenado no array.

## RecallResult

Expor:

```text
session_uuid: UUID | null
```

## Gate SM-604

Comprovar:

- Recall sem Session mantém comportamento anterior;
- Session lazy creation;
- Session archived rejeitada;
- association Query → Session;
- context default false;
- combinações inválidas;
- seleção bounded determinística;
- nenhuma entry truncada;
- snapshot não inclui entry criada depois da admission;
- archive depois da admission não cancela o Recall em andamento;
- sem hits de knowledge e sem Session Context selecionado: resposta padrão de ausência de evidência, LLM não é chamado;
- sem hits de knowledge mas com Session Context selecionado: LLM é chamado, `context`/`references` do `RecallResult` ficam vazios, e `Query.session_context_entry_ids` permanece populado;
- retrieval permanece inalterado;
- provenance registra IDs exatos;
- provenance fail-safe contra cross-Session mismatch;
- nenhuma transação longa cobre chamadas externas;
- role não ganha privilégios de prompt.

---

# SM-605 — Remember, PipelineRun and retry Session integration

## Objetivo

Associar operações duráveis de Remember à Session no nível correto: PipelineRun.

## Remember inputs

Uniformizar:

```text
Remember Text
Remember URL
Remember File
```

para usar a mesma normalização de `session_id` do SM-601.

Remover diferenças de validação atualmente existentes entre os três entry points.

## SubmissionTargets

Expandir o contrato genérico para transportar Session:

```text
dataset_id
source_id
session_id
```

quando aplicável.

Não criar uma segunda submission path específica para Sessions.

## Transactional resolution

Para Remember novo com Session:

```text
BEGIN

resolve/create Dataset
resolve/create Session
verify Session ACTIVE
create PipelineRun(session_id=...)
create PipelineSteps

COMMIT
```

Session lazy creation deve participar da mesma atomicidade de submissão.

Nenhum external I/O dentro desse preparation transaction.

## PipelineRun

Persistir:

```text
PipelineRun.session_id
```

para Remember associado.

Não adicionar `session_id` a Source.

Não tornar `Document.metadata["session_id"]` authoritative.

Nenhum backfill de runs antigos.

## Idempotency

Se um `Idempotency-Key` resolver trabalho já existente:

- retornar/observar o run existente;
- não rejeitar somente porque sua Session foi arquivada depois.

Se a mesma request representar criação de trabalho novo contra uma Session archived:

```text
409 SESSION_ARCHIVED
```

## Manual retry

Expandir snapshot/target recovery genérico para preservar:

```text
dataset_id
source_id
session_id
```

do run original.

Retry permanece permitido mesmo quando a Session original está archived.

Não re-resolver a Session pelo external key no retry.

Preservar a FK UUID authoritative do run original.

## Run API

Adicionar `session_uuid` às projeções públicas de Run.

Adicionar filtro opcional por `session_uuid` na listagem de Runs.

## Remember result

Expor `session_uuid` quando houver associação first-class.

Run histórico pré-v0.3 pode retornar `null` mesmo quando seu payload contém legacy textual `session_id`.

## Gate SM-605

Comprovar:

- três formas de Remember normalizam Session igualmente;
- lazy creation;
- archived new work rejeitado;
- Session + Run criação atomicamente consistente;
- deduplicated Source não adquire Session ownership;
- replay idempotente continua observável após archive;
- retry preserva Session;
- retry archived permitido;
- Run API expõe e filtra Session;
- legacy run permanece sem backfill;
- nenhuma mudança necessária no Neo4j.

---

# SM-606 — Sessions compatibility and lifecycle hardening

## Objetivo

Executar a auditoria cross-feature que comprova que Sessions não alteraram inadvertidamente os invariants estabelecidos do Sofias Memory.

## Cenários obrigatórios

### Legacy

Comprovar:

- dados pré-v0.3 sem Session continuam válidos;
- `MemoryEntry.session_id` legado não é convertido em FK;
- `Document.metadata["session_id"]` legado não gera Session;
- `PipelineRun.input["session_id"]` legado não gera Session;
- Query antiga continua com `session_uuid=null`.

### Forget

Comprovar que Source/Dataset/Everything Forget:

- não remove Session;
- não remove SessionEntry;
- não remove Query audit;
- não remove Feedback;
- não altera Session lifecycle.

Documentar explicitamente que:

```text
DELETE EVERYTHING
```

não é Session/history purge.

### Dataset Delete

Comprovar:

- Session sobrevive;
- SessionEntries sobrevivem;
- Queries históricas sobrevivem;
- knowledge references removidas podem aparecer como unavailable conforme provenance existente.

### Archive

Cobrir corridas:

```text
archive vs SessionEntry append
archive vs Recall admission
archive vs Remember submission
archive vs idempotent Remember replay
archive vs manual retry
```

Nenhum comportamento deve depender de timing não documentado.

### Multi-dataset

Comprovar que uma mesma Session pode possuir Queries sobre:

```text
Dataset A
Dataset A+B
Dataset C
```

sem qualquer `sessions.dataset_id` ou ownership equivalente.

### Neo4j/outbox

Assertar que:

- Session;
- SessionEntry;
- Query↔Session;
- PipelineRun↔Session

não produzem graph_outbox events.

### Config

Validar startup config dos limites de Session Context:

- valores inválidos falham no startup;
- configuração é imutável em runtime;
- nenhuma dependência opcional nova foi introduzida.

## Gate SM-606

Executar suites unit/integration relevantes e adicionar testes específicos suficientes para provar todos os casos acima.

Nenhum smoke final ainda é obrigatório neste ticket; SM-607 é o release gate.

---

# SM-607 — v0.3.0 Sessions release gate

## Objetivo

Fechar documentação, smoke real e release readiness sem acrescentar feature nova.

## Documentação

Atualizar somente o necessário:

- API reference;
- OpenAPI descriptions;
- configuração/env examples;
- architecture/schema documentation;
- README/AGENTS quando a árvore ou invariants necessitarem;
- release notes/changelog;
- PRD original apenas por referência ao amendment/ADR, sem reescrever todo o documento histórico.

Os documentos normativos do release devem permanecer:

```text
Feature Contract v0.3.0 Sessions
ADR-0012
este Technical Backlog
```

Evitar duplicação extensa entre eles.

## Smoke real PostgreSQL

O smoke deve comprovar pelo menos um fluxo completo:

```text
create Session
→ append user SessionEntry
→ Recall sem context
→ Recall RAG com context
→ append assistant SessionEntry
→ Remember associado
→ observar Run associado
→ archive
→ nova atividade rejeitada
→ retry/replay permitido quando aplicável
→ restore
→ nova atividade aceita
```

Incluir multi-dataset no mesmo smoke ou em smoke complementar.

## Provenance smoke

Confirmar que uma resposta RAG com Session Context consegue ser rastreada para:

```text
Query
├── Session
├── SessionEntries efetivamente usadas
└── knowledge references
```

## Delete smoke

Confirmar pelo menos:

```text
Forget
```

e:

```text
Dataset Delete
```

sem remoção da Session/history.

## Neo4j check

Confirmar que nenhuma entidade Session/SessionEntry foi criada e que nenhuma nova categoria de graph_outbox event foi introduzida.

## Quality gate

Executar:

```text
ruff
format/check
mypy
pytest
migration/schema gates
integration tests
smokes v0.3.0
```

conforme tooling oficial do repositório.

Não mascarar testes existentes, não reduzir cobertura contratual e não excluir suites para obter gate verde.

## GATE-v0.3.0

O release somente pode ser marcado como concluído quando:

- SM-601..SM-606 estiverem aprovadas;
- migration estiver validada em banco real;
- API pública estiver coerente com Feature Contract;
- ADR-0012 estiver respeitado;
- legacy compatibility estiver comprovada;
- archive/admission semantics estiverem comprovadas;
- Recall Context provenance estiver comprovada;
- Remember/Run/retry integration estiver comprovada;
- Forget/Dataset Delete preservation estiver comprovada;
- Neo4j permanecer livre de Session state;
- suite completa estiver verde;
- smoke real estiver verde;
- documentação de v0.3.0 estiver atualizada.

Após esse gate, nenhum trabalho de Skills ou Agent Management deve ser incluído retroativamente no v0.3.0.

O próximo release funcional planejado permanece separado.