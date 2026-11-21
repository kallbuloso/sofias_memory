# Sofias Memory — Technical Backlog: Memória Contextual Persistente por Projeto

**Document:** `08-SF-Technical-Backlog-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED\
**Date:** 2026-11-18\
**Project:** Sofias Memory\
**Baseline:**
- `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `05-SF-Contract-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `06-SF-Architecture-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `07-SF-ADRs-Memoria-Contextual-Por-Projeto.md` — APPROVED

**Architecture decisions:**
- ADR-0017 — First-Class Contextual Memory Domain and PostgreSQL Authority — ACCEPTED
- ADR-0018 — ContextCheckpoint Revision and Concurrency Model — ACCEPTED
- ADR-0019 — Provenance-Aware Evidence and Synchronous Forget Propagation — ACCEPTED

---

# 1. Objective

Implementar **Contextual Project Memory v1** no Sofias Memory através de slices verticais, incrementais e verificáveis.

O resultado final deverá permitir que um Consumer:

```text
Session A
    ↓
persist context
    ↓
service restart
    ↓
Session B
    ↓
recover where the work stopped
```

e também consiga recuperar:

```text
what is true now
what happened
why it happened
where the work currently stands
```

sem replay integral do histórico anterior.

---

# 2. Delivery Principles

A implementação deverá seguir os seguintes princípios.

## 2.1 Vertical slices

Cada Slice deverá entregar comportamento observável.

Não serão criados slices do tipo:

```text
create models
create repositories
create services
```

isoladamente.

Essas camadas serão implementadas dentro do Slice que necessita delas.

---

## 2.2 One Slice at a time

A ordem deste backlog representa a ordem recomendada de execução.

Um Slice deverá atingir DONE antes do próximo iniciar, salvo dependência explicitamente paralelizável e sem alteração de baseline.

Execução exige também handoff APPROVED e autorização explícita como CURRENT em AGENTS.md. A existência de 09–16 não autoriza Slices futuros. Os ACs deste backlog mantêm identidade histórica; handoffs especializam/estendem os critérios e seus reports usam a numeração local, sem tratar números entre documentos como equivalentes.

---

## 2.3 No silent scope expansion

Durante implementação, boas ideias fora do objetivo do Slice deverão ser classificadas como:

```text
DEFERRED
```

e não incorporadas silenciosamente.

---

## 2.4 Contract-first implementation

O Contract v1 aprovado é autoridade sobre comportamento público.

Detalhes locais podem ser ajustados durante implementação desde que não alterem:

- semântica;
- ownership;
- lifecycle;
- compatibility;
- security boundary;
- public contract.

---

## 2.5 Full capability advertisement only when complete

Enquanto o backlog ainda estiver incompleto, a aplicação MUST NOT anunciar:

```json
{
  "contracts": {
    "contextual_project_memory": "1"
  }
}
```

como completamente implementado.

O Contract v1 só será anunciado após o gate final deste backlog.

Isso evita que Consumers assumam suporte completo a um contrato parcialmente implementado.

---

# 3. Global Out of Scope

Não fazem parte deste backlog:

- automatic Episode extraction;
- automatic ContextCheckpoint generation;
- automatic Session distillation;
- autonomous memory consolidation;
- LLM contradiction resolution;
- LLM Evidence sufficiency;
- MemorySpace;
- Episode graph projection;
- Episode-to-Episode Neo4j graph;
- automatic global + project composition;
- cross-project recall;
- semantic checkpoint search;
- HNSW/ANN para Episodes;
- memory decay;
- importance scoring;
- multi-user;
- tenancy;
- RBAC;
- per-project ACL;
- Agent Runtime;
- task execution;
- workflow execution.

---

# 4. Implementation Sequence

```text
SM-CTX-001
Episode persistence + lifecycle
      │
      ▼
SM-CTX-002
Context Streams + initial checkpoints
      │
      ▼
SM-CTX-003
Checkpoint revisions + concurrency
      │
      ▼
SM-CTX-004
Evidence + deterministic support
      │
      ▼
SM-CTX-005
Provenance-aware Forget propagation
      │
      ▼
SM-CTX-006
Contextual Recall core
      │
      ▼
SM-CTX-007
Cross-domain recall integration
      │
      ▼
SM-CTX-008
Hardening + longitudinal validation + Contract v1 activation
```

---

# 5. Global Definition of Done

Uma task/Slice só poderá ser considerada DONE quando:

- comportamento previsto estiver implementado;
- migrations necessárias estiverem versionadas;
- public schemas estiverem coerentes com o Contract;
- unit tests relevantes passarem;
- integration tests relevantes passarem;
- nenhum BLOCKER permanecer;
- regressões nos contratos existentes forem verificadas;
- Human Validation prevista tiver sido executada;
- documentação afetada tiver sido atualizada quando aplicável;
- completion evidence estiver registrada.

---

# 6. SM-CTX-001 — First-Class Episodes

## Objective

Entregar `Episode` como recurso first-class persistente, semanticamente recuperável e com lifecycle próprio.

---

## Product Value

Ao final deste Slice, o Sofias Memory deverá conseguir preservar uma experiência histórica explicitamente sem convertê-la em `MemoryItem`, `SessionEntry` ou Knowledge Memory.

O resultado utilizável é:

```text
Consumer
   ↓
create Episode
   ↓
restart
   ↓
retrieve Episode
```

---

## Scope

Implementar:

- persistence de Episode;
- Episode kinds v1;
- lifecycle:
  - `active`;
  - `invalidated`;
  - `forgotten`;
- `occurred_at`;
- provenance;
- optional `context_key`;
- embedding;
- exact pgvector semantic search interno;
- create;
- get;
- list;
- invalidate;
- direct Forget;
- Episode tombstone;
- contextual idempotency para Episode mutations.

Public surface:

```http
POST /api/v1/episodes
GET  /api/v1/episodes
GET  /api/v1/episodes/{episode_id}
POST /api/v1/episodes/{episode_id}/invalidate
POST /api/v1/episodes/{episode_id}/forget
```

