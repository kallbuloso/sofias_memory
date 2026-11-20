# Sofias Memory — Implementation: SM-CTX-002 — Context Streams and Initial Checkpoints

**Document:** `implementation/10-SF-Implementation-SM-CTX-002-Context-Streams-Initial-Checkpoints.md`\
**Status:** APPROVED\
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-002 — Context Streams and Initial Checkpoints`

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
- `11-SF-Implementation-SM-CTX-003-Checkpoint-Revision-Concurrency.md` — dependency clarification approved after the original version of this handoff

---

# 1. Objective

Implementar o segundo vertical slice de Contextual Project Memory:

```text
SM-CTX-002 — Context Streams and Initial Checkpoints
```

Ao final deste Slice, um Consumer deverá conseguir persistir explicitamente:

```text
where this work currently stands
```

para uma linha de continuidade identificada por:

```text
(scope, context_key)
```

e recuperar esse estado depois de:

- nova request;
- nova Session;
- restart do Sofias Memory.

Fluxo funcional básico:

```text
Consumer
    ↓
create ContextCheckpoint
    ↓
persist current continuity
    ↓
restart
    ↓
GET current checkpoint
    ↓
recover current_state / objective / pending / next_action
```

O valor realmente utilizável ao final do Slice é:

> um projeto pode possuir uma ou mais linhas de continuidade duráveis e recuperar explicitamente o estado atual de cada uma sem depender da Session na qual esse estado foi criado.

Este Slice também deve garantir que um Context Stream possa voltar a possuir um current checkpoint depois que o anterior tiver sido explicitamente invalidado ou esquecido.

---

# 2. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-002
```

O executor pode decidir autonomamente detalhes locais e reversíveis.

Não alterar decisões APPROVED.

---

# 3. Expected Entry State

Este documento foi preparado antes da execução física de SM-CTX-001.

Portanto, não assume número específico de migration nem commit específico.

Quando SM-CTX-002 for realmente executado, o repositório deverá possuir SM-CTX-001 concluído e validado.

---

## 3.1 SM-CTX-001 MUST provide

O Slice anterior deverá entregar:

```text
Episode first-class
Episode lifecycle
Episode provenance
Episode direct Forget
Episode idempotency
generic contextual_memory_idempotency
PostgreSQL + pgvector persistence
```

Além disso:

- PostgreSQL permanece authoritative;
- Contextual Memory continua sem Neo4j;
- Contextual mutations continuam síncronas;
- nenhum `PipelineRun` contextual existe.

---

# 4. Dependency Requirement — Generic Contextual Idempotency

SM-CTX-001 deverá criar:

```text
contextual_memory_idempotency
```

como ledger de **Contextual Project Memory**, não como ledger específico de Episodes.

Shape conceitual esperado:

```text
contextual_memory_idempotency
├── id
├── idempotency_key
├── operation
├── request_digest
├── target_resource_id nullable
├── result_resource_id nullable
└── created_at
```

As operations aprovadas incluem:

```text
episode_create
episode_invalidate
episode_forget

checkpoint_create
checkpoint_supersede
checkpoint_invalidate
checkpoint_forget
```

Não utilizar:

```text
target_episode_id
result_episode_id
```

como shape físico do ledger compartilhado.

SM-CTX-002 reutiliza essa infraestrutura; não cria um segundo idempotency ledger.

---

# 5. Critical Semantic Clarification — Creating Current State

`POST /api/v1/context/checkpoints` não significa apenas:

```text
create the first checkpoint ever
```

Seu significado correto é:

> criar um novo ACTIVE checkpoint quando o Context Stream atualmente não possui nenhum ACTIVE checkpoint.

Isso cobre dois casos.

---

## 5.1 Brand-new Context Stream

Nenhum checkpoint existe para:

```text
(scope, context_key)
```

Resultado:

```text
revision = 1
lifecycle = active
```

---

## 5.2 Existing Context Stream without ACTIVE

Pode existir histórico, mas nenhum current checkpoint.

Exemplos:

```text
revision 1 INVALIDATED
```

ou:

```text
revision 1 FORGOTTEN
```

ou, futuramente:

```text
revision 1 SUPERSEDED
revision 2 INVALIDATED
```

Nesse caso, uma criação explícita válida produz:

```text
revision = MAX(existing revision) + 1
lifecycle = active
```

Exemplo:

```text
revision 1 ACTIVE
    ↓ invalidate

revision 1 INVALIDATED

POST /context/checkpoints
    ↓

revision 2 ACTIVE
```

---

## 5.3 Existing ACTIVE

Se existir ACTIVE para:

```text
(scope, context_key)
```

uma nova chamada de Create recebe:

```text
409 CONTEXT_STATE_CONFLICT
```

Não:

- atualiza o ACTIVE;
- faz merge;
- cria revision seguinte;
- supersede automaticamente;
- retorna current silenciosamente.

Depois que SM-CTX-003 existir, evolução normal de ACTIVE ocorre por:

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
```

---

## 5.4 No historical reactivation

Criar um novo current após ausência de ACTIVE nunca significa:

```text
old checkpoint -> ACTIVE again
```

É sempre:

```text
new checkpoint UUID
new statements
new provenance
new revision
```

O histórico permanece intocado.

---

## 5.5 Revision monotonicity

Revision pertence ao Context Stream:

```text
(scope, context_key)
```

e nunca é reutilizada.

Portanto:

```text
revision 1 INVALIDATED
```

seguida de novo Create produz:

```text
revision 2 ACTIVE
```

não revision 1.

Outro exemplo:

```text
revision 1 SUPERSEDED
revision 2 FORGOTTEN
```

seguido de Create produz:

```text
revision 3 ACTIVE
```

Revision é uma sequência histórica do stream, não apenas uma sequência de edges de supersession.

---

# 6. Scope

Implementar neste Slice:

## Domain

- `context_key`;
- Context Stream lógico;
- `ContextCheckpoint`;
- `ContextStatement`;
- checkpoint lifecycle;
- revision allocation;
- checkpoint provenance;
- checkpoint idempotency.

## Persistence

- `context_checkpoints`;
- `context_checkpoint_statements`;
- checkpoint provenance;
- single-ACTIVE protection;
- revision uniqueness;
- statement ordering;
- lifecycle constraints;
- tombstone semantics.

## Service

- create current checkpoint when no ACTIVE exists;
- get current;
- get by ID;
- list checkpoints;
- invalidate;
- direct Forget.

## HTTP

```http
POST /api/v1/context/checkpoints

GET  /api/v1/context/checkpoints
GET  /api/v1/context/checkpoints/current
GET  /api/v1/context/checkpoints/{checkpoint_id}

