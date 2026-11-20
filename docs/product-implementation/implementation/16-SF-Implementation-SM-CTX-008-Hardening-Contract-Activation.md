# Sofias Memory — Implementation: SM-CTX-008 — Contract Hardening and Longitudinal Validation

**Document:** `implementation/16-SF-Implementation-SM-CTX-008-Hardening-Contract-Activation.md`\
**Status:** APPOVED\
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-008 — Contract Hardening and Longitudinal Validation`

**Approved baseline:**
- `../01-SF-Discovery-Memoria-Contextual-Por-Projeto.md`
- `../02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md`
- `../03-SF-PRD-Memoria-Contextual-Por-Projeto.md`
- `../04-SF-Spec-Memoria-Contextual-Por-Projeto.md`
- `../05-SF-Contract-Memoria-Contextual-Por-Projeto.md`
- `../06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`
- `../07-SF-ADRs-Memoria-Contextual-Por-Projeto.md`
- `../08-SF-Technical-Backlog-Memoria-Contextual-Por-Projeto.md`
- `09-SF-Implementation-SM-CTX-001-First-Class-Episodes.md`
- `10-SF-Implementation-SM-CTX-002-Context-Streams-Initial-Checkpoints.md`
- `11-SF-Implementation-SM-CTX-003-Checkpoint-Revision-Concurrency.md`
- `12-SF-Implementation-SM-CTX-004-First-Class-Evidence.md`
- `13-SF-Implementation-SM-CTX-005-Provenance-Aware-Forget.md`
- `14-SF-Implementation-SM-CTX-006-Contextual-Recall-Core.md`
- `15-SF-Implementation-SM-CTX-007-Explain-History-Knowledge.md`

---

# 1. Objective

Implementar o Slice final:

```text
SM-CTX-008 — Contract Hardening and Longitudinal Validation
```

Este Slice não existe para adicionar uma oitava feature.

Ele existe para provar que:

```text
SM-CTX-001
    ↓
SM-CTX-002
    ↓
SM-CTX-003
    ↓
SM-CTX-004
    ↓
SM-CTX-005
    ↓
SM-CTX-006
    ↓
SM-CTX-007
```

formam um único Contract coerente, durável e negociável por Consumers reais.

Somente depois dessa validação o Sofias Memory poderá anunciar publicamente:

```text
Contextual Project Memory Contract v1
```

como suportado.

---

# 2. Core Exit Guarantee

Ao final do Slice deve ser comprovado, em um único cenário longitudinal real:

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
stale conflict
    ↓
Forget
    ↓
restart
    ↓
no resurrection
```

Isso é o principal Acceptance Gate do Slice.

---

# 3. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-008
```

Não adicionar features futuras para “melhorar” o Contract durante hardening.

---

# 4. Required Entry State

Antes da execução:

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
SM-CTX-006 DONE
SM-CTX-007 DONE
```

Todos os BLOCKERs anteriores devem estar resolvidos.

Human Validation de cada Slice deve estar concluída quando exigida.

---

# 5. What This Slice Is

Este Slice é:

```text
contract hardening
integration hardening
compatibility validation
privacy validation
migration validation
longitudinal validation
documentation alignment
capability activation
```

---

# 6. What This Slice Is Not

Não implementar:

```text
automatic distillation
automatic checkpoint creation
automatic Evidence inference
MemorySpace
Contextual Neo4j projection
RRF across memory domains
contradiction resolution
consolidation
memory decay
importance scoring
cross-project recall
cross-project Evidence
multi-user permissions
RBAC
Agent Runtime
Task Runtime
scheduler
background contextual reconciliation
new contextual resource types
```

Tudo isso permanece DEFERRED.

---

# 7. No Scope Creep During Hardening

Finding encontrado durante CTX-008 deve ser classificado como:

```text
BLOCKER
IMPORTANT
MINOR
DEFERRED
```

Não usar “hardening” como justificativa para redesign especulativo.

---

# 8. Contract Activation Gate

Até este Slice passar:

```text
GET /api/v1/info
```

MUST NOT anunciar Contextual Project Memory Contract v1.

Depois de todos os gates passarem:

```text
contracts["contextual_project_memory"] = "1"
```

passa a ser anunciado.

---

# 9. API Contract Version

O application-level:

```text
api_contract_version = "1"
```

permanece:

```text
"1"
```

Contextual Project Memory é uma feature Contract adicional dentro do mesmo API Contract major.

Não alterar para:

```text
"2"
```

apenas porque uma nova feature foi adicionada aditivamente.

---

# 10. Existing Cognitive Contract

O `/info` atual anuncia:

```text
contracts["cognitive_memory"] = "1"
```

Isso permanece intacto.

Depois da ativação:

```json
{
  "api_contract_version": "1",
  "contracts": {
    "cognitive_memory": "1",
    "contextual_project_memory": "1"
  }
}
```

A ordem serializada deve permanecer determinística.

---

# 11. Contextual Project Memory Capability Names

O Contract APPROVED `05` §6.1 define os nomes públicos das capabilities v1. Este handoff implementa e verifica esses nomes; não os define.

Adicionar exatamente:

```text
contextual_project_memory.episodes
contextual_project_memory.checkpoints
contextual_project_memory.recall
contextual_project_memory.explain
contextual_project_memory.history
```

---

# 12. Why These Capabilities

Elas representam superfícies utilizáveis diretamente pelo Consumer.

```text
episodes
    = Episode management Contract disponível

checkpoints
    = ContextCheckpoint management Contract disponível

recall
    = resume/context contextual recall disponível

explain
    = explain intent disponível

history
    = history intent disponível
```

---

# 13. Evidence Is Not a Separate Public Capability

Não anunciar:

```text
contextual_project_memory.evidence
```

porque Evidence:

- não possui management API independente;
- é parte das guarantees de Episodes/Checkpoints/Recall.

---

# 14. Forget Propagation Is Not a Separate Capability

Não anunciar:

```text
contextual_project_memory.forget_propagation
```

porque propagation é Contract invariant.

Consumer não invoca uma operação independente de propagation.

---

# 15. Capability Order

Preservar deterministicamente primeiro as capabilities Cognitive já existentes:

```text
cognitive_memory.write
cognitive_memory.get
cognitive_memory.recall
cognitive_memory.supersede
cognitive_memory.forget
```

depois:

```text
contextual_project_memory.episodes
contextual_project_memory.checkpoints
contextual_project_memory.recall
contextual_project_memory.explain
contextual_project_memory.history
```

Não depender de unordered set.

---

# 16. Consumer Negotiation Rule

Consumer compatível deve validar:

```text
api_contract_version == "1"

contracts["contextual_project_memory"] == "1"
```

e, quando precisar de uma capability específica:

```text
required capability in capabilities
```

---

# 17. No Version-Based Inference

Consumers MUST NOT inferir:

```text
application version >= X
    => contextual project memory supported
```

Capability negotiation é explícita.

---