---

## Out of Scope

- Evidence links;
- ContextCheckpoint;
- Contextual Recall;
- Forget propagation para derivados;
- graph projection.

Requests com `support`/Evidence não deverão ser considerados completamente suportados até SM-CTX-004.

---

## Dependencies

- ADR-0017;
- existing embedding provider;
- PostgreSQL + pgvector;
- existing scope validation rules where reusable.

---

## Implementation Plan

1. Criar migration para Episode e contextual idempotency necessária.
2. Criar domain enums:
   - `EpisodeLifecycle`;
   - `EpisodeKind`.
3. Implementar canonical Episode embedding text.
4. Implementar repository.
5. Implementar idempotency semantics.
6. Implementar Episode Service.
7. Implementar schemas públicos.
8. Implementar HTTP routes.
9. Implementar exact cosine retrieval interno.
10. Garantir ausência de Neo4j/outbox side effects.

---

## Acceptance Criteria

### AC-001.1

É possível criar Episode ACTIVE.

### AC-001.2

Episode recebe UUID server-side.

### AC-001.3

`occurred_at` é independente de `created_at`.

### AC-001.4

Episode persiste após restart.

### AC-001.5

Episode pode ser listado por scope.

### AC-001.6

Episode pode ser filtrado por:

```text
context_key
kind
lifecycle
```

### AC-001.7

Normal listing exclui INVALIDATED/FORGOTTEN quando lifecycle não é solicitado.

### AC-001.8

Invalidate realiza:

```text
ACTIVE -> INVALIDATED
```

### AC-001.9

Forget realiza:

```text
ACTIVE | INVALIDATED -> FORGOTTEN
```

### AC-001.10

FORGOTTEN não retorna conteúdo semântico.

### AC-001.11

Não existe PATCH/PUT público.

### AC-001.12

Retry com mesma Idempotency-Key e mesmo payload não cria Episode adicional.

### AC-001.13

Mesma Idempotency-Key com payload diferente produz conflict.

### AC-001.14

Episode write não produz `graph_outbox`.

---

## Required Tests

Unit:

- kind validation;
- lifecycle transitions;
- canonical embedding text;
- idempotency digest/replay;
- tombstone behavior.

PostgreSQL integration:

- create/get/list;
- restart durability;
- invalidate;
- forget;
- idempotency replay;
- idempotency conflict;
- exact cosine search;
- scope isolation.

Cross-feature:

- Episode não cria `MemoryItem`;
- Episode não cria SessionEntry;
- Episode não cria graph projection.

---

## Human Validation

Criar manualmente um Episode representando uma decisão histórica.

Reiniciar aplicação.

Recuperar o Episode e confirmar:

- conteúdo correto;
- provenance;
- lifecycle;
- `occurred_at`.

Depois:

1. invalidar;
2. verificar ausência no listing normal;
3. esquecer;
4. verificar tombstone.

---

## Completion Evidence

Registrar:

- migration aplicada;
- testes PASS;
- smoke HTTP;
- restart smoke;
- ausência de graph_outbox contextual.

---

## SM-CTX-001 — Evidência de fechamento (DONE)

```text
Status:                  DONE (2026-11-21)
Baseline:                main @ 6a7c698555a64f6b14b4f547fb585512163f1f71
Implementation SHA:      f61e5bfff4078b25fb4630d21495cf2e5565b018
Operational closeout:    35ea170435fc120c24bb45e24e53cb6084b332de
                          (docs(context): record SM-CTX-001 operational closeout)
Merge into main:         b616f216c2203c7d00d6cd952d6d3d4f4c494b58 (--no-ff)
Alembic head:            0019 (down_revision = 0018)
```

- Entregue: Episode first-class (create/get/list/internal semantic retrieval/
  invalidate/direct Forget com tombstone), provenance, ledger genérico
  `contextual_memory_idempotency` (HMAC com domain separation) e persistência
  PostgreSQL + pgvector. Sem Neo4j, graph_outbox ou PipelineRun contextual.
- Testes: suíte local PASS; integração do Slice 19/19 PASS no banco dedicado
  `sofias_memory_ctx_episodes_test`; regressão PostgreSQL relevante PASS.
- Human Validation A–G: PASS (provider de embedding real, banco dedicado).
- Temporal regression gate (pós-correção NTP do host): 20/20 PASS em Create +
  Recall Cognitive imediato com `as_of` padrão; controles negativos sem
  vazamento.
- Achados operacionais e DEFERRED registrados em
  `CONTEXTUAL-PROJECT-MEMORY-DOCUMENT-AUDIT.md`:
  - política transversal de fontes de tempo (`now()` PostgreSQL vs.
    `utc_now()` da aplicação) — risco residual MINOR;
  - fixtures de integração Skills/Agents sem `cognitive_idempotency_hmac_key`;
  - cleanup dos testes de integração de Episodes (tombstones/ledger residuais);
  - rastreabilidade de migrations em desenvolvimento (`verify_only`, logs DDL).

---

# 7. SM-CTX-002 — Context Streams and Initial Checkpoints

## Objective

Entregar Context Stream e primeiro `ContextCheckpoint` persistente.

---

## Product Value

Ao final do Slice, um projeto poderá registrar explicitamente:

```text
onde este trabalho está
```

e recuperar esse estado depois de restart.

---

## Scope

Implementar:

- canonical `context_key`;
- default:
  ```text
  main
  ```
- ContextCheckpoint;
- ContextStatement normalizado;
- statement types:
  - objective;
  - current_state;
  - approved_decision;
  - pending_work;
  - open_item;
  - blocker;
  - next_action;
- create current when no ACTIVE exists, including recovery Create;
- current checkpoint lookup;
- get by ID;
- list/history;
- direct invalidate;
- direct Forget;
- checkpoint tombstone;
- revision 1 for a new stream; MAX(existing revision)+1 for history without ACTIVE;
- contextual idempotency para essas mutations.

Public surface parcial:

```http
POST /api/v1/context/checkpoints
GET  /api/v1/context/checkpoints
GET  /api/v1/context/checkpoints/current
GET  /api/v1/context/checkpoints/{checkpoint_id}
POST /api/v1/context/checkpoints/{checkpoint_id}/invalidate
POST /api/v1/context/checkpoints/{checkpoint_id}/forget
```

---

## Out of Scope

- supersede;
- stale concurrency;
- Evidence;
- Contextual Recall.

---

## Dependencies

- SM-CTX-001;
- ADR-0017;
- ADR-0018.

---

## Implementation Plan

1. Criar migrations:
   - `context_checkpoints`;
   - `context_checkpoint_statements`.
2. Implementar `context_key` validation.
3. Implementar partial unique constraint para ACTIVE.
4. Implementar revision uniqueness.
5. Implementar statement ordering.
6. Implementar Checkpoint Service.
7. Implementar create/get/current/list.
8. Implementar invalidate/forget.
9. Garantir no automatic fallback.

---

## Acceptance Criteria

### AC-002.1

É possível criar primeiro checkpoint para:

```text
(scope, context_key)
```

### AC-002.2

`context_key` omitido resolve para:

```text
main
```

### AC-002.3

Resposta retorna `context_key` resolvido.

### AC-002.4

Primeiro checkpoint recebe:

```text
revision = 1
lifecycle = active
```

### AC-002.5

`current_state` é obrigatório.

### AC-002.6

Statements preservam ordenação.

### AC-002.7

Current lookup retorna ACTIVE.

### AC-002.8

Create no stream enquanto existir ACTIVE produz conflict. Histórico sem ACTIVE aceita novo Create explícito com MAX+1, conforme handoff `10` APPROVED.

### AC-002.9

Mesmo `context_key` pode existir em scopes diferentes.

### AC-002.10

Um projeto pode possuir múltiplos context streams simultâneos.

### AC-002.11

ACTIVE invalidado deixa stream sem current checkpoint.

### AC-002.12

ACTIVE forgotten deixa stream sem current checkpoint.

### AC-002.13

Nenhum histórico é reativado automaticamente.

### AC-002.14

Estado sobrevive a restart.

---

## Required Tests

Unit:

- `context_key`;
- statement validation;
- lifecycle;
- ordering.

PostgreSQL integration:

- initial create;
- recovery Create after Invalidate/Forget, including all-forgotten history after restart;
- checkpoint tombstone retains scope/context_key/revision and prevents revision reuse;
- duplicate ACTIVE;
- current lookup;
- list;
- scope isolation;
- context stream isolation;
- invalidate;
- forget;
- restart.

Concurrency smoke:

- dois creates iniciais simultâneos;
- apenas um pode vencer.

---

## Human Validation

Criar:

```text
project:test
context_key=feature:memory
```

com:

```text
objective
current_state
pending_work
next_action
```

Reiniciar.

Executar current lookup e confirmar o mesmo estado.

Invalidar e confirmar:

```text
CURRENT_CHECKPOINT_NOT_FOUND
```

sem fallback.

---

## SM-CTX-002 — Evidência de fechamento (DONE)

```text
Status:                  DONE (2026-11-21)
Baseline:                main @ b4347f34cb157d493033c9a4f5b8e1de4d8b11f9
Implementation SHA:      a9ab03f0c5c4d0af9aacbb240c75eb2fbc6ec60f
                          (feat(context): add context streams and initial checkpoints)
Merge into main:         14e8c8d60308a53f01662e0a56e5631bd7ee4690 (--no-ff)
Alembic head:            0020 (down_revision = 0019)
```

- Entregue: ContextCheckpoint/ContextStatement first-class para Context Streams
  lógicos `(scope, context_key)` (sem tabelas `context_streams`/`projects`);
  create current quando não há ACTIVE (revision 1 ou `MAX(revision)+1`,
  tombstones incluídos), get, current, list (keyset cursor), invalidate e
  direct Forget com tombstone estrutural (scope/context_key/revision mantidos,
  statements apagados, refs externas e motivo de invalidação scrubbed);
  provenance 1:1; ledger compartilhado `contextual_memory_idempotency`.
- Concorrência: Create serializado por stream com
  `pg_advisory_xact_lock(bigint)` (chave = primeiros 8 bytes de SHA-256 sobre
  domínio versionado + scope + context_key), adquirido antes de ledger/ACTIVE/
  revision; safety nets estruturais: índice único parcial
  `uq_context_checkpoints_active_stream` e
  `UNIQUE(scope, context_key, revision)`. Replay idempotente precede
  `CONTEXT_STATE_CONFLICT`.
- Acceptance Criteria AC-002.1–AC-002.20: PASS.
- Human Validation A–K: PASS — executada pela API real (uvicorn + banco
  dedicado `sofias_memory_ctx_episodes_test`, `DATABASE_MIGRATION_MODE=verify_only`,
  dois restarts reais do processo) e aceita na autorização de closeout.
- Verificações adicionais pela API real: PASS — replay após invalidate/Forget,
  same-key/different-payload, isolamento entre projetos, múltiplos streams
  ACTIVE, MAX+1 após esquecer todas as revisions + restart, ausência de texto
  semântico em tombstones/ledger/logs, sem graph_outbox/PipelineRun.
- Concorrência independente via HTTP real: PASS — stream novo 8 keys → 1×201
  (rev 1) + 7×409; recovery 8 keys → 1×201 (rev 2) + 7×409; mesmo key ×6 → um
  único checkpoint; sonda Invalidate/Forget × Create (30 iterações) sem
  violação.
- Testes: suíte local 2805 passed (672 skipped opt-in); integração do Slice
  19/19 PASS (corridas com contenção real observada em `pg_locks`); regressão
  PostgreSQL relevante 235 PASS (Episodes, Cognitive Memory, Sessions, schema
  guard, UoW, readiness, migration gate); 22 sondas SQL de constraints PASS.
- Migration 0020: upgrade/downgrade/upgrade PASS no banco dedicado e migration
  gate a partir de banco vazio PASS. `cognee_db` permanece em 0019.