POST /api/v1/context/checkpoints/{checkpoint_id}/invalidate
POST /api/v1/context/checkpoints/{checkpoint_id}/forget
```

## Tests

- domain;
- migration;
- persistence;
- idempotency;
- initial-create race;
- recovery-create race;
- lifecycle;
- revision allocation;
- restart durability;
- OpenAPI;
- cross-feature isolation.

---

# 7. Explicit Out of Scope

MUST NOT implementar neste Slice:

```text
checkpoint supersede
checkpoint replacement against an ACTIVE target
stale writer detection
Evidence
support policy
Contextual Recall
Knowledge integration
Cognitive Recall composition
Forget propagation
semantic checkpoint search
checkpoint embeddings
Neo4j projection
PipelineRun
automatic checkpoint generation
automatic Session summarization
```

`SM-CTX-003` será responsável por:

```text
supersession
expected-current concurrency
stale writers
linear lineage
```

`SM-CTX-004` será responsável por Evidence.

Não antecipar nenhum deles.

---

# 8. Context Stream Semantics

Context Stream é a unidade lógica de continuidade:

```text
(scope, context_key)
```

Exemplo:

```text
scope       = project:sofias-memory
context_key = feature:contextual-memory
```

Outro stream no mesmo projeto pode coexistir:

```text
scope       = project:sofias-memory
context_key = release:0.8.0
```

Eles são independentes.

---

# 9. Context Stream Is Logical

Não criar tabela:

```text
context_streams
```

no Contract v1.

Um Context Stream existe logicamente quando recursos utilizam:

```text
scope
context_key
```

Não existe necessidade atual de:

- registration;
- display name;
- ownership;
- metadata;
- status;
- ACL.

Criar uma tabela dedicada agora seria abstração sem função concreta.

---

# 10. Project Registration

Também não criar:

```text
projects
```

como requisito para esta feature.

Um scope válido:

```text
project:<key>
```

é suficiente.

Exemplo:

```text
project:sofias-memory
```

não precisa ser previamente cadastrado.

---

# 11. Scope Semantics

ContextCheckpoint v1 pertence exclusivamente a:

```text
project:<key>
```

Não aceitar:

```text
global
```

para checkpoint.

Manter gramática compatível com Cognitive Memory:

```text
project:[a-z0-9][a-z0-9._-]{0,127}
```

Scope permanece:

```text
context namespace
```

e nunca:

```text
ACL
tenant
owner
authorization boundary
```

---

# 12. `context_key`

Formato aprovado:

```text
[a-z0-9][a-z0-9._:-]{0,127}
```

Características:

- ASCII lowercase;
- case-sensitive;
- 1..128 caracteres;
- sem whitespace;
- comparação exata.

Exemplos:

```text
main
feature:contextual-memory
release:0.8.0
incident:retrieval-001
research:cognee
```

---

# 13. Default Context Stream

Quando `context_key` for omitido em operações que admitem default:

```text
main
```

deve ser utilizado.

A response MUST retornar:

```text
context_key = "main"
```

explicitamente.

Não manter ambiguidade entre:

```text
NULL
```

e:

```text
main
```

para ContextCheckpoint.

Diferentemente de Episode, todo ContextCheckpoint pertence obrigatoriamente a um Context Stream.

---

# 14. ContextCheckpoint Semantics

ContextCheckpoint representa:

```text
where this work currently stands
```

Ele é um durable semantic snapshot de continuidade.

Não é:

- transcript;
- Session;
- Session summary obrigatório;
- Task;
- Workflow;
- distributed lock;
- job state;
- external task authority.

---

# 15. Checkpoint Create Semantics

Create é permitido somente quando:

```text
current ACTIVE checkpoint = none
```

Revision é calculada a partir do histórico do stream.

---

## 15.1 First checkpoint ever

```text
MAX(revision) = none
```

produz:

```text
revision = 1
```

---

## 15.2 Re-establishing current state

```text
MAX(revision) = N
current ACTIVE = none
```

produz:

```text
revision = N + 1
```

---

## 15.3 Existing ACTIVE

```text
current ACTIVE = checkpoint X
```

produz:

```text
CONTEXT_STATE_CONFLICT
```

Não calcular nova revision.

---

# 16. ContextCheckpoint Content

O recurso deve suportar:

```text
objective
current_state
approved_decisions[]
pending_work[]
open_items[]
blockers[]
next_action
```

---

## 16.1 Required Mutation Input

Obrigatórios no Create (e, em SM-CTX-003, no Supersede):

```text
scope
context_key
current_state
provenance
```

---

## 16.2 Optional

```text
objective
approved_decisions[]
pending_work[]
open_items[]
blockers[]
next_action
```

Arrays podem ser vazios.

---

# 17. ContextStatement

Cada unidade semântica será persistida como:

```text
ContextStatement
```

e não como JSON opaco.

Tipos aprovados:

```text
objective
current_state
approved_decision
pending_work
open_item
blocker
next_action
```

---

# 18. Statement Cardinality

## Scalar

No máximo um por checkpoint:

```text
objective
current_state
next_action
```

`current_state` é obrigatório no input de Create/Supersede. Um resultado
INVALIDATED após propagation de SM-CTX-005 pode não possuir esse statement;
FORGOTTEN não possui statements semânticos. Não impor cardinalidade de input
indiscriminadamente a todos os estados persistidos/resultados.

---

## Collection

Zero ou mais:

```text
approved_decision
pending_work
open_item
blocker
```

---

# 19. Statement Ordering

Itens de collection preservam a ordem fornecida pelo Consumer.

Persistir:

```text
ordinal
```

por:

```text
(checkpoint_id, statement_type)
```

Exemplo:

```text
pending_work
ordinal 0
ordinal 1
ordinal 2
```

Ordering MUST ser determinístico.

---

# 20. Statement Identity

Cada statement recebe:

```text
statement_id
```

server-generated UUID.

Mesmo scalars recebem statement identity própria.

Isso prepara o modelo para Evidence field-level no SM-CTX-004 sem mudar a identidade dos statements existentes.

---

# 21. No Evidence Yet

Mesmo que `statement_id` seja criado agora, SM-CTX-002 MUST NOT implementar:

```text
support
evidence
availability derived from evidence
```

Esses conceitos entram em SM-CTX-004.

Não criar Evidence tables antecipadamente.

---

# 22. Public Statement Shape

No Slice atual:

```json
{
  "statement_id": "uuid",
  "text": "PRD e Spec estão aprovados."
}
```

Para scalar:

```json
{
  "current_state": {
    "statement_id": "uuid",
    "text": "PRD e Spec estão aprovados."
  }
}
```

Para collection:

```json
{
  "pending_work": [
    {
      "statement_id": "uuid",
      "text": "Implementar persistence."
    }
  ]
}
```

O Contract v1 completo ganhará Evidence/support posteriormente.

Como o Contract completo ainda não está anunciado, esse crescimento permanece interno à evolução incremental.

---

# 23. Text Validation

Todo statement fornecido deve:

- ser string;
- não estar blank;
- ser normalizado consistentemente.

Não introduzir limites públicos pequenos não aprovados.

Não truncar conteúdo silenciosamente.

Se surgir necessidade real de novo limite público:

```text
Contract decision required
```

---

# 24. Provenance

ContextCheckpoint possui provenance própria.

Preferir relação 1:1:

```text
context_checkpoint_provenance
```

em vez de JSON opaco.

Shape público:

```text
origin_kind
source_system
conversation_ref?
turn_ref?
```

Usar os mesmos origin kinds aprovados:

```text
user_asserted
tool_observed
imported
inferred
assistant_generated
```

---

# 25. Confidence

O Contract de ContextCheckpoint não define confidence como propriedade do checkpoint.

Não adicionar.

Confidence pertence a outros domínios quando explicitamente definido.

---

# 26. Proposed Relational Model

## 26.1 `context_checkpoints`

Estrutura conceitual:

```text
context_checkpoints
├── id UUID PK
├── scope TEXT NOT NULL for every lifecycle, including forgotten
├── context_key TEXT NOT NULL for every lifecycle, including forgotten
├── revision INTEGER NOT NULL
├── lifecycle checkpoint_lifecycle NOT NULL
├── created_at TIMESTAMPTZ NOT NULL
├── superseded_at TIMESTAMPTZ nullable
├── superseded_by UUID nullable
├── invalidated_at TIMESTAMPTZ nullable
├── invalidation_reason TEXT nullable
└── forgotten_at TIMESTAMPTZ nullable
```

Os campos de supersession podem existir estruturalmente neste Slice porque fazem parte do lifecycle aprovado e serão utilizados imediatamente por SM-CTX-003.

Nenhuma operação de SM-CTX-002 deve produzir `superseded`.

---

# 27. `context_checkpoint_statements`

Estrutura conceitual:

```text
context_checkpoint_statements
├── id UUID PK
├── checkpoint_id UUID FK
├── statement_type
├── ordinal INTEGER NOT NULL
├── content TEXT NOT NULL
└── created_at TIMESTAMPTZ NOT NULL
```

Neste Slice não adicionar ainda:

```text
support_mode
Evidence FK
Evidence availability
```

Esses campos pertencem ao SM-CTX-004.

---

# 28. `context_checkpoint_provenance`

Estrutura conceitual:

```text
context_checkpoint_provenance
├── checkpoint_id UUID PK/FK
├── origin_kind
├── source_system
├── conversation_ref nullable
└── turn_ref nullable
```

External refs devem poder ser scrubbed no Forget.

---

# 29. Lifecycle Enum

Criar o enum completo aprovado:

```text
active
superseded
invalidated
forgotten
```

Embora `superseded` ainda não seja produzido por API neste Slice, ele já pertence ao domínio aprovado e será usado pelo próximo Slice.

---

# 30. Lifecycle — ACTIVE

ACTIVE significa:

> checkpoint current de um Context Stream.

Um Context Stream possui no máximo:

```text
1 ACTIVE checkpoint
```

---

# 31. Lifecycle — SUPERSEDED

O enum e schema podem suportar:

```text
superseded
```

mas SM-CTX-002 não produz esse lifecycle.

Sua criação funcional pertence ao SM-CTX-003.

---

# 32. Lifecycle — INVALIDATED

INVALIDATED significa:

> checkpoint preservado historicamente, mas não confiável como current continuity.

Direct invalidation:

```text
ACTIVE -> INVALIDATED
```

Depois disso o stream fica sem current até uma nova criação explícita.

---

# 33. Lifecycle — FORGOTTEN

FORGOTTEN significa:

> conteúdo semântico recuperável foi removido.

Pode resultar neste Slice de:

```text
ACTIVE -> FORGOTTEN
INVALIDATED -> FORGOTTEN
```

No futuro:

```text
SUPERSEDED -> FORGOTTEN
```

também será suportado.

---

# 34. No Automatic Fallback

Se ACTIVE for:

```text
invalidated
```

ou:

```text
forgotten
```

o Context Stream passa a possuir:

```text
current checkpoint = none
```

Nunca reativar histórico automaticamente.

A saída desse estado é um novo:

```http
POST /api/v1/context/checkpoints
```

com snapshot completo e nova revision.

---

# 35. Single-ACTIVE Database Constraint

A principal invariante deve ser protegida estruturalmente pelo PostgreSQL.

Conceitualmente:

```sql
UNIQUE (scope, context_key)
WHERE lifecycle = 'active'
```

Não depender apenas de:

```text
SELECT current
then INSERT
```

---

# 36. Revision Constraint

Dentro de:

```text
(scope, context_key)
```

revision é única.

Constraint:

```text
UNIQUE(scope, context_key, revision)
```

SM-CTX-002 pode criar:

```text
revision 1
```

ou uma revision posterior quando o stream não possui ACTIVE.

Exemplo:

```text
revision 1 INVALIDATED
revision 2 ACTIVE
```

---

# 37. Revision Allocation Under Concurrency

Allocation deve ser race-safe.

Não basta:

```text
SELECT MAX(revision)
then INSERT
```

sem proteção transacional adequada.

Dois callers concorrentes tentando reestabelecer o mesmo stream não podem criar:

```text
revision N+1
revision N+2
```

como dois winners.

Resultado correto:

```text
one winner
one CONTEXT_STATE_CONFLICT or serialized losing outcome
exactly one ACTIVE
```

A unique revision constraint e a single-ACTIVE constraint são safety nets estruturais.

---

# 38. Scope Constraint

PostgreSQL deve proteger gramática de scope:

```text
^project:[a-z0-9][a-z0-9._-]{0,127}$
```

`global` não é válido para ContextCheckpoint.

---

# 39. Context Key Constraint

PostgreSQL deve proteger:

```text
^[a-z0-9][a-z0-9._:-]{0,127}$
```

Não depender apenas do Pydantic validator.

---

# 40. Statement Constraints

Proteger ao menos:

```text
content not blank
ordinal >= 0
```

Adicionar:

```text
UNIQUE(checkpoint_id, statement_type, ordinal)
```

---

## 40.1 Scalar uniqueness

Também proteger estruturalmente que:

```text
objective
current_state
next_action
```

não tenham múltiplas instâncias no mesmo checkpoint.

Pode ser usado partial unique index ou constraint equivalente simples.

Não implementar trigger complexo apenas para isso.

---

# 41. Current State Required

Todo checkpoint ACTIVE criado por API deve possuir exatamente um:

```text
current_state
```

Essa garantia MUST existir no service/schema.

Não introduzir trigger PostgreSQL apenas para verificar existência de child row se isso adicionar complexidade desproporcional.

Integration tests devem provar a invariante.

---

# 42. Supersession Columns

Os campos:

```text
superseded_at
superseded_by
```

podem ser adicionados no schema agora para evitar remodelagem imediatamente no SM-CTX-003.

Entretanto:

- nenhuma rota CTX-002 os modifica;
- nenhum service CTX-002 cria SUPERSEDED;
- nenhum comportamento de supersession é considerado implementado.

Isso é schema readiness, não antecipação funcional.

---

# 43. Lineage FK

Se `superseded_by` for criado neste Slice:

```text
FK -> context_checkpoints.id
ON DELETE RESTRICT
```

Não usar CASCADE.

Checkpoint não possui hard delete público.

Constraints adicionais de lineage podem ser implementadas no SM-CTX-003 quando supersession passar a existir de fato.

---

# 44. Forget Tombstone

Direct Forget deve preservar somente estrutura necessária.

Checkpoint tombstone MUST preservar:

```text
checkpoint_id
scope
context_key
revision
lifecycle
created_at
superseded_by / superseded_at when applicable
invalidated_at if historically applicable
forgotten_at
minimal structural provenance
```

Todo conteúdo semântico deve desaparecer. `scope` e `context_key` são identidade
estrutural obrigatória do stream e MUST NOT ser scrubbed, inclusive quando todas
as revisions forem FORGOTTEN. Tombstones continuam participando da constraint
`UNIQUE(scope, context_key, revision)` e de `MAX(existing revision) + 1`.
Não copiar o scrub de scope de Episode/Cognitive Memory para Checkpoint.

---

# 45. Statement Scrub Strategy

No direct Forget, a estratégia preferida neste Slice é:

```text
delete ContextStatement rows
```

do checkpoint esquecido.

Motivos:

- statements contêm semantic content;
- Evidence ainda não existe;
- ContextStatement não é Evidence source type no Contract v1;
- não há valor funcional em preservar conteúdo vazio.

Não hard-delete o checkpoint.

---

# 46. Provenance Forget

No Forget:

```text
conversation_ref
turn_ref
```

devem ser scrubbed.

Pode permanecer:

```text
origin_kind
source_system
```

como provenance estrutural mínima, seguindo a semântica já utilizada por Cognitive Memory/Episode quando compatível.

---

# 47. Invalidation Reason

`POST .../invalidate` recebe:

```json
{
  "reason": "Checkpoint is no longer considered reliable."
}
```

O motivo deve ser persistido.

Não depender de log como audit trail.

Campo recomendado:

```text
invalidation_reason
```

Quando o checkpoint for posteriormente FORGOTTEN:

```text
invalidation_reason = NULL
```

para não preservar conteúdo textual desnecessário.

---

# 48. Create API

Endpoint:

```http
POST /api/v1/context/checkpoints
```

Header obrigatório:

```http
Idempotency-Key
```

Request conceitual:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",

  "objective": {
    "text": "Implementar memória contextual persistente."
  },

  "current_state": {
    "text": "PRD, Spec e Contract estão aprovados."
  },

  "approved_decisions": [
    {
      "text": "Episode será first-class."
    }
  ],

  "pending_work": [
    {
      "text": "Implementar persistence."
    }
  ],

  "open_items": [],

  "blockers": [],

  "next_action": {
    "text": "Implementar SM-CTX-001."
  },

  "provenance": {
    "origin_kind": "assistant_generated",
    "source_system": "sofias-personal-assistant"
  }
}
```