# 18. Contract Removal Semantics

Depois de anunciado:

```text
contextual_project_memory = "1"
```

não remover ou alterar silenciosamente comportamento compatível dentro do major `1`.

Breaking change exige novo Contract major/decisão explícita.

---

# 19. Full Public Route Matrix

Ao ativar Contract v1, estas rotas devem existir.

## Episodes

```http
POST /api/v1/episodes
GET  /api/v1/episodes
GET  /api/v1/episodes/{episode_id}

POST /api/v1/episodes/{episode_id}/invalidate
POST /api/v1/episodes/{episode_id}/forget
```

---

# 20. Checkpoints

```http
POST /api/v1/context/checkpoints

GET  /api/v1/context/checkpoints
GET  /api/v1/context/checkpoints/current
GET  /api/v1/context/checkpoints/{checkpoint_id}

POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
POST /api/v1/context/checkpoints/{checkpoint_id}/invalidate
POST /api/v1/context/checkpoints/{checkpoint_id}/forget
```

---

# 21. Contextual Recall

```http
POST /api/v1/context/recall
```

Intents:

```text
resume
context
explain
history
```

---

# 22. Forbidden Contextual Methods

MUST NOT existir:

```http
PATCH /api/v1/episodes/{id}
PUT   /api/v1/episodes/{id}
DELETE /api/v1/episodes/{id}

PATCH /api/v1/context/checkpoints/{id}
PUT   /api/v1/context/checkpoints/{id}
DELETE /api/v1/context/checkpoints/{id}
```

---

# 23. Forbidden Evidence API

MUST NOT existir:

```http
POST   /api/v1/evidence
GET    /api/v1/evidence
PATCH  /api/v1/evidence/*
DELETE /api/v1/evidence/*
```

---

# 24. Forbidden Runtime-Shaped APIs

MUST NOT aparecer por consequência da feature:

```text
/context/run
/context/execute
/context/task
/context/agent
/context/plan
/context/tools
```

ou equivalentes.

---

# 25. Authentication Compatibility

Todas as novas rotas seguem:

```http
X-API-Key
```

como o restante da API protegida.

Não criar auth paralelo.

---

# 26. `/health` Compatibility

Continuam públicos:

```http
GET /health/live
GET /health/ready
```

Não alterar sem relação com a feature.

---

# 27. Success Envelope

Todas as novas respostas seguem:

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid",
    "timestamp": "..."
  }
}
```

conforme infraestrutura existente.

---

# 28. Error Envelope

Errors seguem:

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "Safe message.",
    "details": {},
    "request_id": "uuid"
  }
}
```

Não permitir FastAPI default `422` envelope escapar das novas rotas quando o projeto já converte validações para seu Contract.

---

# 29. Error Code Audit

Auditar todos os códigos contextuais efetivamente implementados.

No mínimo, semânticas equivalentes a:

```text
INVALID_REQUEST
INVALID_SCOPE
INVALID_CONTEXT_KEY
INVALID_EPISODE_KIND

EPISODE_NOT_FOUND
CHECKPOINT_NOT_FOUND
CURRENT_CHECKPOINT_NOT_FOUND

IDEMPOTENCY_CONFLICT
EPISODE_STATE_CONFLICT
CHECKPOINT_STATE_CONFLICT
CONTEXT_STATE_CONFLICT

EVIDENCE_NOT_FOUND
EVIDENCE_UNAVAILABLE
INVALID_EVIDENCE_CYCLE

DEPENDENCY_UNAVAILABLE
INTERNAL_ERROR
```

O Contract aprovado vence se os nomes normativos divergirem.

---

# 30. Stable Error Semantics

Mesmo erro conceitual não deve receber códigos diferentes entre:

- service;
- route;
- race path;
- replay path.

---

# 31. Idempotency Contract Audit

Todas as mutations contextuais exigem:

```http
Idempotency-Key
```

---

# 32. Episode Mutation Audit

Obrigatório para:

```text
episode create
episode invalidate
episode forget
```

---

# 33. Checkpoint Mutation Audit

Obrigatório para:

```text
checkpoint create
checkpoint supersede
checkpoint invalidate
checkpoint forget
```

---

# 34. Same-Key / Same-Request

Deve retornar:

```text
same logical outcome
```

sem nova mutation.

---

# 35. Same-Key / Different-Request

Deve retornar:

```text
IDEMPOTENCY_CONFLICT
```

---

# 36. Replay Precedence Audit

Replay válido deve vencer state conflict produzido pela própria operação original.

Validar especialmente:

```text
Episode Forget replay
Checkpoint Create replay
Checkpoint Supersede replay
Checkpoint Invalidate replay
Checkpoint Forget replay
```

---

# 37. Resource-Generic Ledger Audit

Confirmar que:

```text
contextual_memory_idempotency
```

continua genérico.

Não deve ter sido remodelado implicitamente em:

```text
Episode-only
Checkpoint-only
```

---

# 38. HMAC Audit

Confirmar:

```text
HMAC-SHA-256
domain separation
no raw semantic request persistence
```

---

# 39. Secret Audit

Confirmar:

```text
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

reutilizada conforme aprovado, com domain contextual separado.

Não aparece em:

- logs;
- `/info`;
- exceptions;
- OpenAPI examples.

---

# 40. Episode Lifecycle Audit

State machine final:

```text
ACTIVE
  ├── INVALIDATED
  └── FORGOTTEN

INVALIDATED
  └── FORGOTTEN

FORGOTTEN
  └── terminal
```

Nenhum restore/unforget.

---

# 41. Checkpoint Lifecycle Audit

Final semantics:

```text
ACTIVE
    ├── SUPERSEDED
    ├── INVALIDATED
    └── FORGOTTEN

SUPERSEDED
    ├── INVALIDATED
    └── FORGOTTEN

INVALIDATED
    └── FORGOTTEN

FORGOTTEN
    └── terminal
```

---

# 42. Single Current Audit

Para cada:

```text
(scope, context_key)
```

existe:

```text
0 or 1 ACTIVE
```

DB-authoritative.

---

# 43. Revision Audit

Por Context Stream:

```text
revision unique
revision monotonic
revision never reused
```

---

# 44. Recovery Create Audit

Depois de:

```text
ACTIVE -> INVALIDATED
```

ou:

```text
ACTIVE -> FORGOTTEN
```

novo explicit Create pode estabelecer:

```text
MAX(revision) + 1 ACTIVE
```

sem historical reactivation.

---

# 45. Supersession Audit

Successful:

```text
A ACTIVE
    ↓