- CI GitHub Actions: não verificado (sem evidência coletada).
- Correções feitas durante a validação independente (incluídas em `a9ab03f`):
  identificadores de constraint encurtados para caber no limite de 63 bytes do
  PostgreSQL (com teste de guarda); holder do lock nos testes de corrida usa a
  API de transação do SQLAlchemy.
- MINOR:
  - `ruff format --check .` acusa ~20 arquivos pré-existentes (CRLF), sem
    relação com o Slice;
  - replay de Create devolve o recurso no estado atual (inclusive tombstone),
    com 201, seguindo o precedente de Episodes;
  - `invalidation_reason` é persistido mas não exposto (Contract §24);
  - `context_key: null` explícito no Create é rejeitado (apenas omissão → `main`);
  - SUPERSEDED → invalidate/forget responde conflito (estado inalcançável até
    SM-CTX-003, que habilita essas transições).
- DEFERRED:
  - política transversal de fontes de tempo (`now()` PostgreSQL vs.
    `utc_now()` da aplicação em `created_at`/lifecycle timestamps);
  - Create rejeitado com 409 não é registrado no ledger (mesmo padrão de
    Episodes; Contract §63 desaconselha blind retry);
  - fixtures de integração Skills/Agents sem `cognitive_idempotency_hmac_key`
    (pré-existente).

---

# 8. SM-CTX-003 — Checkpoint Revision, Supersession and Concurrency

## Objective

Entregar evolução segura e versionada do ContextCheckpoint.

---

## Product Value

Ao final deste Slice, múltiplos callers poderão atualizar continuidade sem risco de `last write wins` silencioso.

---

## Scope

Implementar:

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
```

com:

- full replacement;
- revision monotônica;
- atomic supersession;
- lineage;
- stale detection;
- locking;
- unique ACTIVE protection;
- idempotency under race;
- history preservado.

---

## Out of Scope

- Evidence;
- Contextual Recall.

---

## Dependencies

- SM-CTX-002;
- ADR-0018.

---

## Implementation Plan

1. Implementar transactional supersession.
2. Resolver idempotency antes de lifecycle conflict.
3. Lock current state.
4. Validar target ACTIVE.
5. Calcular próxima revision.
6. Criar replacement completo.
7. Atualizar old checkpoint:
   ```text
   ACTIVE -> SUPERSEDED
   ```
8. Persistir lineage.
9. Tratar unique constraint como safety net.
10. Mapear stale conflict.

---

## Acceptance Criteria

### AC-003.1

A → B gera:

```text
A = SUPERSEDED
B = ACTIVE
```

### AC-003.2

B recebe:

```text
revision = A.revision + 1
```

### AC-003.3

Supersession é atômica.

### AC-003.4

Não existe janela commitada com dois ACTIVE.

### AC-003.5

Writer stale recebe:

```text
CONTEXT_STATE_CONFLICT
```

### AC-003.6

Writer stale não cria recurso.

### AC-003.7

Current state vencedor permanece inalterado após stale conflict.

### AC-003.8

Retry da mutation já concluída retorna mesmo resultado lógico.

### AC-003.9

Retry não cria revision adicional.

### AC-003.10

Full replacement não herda silenciosamente statements omitidos.

### AC-003.11

Historical checkpoints permanecem listáveis.

### AC-003.12

Invalidation/Forget de current não reativa superseded.

---

## Required Tests

PostgreSQL integration obrigatório:

- A → B;
- A → B → C;
- stale race;
- simultaneous supersede;
- same-key retry;
- same-key/different-payload conflict;
- revision uniqueness;
- active uniqueness;
- transaction rollback;
- restart after revisions.

---

## Human Validation

1. Criar A.
2. Ler A em dois clients.
3. Client 1 cria B.
4. Client 2 tenta A → C.
5. Confirmar:
   ```text
   B ACTIVE
   C inexistente
   ```
6. Listar histórico e confirmar lineage.

---

## SM-CTX-003 — Evidência de fechamento (DONE)

```text
Status:                  DONE (2026-11-21)
Baseline:                main @ 522dc403424d8fd693d122b476200eb3a9e2b7fe
Implementation SHA:      05376ee4ddbf0ce8f1bfdabb7f9895070001b9dd
                          (feat(context): add atomic checkpoint supersession)