---

# 49. Create Response

Successful create:

```http
201 Created
```

Response possui:

```text
checkpoint_id
scope
context_key
revision
lifecycle = active
statements with statement_id
provenance
created_at
```

Revision pode ser:

```text
1
```

para stream novo, ou:

```text
N+1
```

para stream histórico sem ACTIVE.

---

# 50. Create Conflict with Existing ACTIVE

Se existir ACTIVE para:

```text
(scope, context_key)
```

uma nova operação lógica recebe:

```text
409 CONTEXT_STATE_CONFLICT
```

Não:

- atualizar;
- mergear;
- supersede;
- retornar current silenciosamente.

---

# 51. Idempotency Replay Precedence

Se a mesma criação já venceu e a response foi perdida:

```text
same Idempotency-Key
same semantic request
```

a retry MUST retornar o resultado original.

Ela não pode ser convertida em:

```text
CONTEXT_STATE_CONFLICT
```

apenas porque o ACTIVE criado pela própria primeira execução agora existe.

Portanto:

```text
idempotency replay resolution
```

precede:

```text
active checkpoint conflict
```

semanticamente.

---

# 52. Current Checkpoint Endpoint

```http
GET /api/v1/context/checkpoints/current
```

Query:

```text
scope        required
context_key  optional, default main
```