A SUPERSEDED
B ACTIVE
```

atomicamente.

---

# 46. Stale Writer Audit

Duas writers sobre A:

```text
one winner
one CONTEXT_STATE_CONFLICT
```

Nunca last-write-wins silencioso.

---

# 47. No Blind Retry Audit

Loser stale não deve automaticamente supersede o novo current.

---

# 48. Evidence Contract Audit

Source types exatamente:

```text
session_entry
memory_item
episode
source
document
chunk
```

---

# 49. No Extra Evidence Sources

Não permitir publicamente:

```text
dataset
checkpoint
context_statement
query
relation
entity
skill
agent
```

no Contract v1.

---

# 50. Provenance vs Evidence Audit

Confirmar estruturalmente:

```text
Provenance != Evidence
```

Nenhum backfill implícito.

---

# 51. Pre-Evidence Resource Compatibility

Episodes/Checkpoints com:

```text
support = null
```

continuam válidos.

---

# 52. Support Modes Audit

Exatamente:

```text
all_required
any_sufficient
```

---

# 53. No LLM Support Decision

Zero LLM call para Evidence/support.

---

# 54. Evidence Availability Audit

Resolver authoritative state.

Não confiar em stale cache.

---

# 55. Session Archive Audit

Session archive:

```text
!= Forget
```

SessionEntry continua Evidence disponível.

---

# 56. MemoryItem Historical Evidence Audit

```text
ACTIVE -> AVAILABLE
SUPERSEDED -> AVAILABLE
FORGOTTEN -> FORGOTTEN
```

---

# 57. Episode Evidence Audit

```text
ACTIVE -> AVAILABLE
INVALIDATED -> UNAVAILABLE
FORGOTTEN -> FORGOTTEN
```

---

# 58. Knowledge Evidence Audit

Source/Document/Chunk availability respeita:

- Source lifecycle;
- current generation;
- `is_active`;
- Forget semantics.

---

# 59. Forget Propagation Audit

Successful Forget garante:

```text
unsupported derived contextual semantic content
    = not recoverable
```

---

# 60. `all_required` Forget Audit

Loss de uma required Evidence:

```text
derived owner unsupported
```

---

# 61. `any_sufficient` Forget Audit

Enquanto alguma Evidence estiver AVAILABLE:

```text
owner preserved
```

---

# 62. Episode Destructive Propagation

Unsupported derived Episode:

```text
-> FORGOTTEN tombstone
```

Não apenas INVALIDATED com payload preservado.

---

# 63. Statement Propagation

Unsupported ContextStatement:

```text
removed/scrubbed
```

---

# 64. Current State Propagation

Loss de:

```text
current_state
```

invalida checkpoint.

---

# 65. No Fallback Audit

Depois de current invalidado/forgotten:

```text
no previous historical revision becomes ACTIVE
```

---

# 66. Transitive Forget Audit

Exemplo:

```text
Source
  ↓
Episode A
  ↓
Episode B
  ↓
current_state
```

Forget Source converge transitivamente.

---

# 67. Non-Resurrection Audit

Depois de Forget e restart:

```text
no semantic resurrection
```

---

# 68. Re-cognify Non-Resurrection

Após:

```text
memory_only Forget
    ↓
re-cognify
```

contexto já scrubbed não reaparece.

---

# 69. Cognitive Forget Integration Audit

Precise Cognitive Memory Forget + contextual propagation:

```text
same PostgreSQL atomic boundary
```

---

# 70. Episode Forget Integration Audit

Direct Episode Forget + propagation:

```text
same PostgreSQL atomic boundary
```

---

# 71. Knowledge Forget Audit

Contextual propagation ocorre em:

```text
authoritative PostgreSQL mutation
```

antes de external convergence/finalization.

---

# 72. Knowledge Success Guarantee

```text
PipelineRun SUCCEEDED
    =>
contextual propagation complete
```

---

# 73. `202 Accepted` Semantics Audit

`wait=false`:

```text
202
```

não significa completion.

Documentação deve deixar isso explícito.

---

# 74. Contextual Recall Intent Audit

Ao ativar Contract:

```text
resume
context
explain
history
```

todos devem estar funcionais.

---

# 75. `resume` Audit

Prioriza:

```text
current checkpoint
current project memories
relevant Episodes
```

---

# 76. `context` Audit

Prioriza relevância semântica dentro do project boundary.

---

# 77. `explain` Audit

Usa:

```text
Evidence
```

como causal relation.

Nunca checkpoint lineage.

---

# 78. `history` Audit

Usa:

```text
historical checkpoints
Episodes
```

como chronology.

---

# 79. Lineage Is Not Causality

Contract-level invariant:

```text
superseded_by
revision order
    !=
Evidence
```

Adicionar regression test explícito.

---

# 80. Semantic Relevance Is Not Causality

Knowledge hit semanticamente semelhante:

```text
!= Evidence
```

---

# 81. Global Cognitive Isolation

Automatic contextual bundle nunca inclui:

```text
global Cognitive Memory
```

---

# 82. Project-Wide Episode Semantics

Episode:

```text
scope = requested project
context_key = NULL
```

é elegível para qualquer Context Stream daquele projeto.

---

# 83. Other Context Stream Isolation

Context-specific Episode de outro stream:

```text
excluded
```

---

# 84. Project Isolation

Contextual roots/current project memories de outro project nunca são incluídos. Evidence explicitamente referenciada segue CTX-004: global MemoryItem pode ser leaf, sem inclusão automática em current_memories. SessionEntry/Knowledge não possuem project ownership inferido.

---

# 85. Knowledge Dataset Explicitness

Knowledge retrieval só ocorre quando:

```text
datasets explicitly provided
```

---

# 86. No `main` Default

No Contextual Recall:

```text
datasets omitted
    !=
