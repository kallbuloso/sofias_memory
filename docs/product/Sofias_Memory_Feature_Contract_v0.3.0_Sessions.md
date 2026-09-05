# Sofias Memory — Feature Contract v0.3.0: Sessions

**Status:** Proposed  
**Target release:** v0.3.0  
**Feature:** First-class durable Sessions

---

## 1. Objetivo

O v0.3.0 introduz **Sessions first-class e duráveis** no Sofias Memory.

Uma Session representa um **contexto temporal persistente de interação ou execução**. Ela pode agrupar conversas, chamadas de agentes, workflows ou outras sequências contextuais relacionadas.

Session não é:

- authentication ou authorization boundary;
- Dataset;
- cache;
- memória semântica permanente;
- agent runtime;
- mecanismo de TTL;
- sincronização automática para knowledge;
- entidade do Neo4j.

PostgreSQL permanece como fonte de verdade.

---

## 2. Princípios

### 2.1 Session é contexto, não memória permanente

```text
Session
    ↓
contexto temporal

Remember
    ↓
memória permanente
```

Conteúdo de Session somente entra na memória permanente por operação explícita.

Não existe promoção, aprendizado ou sincronização automática no v0.3.0.

### 2.2 Session pode atravessar múltiplos Datasets

Session não pertence a um Dataset.

O contexto de Dataset é registrado no nível da operação que o utiliza, especialmente em `Query.dataset_ids` e em `PipelineRun.dataset_id`.

### 2.3 Compatibilidade é obrigatória

Chamadas existentes sem `session_id` devem manter sua semântica.

O uso existente de `session_id` em Remember e Recall deve continuar válido.

A introdução de Sessions não pode tornar histórico de versões anteriores artificialmente completo.

### 2.4 Explicit is better than automatic

Não existem no v0.3.0:

- auto-created SessionEntry a partir de Recall;
- automatic session history injection;
- automatic memory promotion;
- automatic restore de Session archived;
- automatic TTL;
- automatic query rewriting.

---

# 3. Identidade da Session

Cada Session possui duas identidades.

### `session_uuid`

UUID gerado pelo Sofias Memory.

É a identidade estrutural usada em relacionamentos internos e URLs públicas.

### `session_id`

String externa estável fornecida pelo caller.

Exemplo:

```text
sofias-assistant:conversation:98231
```

Regras:

- 1 a 255 caracteres após trim;
- case-sensitive;
- imutável;
- não pode ser vazio;
- não é slugified;
- não é normalizado para lowercase;
- é globalmente único na instância.

Se `POST /sessions` omitir `session_id`, o servidor gera um UUID e utiliza sua representação textual como `session_id`.

---

# 4. Modelo conceitual

## 4.1 Session

Campos mínimos:

```text
id              UUID PK
key             TEXT UNIQUE NOT NULL
name            TEXT nullable
status          active | archived
metadata        JSONB NOT NULL
created_at      TIMESTAMPTZ
updated_at      TIMESTAMPTZ
archived_at     TIMESTAMPTZ nullable
```

Na API:

```text
id  → session_uuid
key → session_id
```

`updated_at` representa alteração administrativa da própria Session.

Append de SessionEntry, Recall e Remember **não atualizam `updated_at`**.

Não existem no v0.3.0:

```text
last_active_at
last_accessed_at
expires_at
deleted_at
```

---

## 4.2 SessionEntry

SessionEntry registra contexto temporal explicitamente fornecido pelo caller.

Campos mínimos:

```text
id           UUID PK
session_id   UUID FK → sessions.id
external_id  TEXT nullable
role         TEXT NOT NULL
content      TEXT NOT NULL
metadata     JSONB NOT NULL
created_at   TIMESTAMPTZ
```

Na API:

```text
id         → entry_id
session_id → session_uuid
```

Formato público:

```text
entry_id
session_uuid
external_id
role
content
metadata
created_at
```

`SessionEntry` é append-only no v0.3.0.

Não existem operações públicas de update ou delete.

`role` é open-ended `TEXT`, não PostgreSQL ENUM.

Exemplos possíveis:

```text
user
assistant
agent
workflow
tool
```

O valor de `role` é apenas metadata contextual.

Ele **nunca** é transformado automaticamente em role privilegiada de um provider LLM.

### `external_id`

`external_id` é uma identidade de correlation/idempotency opcional, fornecida pelo caller,
para permitir append retry-safe de SessionEntry.

Regras:

```text
string | null
```

- optional;
- caller-supplied;
- trimmed;
- 1 a 255 caracteres quando presente;
- case-sensitive;
- imutável;
- unique dentro de uma única Session;
- o mesmo `external_id` pode existir em Sessions diferentes.