Se existir ACTIVE:

```http
200 OK
```

Se não existir:

```http
404 CURRENT_CHECKPOINT_NOT_FOUND
```

MUST NOT retornar:

- latest invalidated;
- latest forgotten;
- latest superseded;
- qualquer histórico por conveniência.

---

# 53. Get by ID

```http
GET /api/v1/context/checkpoints/{checkpoint_id}
```

## ACTIVE

Retorna conteúdo.

## INVALIDATED

Retorna conteúdo + invalidation metadata.

## FORGOTTEN

Retorna tombstone.

## SUPERSEDED

O schema deve ser capaz de representá-lo, embora SM-CTX-002 ainda não produza esse lifecycle.

---

# 54. List Endpoint

```http
GET /api/v1/context/checkpoints
```

Filtros do Contract:

```text
scope        required
context_key  optional
lifecycle    optional
limit        bounded
cursor       optional
```

---

# 55. Normal Listing

Quando `lifecycle` não for fornecido:

```text
active only
```

Isso mantém a operação comum orientada ao estado utilizável.

Histórico exige filtro/intenção explícita.

---

# 56. List Ordering

Dentro de um Context Stream:

```text
revision DESC
checkpoint_id ASC
```

Quando `context_key` não for fornecido e múltiplos streams estiverem sendo listados:

```text
context_key ASC
revision DESC
checkpoint_id ASC
```

Não comparar revision de streams diferentes como uma sequência global.

---

# 57. Pagination

Usar bounded cursor pagination.

Não criar framework genérico.

Cursor deve ser opaco.

Keyset pagination pode codificar os campos da ordenação.

Não usar offset como contrato obrigatório para esta nova API.

---

# 58. Checkpoint Invalidate

Endpoint:

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/invalidate
```

Header obrigatório:

```http
Idempotency-Key
```

Request:

```json
{
  "reason": "Checkpoint is no longer considered reliable."
}
```

---

# 59. Invalidation State Behaviour

Neste Slice:

```text
ACTIVE
    -> INVALIDATED

INVALIDATED
    -> state no-op

FORGOTTEN
    -> CHECKPOINT_STATE_CONFLICT