["main"]
```

---

# 87. Project Scope Is Not Dataset

Nunca inferir mapping.

---

# 88. Knowledge Retrieval Semantics

Pode usar:

```text
vector
lexical
RRF inside Knowledge collection
```

---

# 89. No Cross-Domain Ranking

Não fundir scores de:

```text
Memory
Episode
Knowledge
Checkpoint
```

em um único score global.

---

# 90. Single Embedding Audit

Request com semantic retrieval em múltiplos domínios:

```text
embedding provider calls <= 1
```

---

# 91. Required Query and Internal Fallback Audit

Confirmar query obrigatória em todos os intents HTTP: ausência/null retorna 422 INVALID_REQUEST. Seleção determinística, chronology e Evidence traversal podem operar internamente sem embedding. Com provider failure, não chamar novamente o provider para fallback seguro; usar PARTIAL_CONTEXT/degraded_sources.

---

# 92. Embedding Degradation

Provider unavailable:

```text
PARTIAL_CONTEXT with affected degraded_sources
```

quando deterministic fallback continua possível.

---

# 93. Knowledge Lexical Fallback

Query + explicit datasets + embedding failure:

```text
lexical-only Knowledge retrieval allowed
```

com warning.

---

# 94. PostgreSQL Failure

Database failure:

```text
request fails
```

Não fabricar empty bundle.

---

# 95. Neo4j Independence

Toda Contextual Project Memory v1 deve permanecer funcional sem Neo4j para:

```text
Episodes
Checkpoints
Evidence
Contextual Recall
Forget propagation
```

Knowledge base ainda pode possuir seu próprio Neo4j projection em outros endpoints, mas contextual behavior não depende dela.

---

# 96. Zero Contextual `graph_outbox`

Pure contextual writes:

```text
0 contextual graph_outbox events
```

Knowledge operations continuam gerando seus próprios eventos normais.

---

# 97. Zero Contextual PipelineRuns

Episodes/Checkpoints/Recall não criam PipelineRun.

Forget propagation não cria derived PipelineRun.

---

# 98. Recall Read-Only Audit

Contextual Recall cria zero:

```text
Query
Session
SessionEntry
MemoryItem
Episode
Checkpoint
Evidence link
PipelineRun
graph_outbox
```

---

# 99. No LLM Audit

Contextual Recall v1:

```text
0 chat completion calls
```

em todos os intents.

---

# 100. No RAG Answer

Bundle não contém generated answer.

---

# 101. Bounded Recall Audit

Todas as collections possuem bounds.

---

# 102. Deterministic Ordering Audit

Ties usam stable ordering.

Mesmo state + mesmo embedding:

```text
same identities
same ordering
```

---

# 103. Warning Determinism

Warnings também em ordem estável.

---

# 104. Public Degradation Semantics

O Contract público usa degraded_sources e warnings do `05` §51. Não expor um boolean degraded ou warning conceitual novo. Dependency failure observável difere de empty data, missing checkpoint e healthy truncation.

---

# 105. Missing Current Semantics

```text
continuity_available=false
degraded_sources=[]
```

quando essa for a única condição.

---

# 106. Forget Safety Across Recall

Nenhum dos quatro intents pode ser escape hatch para forgotten content.

Especialmente:

```text
history
explain
```

---

# 107. Migration Chain Validation

Validar a sequência completa produzida por CTX-001..007.

Não assumir números de migration deste documento.

---

# 108. Migration Head

Ao iniciar:

```bash
uv run alembic heads
```

deve retornar exatamente um head.

---

# 109. Fresh Database Upgrade

Em banco vazio:

```bash
uv run alembic upgrade head
```

deve produzir schema completo.

---

# 110. Upgrade From Pre-Contextual Baseline

Validar upgrade real a partir do último head imediatamente anterior ao CTX-001.

Planejamento original esperava:

```text
0018
```

mas usar o head histórico real produzido pelo repositório.

Caminho:

```text
pre-contextual head
    ↓
all contextual migrations
    ↓