Merge into main:         c70ab8609b4ba212f568bc01040675e9d80a34ad (--no-ff)
Alembic head:            0021 (down_revision = 0020)
```

- Entregue: `POST /api/v1/context/checkpoints/{checkpoint_id}/supersede` (path id =
  expected current; full replacement sem herança de campos omitidos; novos
  statements/provenance; `revision = old + 1`; old → SUPERSEDED com
  `superseded_by`); stale writer → `409 CONTEXT_STATE_CONFLICT` com
  `expected_checkpoint_id`/`current_checkpoint_id`, sem criar recurso; replay
  idempotente precede rejeição de lifecycle; SUPERSEDED → INVALIDATED/FORGOTTEN
  preservando lineage, sem cascata para descendentes.
- Migration 0021 (aditiva): índice único parcial
  `uq_context_checkpoints_superseded_by` (lineage linear) e FK de lineage
  `DEFERRABLE INITIALLY IMMEDIATE`, diferida apenas dentro da transação de
  supersede e verificada no COMMIT. Upgrade/downgrade/upgrade PASS no banco
  dedicado; downgrade restaura FK `NOT DEFERRABLE` e remove o índice. 0001–0020
  inalteradas.
- Achado de concorrência identificado e corrigido antes do commit: inversão de
  ordem de locks (Supersede: key → linha; Invalidate/Forget: linha → key)
  causava deadlock PostgreSQL (HTTP 500) com o mesmo Idempotency-Key em
  operações diferentes sobre o mesmo checkpoint; reproduzido de forma
  determinística (`DeadlockDetectedError`). Correção: ordem única
  key claim → row lock em Supersede/Invalidate/Forget (handoffs 10 §73–74 e
  11 §33); Create segura apenas o advisory lock do stream.
- Acceptance Criteria AC-003.1–AC-003.12: PASS.
- Human Validation A–J: PASS — pela API real (uvicorn + banco dedicado
  `sofias_memory_ctx_episodes_test`, `DATABASE_MIGRATION_MODE=verify_only`,
  restart real do processo).
- Validações críticas adicionais: 12/12 PASS.
  - Pela API real + inspeção direta do PostgreSQL (11/12): full replacement,
    novas identidades de statements/provenance, same-key/different-payload,
    races same-key e different-key (incl. rajada de 8), Supersede × Invalidate
    e Supersede × Forget nas duas ordens, mesmo key entre operações diferentes
    (6 ordens, 0 HTTP 500, nenhum deadlock), replay após replacement
    SUPERSEDED e FORGOTTEN, isolamento de streams/projetos, ausência de rotas
    extras/graph_outbox/PipelineRun. A contenção via API foi coordenada por
    `SELECT ... FOR UPDATE` com cada concorrente observado bloqueado em
    `pg_stat_activity`.
  - Somente por harness de integração (1/12): rollback atômico com falha
    injetada após a transição do alvo e após o insert do replacement
    (Supersede), e após a mutação em Forget/Invalidate — checkpoint,
    replacement, statements, provenance e claim do ledger revertidos.
- Testes: suíte local 2822 passed (710 skipped opt-in); regressão de integração
  PostgreSQL 273 passed (Episodes, Cognitive Memory, Sessions, checkpoints,
  schema guard, UoW, readiness, migration gate); suítes de checkpoint
  (supersede, lock-order, SM-CTX-002) 57/57 em 3 execuções; verificação focada
  final 272 passed.
- CI GitHub Actions: não verificado (sem evidência coletada).
- MINOR:
  - banco de teste contém 6 registros de ledger órfãos dos ensaios de
    SM-CTX-002 (checkpoints removidos pelo downgrade de 0020 durante a validação
    daquele Slice; sem defeito de código);
  - `ruff format --check .` acusa arquivos pré-existentes (CRLF).
- DEFERRED:
  - política transversal de fontes de tempo (`now()` PostgreSQL vs.
    `utc_now()` da aplicação);
  - mutations rejeitadas não registradas no ledger (padrão aprovado);
  - métricas de supersede (opcionais no handoff 11 §111);
  - resíduos dos testes de integração de Episodes no banco dedicado;
  - fixtures Skills/Agents sem `cognitive_idempotency_hmac_key` (pré-existente).

---

# 9. SM-CTX-004 — First-Class Evidence and Deterministic Support

## Objective

Entregar Evidence first-class para Episodes e ContextStatements.

---

## Product Value

Ao final deste Slice, o Sofias Memory deverá conseguir responder estruturalmente:

```text
De onde veio esta informação?
```

e saber deterministicamente se ela continua sustentada.

---

## Scope

Implementar:

- `contextual_evidence_refs`;
- Episode-Evidence links;
- Statement-Evidence links;
- Evidence source types v1:
  - session_entry;
  - memory_item;
  - episode;
  - source;
  - document;
  - chunk;
- availability;
- `all_required`;
- `any_sufficient`;
- internal source resolvers;
- cycle detection;
- source existence validation;
- support state evaluation;
- Contract request/response support payloads.

---

## Out of Scope

- automatic inference;
- LLM sufficiency;
- Forget propagation — será SM-CTX-005.

---

## Dependencies

- SM-CTX-001;
- SM-CTX-002;
- SM-CTX-003;
- ADR-0019.

---

## Implementation Plan

1. Criar Evidence tables/migrations.
2. Implementar `EvidenceSourceType`.
3. Implementar resolvers por domínio.
4. Implementar get-or-create `(source_type, source_id)`.
5. Implementar Episode links.
6. Implementar ContextStatement links.
7. Implementar support evaluator.
8. Implementar recursive cycle detection para Episode dependencies.
9. Integrar criação de Episode.
10. Integrar criação/supersession de checkpoint.
11. Expor Evidence availability nas responses.

---

## Acceptance Criteria

### AC-004.1

Episode pode referenciar SessionEntry.

### AC-004.2

Episode pode referenciar MemoryItem.

### AC-004.3

Episode pode referenciar outro Episode.

### AC-004.4

ContextStatement pode possuir Evidence própria.

### AC-004.5

Evidence inexistente é rejeitada.

### AC-004.6

Evidence forgotten/unavailable obrigatória é rejeitada em novo conteúdo derivado.

### AC-004.7

`all_required` é suportado.

### AC-004.8

`any_sufficient` é suportado.

### AC-004.9

Self-reference é rejeitada.

### AC-004.10

Cycle indireto é rejeitado.

### AC-004.11

Evidence não copia integralmente source content.

### AC-004.12

Mesmo source ref é canonicalizado/reutilizado.

### AC-004.13

Evidence em checkpoints é statement-level.

---

## Required Tests

Unit:

- support evaluator;
- source-type dispatch;
- cycle detection.

PostgreSQL integration:

- each source type;
- duplicate evidence ref;
- Episode evidence;
- statement evidence;
- all_required;
- any_sufficient;
- direct cycle;
- indirect cycle;
- concurrent Evidence creation.

Cross-feature:

- SessionEntry resolver;
- MemoryItem resolver;
- Knowledge source/document/chunk resolver.

---

## Human Validation

Criar Episode sustentado por:

```text
SessionEntry A
MemoryItem B
```

com:

```text
any_sufficient
```

Recuperar e confirmar Evidence refs e availability.

Criar checkpoint em que apenas `next_action` possui Evidence.

Confirmar que Evidence é associada ao statement correto.

---

# 10. SM-CTX-005 — Provenance-Aware Forget Propagation

## Objective

Garantir que Forget de uma Evidence interna impeça resurrection de conteúdo através de Episodes ou ContextCheckpoints derivados.

---

## Product Value

Ao final deste Slice:

> esquecer realmente significa esquecer dentro do novo domínio contextual.

---

## Scope

Implementar:

- `ContextualEvidenceInvalidator`;
- synchronous propagation;
- transitive propagation;
- support reevaluation;
- Episode invalidation/scrub;
- ContextStatement scrub/unavailability;
- ACTIVE checkpoint invalidation se `current_state` perder suporte;
- no historical fallback;
- integration com:
  - Cognitive Forget;
  - Knowledge Forget;
  - Episode direct Forget;
- provenance/evidence state update;
- corruption/cycle fail-closed handling.

---

## Out of Scope

- asynchronous propagation;
- LLM evaluation;
- external Evidence lifecycle monitoring.

---

## Dependencies

- SM-CTX-004;
- ADR-0019.

---

## Implementation Plan

1. Implementar dependency traversal.
2. Implementar source Evidence state transition.
3. Implementar support reevaluation.
4. Implementar Episode unsupported behavior.
5. Implementar statement unsupported behavior.
6. Implementar checkpoint structural evaluation.
7. Integrar Cognitive Forget transaction.
8. Integrar Knowledge Forget authoritative step.
9. Integrar direct Episode Forget.
10. Garantir transitive stabilization.
11. Garantir no resurrection after successful Forget.

---

## Acceptance Criteria

### AC-005.1

Forget de Evidence única em `all_required` remove disponibilidade do conteúdo derivado.

### AC-005.2

Forget de uma Evidence em `any_sufficient` preserva conteúdo se outra AVAILABLE existir.

### AC-005.3

Provenance reflete Evidence perdida.

### AC-005.4

Unsupported Episode não participa de recall normal.

### AC-005.5

Unsupported ContextStatement não retorna conteúdo.

### AC-005.6

Checkpoint pode sobreviver parcialmente se `current_state` continuar válido.

### AC-005.7

Perda de suporte de `current_state` invalida ACTIVE checkpoint.

### AC-005.8

Checkpoint anterior não é reativado.

### AC-005.9

Forget de Episode propaga para dependentes.

### AC-005.10

Propagação transitiva alcança estado estável.

### AC-005.11

Após successful Forget, não existe janela em que derivado unsupported permaneça recuperável.

### AC-005.12

Nenhum LLM participa da decisão.

---

## Required Tests

PostgreSQL integration crítico:

- Cognitive Memory → Episode;
- Memory → Episode → Episode;
- Episode → checkpoint statement;
- Knowledge source → Episode;
- all_required;
- any_sufficient;
- partial checkpoint;
- current_state loss;
- active checkpoint invalidation;
- transitive propagation;
- direct Episode Forget;
- transaction rollback;
- failure injection.

Regression:

- existing Forget semantics continuam funcionando.

---

## Human Validation

Construir:

```text
Memory A
   ↓