```

No futuro:

```text
SUPERSEDED -> INVALIDATED
```

será suportado quando SM-CTX-003 produzir esse estado.

---

# 60. Current After Invalidation

Se o checkpoint invalidado era ACTIVE:

```text
current checkpoint = none
```

`GET /current` deve retornar:

```text
CURRENT_CHECKPOINT_NOT_FOUND
```

Nenhum estado anterior é promovido.

O Consumer pode então explicitamente criar um novo checkpoint:

```http
POST /api/v1/context/checkpoints
```

que receberá a próxima revision.

---

# 61. Checkpoint Forget

Endpoint:

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/forget
```

Header obrigatório:

```http
Idempotency-Key
```

---

# 62. Forget State Behaviour

Neste Slice:

```text
ACTIVE
    -> FORGOTTEN

INVALIDATED
    -> FORGOTTEN

FORGOTTEN
    -> state no-op
```

No futuro:

```text
SUPERSEDED -> FORGOTTEN
```

será suportado.

---

# 63. Current After Forget

Se ACTIVE for esquecido:

```text
current checkpoint = none
```

Não criar fallback.

O Consumer poderá criar explicitamente um novo current, com próxima revision.

---

# 64. No PATCH / PUT

MUST NOT existir:

```http
PATCH /api/v1/context/checkpoints/{id}
PUT   /api/v1/context/checkpoints/{id}
```

Mudança semântica de um ACTIVE ocorrerá por:

```text
supersede
```

no SM-CTX-003.

Create sobre stream sem ACTIVE é criação de novo current, não update.

---

# 65. Idempotency Operations

O ledger contextual criado no SM-CTX-001 deverá suportar agora:

```text
checkpoint_create
checkpoint_invalidate
checkpoint_forget
```

Ainda não implementar comportamento de:

```text
checkpoint_supersede
```

embora o enum possa já conter o valor aprovado.

---

# 66. Checkpoint Create Canonical Request

O digest deve incluir semanticamente:

```text
operation
scope
resolved context_key
objective
current_state
approved_decisions
pending_work
open_items
blockers
next_action
provenance
```

Não incluir:

```text
generated checkpoint_id
generated statement_ids
created_at
server-allocated revision
transport headers
HMAC secret
```

Importante:

> revision não deve entrar no canonical request porque é resultado server-side da operação, calculado a partir do stream atual.

---

# 67. Collection Ordering and Idempotency

A ordem de arrays é semanticamente relevante para o snapshot.

Portanto:

```text
pending_work=[A,B]
```

e:

```text
pending_work=[B,A]
```

podem produzir digest diferente.

Não ordenar arrays automaticamente apenas para fabricar equivalência.

---

# 68. Create Transaction

Não existe external provider call.

Fluxo recomendado:

```text
validate request
normalize scope/context_key/statements
compute digest

BEGIN

claim/check idempotency
    ↓
same-key replay? return winner
    ↓
resolve current ACTIVE for stream
    ↓
if ACTIVE exists -> conflict
    ↓
determine next revision
    ↓
create checkpoint
create provenance
create statements
persist idempotency outcome

COMMIT
```

---

# 69. Revision Allocation Strategy

A implementação deve serializar adequadamente a criação de current para o mesmo Context Stream.

Não introduzir tabela `context_streams` apenas para obter uma lock row.

Soluções PostgreSQL aceitáveis incluem:

- advisory transaction lock derivado deterministicamente de `(scope, context_key)`;
- lock sobre row histórica apropriada quando existir;
- combinação de constraints + retry/translation determinística;
- outra solução simples e database-authoritative.

Não usar:

- process-local mutex como correctness boundary;
- Redis;
- distributed lock externo.

Se advisory lock for escolhido, deve ser:

```text
PostgreSQL transaction-scoped
```

e derivado sem colisão semanticamente perigosa na prática.

A decisão local deve ser documentada no Implementation Report.

---

# 70. Create Race — Brand-New Stream

Cenário:

```text
Writer A
Idempotency-Key = A

Writer B
Idempotency-Key = B

same brand-new (scope, context_key)
```

Resultado obrigatório:

```text
one ACTIVE checkpoint
one revision 1
one winner
one CONTEXT_STATE_CONFLICT
```

Nunca:

```text
revision 1 ACTIVE
revision 2 ACTIVE
```

e nunca dois revision 1.

---

# 71. Create Race — Existing Stream without ACTIVE

Estado:

```text
revision 1 INVALIDATED
```

Dois callers concorrentes fazem Create.

Resultado:

```text
exactly one revision 2 ACTIVE
one loser conflict
```

Nunca:

```text
revision 2 ACTIVE
revision 3 ACTIVE
```

produzidas por duas tentativas concorrentes contra o mesmo estado sem current.

O loser não deve “reinterpretar” automaticamente o estado após o winner e criar a próxima revision.

---

# 72. Create Race — Same Key

Cenário:

```text
same key
same request
two concurrent callers
```

Resultado:

```text
one checkpoint
both resolve same logical result
```

Nenhum orphan:

- ledger;
- checkpoint;
- provenance;
- statement.

---

# 73. Invalidate Transaction

Fluxo:

```text
claim/check idempotency
    ↓
same replay? return original outcome
    ↓
lock checkpoint
    ↓
validate lifecycle
    ↓
persist invalidation_reason
ACTIVE -> INVALIDATED
set invalidated_at
commit
```

---

# 74. Forget Transaction

Fluxo:

```text
claim/check idempotency
    ↓
same replay? return original outcome
    ↓
lock checkpoint
    ↓
validate resource existence/lifecycle

delete semantic statement rows
scrub external provenance refs
scrub invalidation_reason
set lifecycle = forgotten
set forgotten_at

preserve scope/context_key/revision and applicable lineage/timestamps

commit
```

Não criar background job.

---

# 75. Repository Layer

Criar somente repositories necessários.

Prováveis:

```text
ContextCheckpointRepository
ContextCheckpointStatementRepository
ContextCheckpointProvenanceRepository
```

Reutilizar:

```text
ContextualMemoryIdempotencyRepository
```

produzido pelo SM-CTX-001.

Não criar:

```text
GenericContextRepository
GenericRevisionEngine
ProjectRepository
ContextStreamRepository
```

---

# 76. Unit of Work

Adicionar repositories ao `PostgresUnitOfWork`.

Garantir:

- initialization;
- properties;
- `_clear()`.

Não criar UoW paralelo para Contextual Memory.

---

# 77. Service Layer

Criar service explícito:

```text
ContextCheckpointService
```

Responsabilidades neste Slice:

```text
create_current
get_current
get
list
invalidate
forget
```

Nome interno `create_initial` é desencorajado porque o endpoint também cria um novo current depois de ausência de ACTIVE.

Não adicionar ainda:

```text
supersede
```

como comportamento funcional.

---

# 78. Schemas

Criar:

```text
sofias_memory/schemas/context_checkpoints.py
```

Shapes prováveis:

```text
ContextStatementCreateRequest
ContextStatementResult

ContextCheckpointProvenanceCreateRequest
ContextCheckpointProvenanceResult

ContextCheckpointCreateRequest
ContextCheckpointResult

ContextCheckpointInvalidateRequest
ContextCheckpointListResult
```

Não expor ORM models.

Separar explicitamente os contratos de input e result:

```text
ContextCheckpointCreateRequest.current_state = required
ContextCheckpointSupersedeRequest.current_state = required (SM-CTX-003)
ContextCheckpointResult.current_state = nullable/absent for legitimate scrubbed states
```

É permitido compartilhar validators de texto, mas não reutilizar um schema de
mutation que obrigue `current_state` em todos os resultados. SM-CTX-002 deve
representar tombstone; SM-CTX-005 exercitará também INVALIDATED sem current_state.
Isso não implementa propagation antecipadamente.

---

# 79. HTTP Layer

Criar:

```text
sofias_memory/api/routes/context_checkpoints.py
```

Routes:

- validam transport;
- criam service;
- chamam service;
- retornam envelope;
- mapeiam errors.

Não conter:

- SQL;
- row locking;
- lifecycle decision;
- revision allocation;
- idempotency algorithm.

---

# 80. Error Codes Required

Implementar os códigos aplicáveis:

```text
INVALID_REQUEST
INVALID_SCOPE
INVALID_CONTEXT_KEY

CHECKPOINT_NOT_FOUND
CURRENT_CHECKPOINT_NOT_FOUND

IDEMPOTENCY_CONFLICT
CONTEXT_STATE_CONFLICT
CHECKPOINT_STATE_CONFLICT
```

Não adicionar Evidence errors ainda.

---

# 81. Migration Strategy

Criar migration posterior ao migration head produzido por SM-CTX-001.

Não congelar neste documento um revision number específico.

Ao iniciar implementação:

```bash
uv run alembic heads
```

e usar o head real.

A migration deve ser aditiva.

---

# 82. Migration Content

Criar:

```text
checkpoint lifecycle enum
statement type enum

context_checkpoints
context_checkpoint_statements
context_checkpoint_provenance
```

e:

- FKs;
- partial unique ACTIVE index;
- revision unique constraint;
- scope/context grammar constraints;
- statement order constraints;
- indexes necessários.

Não criar Evidence tables.

---

# 83. Suggested Indexes

## Current lookup

```text
(scope, context_key, lifecycle)
```

---

## Revision/history

```text
(scope, context_key, revision)
```

com uniqueness.

---

## Single ACTIVE

Partial unique:

```text
(scope, context_key)
WHERE lifecycle='active'
```

---

## Statement hydration

```text
(checkpoint_id, statement_type, ordinal)
```

---

# 84. No Embedding

ContextCheckpoint não recebe embedding neste Slice.

Não chamar embedding provider em:

- create;
- get;
- list;
- invalidate;
- forget.

Current checkpoint é lookup direto.

Semantic checkpoint search está DEFERRED.

---

# 85. No Session Ownership

ContextCheckpoint não recebe:

```text
session_id FK
```

como owner.

Provenance pode referenciar interação externa, mas:

```text
Session != owner of checkpoint
```

Checkpoint pertence a:

```text
(scope, context_key)
```

---

# 86. Session Isolation

Criar checkpoint não:

- cria Session;
- cria SessionEntry;
- modifica Session;
- arquiva/restaura Session;
- depende da Session estar ativa.

Context state atravessa Sessions.

---

# 87. Episode Isolation

Criar checkpoint não:

- cria Episode;
- modifica Episode;
- promove Episode;
- exige Episode existente.

Evidence entre eles virá depois.

---

# 88. Cognitive Memory Isolation

Criar checkpoint não:

- cria MemoryItem;
- supersede MemoryItem;
- consulta Cognitive Recall.

Integração de leitura ocorrerá no SM-CTX-006.

---

# 89. Knowledge Isolation

Nenhum Dataset é obrigatório.

Não inferir:

```text
project:<key> == dataset
```

Não criar Dataset automaticamente.

---

# 90. Graph Isolation

Nenhuma operação deste Slice:

- cria graph_outbox;
- grava Neo4j;
- consulta Neo4j.

Adicionar teste de regressão quando útil.

---

# 91. Pipeline Isolation

Nenhuma mutation cria:

```text
PipelineRun
PipelineStep
```

Todas são síncronas.

---

# 92. OpenAPI

Durante este Slice passam a existir apenas as rotas de checkpoint previstas aqui.

MUST NOT aparecer ainda:

```http
POST /api/v1/context/checkpoints/{id}/supersede

POST /api/v1/context/recall
```

Adicionar contract test.

---

# 93. Contract Advertisement

Ainda MUST NOT anunciar:

```text
contextual_project_memory = "1"
```

em `/api/v1/info`.

Mesmo com Episodes + current checkpoints implementados, o Contract está incompleto.

---

# 94. Unit Tests

Cobrir no mínimo:

## Context key

- valid values;
- invalid uppercase;
- invalid whitespace;
- max boundary;
- default `main`.

## Statements

- current_state required;
- mutation input rejects missing/null current_state; result accepts forgotten tombstone;
- scalar cardinality;
- collections preserve ordering;
- blank text rejected.

## Lifecycle

- ACTIVE → INVALIDATED;
- ACTIVE → FORGOTTEN;
- INVALIDATED → FORGOTTEN;
- FORGOTTEN terminal/no-op semantics.

## Revision

- new stream -> revision 1;
- historical stream without ACTIVE -> max+1;
- revision never reused.
- tombstone always retains scope/context_key/revision;

## Idempotency

- canonical request stable;
- context_key default participates as resolved `main`;
- array order affects digest;
- same request same digest;
- changed statement changes digest;
- server-assigned revision does not affect digest.

---

# 95. Migration Tests

Validar:

- migration chain from CTX-001 head;
- tables;
- enum values;
- indexes;
- constraints;
- active uniqueness;
- revision uniqueness;
- scope grammar;
- context_key grammar;
- downgrade.

Não depender de `Base.metadata.create_all()`.

---

# 96. PostgreSQL Integration Tests

Obrigatórios:

- first create;
- recovery create after invalidation;
- recovery create after Forget;
- Forget of every revision, restart, then Create allocates MAX+1 using tombstones;
- forgotten revisions retain stream/revision identity and revision uniqueness;
- current lookup;
- get by id;
- list;
- default `main`;
- multiple streams;
- same stream different project;
- duplicate create while ACTIVE exists;
- active uniqueness;
- revision monotonicity;
- invalidation;
- forget;
- tombstone;
- restart;
- idempotency replay;
- idempotency conflict;
- race tests.

---

# 97. Brand-New Create Race Test

Usar PostgreSQL real.

Cenário:

```text
same new scope
same new context_key
different Idempotency-Key
two concurrent creates
```

Validar:

```text
COUNT(active) = 1
MAX(revision) = 1
```

e:

```text
one request succeeds
one request gets CONTEXT_STATE_CONFLICT
```

---

# 98. Recovery Create Race Test

Estado inicial:

```text
revision 1 INVALIDATED
no ACTIVE
```

Dois callers concorrentes fazem Create.

Validar:

```text
exactly one revision 2
revision 2 ACTIVE
exactly one winner
one loser conflict
```

Não permitir revision 3 produzida pelo loser automaticamente.

---

# 99. Same-Key Race Test

Cenário:

```text
same scope
same context_key
same key
same payload
two concurrent creates
```

Validar:

```text
one checkpoint row
same checkpoint_id returned
one revision
one statement set
one provenance row
one idempotency outcome
```

---

# 100. Restart Durability

Criar:

```text
project:test
context_key=feature:memory
revision=1
```

Fechar boundary de aplicação/session.

Criar nova aplicação/service.

Executar:

```text
GET current
```

Confirmar conteúdo integral.

Também validar um stream com:

```text
revision 1 INVALIDATED
revision 2 ACTIVE
```

sobrevive ao restart com revision/current corretos.

---

# 101. Multiple Context Streams

Criar:

```text
(project:test, main)
(project:test, feature:a)
(project:test, feature:b)
```

Todos podem possuir ACTIVE simultaneamente.

A single-ACTIVE constraint é por:

```text
(scope, context_key)
```

não por project inteiro.

---

# 102. Project Isolation

Criar:

```text
(project:a, main)
(project:b, main)
```

Eles são independentes.

`GET current project:a` nunca retorna `project:b`.

---

# 103. Invalidation Validation

Após:

```text
ACTIVE -> INVALIDATED
```

validar:

```text
GET by id = invalidated checkpoint
GET current = CURRENT_CHECKPOINT_NOT_FOUND
normal active list = excludes invalidated
```

Depois criar novo current.

Validar:

```text
old revision = invalidated
new revision = old revision + 1
new lifecycle = active
```

---

# 104. Forget Validation

Após Forget:

```text
GET by id
```

retorna tombstone.

MUST NOT retornar:

- objective;
- current_state;
- decisions;
- pending work;
- open items;
- blockers;
- next action;
- invalidation reason;
- external provenance refs.

Depois criar novo current.

Validar revision monotônica e presença obrigatória de:
`checkpoint_id`, `scope`, `context_key`, `revision`, `lifecycle`, `created_at`,
`forgotten_at` e lineage/timestamps aplicáveis no tombstone. Esquecer todas as
revisions, reiniciar e recriar o current MUST continuar a sequência, sem reuse.

---

# 105. No Lineage on Recovery Create

Quando um novo current é criado depois de INVALIDATED ou FORGOTTEN:

```text
old.superseded_by remains NULL
```

A nova revision não é apresentada como replacement por supersession.

Isso é importante para distinguir:

```text
explicit recovery create
```

de:

```text
normal supersession
```

que pertence ao CTX-003.

---

# 106. Cross-Feature Regression

Confirmar que checkpoint operations não alteram:

```text
Episodes
MemoryItems
Sessions
Datasets
PipelineRuns
graph_outbox
```

---

# 107. Logging and Privacy

Não logar:

- statement text;
- invalidation reason;
- Idempotency-Key;
- full request body.

Structured logging pode usar:

```text
operation
checkpoint_id
scope when safe
context_key when safe
revision
lifecycle
```

---

# 108. Human Validation

Executar pela API real.

## Step A — Create default stream

Request sem `context_key`.

Confirmar:

```text
context_key = main
revision = 1
lifecycle = active
```

---

## Step B — Current lookup

```http
GET /api/v1/context/checkpoints/current
?scope=project:contextual-memory-validation
&context_key=main
```

Confirmar conteúdo.

---

## Step C — Restart

Reiniciar Sofias Memory.

Repetir current lookup.

Confirmar continuidade.

---

## Step D — Second stream

Criar:

```text
context_key=feature:v1
```

Confirmar que:

```text
main
feature:v1
```

possuem ACTIVE independentemente.

---

## Step E — Duplicate create while ACTIVE exists

Tentar novo Create em:

```text
feature:v1
```

com Idempotency-Key nova.

Esperado:

```text
409 CONTEXT_STATE_CONFLICT
```

---

## Step F — Replay

Repetir a criação original com mesma Idempotency-Key.

Esperado:

```text
same checkpoint_id
```

não conflict.

---

## Step G — Invalidate

Invalidar `feature:v1`.

Confirmar:

```text
GET by id -> invalidated
GET current -> 404
```

---

## Step H — Re-establish current

Criar novo checkpoint para:

```text
feature:v1
```

com nova key e snapshot completo.

Esperado:

```text
old revision 1 INVALIDATED
new revision 2 ACTIVE
```

Confirmar:

```text
old.superseded_by = null
```

---

## Step I — Forget current

Esquecer revision 2.

Confirmar:

```text
GET current -> 404
```

e tombstone sem conteúdo semântico.

---

## Step J — Re-establish again

Criar novo current.

Esperado:

```text
revision 3 ACTIVE
```

Não revision 1 nem revision 2.

---

## Step K — Regression

Confirmar:

- Episodes continuam funcionando;
- Cognitive Memory continua funcionando;
- Sessions continuam funcionando;
- nenhum PipelineRun contextual;
- nenhum graph_outbox contextual.

---

# 109. Acceptance Criteria

## AC-002.1

É possível criar checkpoint para:

```text
(scope, context_key)
```

quando o stream não possui ACTIVE.

## AC-002.2

`context_key` omitido resolve para:

```text
main
```

## AC-002.3

Response retorna `context_key` resolvido.

## AC-002.4

Primeiro checkpoint de stream novo recebe:

```text
revision = 1
lifecycle = active
```

## AC-002.5

`current_state` é obrigatório no Create e permanece obrigatório no futuro Supersede;
o result aceita ausência legítima por tombstone/propagation, sem relaxar o input.

## AC-002.6

Statements preservam ordenação.

## AC-002.7

Current lookup retorna ACTIVE.

## AC-002.8

Create no mesmo stream enquanto ACTIVE existe produz conflict.

## AC-002.9

Mesmo `context_key` pode existir em scopes diferentes.

## AC-002.10

Um projeto pode possuir múltiplos Context Streams simultâneos.

## AC-002.11

ACTIVE invalidado deixa stream sem current checkpoint.

## AC-002.12

ACTIVE forgotten deixa stream sem current checkpoint.

## AC-002.13

Nenhum histórico é reativado automaticamente.

## AC-002.14

Estado sobrevive a restart.

---

# 110. Additional Acceptance Criteria Introduced by the Amendment

## AC-002.15