current head
```

deve passar sem data loss incompatível.

---

# 111. Existing Data Compatibility

Seed pre-contextual:

```text
Datasets
Sources
Knowledge
Sessions
Skills
Agents
Cognitive Memory
```

Upgrade.

Confirmar que continuam utilizáveis.

---

# 112. No Contextual Backfill

Upgrade não deve fabricar:

```text
Episodes
Checkpoints
Evidence
```

a partir de dados antigos.

---

# 113. Downgrade Validation

Para migrations contextuais que declaram downgrade seguro:

```bash
upgrade head
downgrade through contextual migration boundary
upgrade head
```

deve funcionar no harness apropriado.

Não exigir destructive downgrade onde migration explicitamente documenta limitação incompatível, mas isso deve ser finding/documented.

---

# 114. Migration Constraint Audit

Confirmar no PostgreSQL real:

```text
single ACTIVE checkpoint
unique revision
scope grammar
context_key grammar
Evidence canonical uniqueness
Evidence link uniqueness
lifecycle constraints
lineage constraints
```

---

# 115. OpenAPI Snapshot Audit

Gerar schema e verificar superfícies.

---

# 116. No Internal Terminology Leak

OpenAPI public descriptions não devem conter:

```text
SM-CTX-001
SM-CTX-008
ADR ticket numbers
implementation handoff language
Codex instructions
```

---

# 117. OpenAPI Examples

Examples devem:

- ser válidos;
- usar Contract terminology;
- não conter secrets;
- não conter dados pessoais reais.

---

# 118. OpenAPI Mutation Headers

Documentar `Idempotency-Key` nas mutations contextuais.

---

# 119. OpenAPI Auth

Documentar `X-API-Key` conforme padrão atual.

---

# 120. OpenAPI Lifecycle Shapes

Forgotten tombstones precisam caber nos mesmos response models lifecycle-safe. Checkpoint preserva scope/context_key/revision e lineage/timestamps aplicáveis; INVALIDATED após propagation aceita current_state null/ausente, enquanto Create/Supersede continuam exigindo-o.

Não depender de schema alternativo secreto.

---

# 121. Compatibility Regression — Knowledge

Executar suites existentes de:

```text
Remember
Cognify
Recall
Feedback
Improve
Forget
Runs
Graph
Provenance
```

relevantes.

---

# 122. Compatibility Regression — Sessions

Executar:

```text
Session management
SessionEntries
Session Context behavior
```

---

# 123. Compatibility Regression — Skills

Executar management/resolve tests relevantes.

---

# 124. Compatibility Regression — Agents

Executar Agent Profile management tests relevantes.

---

# 125. Compatibility Regression — Cognitive Memory

Executar:

```text
Create
Get
Recall
Supersede
Forget
idempotency
temporal truth
```

---

# 126. API Backward Compatibility

Adicionar Contextual Project Memory não deve mudar comportamento público antigo sem decisão APPROVED.

---

# 127. `/info` Additive Compatibility

Antes:

```json
{
  "contracts": {
    "cognitive_memory": "1"
  }
}
```

Depois:

```json
{
  "contracts": {
    "cognitive_memory": "1",
    "contextual_project_memory": "1"
  }
}
```

Nunca remover cognitive.

---

# 128. `/info` Capabilities Additive Compatibility

Existing Cognitive capabilities permanecem exatamente disponíveis.

Contextual capabilities são adicionadas.

---

# 129. ApplicationInfo Description

Atualizar descrição atualmente Cognitive-specific de:

```text
capabilities
```

para linguagem feature-neutral.

Não deixar documentação dizendo que a lista contém apenas Cognitive Memory quando já conter Contextual Project Memory.

---

# 130. `/info` Security

Ainda não expõe:

- API key;
- HMAC key;
- storage credentials;
- database URLs;
- semantic content.

---

# 131. Documentation Hardening

Atualizar documentação pública necessária.

No mínimo:

```text
README.md
docs/api.md
docs/wiki/API-Guide.md
docs/wiki/Core-Concepts.md
docs/wiki/Integration-Guide.md
CHANGELOG.md
```

somente onde necessário e conforme convenção do repositório.

---

# 132. Documentation Must Match Reality

Não documentar:

- future feature;
- deferred feature;
- automatic behavior inexistente;
- graph contextual;
- LLM contextual answer;
- automatic checkpointing.

---

# 133. Core Concepts Documentation

Explicar claramente:

```text
MemoryItem
Episode
ContextCheckpoint
SessionEntry
Knowledge
Evidence
```

e suas diferenças.

---

# 134. Integration Documentation

Consumer deve entender:

```text
create Episode explicitly
create/supersede Checkpoint explicitly
recall context
handle stale conflict
handle missing current
negotiate Contract via /info
```

---

# 135. Forget Documentation

Explicar:

```text
successful Forget may propagate through explicit Evidence dependencies
```

sem prometer blind cascade.

---

# 136. `memory_only` Documentation

Explicar que:

- Source pode ser preservada para reprocessamento;
- contextual derived content que perdeu required support pode ser scrubbed;
- re-cognify não ressuscita automaticamente esse contexto.

---

# 137. Privacy Documentation

Contextual Recall retorna:

```text
context
```

não authority.

---

# 138. Consumer Security Documentation

Consumer continua responsável por:

```text
authorization
planning
execution
tool permission
```

---

# 139. Longitudinal Validation — Purpose

Criar uma integração/smoke real que atravesse os principais conceitos sem mocks de domínio.

Mocks de embedding podem ser usados apenas para tornar ranking determinístico quando apropriado.

PostgreSQL deve ser real.

---

# 140. Longitudinal Scenario — Setup

Todas as chamadas públicas de Recall neste cenário incluem `query` válida e
obrigatória conforme Contract `05` §39, inclusive exemplos reduzidos a `intent`.

Criar project:

```text
project:ctx-longitudinal
```

e stream:

```text
main
```

Criar Dataset explícito:

```text
ctx-docs
```

com Knowledge source processada.

---

# 141. Step A — Cognitive Truth

Criar:

```text
MemoryItem M1
scope=project:ctx-longitudinal
type=semantic
```

Exemplo conceitual:

```text
PostgreSQL is authoritative.
```

---

# 142. Step B — Episode

Criar:

```text
Episode E1
kind=decision
```

sustentado explicitamente por M1 ou Knowledge Evidence apropriada.

---

# 143. Step C — Initial Checkpoint

Criar:

```text
Checkpoint C1
revision=1
ACTIVE
```

com:

```text
objective
current_state
approved_decision
pending_work
next_action
```

e Evidence statement-level onde apropriado.

---

# 144. Step D — Restart #1

Encerrar application boundary.

Subir nova instance/service.

Não reutilizar ORM session antiga.

---

# 145. Step E — Resume

Executar:

```text
intent=resume
```

Esperado:

```text
C1 current
M1 current memory
E1 relevant Episode
continuity_available=true
```

---

# 146. Step F — Explain

Executar:

```text
intent=explain
include_evidence=true
```

Confirmar causal trace declarada.

Não aceitar chronology inventada.

---

# 147. Step G — History

Executar:

```text
intent=history
```

Inicialmente:

```text
no historical checkpoint
E1 historical event
C1 current
```

---

# 148. Step H — Supersede

Consumer A lê C1.

Consumer B também lê C1.

Consumer A executa:

```text
C1 -> C2
```

Esperado:

```text
C1 SUPERSEDED
C2 ACTIVE
revision=2
```

---

# 149. Step I — Stale Conflict

Consumer B tenta:

```text
C1 -> C3
```

com nova Idempotency-Key.

Esperado:

```text
409 CONTEXT_STATE_CONFLICT
```

C3 não existe.

C2 continua ACTIVE.

---

# 150. Step J — Replay

Repetir operation de A com mesma key/payload.

Esperado:

```text
same C2
```

Nenhuma revision 3.

---

# 151. Step K — History After Supersede

`intent=history`.

Esperado:

```text
current_checkpoint=C2
historical_checkpoints contains C1
```

---

# 152. Step L — Knowledge Composition

Executar contextual recall com:

```text
datasets=["ctx-docs"]
query=...
```

Confirmar Knowledge refs.

---

# 153. Step M — Forget Root Evidence

Esquecer um resource authoritative que sustenta E1/C2 current_state de forma que support se torne insuficiente.

---

# 154. Step N — Propagation

Após successful Forget:

```text
unsupported Episode -> FORGOTTEN
unsupported current_state removed
C2 -> INVALIDATED
```

quando esse for o support graph do cenário.

---

# 155. Step O — Resume After Forget

Executar:

```text
intent=resume
```

Esperado:

```text
continuity_available=false
current_checkpoint=null
```

Nenhum fallback para C1.

---

# 156. Step P — Explain After Forget

Não reconstruir forgotten semantic content através da Evidence chain.

---

# 157. Step Q — History After Forget

Não expor conteúdo esquecido.

C1 histórico pode continuar conforme seu próprio support/lifecycle.

C2 invalidado pode aparecer apenas com statements que sobreviveram e nunca com forgotten `current_state`.

---

# 158. Step R — Explicit Recovery

Criar novo checkpoint:

```text
C3
revision=3
ACTIVE
```

via:

```http
POST /context/checkpoints
```

porque não existe ACTIVE.

---

# 159. No Lineage on Recovery Create

Esperado:

```text
C2.superseded_by
```

não é automaticamente C3.

C3 é novo current explícito, não Supersede de C2 invalidado.

---

# 160. Step S — Restart #2

Reiniciar novamente.

---

# 161. Step T — Final Resume

Esperado:

```text
C3 current
revision=3
forgotten content absent
no historical fallback
no resurrection
```

---

# 162. Step U — Final Explain

Confirmar apenas Evidence ainda existente e válida.

---

# 163. Step V — Final History

Confirmar:

- chronology correta;
- lifecycles corretos;
- forgotten content ausente;
- revision sequence preservada.

---

# 164. Longitudinal Scenario Pass Condition

O cenário somente passa se a sequência completa ocorrer sem:

```text
manual database repair
manual graph repair
manual context reconstruction
application-specific workaround
```

---

# 165. Restart Is Mandatory

Não substituir restart por:

```text
new SQLAlchemy Session only
```

A validação deve atravessar boundary equivalente a nova application instance.

---

# 166. Longitudinal Scenario Must Use Public API

Sempre que houver public endpoint, usar a API real.

Internal service calls podem ser usados apenas para setup técnico não exposto publicamente.

---

# 167. No White-Box Success Shortcut

Não considerar scenario PASS apenas inspecionando rows.

Rows podem complementar assertions.

Valor principal deve ser provado pelas superfícies reais do Consumer.

---

# 168. Contract Negotiation Validation

Antes de ativar no código final do Slice, testes devem comprovar todas as features.

Depois:

```text
/info
```

anuncia Contract.

---

# 169. Activation Ordering

Implementação recomendada:

```text
1. harden everything
2. longitudinal test passes
3. compatibility suite passes
4. update documentation
5. add /info contract/capabilities
6. contract activation tests
```

Não começar o Slice alterando `/info`.

---

# 170. Why Activation Last

Enquanto:

```text
contracts["contextual_project_memory"] missing
```

Consumers corretos não assumem suporte completo.

Essa é nossa safety valve durante implementação incremental.

---

# 171. Contract Activation Unit Test

Depois da ativação:

```text
contracts == {
    "cognitive_memory": "1",
    "contextual_project_memory": "1",
}
```

considerando qualquer outro Contract já legitimamente existente no repositório no momento da execução.

Não apagar contratos adicionados por trabalho concorrente legítimo.

---

# 172. Capability Test

A lista deve conter exatamente as cinco Contextual capabilities congeladas em `05` §6.1, sem Evidence/Forget independentes e sem anúncio antes do gate.

---

# 173. Capability/Route Coherence

Cada advertised capability deve corresponder a comportamento real.

Por exemplo:

```text
contextual_project_memory.explain
```

só pode existir porque:

```text
intent=explain
```

está funcional.

---

# 174. No Phantom Capability

Testar que não existem capabilities de features DEFERRED.

---

# 175. Capability Naming Stability

As strings já pertencem ao public Contract APPROVED `05` §6.1. Este Slice somente ativa seu anúncio após validação completa.

Não renomear por estética.

---

# 176. Legacy API Regression

Executar OpenAPI route-set regression para garantir que novas routes são aditivas.

---

# 177. Existing Forbidden Prefix Regression

Continuam proibidos:

```text
/auth
/users
/permissions
/api-keys
/settings
/configuration
/sync
/cloud
/serve
/push
```

salvo mudança legítima externa ao projeto atual que precise ser reconciliada conscientemente.

---

# 178. Security Test — API Key

Todas as contextual routes rejeitam:

```text
missing key
invalid key
```

segundo padrão existente.

---

# 179. Security Test — Logs

Executar operations com sentinel semantic content.

Capturar logs.

Assert que sentinel não aparece onde privacy policy proíbe.

---

# 180. Security Test — Forgotten Content

Usar sentinel único, por exemplo conceitualmente:

```text
CTX_FORGET_SENTINEL_<uuid>
```

Forget.

Procurar em responses recuperáveis normais.

Sentinel não pode reaparecer.

---

# 181. Database Privacy Inspection

Após Forget, verificar tabelas contextuais que poderiam guardar semantic copies:

```text
episodes
episode_provenance
context_checkpoint_statements
context checkpoint provenance
contextual idempotency
Evidence links
```

Sentinel não deve permanecer onde Contract exige scrub.

---

# 182. Idempotency Privacy Inspection

Request digest não deve permitir raw semantic text.

Não persistir raw payload.

---

# 183. Evidence Privacy Inspection

Canonical Evidence refs armazenam identidade, não copied source content.

---

# 184. Tombstone Privacy Inspection

Episode/Checkpoint forgotten response não retorna semantic payload.

---

# 185. OpenAPI Privacy

Examples não contêm sentinel/secrets reais.

---

# 186. Performance Sanity — Recall

Não é benchmark de otimização prematura.

Mas validar que bounded recall não produz:

```text
obvious N+1 explosion
unbounded Evidence traversal
full-table load
```

---

# 187. Performance Sanity — Evidence

Batch lookup onde material.

---

# 188. Performance Sanity — History

Historical limits devem ser aplicados no banco quando possível, não apenas depois de carregar tudo.

---

# 189. Performance Sanity — Knowledge

Top-k/bounds aplicados antes da resposta final.

---

# 190. Performance Sanity — Forget Traversal

Iterative bounded-by-DAG traversal.

Não recursive Python stack ilimitada.

---

# 191. No ANN Introduction

Exact pgvector continua baseline para:

```text
Cognitive Memory
Episode
```

Não adicionar HNSW/IVFFlat neste Slice.

---

# 192. No Cross-Type RRF

Continua proibido.

---

# 193. No Contextual Neo4j Projection

Não adicionar apenas para “completar arquitetura”.

---

# 194. No Automatic Distillation

Não criar:

```text
Session close
    -> checkpoint automatically