Episode B
   ↓
Episode C
   ↓
Checkpoint current_state D
```

Esquecer A.

Confirmar que nenhum conteúdo dependente exclusivamente dessa cadeia permanece recuperável.

Repetir com `any_sufficient` e segunda Evidence independente para confirmar preservação correta.

---

# 11. SM-CTX-006 — Contextual Recall Core

## Objective

Entregar primeira recuperação estruturada de Project Context.

---

## Product Value

Ao final deste Slice, uma nova Session deverá conseguir perguntar:

```text
Onde paramos?
```

e obter continuidade persistente sem replay integral da Session anterior.

---

## Scope

Implementar:

```http
POST /api/v1/context/recall
```

com intents:

```text
resume
context
```

e fontes:

- current ContextCheckpoint;
- current Cognitive Memory;
- Episodes.

Implementar:

- query embedding;
- Episode exact cosine retrieval;
- intent planning determinístico;
- bounded output;
- result classification;
- warnings;
- `continuity_available`;
- scope isolation;
- `context_key`;
- no auto-global;
- no Session replay;
- non-generative structured response.

---

## Out of Scope

- `explain`;
- `history`;
- Knowledge Recall integration;
- degraded Knowledge semantics;
- Evidence expansion avançada.

---

## Dependencies

- SM-CTX-005;
- existing Cognitive Memory recall.

---

## Implementation Plan

1. Criar request/response schemas.
2. Implementar intent planner.
3. Implementar checkpoint reader.
4. Integrar Cognitive Recall internamente.
5. Implementar Episode semantic retrieval.
6. Implementar bounded merge.
7. Implementar classifications.
8. Implementar warnings:
   - `NO_CURRENT_CHECKPOINT`;
   - `AMBIGUOUS_CURRENT_TRUTH` quando detectável.
9. Implementar `resume`.
10. Implementar `context`.

---

## Acceptance Criteria

### AC-006.1

`resume` retorna current checkpoint quando existe.

### AC-006.2

`resume` sem checkpoint retorna:

```text
continuity_available=false
```

sem inventar estado.

### AC-006.3

`resume` pode incluir current Cognitive Memory relevante.

### AC-006.4

`resume` pode incluir relevant Episodes.

### AC-006.5

`context` retorna conjunto seletivo e bounded.

### AC-006.6

Historical Episodes são classificados como:

```text
historical_episode
```

### AC-006.7

Current Cognitive Memory é classificada como:

```text
current_semantic
```

### AC-006.8

Current checkpoint é classificado explicitamente.

### AC-006.9

`project:a` não recupera `project:b`.

### AC-006.10

Global Cognitive Memory não é adicionada automaticamente.

### AC-006.11

Nenhuma resposta final LLM é necessária.

### AC-006.12

Session history integral não é carregada.

---

## Required Tests

Unit:

- intent source planning;
- bounded merge;
- classification;
- warnings.

Integration:

- checkpoint + cognitive + episodes;
- scope isolation;
- absent checkpoint;
- historical/current distinction;
- restart.

---

## Human Validation

### Session A

Persistir:

- MemoryItem;
- Episode;
- current checkpoint.

Encerrar processo.

### Session B

Após restart:

```text
intent=resume
query="Onde paramos?"
```

Confirmar que resultado contém:

```text
objective
current_state
pending_work
next_action
```

sem replay de Session A.

---

# 12. SM-CTX-007 — Explain, History and Knowledge Integration

## Objective

Completar Contextual Recall v1 com recuperação causal, histórica e Knowledge Memory opcional.

---

## Product Value

Ao final deste Slice, o Consumer poderá perguntar:

```text
Por que tomamos essa decisão?
```

e:

```text
O que decidimos antes?
```

com distinção explícita entre estado vigente e histórico.

---

## Scope

Adicionar intents:

```text
explain
history
```

Adicionar:

- Evidence expansion;
- `include_evidence`;
- bounded Evidence expansion;
- historical checkpoints;
- Knowledge Recall integration;
- explicit `datasets`;
- no Dataset inference;
- partial/degraded source semantics;
- `PARTIAL_CONTEXT`;
- `EVIDENCE_UNAVAILABLE`;
- `degraded_sources`;
- source-specific ranking;
- deterministic cross-source composition.

---

## Out of Scope

- cross-domain RRF/rank fusion; existing RRF inside Knowledge retrieval may be reused;
- LLM source router;
- automatic Dataset selection;
- semantic checkpoint embeddings.

---

## Dependencies

- SM-CTX-006;
- existing Knowledge Recall.

---

## Implementation Plan

1. Implementar `explain`.
2. Implementar `history`.
3. Implementar historical checkpoint reader.
4. Implementar Evidence expansion.
5. Implementar Knowledge Recall adapter interno.
6. Requerer `datasets` explícitos para Knowledge.
7. Implementar degraded-source classification.
8. Implementar warning aggregation.
9. Implementar deterministic per-intent quotas.
10. Garantir que raw source scores não sejam comparados ingenuamente.

---

## Acceptance Criteria

### AC-007.1

`explain` pode retornar current semantic + Episode + Evidence.

### AC-007.2

`history` retorna Episodes históricos.

### AC-007.3

`history` pode retornar superseded checkpoints.

### AC-007.4

Histórico permanece explicitamente classificado.

### AC-007.5

Knowledge só é consultada quando `datasets` forem fornecidos.

### AC-007.6

Project scope não infere Dataset por nome.

### AC-007.7

`include_evidence=false` não expande conteúdo completo.

### AC-007.8

`include_evidence=true` respeita Evidence limit.

### AC-007.9

Evidence forgotten não é expandida.

### AC-007.10

Falha opcional de Knowledge pode produzir:

```text
PARTIAL_CONTEXT
```

### AC-007.11

Resultado parcial identifica `degraded_sources`.

### AC-007.12

Falha de fonte não é substituída por conteúdo inventado.

### AC-007.13

Cross-source composition não usa raw score global como ordem absoluta.

---

## Required Tests

Unit:

- explain planning;
- history planning;
- evidence expansion;
- knowledge gating;
- degradation logic;
- source quotas.

Integration:

- Cognitive + Episode + Evidence;
- historical checkpoint;
- explicit Dataset;
- Knowledge unavailable;
- Evidence unavailable;
- bounded output.

---

## Human Validation

Executar:

```text
intent=explain
query="Por que Episode foi criado como agregado separado?"
```

Confirmar retorno de:

- decisão atual;
- Episode histórico;
- Evidence.

Depois:

```text
intent=history
query="Quais decisões anteriores tivemos?"
```

Confirmar que histórico não é confundido com current truth.

---

# 13. SM-CTX-008 — Contract Hardening and Longitudinal Validation

## Objective

Provar que Contextual Project Memory v1 está completa, compatível, recuperável e pronta para ser anunciada publicamente.

---

## Product Value

Este Slice transforma os incrementos anteriores em uma feature oficialmente utilizável por Consumers externos.

---

## Scope

Executar hardening final:

- Contract v1 compliance;
- capability negotiation;
- error envelope;
- all stable error codes;
- pagination;
- bounded server limits;
- restart recovery;
- backup/restore behavior;
- full longitudinal scenario;
- cross-feature regression;
- concurrency hardening;
- Forget hardening;
- observability;
- API documentation;
- Wiki/Core Concepts update;
- operations/development docs when affected;
- full contract advertisement.

Somente ao final deste Slice:

```json
{
  "contracts": {
    "contextual_project_memory": "1"
  }
}
```

poderá ser anunciado.

---

## Out of Scope

Qualquer feature DEFERRED definida no baseline.

---

## Dependencies

- SM-CTX-001 through SM-CTX-007.

---

## Implementation Plan

1. Audit public API against Contract v1.
2. Audit schemas and enums.
3. Audit HTTP/error mappings.
4. Audit idempotency.
5. Audit pagination.
6. Audit server-side bounds.
7. Run race tests.
8. Run Forget propagation matrix.
9. Run longitudinal benchmark scenario.
10. Run restart tests.
11. Run backup/restore smoke when appropriate.
12. Run existing regression suite.
13. Verify zero mandatory Neo4j dependency for contextual domain.
14. Update `/api/v1/info`.
15. Update documentation.
16. Perform Human Validation end-to-end.

---

## Acceptance Criteria

### AC-008.1

Todos os endpoints do Contract v1 existem.

### AC-008.2

Nenhum endpoint proibido foi introduzido.

### AC-008.3

Stable error codes correspondem ao Contract.

### AC-008.4

Todas as mutations exigem Idempotency-Key.

### AC-008.5

Pagination é bounded.

### AC-008.6

Contextual Recall é bounded server-side.

### AC-008.7

Contextual data sobrevive a restart.

### AC-008.8

Current checkpoint é recuperado corretamente após restart.

### AC-008.9

Backup/restore PostgreSQL preserva domínio contextual.

### AC-008.10

Neo4j indisponível não impede Episode/Checkpoint writes.

### AC-008.11

Contextual writes não criam graph projection.

### AC-008.12

Cross-session continuity funciona.

### AC-008.13

Current vs historical distinction funciona.

### AC-008.14

Causal recall funciona.

### AC-008.15

Forget propagation funciona em cadeia.

### AC-008.16

Existing v0.7 Cognitive Memory consumers continuam funcionando.

### AC-008.17

Existing Sessions continuam funcionando.

### AC-008.18

Existing Knowledge Recall continua funcionando.

### AC-008.19

No BLOCKER permanece.

### AC-008.20

Somente após todos os critérios anteriores a aplicação anuncia:

```text
contextual_project_memory = "1"
```

---

## Required Tests

### Full regression

- unit;
- integration;
- existing Cognitive Memory;
- Sessions;
- Knowledge Memory;
- Forget;
- migration bootstrap.

### New concurrency matrix

- initial checkpoint race;
- supersession race;
- idempotency race;
- Evidence creation race.

### New Forget matrix

- MemoryItem → Episode;
- SessionEntry → Episode validates Evidence and archive availability only; no public SessionEntry Forget hook exists;
- Episode → Episode;
- Episode → ContextStatement;
- Knowledge → Episode;
- all_required;
- any_sufficient;
- partial checkpoint;
- current_state invalidation;
- transitive chain.

### Longitudinal scenario

```text
Session A
    ↓