Stream histórico sem ACTIVE aceita novo Create explícito.

## AC-002.16

Novo Create após histórico usa:

```text
MAX(revision) + 1
```

## AC-002.17

Revision nunca é reutilizada, mesmo após todas as revisions serem FORGOTTEN:
tombstones preservam scope/context_key/revision e contam para MAX+1 após restart.

## AC-002.18

Recovery Create não cria `superseded_by` no checkpoint histórico.

## AC-002.19

Dois recovery Creates concorrentes produzem exatamente um novo ACTIVE.

## AC-002.20

Loser de recovery-create race não cria revision adicional.

---

# 111. Additional Slice Invariants

Além dos Acceptance Criteria:

```text
no Project table
no ContextStream table
no embedding
no semantic checkpoint search
no Evidence
no supersede endpoint
no Contextual Recall
no Neo4j
no PipelineRun
no automatic historical reactivation
```

devem permanecer verdadeiros ao final do Slice.

---

# 112. Required Test Commands

Executar o conjunto aplicável:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

Além da suite PostgreSQL opt-in dedicada.

Migration:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Não declarar PASS do que não foi executado.

---

# 113. Findings Classification

Durante implementação:

## BLOCKER

Exemplos:

- single-ACTIVE não garantido;
- duplicate current sob race;
- idempotency replay vira state conflict;
- Forget deixa semantic content;
- historical checkpoint reativado;
- revision reutilizada;
- recovery race cria duas revisions;
- Contract exige redesign.

## IMPORTANT

Afeta diretamente robustez/objetivo do Slice.

## MINOR

Não impede valor entregue.

## DEFERRED

Pertence a outro Slice ou feature.

---

# 114. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- criar Project table;
- criar ContextStream table;
- alterar context_key grammar;
- aceitar global;
- mudar lifecycle;
- implementar PATCH;
- implementar Supersede antecipadamente;
- adicionar Evidence;
- adicionar embedding;
- adicionar Redis/lock externo;
- usar Neo4j;
- alterar public Contract;
- criar migration destrutiva;
- reativar checkpoint histórico;
- tornar recovery Create um supersede implícito.

---

# 115. Expected File Impact

Indicativo:

```text
migrations/versions/<next>_create_context_checkpoints.py

sofias_memory/domain/
    contextual_memory.py

sofias_memory/schemas/
    context_checkpoints.py

sofias_memory/api/routes/
    context_checkpoints.py

sofias_memory/services/
    context_checkpoints.py

sofias_memory/infrastructure/postgres/models/
    context_checkpoint.py
    context_checkpoint_statement.py
    context_checkpoint_provenance.py

sofias_memory/infrastructure/postgres/repositories/
    context_checkpoints.py
    context_checkpoint_statements.py
    context_checkpoint_provenance.py
```

Alterações prováveis:

```text
domain exports
models exports
repository exports
unit_of_work.py
app.py
schemas/common.py
tests
```

Não forçar essa árvore se o código real indicar solução local mais coerente.

---

# 116. Implementation Order

Ordem recomendada:

```text
1. verify SM-CTX-001 completion
2. verify generic contextual idempotency ledger
3. domain primitives
4. migration
5. ORM models
6. repositories + UoW
7. schemas
8. create-current service
9. revision allocation
10. create HTTP route
11. current/get
12. list + pagination
13. invalidate
14. forget/tombstone
15. idempotency replay hardening
16. brand-new race tests
17. recovery-create race tests
18. restart tests
19. OpenAPI protection
20. cross-feature regression
21. Human Validation
```

---

# 117. Slice Exit Criteria

SM-CTX-002 estará tecnicamente concluído quando:

```text
objective implemented
AC-002.1..20 satisfied
single ACTIVE DB invariant proven
brand-new create race proven
recovery create race proven
idempotency replay precedence proven
revision monotonicity proven
statement ordering proven
tombstone proven
restart durability proven
no historical reactivation
no graph side effect
no PipelineRun side effect
no BLOCKER remains
```

---

# 118. Expected Implementation Report

O executor deverá retornar:

```text
SM-CTX-002 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- previous migration head:
- new migration head:
- tables/enums:
- constraints/indexes:
- revision allocation strategy:

Dependencies verified
- SM-CTX-001:
- contextual idempotency ledger:

Verified
- unit:
- integration:
- HTTP:
- brand-new create race:
- recovery-create race:
- same-key race:
- revision monotonicity:
- restart:
- graph isolation:
- pipeline isolation:

Acceptance Criteria
- AC-002.1 PASS/FAIL
- AC-002.2 PASS/FAIL
- AC-002.3 PASS/FAIL
- AC-002.4 PASS/FAIL
- AC-002.5 PASS/FAIL
- AC-002.6 PASS/FAIL
- AC-002.7 PASS/FAIL
- AC-002.8 PASS/FAIL
- AC-002.9 PASS/FAIL
- AC-002.10 PASS/FAIL
- AC-002.11 PASS/FAIL
- AC-002.12 PASS/FAIL
- AC-002.13 PASS/FAIL
- AC-002.14 PASS/FAIL
- AC-002.15 PASS/FAIL
- AC-002.16 PASS/FAIL
- AC-002.17 PASS/FAIL
- AC-002.18 PASS/FAIL
- AC-002.19 PASS/FAIL
- AC-002.20 PASS/FAIL

Findings

BLOCKER
- none / ...

IMPORTANT
- none / ...

MINOR
- none / ...

DEFERRED
- none / ...

Known issues
- ...

Human validation
- READY / NOT READY

Git
- branch:
- commit:
- working tree:
```

Não avançar automaticamente para SM-CTX-003.

---

# 119. Handoff to Executor

## Objective

Implementar:

```text
SM-CTX-002 — Context Streams and Initial Checkpoints
```

## Required entry condition

`SM-CTX-001` deve estar:

```text
DONE
```

e Human Validation concluída quando exigida.

## Important dependencies

`contextual_memory_idempotency` deve estar preparado para mutations heterogêneas de Contextual Project Memory.

Create de checkpoint significa:

```text
create current when no ACTIVE exists
```

e não apenas:

```text
create revision 1 once
```

## Scope

Somente:

```text
Context Stream identity
ContextCheckpoint
ContextStatements
Checkpoint provenance
Create current
Get
Current
List
Invalidate
Direct Forget
Revision allocation
Idempotency
Durability
```

## Out of Scope

```text
Supersede
expected-current stale concurrency
Evidence
Forget propagation
Contextual Recall
Knowledge composition
checkpoint embeddings
Neo4j
```

## Expected result

Ao final:

> o Sofias Memory consegue persistir e recuperar de forma durável exatamente um estado current por Context Stream, remover explicitamente esse current por Invalidate/Forget e posteriormente estabelecer uma nova revision current sem reativar histórico ou confundir essa operação com Supersede.