```

---

# 195. No Automatic Episode Extraction

Continua explícito/supervised-first.

---

# 196. No Contradiction Solver

Não resolver conflicts semânticos.

---

# 197. No Automatic Consolidation

Não juntar Episodes/Memories automaticamente.

---

# 198. No Importance/Centrality

Fora desta feature.

---

# 199. No Contextual Cleanup Scheduler

Não criar background reconciliation regular.

---

# 200. Startup Behavior

Startup não deve:

- inferir Evidence;
- backfill Episodes;
- create checkpoints;
- reconcile Contextual Memory semantic state automaticamente.

Migrations/startup verification normais continuam.

---

# 201. Configuration Audit

Idealmente CTX-001..008 não devem ter criado grande conjunto de settings.

Auditar novos settings.

Cada um deve ter motivo real.

---

# 202. No New Operational Dependency

Final feature não deve depender de:

```text
Redis
RabbitMQ
new database
new worker type
```

---

# 203. PostgreSQL Authority Audit

Provar que todos os contextual resources são recuperáveis com Neo4j indisponível.

---

# 204. Backup Semantics

Contextual state está contido em PostgreSQL.

Nenhum novo backup subsystem necessário.

---

# 205. Storage Semantics

Contextual resources não criam Source original blobs próprios.

---

# 206. Deployment Compatibility

Executar:

```bash
docker compose config
```

quando aplicável ao repositório.

Não alterar deployment sem necessidade.

---

# 207. Fresh Startup

Application inicia com schema head.

---

# 208. Startup With Wrong Schema

Com migration mode relevante, comportamento permanece conforme ADR existente.

Contextual feature não cria bypass.

---

# 209. Full Static Quality Gate

Obrigatório:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
```

---

# 210. Full Test Gate

Obrigatório:

```bash
uv run pytest
```

Não apenas contextual tests.

---

# 211. Contextual PostgreSQL Gate

Executar todas as suites opt-in criadas em:

```text
SM-CTX-001
SM-CTX-002
SM-CTX-003
SM-CTX-004
SM-CTX-005
SM-CTX-006
SM-CTX-007
```

contra banco PostgreSQL dedicado.

---

# 212. Knowledge PostgreSQL Regression

Executar suites Knowledge reais relevantes.

---

# 213. Cognitive PostgreSQL Regression

Executar Cognitive Memory real PostgreSQL suite.

---

# 214. Sessions Regression

Executar suite real relevante.

---

# 215. Migration Gate