Este conceito não é vinculado a nenhum caller específico (ex.: Sofia's Assistant) nem a
qualquer noção de `Turn`. É uma primitive genérica de correlation.

---

# 5. Lifecycle

Estados:

```text
active
  ↓ archive
archived
  ↓ restore
active
```

Não existem estados:

```text
deleting
deleted
expired
```

no v0.3.0.

### Active

Permite nova atividade.

### Archived

Permite leitura e alteração administrativa, mas bloqueia nova atividade contextual.

Uma Session archived não é restaurada automaticamente.

---

# 6. Admission semantics

Archive funciona como **admission barrier**.

Depois que uma Session está archived, são bloqueadas novas operações associadas a ela:

- append de SessionEntry;
- novo Recall;
- novo Remember.

Operações já admitidas antes do archive continuam normalmente.

Archive não:

- cancela PipelineRuns;
- interrompe Recall em andamento;
- remove histórico;
- remove memória permanente.

Manual retry de PipelineRun já existente continua permitido e preserva a Session original mesmo que ela esteja archived.

Replay idempotente de trabalho já criado também continua observável.

---

# 7. Lazy creation

Remember e Recall continuam aceitando `session_id`.

Quando uma operação nova recebe uma chave válida:

```text
resolve Session by key
       │
       ├── exists + active → use
       ├── exists + archived → reject
       └── missing → create active Session
```

Criação lazy deve ser concurrency-safe.

`UNIQUE(sessions.key)` é a defesa authoritative.

Duas requisições concorrentes com a mesma chave devem convergir para uma única Session.

---

# 8. API — Session Management

## `POST /api/v1/sessions`

Cria uma Session explicitamente.

Request:

```json
{
  "session_id": "sofias-assistant:conversation:98231",
  "name": "Planejamento do projeto",
  "metadata": {
    "origin": "sofias_assistant"
  }
}
```

`session_id`, `name` e `metadata` podem ser omitidos conforme seus defaults.

Sucesso:

```text
201 Created
```

Uma `session_id` já existente não é tratada como upsert.

Resultado:

```text
409 Conflict
```

---

## `GET /api/v1/sessions`

Lista Sessions.

Parâmetros:

```text
limit
offset
status
session_id
```

Defaults de paginação seguem o padrão geral da API:

```text
limit=50
offset=0
```

`session_id` faz exact match e pode ser utilizado para converter uma chave externa conhecida em `session_uuid`.

---

## `GET /api/v1/sessions/{session_uuid}`

Retorna uma Session.

Session inexistente:

```text
404 INVALID_REQUEST
```

---

## `PATCH /api/v1/sessions/{session_uuid}`

Permite alterar somente:

```text
name
metadata
```

`session_id` é imutável.

`status` não é controlado por PATCH.

`metadata` substitui integralmente o objeto atual; não existe deep merge implícito.

---

## `POST /api/v1/sessions/{session_uuid}/archive`

Sem body.

Idempotente:

```text
active   → archived
archived → archived
```

---

## `POST /api/v1/sessions/{session_uuid}/restore`

Sem body.

Idempotente:

```text
archived → active
active   → active
```

---

# 9. API — SessionEntry

## `POST /api/v1/sessions/{session_uuid}/entries`

Adiciona uma SessionEntry explicitamente.

Request:

```json
{
  "external_id": "caller-stable-id",
  "role": "user",
  "content": "Quais clientes discutimos anteriormente?",
  "metadata": {}
}
```

`external_id` é opcional.

A operação deve verificar a Session sob transação adequada antes do insert.

Session archived:

```text
409 SESSION_ARCHIVED
```

### Sem `external_id`

Comportamento append normal:

```text
sempre cria uma nova SessionEntry
→ 201
```

### Com `external_id` ainda inexistente na Session

```text
cria SessionEntry
→ 201
```

### Replay: mesmo `external_id` + mesmo payload semântico

A operação resolve a SessionEntry já existente. Não cria uma segunda row.

Payload semântico, para efeito deste contrato, é exatamente:

```text
role
content
metadata
```

`external_id` identifica a operação; `entry_id`/`created_at` gerados não entram na
comparação.

Para manter o endpoint simples e consistente como operação de criação idempotente:

```text
201
```

também no replay idempotente, retornando a mesma SessionEntry (mesmo `entry_id`).
Não existe `200` reservado para distinguir replay de criação nova.

### Mesmo `external_id` + payload semântico diferente

```text
409 IDEMPOTENCY_CONFLICT
```

A SessionEntry existente nunca é mutada. Nenhum novo ErrorCode é introduzido para este
caso — reutiliza-se `IDEMPOTENCY_CONFLICT`, o mesmo já usado por outras operações
idempotentes do Sofias Memory.

### Concorrência

Duas requisições concorrentes para:

```text
same session_uuid
same external_id
same payload semântico
```

devem convergir para uma única SessionEntry.

Para:

```text
same session_uuid
same external_id
different payload semântico
```

uma requisição pode vencer a criação; a outra deve observar `409 IDEMPOTENCY_CONFLICT`.

A defesa authoritative no PostgreSQL é a partial unique index
`UNIQUE(session_id, external_id) WHERE external_id IS NOT NULL`, já criada pela
foundation de persistência (SM-601). A aplicação desta lógica de replay/conflito no
endpoint HTTP é responsabilidade do ticket que implementa a SessionEntry API.

---

## `GET /api/v1/sessions/{session_uuid}/entries`

Lista entries temporalmente.

Parâmetros:

```text
limit
offset
order=asc|desc
```

Default:

```text
order=asc
```

Ordenação total:

```text
created_at
id
```

Não existe semantic search de SessionEntry no v0.3.0.

---

# 10. Query integration

`Query` passa a possuir associação opcional com Session.

Conceitualmente:

```text
queries.session_id UUID nullable
    → sessions.id
    ON DELETE SET NULL
```

A API expõe o valor como:

```text
session_uuid
```

Query sem Session continua válida.

---

## 10.1 Session Context provenance

Query também registra os IDs exatos das SessionEntries utilizadas na geração RAG:

```text
session_context_entry_ids UUID[]
```

A ordem do array é semântica:

```text
oldest → newest
```

Esses IDs representam exatamente o contexto utilizado naquela geração.

Não são tratados como set.

---

# 11. Recall integration

Recall continua aceitando:

```text
session_id
```

e passa a aceitar:

```text
include_session_context
```

Default:

```text
false
```

Portanto chamadas existentes mantêm a semântica atual.

---

## 11.1 Sem Session

```text
session_id = null
```

Recall funciona como antes.

Query recebe:

```text
session_uuid = null
```

---

## 11.2 Session sem contexto

```text
session_id != null
include_session_context = false
```

A Session é resolvida ou criada.

A Query é associada à Session.

Nenhum SessionEntry é injetado na geração.

---

## 11.3 Session com contexto

Contexto de Session é permitido somente quando:

```text
session_id != null
mode == rag
only_context == false
include_session_context == true
```

Combinações inválidas retornam:

```text
400 INVALID_REQUEST
```

O parâmetro nunca é silenciosamente ignorado.

---

# 12. Context selection

O v0.3.0 não altera retrieval.

```text
request.query
    ↓
retrieval atual
```

Session Context entra apenas na geração RAG.

O servidor controla limites por configuração imutável de startup, incluindo no mínimo:

```text
SESSION_CONTEXT_MAX_ENTRIES
SESSION_CONTEXT_MAX_CHARS
```

O algoritmo utiliza somente **SessionEntries completas**.

Nunca trunca uma entry individual.

A seleção deve produzir um sufixo cronológico contíguo das entries mais recentes que caiba nos limites.

Se a entry mais recente não couber sozinha:

```text
session context = []
```

O Recall continua normalmente.

---

# 13. Context safety

SessionEntry é dado não confiável.

Seu `role` não determina privilégios de prompt.

O conteúdo deve ser fornecido ao LLM dentro de um bloco contextual delimitado e tratado como informação, não como system instruction.

O Sofias Memory não converte automaticamente:

```text
role=system
role=assistant
role=tool
```

em roles nativas do provider.

---

# 14. Query provenance

`GET /api/v1/provenance/query/{query_id}` passa a expor:

```text
session_uuid
session_context
```

Cada SessionEntry reidratada deve ser validada contra a Session da Query.

Um ID armazenado em `session_context_entry_ids` nunca autoriza retornar uma entry pertencente a outra Session.

A Query continua expondo sua provenance de knowledge normalmente.

Assim uma resposta pode ser auditada contra:

```text
Session Context
+
Knowledge references
```

---

# 15. Session query history

## `GET /api/v1/sessions/{session_uuid}/queries`

Lista Queries associadas à Session.

Retorna uma projeção resumida.

Referências completas permanecem responsabilidade do endpoint existente de Query Provenance.

---

# 16. Remember integration

Remember continua aceitando `session_id`.

Para um novo Remember com Session:

```text
resolve/create Session
resolve Dataset
create PipelineRun
create PipelineSteps
commit
```

A resolução da Session ocorre dentro da mesma transação de submissão do trabalho.

`SubmissionTargets` passa conceitualmente a transportar:

```text
dataset_id
source_id
session_id
```

`PipelineRun` recebe:

```text
session_id UUID nullable
    → sessions.id
    ON DELETE SET NULL
```

A associação representa:

```text
Session → operation
```

e não:

```text
Session → Source
```

Uma Source deduplicada pode ter sido utilizada por várias Sessions.

---

# 17. Run integration

Run summary/detail passa a expor:

```text
session_uuid
```

A listagem de Runs aceita filtro opcional:

```text
session_uuid
```

Manual retry preserva:

```text
dataset_id
source_id
session_id
```

do run original.

---

# 18. Delete semantics

## SessionEntry

```text
Session → SessionEntry
ON DELETE CASCADE
```

É um strict owned child.

Apesar disso, hard delete de Session não possui endpoint público no v0.3.0.

## Query

```text
Session → Query
ON DELETE SET NULL
```

Query é audit/provenance independente.

## PipelineRun

```text
Session → PipelineRun
ON DELETE SET NULL
```

Run é audit operacional independente.

---

# 19. Forget e Dataset Delete

Forget não remove:

- Session;
- SessionEntry;
- Query;
- Feedback;
- PipelineRun.

`DELETE EVERYTHING` significa toda a memória/knowledge gerenciada pelo workflow Forget.

Não significa purge integral de histórico contextual, auditoria ou operação da instância.

Dataset Delete também não remove Sessions ou seu histórico.

Referências históricas a conhecimento removido podem passar a ser retornadas como indisponíveis pela provenance existente.

---

# 20. Legacy compatibility

First-class Sessions começam no v0.3.0.

Não existe backfill heurístico de Session baseado em:

- `Document.metadata["session_id"]`;
- `MemoryEntry.session_id`;
- `PipelineRun.input["session_id"]`;
- qualquer outro metadata histórico.

Esses dados permanecem históricos/legados.

Replay de um PipelineRun pré-v0.3 pode continuar apresentando:

```text
session_uuid = null
```

mesmo que seu payload legado contenha uma string `session_id`.

Isso é comportamento esperado.

---

# 21. Normalização compartilhada

Todos os pontos de entrada usam exatamente a mesma regra de validação/normalização de `session_id`:

```text
POST /sessions
Remember text
Remember file
Remember URL
Recall
```

Sem implementações independentes por endpoint.

---

# 22. Neo4j

Sessions e SessionEntries não são projetadas para Neo4j.

Não existem:

```text
(:Session)
(:SessionEntry)
```

O graph database continua restrito ao knowledge graph reconstructible a partir do PostgreSQL.

---

# 23. Security boundary

Session não é security principal.

Conhecer um `session_id` ou `session_uuid` não concede autorização adicional.

O mecanismo de autenticação da instância permanece independente da Session.

Não existem no v0.3.0:

- per-session API keys;
- per-session ACL;
- ownership;
- users;
- tenants;
- roles de autorização.

---

# 24. Out of scope — v0.3.0

Explicitamente fora deste release:

- Redis/session cache;
- TTL;
- auto-expiration;
- hard Session purge;
- Session embeddings;
- semantic Session search;
- automatic summaries;
- context compaction;
- query rewriting;
- coreference resolution;
- automatic Session → Memory promotion;
- automatic knowledge extraction de Session;
- automatic Query → SessionEntry;
- Session → Neo4j projection;
- Agent;
- AgentConnection;
- agent trace;
- Skill;
- SkillRun;
- tools/runtime execution;
- per-session authentication;
- per-agent authentication.

---

# 25. Invariants obrigatórios

1. PostgreSQL é authoritative para Sessions.

2. Session nunca pertence obrigatoriamente a um Dataset.

3. `session_id` é globalmente único, case-sensitive e imutável.

4. Session archived não admite nova atividade.

5. Archive não cancela trabalho já admitido.

6. Retry preserva a Session original.

7. Recall sem Session continua funcionando como antes.

8. `include_session_context=false` é o default.

9. Session Context não altera retrieval no v0.3.0.

10. Somente SessionEntries completas podem compor contexto RAG.

11. Query persiste exatamente quais SessionEntries foram utilizadas.

12. SessionEntry não é criada automaticamente por Recall.

13. Session Context não recebe privilégios de prompt derivados de `role`.

14. Forget não é Session purge.

15. Dataset Delete não é Session purge.

16. Nenhuma Session histórica é materializada por inferência/backfill.

17. Sessions e SessionEntries nunca são projetadas para Neo4j.

18. Session não é authorization boundary.

---

# 26. Critério de conclusão do v0.3.0

A feature é considerada concluída quando:

- Sessions e SessionEntries possuem persistência PostgreSQL e lifecycle aprovado;
- a API de management funciona com paginação e contratos estáveis;
- lazy creation é concurrency-safe;
- Remember e Recall usam a mesma normalização de `session_id`;
- Queries registram Session e Session Context utilizado;
- Query Provenance reidrata corretamente o contexto;
- PipelineRuns registram Session e retries preservam a associação;
- archive bloqueia somente atividade nova;
- Forget e Dataset Delete preservam Session/history;
- nenhuma alteração é necessária no Neo4j;
- compatibilidade de chamadas pré-v0.3 sem Session permanece validada;
- integration/smoke tests confirmam os invariants deste contrato.