MemoryItem
Episode
Checkpoint A
    ↓
restart
    ↓
Session B
    ↓
resume
explain
history
    ↓
Checkpoint A -> B
    ↓
stale conflict
    ↓
Forget Evidence
    ↓
validate propagation
```

---

## Human Validation

Executar o cenário completo usando API real.

### A — Project setup

```text
scope       = project:contextual-memory-validation
context_key = feature:v1
```

### B — Session A

Persistir:

- temporal history;
- current semantic decision;
- Episode da decisão;
- ContextCheckpoint.

### C — Restart

Reiniciar aplicação.

### D — Session B

Validar:

```text
resume
```

sem reenviar Session A.

### E — Explain

Validar justificativa histórica.

### F — History

Validar decisões anteriores.

### G — Concurrency

Executar stale checkpoint attempt.

### H — Forget

Esquecer Evidence e confirmar ausência de resurrection.

### I — Compatibility

Executar smoke de consumidor Cognitive Memory v0.7 sem uso da nova feature.

---

## Completion Evidence

Registrar:

- test commands;
- PASS results;
- migration head;
- smoke payloads;
- race evidence;
- Forget evidence;
- restart evidence;
- compatibility evidence;
- final `/api/v1/info`;
- documentação atualizada.

---

# 14. Slice Dependency Matrix

| Slice | Depends On | Delivers |
|---|---|---|
| SM-CTX-001 | baseline | Durable Episodes |
| SM-CTX-002 | CTX-001 | Durable current continuity |
| SM-CTX-003 | CTX-002 | Safe checkpoint evolution |
| SM-CTX-004 | CTX-001, CTX-002, CTX-003 | Traceable Evidence |
| SM-CTX-005 | CTX-004 | Strong contextual Forget |
| SM-CTX-006 | CTX-005 | Cross-session resume |
| SM-CTX-007 | CTX-006 | Explain/history/knowledge |
| SM-CTX-008 | CTX-001..007 | Public Contract v1 |

---

# 15. Functional Milestones

A implementação produzirá quatro milestones observáveis.

## Milestone A — Historical Memory

Após:

```text
SM-CTX-001
```

o sistema consegue preservar experiências.

---

## Milestone B — Continuity

Após:

```text
SM-CTX-003
```

o sistema consegue preservar e evoluir de forma segura:

```text
where work currently stands
```

---

## Milestone C — Trustworthy Context

Após:

```text
SM-CTX-005
```

experiências e checkpoints possuem:

- Evidence;
- provenance;
- Forget consistente.

---

## Milestone D — Contextual Memory Product

Após:

```text
SM-CTX-008
```

o Consumer consegue:

```text
resume
explain
inspect history
use source-backed context
survive restart
forget safely
```

através do Contract v1 completo.

---

# 16. Explicit Deferred Backlog

Os itens abaixo não devem aparecer incidentalmente durante CTX-001..008.

## Future — Automatic Distillation

```text
Session history
    ↓