Executar em database descartável:

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic heads
```

Esperado:

```text
exactly one head
current == head
```

---

# 216. Migration Roundtrip

Onde seguro:

```bash
uv run alembic downgrade <pre-contextual-boundary-or-tested-step>
uv run alembic upgrade head
```

Usar estratégia compatível com migrations reais.

---

# 217. OpenAPI Gate

Contract test completo.

---

# 218. Longitudinal Gate

O cenário das seções 140–163 é obrigatório.

Não substituir por conjunto de unit tests separados.

---

# 219. Restart Gate

Pelo menos dois restart boundaries no longitudinal scenario.

---

# 220. Forget Non-Resurrection Gate

Obrigatório.

---

# 221. Concurrency Gate

Obrigatório:

```text
checkpoint stale writer
same-key retry
different-key conflict
```

---

# 222. Privacy Gate

Obrigatório sentinel Forget test.

---

# 223. Read-Only Recall Gate

Obrigatório.

---

# 224. Capability Activation Gate

Somente depois de todos os anteriores.

---

# 225. Human Validation — Phase 1

Consultar:

```http
GET /api/v1/info
```

antes da ativação final em development branch/harness quando possível.

Confirmar que nenhuma capability contextual está prematuramente anunciada.

---

# 226. Human Validation — Phase 2

Executar longitudinal scenario completo via API.

---

# 227. Human Validation — Phase 3

Depois da validação e ativação final:

```http
GET /api/v1/info
```

Confirmar:

```text
contracts.contextual_project_memory = "1"
```

---

# 228. Human Validation — Phase 4

Confirmar capabilities:

```text
contextual_project_memory.episodes
contextual_project_memory.checkpoints
contextual_project_memory.recall
contextual_project_memory.explain
contextual_project_memory.history
```

---

# 229. Human Validation — Phase 5

Executar Consumer-like capability negotiation:

```text
read /info
assert API contract
assert feature contract
assert required capabilities
then invoke contextual routes
```

---

# 230. Human Validation — Phase 6

Testar comportamento sem Neo4j disponível quando harness seguro permitir.

Episodes/Checkpoints/Contextual Recall continuam funcionais.

---

# 231. Human Validation — Phase 7

Reexecutar um fluxo Cognitive Memory normal.

Confirmar ausência de regression.

---

# 232. Human Validation — Phase 8

Reexecutar Knowledge Recall normal.

Confirmar ausência de regression.

---

# 233. Human Validation — Phase 9

Reexecutar Session flow normal.

Confirmar ausência de regression.

---

# 234. Human Validation — Phase 10

Inspecionar OpenAPI.

Confirmar:

- routes corretas;
- methods corretos;
- schemas;
- errors;
- headers;
- nenhuma route futura.

---

# 235. Acceptance Criteria — Contract

## AC-008.1

Todos os endpoints do Contextual Project Memory Contract v1 existem.

## AC-008.2

Nenhum endpoint contextual futuro/proibido é exposto.

## AC-008.3

Todos os quatro recall intents estão funcionais.

## AC-008.4

Mutation idempotency está coerente em toda a feature.

## AC-008.5

Error envelopes/codes são estáveis.

---

# 236. Acceptance Criteria — Durability and Lifecycle

## AC-008.6

Episodes sobrevivem restart.

## AC-008.7

Checkpoints/revisions sobrevivem restart.

## AC-008.8

Evidence/support sobrevivem restart.

## AC-008.9

Exactly one ACTIVE checkpoint invariant continua protegido.

## AC-008.10

Stale writer conflict continua correto.

---

# 237. Acceptance Criteria — Forget

## AC-008.11

Successful Forget impede recuperação de unsupported derived semantic content.

## AC-008.12

Forget propagation permanece Evidence-aware.

## AC-008.13

`any_sufficient` preserva conteúdo independentemente suportado.

## AC-008.14

No historical fallback ocorre após current support loss.

## AC-008.15

Restart/re-cognify não ressuscita conteúdo esquecido.

---

# 238. Acceptance Criteria — Recall

## AC-008.16

Resume recupera continuidade current corretamente.

## AC-008.17

Context respeita project/context isolation e support eligibility atual sem mutar lifecycle. ACTIVE com support insuficiente após ordinary invalidation não é contexto confiável; após successful Forget, conteúdo que deveria estar scrubbed nunca é exposto.

## AC-008.18

Explain usa Evidence, não lineage.

## AC-008.19

History mostra chronology sem forgotten content.

## AC-008.20

Global Cognitive Memory não entra automaticamente.

---

# 239. Acceptance Criteria — Knowledge

## AC-008.21

Knowledge só é consultado com datasets explicitamente solicitados.

## AC-008.22

Omitted datasets não defaultam para `main`.

## AC-008.23

Knowledge relevance não vira Evidence.

## AC-008.24

Contextual Knowledge integration não usa Neo4j.

## AC-008.25

Contextual Recall não cria Query audit rows.

---

# 240. Acceptance Criteria — Architecture

## AC-008.26

PostgreSQL continua authority.

## AC-008.27

Pure contextual writes criam zero graph_outbox.

## AC-008.28

Contextual mutations não criam PipelineRun.

## AC-008.29

Contextual Recall não chama LLM.

## AC-008.30

Nenhum novo operational datastore foi introduzido.

---

# 241. Acceptance Criteria — Compatibility

## AC-008.31

Knowledge Memory regressions passam.

## AC-008.32

Sessions regressions passam.

## AC-008.33

Skills regressions passam.

## AC-008.34

Agent Profile regressions passam.

## AC-008.35

Cognitive Memory regressions passam.

---

# 242. Acceptance Criteria — Migration

## AC-008.36

Fresh migration to head passa.

## AC-008.37

Upgrade de pre-contextual baseline passa.

## AC-008.38

Existe exatamente um Alembic head.

## AC-008.39

Nenhum backfill contextual automático ocorre.

---

# 243. Acceptance Criteria — Privacy

## AC-008.40

Forgotten semantic sentinel não reaparece nas superfícies normais.

## AC-008.41

Contextual idempotency ledger não contém raw semantic payload.

## AC-008.42

Evidence refs não duplicam source semantic content.

## AC-008.43

Logs não expõem semantic content proibido/secrets.

---

# 244. Acceptance Criteria — Negotiation

## AC-008.44

`api_contract_version` permanece `"1"`.

## AC-008.45

`contracts["cognitive_memory"] == "1"` permanece.

## AC-008.46

`contracts["contextual_project_memory"] == "1"` é anunciado somente após hardening.

## AC-008.47

As cinco contextual capabilities congeladas são anunciadas.

## AC-008.48

Nenhuma phantom/future contextual capability é anunciada.

---

# 245. Acceptance Criteria — Longitudinal

## AC-008.49

O cenário:

```text
persist
→ restart
→ resume
→ explain
→ history
→ supersede
→ stale conflict
→ Forget
→ explicit recovery
→ restart
→ no resurrection
```

passa integralmente.

## AC-008.50

Nenhum passo do cenário requer reparo manual ou acesso direto ao banco para produzir o resultado funcional esperado.

---

# 246. BLOCKER Definition

Qualquer falha em:

```text
AC-008.1..50
```

relacionada à garantia normativa correspondente pode bloquear Contract activation.

Especialmente:

- privacy;
- Forget;
- concurrency;
- project isolation;
- migration;
- public Contract;
- capability truthfulness.

---

# 247. IMPORTANT Findings

Podem ser corrigidos antes do closeout quando localizados e compatíveis com o Contract.

Exemplo:

- N+1 pequeno;
- warning ordering inconsistente;
- documentação desatualizada;
- falta de index comprovadamente necessária.

---

# 248. MINOR Findings

Não impedem Contract se não afetarem:

- correctness;
- security;
- durability;
- compatibility;
- usability prometida.

---

# 249. DEFERRED Findings

Exemplos:

```text
automatic distillation
checkpoint embeddings
ANN
MemorySpace
contradiction resolution
graph projection
contextual audit history
```

Não implementar neste Slice.

---

# 250. When to Stop and Escalate

Voltar para decisão humana se hardening revelar necessidade de:

- mudar public Contract;
- renomear capability congelada;
- alterar support semantics;
- alterar Forget guarantee;
- alterar context stream semantics;
- alterar lifecycle;
- introduzir new datastore;
- usar Neo4j contextual;
- usar LLM contextual;
- criar background convergence;
- adicionar historical backfill;
- alterar API Contract major.

---

# 251. Expected File Impact

Prováveis alterações:

```text
sofias_memory/api/routes/info.py
tests/unit/test_info.py

contextual services/schemas/routes/repositories
    only for hardening fixes

tests/contract/
tests/integration/
tests/e2e/ or equivalent

README.md
docs/api.md
docs/wiki/*
CHANGELOG.md
```

Preferência:

```text
no new domain table
no new feature resource
```

---

# 252. Migration Impact

Ideal:

```text
no migration
```

Se hardening encontrar constraint/index realmente ausente:

```text
small additive migration
```

é permitido.

Qualquer schema redesign relevante precisa voltar para decisão.

---

# 253. Implementation Order

Ordem recomendada:

```text
1. verify CTX-001..007 closeout
2. inventory final public Contract
3. inventory final database invariants
4. full contextual test pass
5. legacy regression pass
6. migration chain validation
7. OpenAPI contract audit
8. idempotency audit
9. lifecycle/concurrency audit
10. Evidence/support audit
11. Forget/privacy audit
12. Recall/read-only audit
13. Knowledge integration audit
14. longitudinal scenario
15. restart/non-resurrection
16. documentation alignment
17. /info contract activation
18. /info capability tests
19. full final suite
20. Human Validation
```

---

# 254. Activation Must Be Near the End

Não adicionar:

```text
contracts["contextual_project_memory"]
```

no primeiro commit do Slice e depois “esperar os testes”.

O código deve anunciar somente o que já está funcionando.

---

# 255. Implementation Report

O executor deverá retornar:

```text
SM-CTX-008 — Implementation Report

Implemented
- ...

Hardening fixes
- ...

Changed files
- ...

Database
- pre-contextual baseline:
- previous head:
- final head:
- migration added in CTX-008: yes/no
- single Alembic head:
- fresh upgrade:
- pre-contextual upgrade:
- downgrade/roundtrip:

Final Contract inventory
- Episodes:
- Checkpoints:
- Contextual Recall:
- Evidence:
- Forget propagation:
- intents:

Compatibility negotiation
- api_contract_version:
- contracts:
- capabilities:

OpenAPI
- expected routes:
- forbidden routes:
- mutation headers:
- auth:
- error envelopes:

Architecture invariants
- PostgreSQL authority:
- Neo4j independence:
- contextual graph_outbox:
- contextual PipelineRuns:
- contextual LLM calls:
- automatic behavior:

Verified
- ruff:
- format:
- mypy:
- full pytest:
- contextual PostgreSQL:
- Knowledge regression:
- Sessions regression:
- Skills regression:
- Agents regression:
- Cognitive regression:

Idempotency
- Episode:
- Checkpoint Create:
- Checkpoint Supersede:
- Invalidate:
- Forget:
- race/replay:

Evidence
- source types:
- support modes:
- availability:
- cycles:
- provenance isolation:

Forget
- MemoryItem:
- Episode:
- Source:
- Dataset:
- Everything:
- Dataset Delete:
- memory_only:
- re-cognify:
- transitive:
- any_sufficient:
- no resurrection:

Recall
- resume:
- context:
- explain:
- history:
- project isolation:
- global Cognitive exclusion:
- explicit datasets:
- no main fallback:
- single embedding:
- degraded fallback:
- read-only:
- no LLM:
- no Neo4j:
- no Query audit:

Privacy
- sentinel test:
- tombstones:
- idempotency ledger:
- Evidence refs:
- logs:

Longitudinal scenario
- persist: PASS/FAIL
- restart #1: PASS/FAIL
- resume: PASS/FAIL
- explain: PASS/FAIL
- history: PASS/FAIL
- supersede: PASS/FAIL
- stale conflict: PASS/FAIL
- replay: PASS/FAIL
- Forget: PASS/FAIL
- no fallback: PASS/FAIL
- explicit recovery: PASS/FAIL
- restart #2: PASS/FAIL
- no resurrection: PASS/FAIL
- final explain/history: PASS/FAIL

Acceptance Criteria
- AC-008.1 PASS/FAIL
...
- AC-008.50 PASS/FAIL

Findings

BLOCKER
- none / ...

IMPORTANT
- none / ...

MINOR
- none / ...

DEFERRED
- none / ...

Documentation
- README:
- API:
- Wiki:
- CHANGELOG:

Human validation
- READY / NOT READY

Contract activation
- READY / NOT READY
- contextual_project_memory advertised: yes/no

Git
- branch:
- commit:
- working tree:
```

---

# 256. Contract Activation Rule

O executor MUST NOT declarar:

```text
Contract activation = READY
```

se qualquer BLOCKER permanecer.

---

# 257. No Partial Success Claim

Não aceitar relatório como:

```text
49/50 criteria passed, close enough
```

se o critério restante for Contract normative.

---

# 258. Human Validation Is Final Gate

Mesmo com todas as suites verdes:

```text
Human Validation
```

continua necessária antes do projeto considerar SM-CTX-008 concluído.

---

# 259. Handoff to Executor

## Objective

Implementar:

```text
SM-CTX-008 — Contract Hardening and Longitudinal Validation
```

## Required entry conditions

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
SM-CTX-006 DONE
SM-CTX-007 DONE
```

## Do not add new product features

Este Slice:

```text
proves
hardens
documents
activates
```

o que já foi aprovado.

## Contract activation

Somente depois dos gates:

```text
contracts["contextual_project_memory"] = "1"
```

e:

```text
contextual_project_memory.episodes
contextual_project_memory.checkpoints
contextual_project_memory.recall
contextual_project_memory.explain
contextual_project_memory.history
```

podem ser anunciados.

## Mandatory longitudinal proof

```text
persist
→ restart
→ resume
→ explain
→ history
→ supersede
→ stale conflict
→ replay
→ Forget
→ no current fallback
→ explicit recovery
→ restart
→ no resurrection
```

## Expected result

Ao final:

> Contextual Project Memory deixa de ser um conjunto de Slices implementados e passa a ser um Contract público v1 comprovadamente coerente: persistente entre processos, concorrente com segurança, provenance-aware, Forget-safe, read-only no Recall, compatível com os domínios existentes e explicitamente negociável por qualquer Consumer através de `/api/v1/info`.