candidate Episodes
```

Requer Product/Spec próprios.

---

## Future — Automatic Checkpoints

Geração automática baseada em atividade.

Requer política de quando checkpoint deve ser criado.

---

## Future — MemorySpace

Possível agregador lógico para:

```text
project
case
research
topic
workflow
```

---

## Future — Graph Projection

Episode relationships em Neo4j somente após benchmark.

---

## Future — Rank Fusion

Cross-domain RRF ou equivalente somente após medição. Isso não impede RRF existente dentro da coleção Knowledge no handoff `15`.

---

## Future — Contradiction Detection

Detectar possíveis divergências sem resolvê-las automaticamente.

---

## Future — Consolidation

Compactação/síntese contextual supervisionada ou automática.

---

## Future — Cross-Project Context

Explicitamente fora do Contract v1.

---

# 17. Implementation Guardrails

Durante todos os Slices:

### MUST NOT

- adicionar `memory_type=episodic`;
- transformar SessionEntry em Episode automaticamente;
- tratar Project Scope como ACL;
- projetar Contextual Memory no Neo4j sem novo ADR;
- introduzir Redis/message broker para a feature;
- adicionar Agent Runtime;
- adicionar task execution;
- fazer LLM decidir Forget;
- fazer LLM decidir Evidence sufficiency;
- habilitar automatic distillation;
- reativar checkpoint histórico automaticamente;
- mudar contratos existentes silenciosamente.

### MUST

- manter PostgreSQL authoritative;
- preservar idempotency;
- preservar provenance;
- manter writes consistentes;
- manter recall bounded;
- manter current/historical distinction;
- manter Consumers antigos compatíveis.

---

# 18. Backlog Completion Definition

O backlog **Contextual Project Memory v1** estará concluído somente quando:

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
SM-CTX-006 DONE
SM-CTX-007 DONE
SM-CTX-008 DONE
```

e o cenário longitudinal provar:

```text
persist
    ↓
restart
    ↓
resume
    ↓
explain
    ↓
history
    ↓
supersede
    ↓
concurrency conflict
    ↓
Forget
    ↓
no resurrection
```

Somente nesse estado:

```text
contextual_project_memory = "1"
```

deverá ser considerado implementado.
