# Sofias Memory — Contract v1: Memória Contextual Persistente por Projeto

**Document:** `05-SF-Contract-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED
**Version:** v1\
**Date:** 2026-11-17\
**Project:** Sofias Memory\
**Baseline:**
- `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md` — APPROVED

---

# 1. Purpose

Este Contract define o acordo público de interoperabilidade entre o **Sofias Memory** e consumidores da capability de **Memória Contextual Persistente por Projeto**.

O Contract estabelece:

- recursos públicos;
- ownership;
- responsabilidades;
- identidade;
- payloads conceituais;
- lifecycle observável;
- idempotência;
- concorrência;
- Evidence;
- Contextual Recall;
- Forget;
- error semantics;
- compatibility negotiation;
- versionamento;
- trust boundaries.

Este documento é normativo para o Contract `contextual_project_memory = "1"`.

---

# 2. Parties

## 2.1 Memory Service

**Sofias Memory**

Responsável por:

- persistência;
- validação estrutural;
- lifecycle;
- concorrência;
- idempotência;
- provenance;
- Evidence relationships;
- Contextual Recall;
- Forget semantics;
- compatibility negotiation.

---

## 2.2 Consumer

Qualquer aplicação que utilize o Contract.

Exemplos:

- assistente conversacional;
- agent runtime;
- aplicação;
- automation;
- serviço interno.

O Consumer continua responsável por:

- decidir o que deve ser persistido;
- extrair candidatos;
- obter confirmação quando necessária;
- planejamento;
- execução;
- permissões;
- composição de prompt;
- UX.

---

# 3. Ownership

| Concern | Authority |
|---|---|
| Episode identity | Sofias Memory |
| Episode lifecycle | Sofias Memory |
| ContextCheckpoint identity | Sofias Memory |
| Checkpoint current state | Sofias Memory |
| Evidence relationships | Sofias Memory |
| Project scope supplied | Consumer |
| Context stream supplied | Consumer |
| Episode content | Consumer |
| Checkpoint content | Consumer |
| Decision to persist | Consumer |
| Prompt composition | Consumer |
| Operational tasks | External system |
| Agent planning | Consumer/runtime |
| Permissions | Consumer/runtime |
| Tool execution | Consumer/runtime |

---

# 4. Authentication

A capability utiliza o mecanismo de autenticação vigente da instância.

No baseline atual:

```http
X-API-Key: <api-key>
```

A existência de:

```text
project:<key>
```

não cria autorização adicional.

Project Scope é namespace contextual.

Não é security boundary.

---

# 5. Trust Boundary

Todo conteúdo persistido e recuperado por este Contract é:

```text
untrusted context
```

Memory content MUST NOT:

- modificar Policy;
- criar Permission;
- conceder autorização;
- substituir system instructions;
- iniciar execução privilegiada;
- controlar Agent Runtime.

O Consumer MUST tratar conteúdo recuperado como dados.

---

# 6. Compatibility Negotiation

O suporte ao Contract MUST ser anunciado explicitamente em `GET /api/v1/info`,
somente depois de passar o activation gate de `SM-CTX-008`.

Campos de negociação (dentro de `data` do success envelope padrão):

```json
{
  "api_contract_version": "1",
  "contracts": {
    "contextual_project_memory": "1"
  }
}
```

## 6.1 Capability Amendment — 2026-11-20

Este amendment substitui os exemplos anteriores de nomes de capabilities.
A ativação de Contract v1 MUST anunciar exatamente estas cinco capabilities
contextuais, preservando as capabilities de outras features já existentes:

```json
{
  "capabilities": [
    "contextual_project_memory.episodes",
    "contextual_project_memory.checkpoints",
    "contextual_project_memory.recall",
    "contextual_project_memory.explain",
    "contextual_project_memory.history"
  ]
}
```

`contracts["contextual_project_memory"] = "1"` e essas cinco capabilities MUST NOT
ser anunciados antes do activation gate completo. `api_contract_version` permanece
`"1"`; a evolução é aditiva e preserva o Contract cognitivo existente.
Evidence e Forget propagation são garantias integrantes do Contract, sem
capabilities independentes. Os nomes públicos são definidos aqui, não nos handoffs.

Consumers MUST NOT inferir suporte através de:

```text
application_version >= X
```

---

# 7. Public Resource Model

O Contract introduz dois recursos públicos:

```text
Episode
ContextCheckpoint
```

e uma capability:

```text
Contextual Recall
```

---

# 8. Project Scope Contract

Todos os recursos contextuais pertencem a um Project Scope.

Formato:

```text
project:<key>
```

A validação e canonicalização do Project Scope MUST seguir o contrato vigente de Cognitive Memory.

Este Contract não redefine essa sintaxe.

---

# 9. Context Stream Contract

ContextCheckpoint pertence adicionalmente a uma linha lógica de continuidade:

```text
context_key
```

A identidade lógica do stream é:

```text
(scope, context_key)
```

---

## 9.1 Canonical `context_key`

O Contract v1 adota:

```text
[a-z0-9][a-z0-9._:-]{0,127}
```

Portanto:

- mínimo: 1 caractere;
- máximo: 128 caracteres;
- somente ASCII lowercase;
- espaços não são permitidos;
- comparação é exata.

Exemplos válidos:

```text
main
feature:contextual-memory
release:0.8.0
incident:retrieval-001
research:cognee
```

---

## 9.2 Default

Quando `context_key` for omitido:

```text
main
```

é utilizado.

A resposta pública MUST retornar o valor resolvido.

---

# 10. Provenance Contract

Episode e ContextCheckpoint MUST possuir provenance.

O Contract reutiliza os seguintes `origin_kind`:

```text
user_asserted
tool_observed
imported
inferred
assistant_generated
```

`source_system` é obrigatório.

Exemplo:

```json
{
  "origin_kind": "user_asserted",
  "source_system": "sofias-personal-assistant",
  "conversation_ref": "conversation:42",
  "turn_ref": "turn:18"
}
```

Provider Session IDs não fazem parte desta provenance.

---

## 10.1 Confidence

`confidence` MAY ser fornecido.

Intervalo:

```text
0.0 <= confidence <= 1.0
```

Para:

```text
origin_kind = inferred
```

`confidence` MUST ser informado.

Confidence não concede autoridade adicional ao conteúdo.

---

# 11. Evidence Contract

Evidence conecta conteúdo derivado à informação que o sustenta.

Cada referência possui conceitualmente:

```text
source_type
source_id
availability
```

---

## 11.1 Internal Evidence Types — v1

O Contract v1 suporta inicialmente:

```text
session_entry
memory_item
episode
source
document
chunk
```

Novos tipos poderão ser adicionados de forma backward-compatible.

---

## 11.2 Evidence Reference

Request:

```json
{
  "source_type": "session_entry",
  "source_id": "uuid"
}
```

Response:

```json
{
  "evidence_id": "uuid",
  "source_type": "session_entry",
  "source_id": "uuid",
  "availability": "available"
}
```

Estados observáveis:

```text
available
unavailable
forgotten
```

---

## 11.3 Evidence Does Not Copy Source

Evidence MUST NOT armazenar uma cópia integral da fonte apenas para facilitar recall.

A referência identifica a fonte.

Conteúdo derivado poderá existir no recurso contextual, mas fica sujeito a Forget propagation.

---

## 11.4 External Provenance

Uma origem externa MAY constar na provenance.

Entretanto, uma referência externa não participa automaticamente de lifecycle-aware Forget propagation porque o Sofias Memory não possui autoridade sobre seu lifecycle.

O Consumer continua responsável por comunicar qualquer invalidação externa que exija alteração contextual.

## 11.5 Explicit Evidence Project Isolation

Para owner contextual em `project:X`, Evidence do tipo `episode` MUST apontar
para Episode do mesmo `project:X`. Evidence `memory_item` em `project:Y` exige
`Y == X`. Um MemoryItem `global` MAY ser Evidence somente por referência explícita;
isso não permite inclusão automática de Global Cognitive Memory no Recall (§46).
Não inferir project ownership para SessionEntry, Source, Document ou Chunk:
esses recursos seguem suas identidades e lifecycles próprios.
Uma referência que viole o matching de projeto é `422 INVALID_EVIDENCE_REFERENCE`.

---

# 12. Evidence Support Policy

Um conteúdo derivado pode declarar:

```text
all_required
any_sufficient
```

---

## 12.1 `all_required`

Todas as Evidence refs são necessárias.

Se qualquer uma deixar de estar disponível:

```text
support = lost
```

---

## 12.2 `any_sufficient`

Cada Evidence ref declarada é considerada suficiente independentemente.

Enquanto pelo menos uma permanecer disponível:

```text
support = available
```

---

## 12.3 No AI Decision

O Sofias Memory MUST NOT utilizar um LLM para decidir automaticamente se evidência restante ainda é suficiente no Contract v1.

A política é declarada pelo Consumer e aplicada deterministicamente pelo Memory.

---

# 13. Evidence Set

Modelo conceitual:

```json
{
  "mode": "all_required",
  "refs": [
    {
      "source_type": "session_entry",
      "source_id": "uuid"
    }
  ]
}
```

Evidence MAY ser omitida quando o conteúdo não for derivado de um recurso Memory existente e a provenance for suficiente.

---

# 14. Episode Public Model

Um Episode não esquecido possui conceitualmente:

```text
episode_id
scope
context_key?
kind
summary
situation?
reason?
outcome?
occurred_at
lifecycle
confidence?
support?
provenance
created_at
invalidated_at?
forgotten_at?
```

---

# 15. Episode Kind

O Contract v1 suporta:

```text
decision
investigation
experiment
incident
discovery
change
outcome
learning
milestone
```

Novos kinds exigem evolução explícita de Contract se alterarem comportamento esperado.

---

# 16. Episode Lifecycle

Estados:

```text
active
invalidated
forgotten
```

Transições:

```text
ACTIVE
  ├──> INVALIDATED
  └──> FORGOTTEN

INVALIDATED
  └──> FORGOTTEN

FORGOTTEN
  └──> terminal
```

Episode não utiliza `superseded` como lifecycle normal.

---

# 17. Episode Create

## Endpoint

```http
POST /api/v1/episodes
```

## Required Headers

```http
X-API-Key: <key>
Idempotency-Key: <caller-generated-key>
Content-Type: application/json
```

`Idempotency-Key` é obrigatório para mutations deste Contract.

---

## 17.1 Request

Exemplo:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "kind": "decision",
  "summary": "Episode foi escolhido como agregado first-class.",
  "situation": "Foram avaliadas duas estratégias para memória episódica.",
  "reason": "Experiência histórica possui lifecycle diferente de current semantic truth.",
  "outcome": "MemoryItem continua restrito a profile e semantic.",
  "occurred_at": "2026-11-17T21:00:00Z",
  "support": {
    "mode": "all_required",
    "refs": [
      {
        "source_type": "session_entry",
        "source_id": "uuid"
      }
    ]
  },
  "provenance": {
    "origin_kind": "user_asserted",
    "source_system": "sofias-personal-assistant",
    "conversation_ref": "conversation:42",
    "turn_ref": "turn:18"
  }
}
```

`context_key` é opcional para Episode.

Quando omitido, o Episode é considerado relevante ao Project Scope como um todo.

---

## 17.2 Response

```http
201 Created
```

Exemplo conceitual:

```json
{
  "episode_id": "uuid",
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "kind": "decision",
  "summary": "Episode foi escolhido como agregado first-class.",
  "situation": "...",
  "reason": "...",
  "outcome": "...",
  "occurred_at": "2026-11-17T21:00:00Z",
  "lifecycle": "active",
  "support": {
    "mode": "all_required",
    "evidence": [
      {
        "evidence_id": "uuid",
        "source_type": "session_entry",
        "source_id": "uuid",
        "availability": "available"
      }
    ]
  },
  "provenance": {
    "origin_kind": "user_asserted",
    "source_system": "sofias-personal-assistant",
    "conversation_ref": "conversation:42",
    "turn_ref": "turn:18"
  },
  "created_at": "2026-11-17T22:00:00Z"
}
```

---

# 18. Episode Get

```http
GET /api/v1/episodes/{episode_id}
```

ACTIVE e INVALIDATED podem retornar conteúdo.

FORGOTTEN retorna tombstone.

Exemplo:

```json
{
  "episode_id": "uuid",
  "lifecycle": "forgotten",
  "summary": null,
  "situation": null,
  "reason": null,
  "outcome": null,
  "support": null,
  "forgotten_at": "2026-11-18T10:00:00Z"
}
```

---

# 19. Episode List

```http
GET /api/v1/episodes
```

Filtros mínimos:

```text
scope
context_key
kind
lifecycle
limit
cursor
```

`scope` é obrigatório.

Normal listing SHOULD retornar apenas:

```text
active
```

quando `lifecycle` não for explicitamente solicitado.

---

# 20. Episode Invalidate

```http
POST /api/v1/episodes/{episode_id}/invalidate
```

Headers:

```http
Idempotency-Key: ...
```

Request:

```json
{
  "reason": "The original episode was found to be inaccurate.",
  "replacement_episode_id": "optional-uuid"
}
```

`replacement_episode_id` é opcional.

Quando fornecido:

- deve existir;
- não pode estar FORGOTTEN;
- deve pertencer ao mesmo Project Scope.

A operação não cria automaticamente o replacement.

---

## 20.1 State Behaviour

```text
ACTIVE      -> INVALIDATED
INVALIDATED -> 200 state no-op
FORGOTTEN   -> conflict
```

INVALIDATED Episodes são excluídos de Contextual Recall normal.

---

# 21. Episode Forget

```http
POST /api/v1/episodes/{episode_id}/forget
```

Headers:

```http
Idempotency-Key: ...
```

Semântica:

```text
ACTIVE      -> FORGOTTEN
INVALIDATED -> FORGOTTEN
FORGOTTEN   -> 200 state no-op
```

Forget MUST scrub conteúdo recuperável e Evidence refs conforme a política de tombstone.

Não existe unforget.

---

# 22. Episode Mutation Restriction

Não existe no Contract v1:

```http
PATCH /episodes/{episode_id}
PUT   /episodes/{episode_id}
```

Conteúdo semântico não é atualizado in-place.

Correção utiliza:

```text
invalidate + optional corrected Episode
```

---

# 23. Context Statement

ContextCheckpoint é composto por unidades semânticas independentes.

Cada unidade utiliza:

```text
ContextStatement
```

Modelo conceitual:

```json
{
  "statement_id": "server-generated-uuid",
  "text": "Implementar a persistência do novo recurso.",
  "support": {
    "mode": "all_required",
    "evidence": [
      {
        "evidence_id": "uuid",
        "source_type": "session_entry",
        "source_id": "uuid",
        "availability": "available"
      }
    ]
  }
}
```

No request, `statement_id` não é fornecido.

---

# 24. ContextCheckpoint Public Model

Modelo:

```text
checkpoint_id
scope
context_key
revision
lifecycle
objective?
current_state
approved_decisions[]
pending_work[]
open_items[]
blockers[]
next_action?
provenance
created_at
superseded_at?
superseded_by?
invalidated_at?
forgotten_at?
```

---

# 25. ContextCheckpoint Content

## Required Mutation Input

```text
scope
context_key
current_state
provenance
```

## Optional

```text
objective
approved_decisions[]
pending_work[]
open_items[]
blockers[]
next_action
```

Arrays MAY ser vazios.

Create e Supersede MUST exigir `current_state`. O modelo de resultado de um
checkpoint persistido MUST aceitar `current_state` null/ausente quando propagation
o removeu legitimamente e invalidou o checkpoint (§57), ou em tombstone FORGOTTEN.
Essa assimetria de input/result não autoriza mutations sem `current_state`.

---

# 26. ContextCheckpoint Lifecycle

Estados:

```text
active
superseded
invalidated
forgotten
```

Transições:

```text
ACTIVE
  ├──> SUPERSEDED
  ├──> INVALIDATED
  └──> FORGOTTEN

SUPERSEDED
  ├──> INVALIDATED
  └──> FORGOTTEN

INVALIDATED
  └──> FORGOTTEN

FORGOTTEN
  └──> terminal
```

---

# 27. ContextCheckpoint Create

## Endpoint

```http
POST /api/v1/context/checkpoints
```

Headers:

```http
X-API-Key: <key>
Idempotency-Key: <key>
Content-Type: application/json
```

---

## 27.1 Behaviour

Este endpoint cria um novo ACTIVE checkpoint quando não existe ACTIVE em:

```text
(scope, context_key)
```

Se já existir ACTIVE:

```http
409 CONTEXT_STATE_CONFLICT
```

O caller deve utilizar Supersede.

Para stream novo, `revision = 1`. Para stream histórico sem ACTIVE, inclusive
após Invalidate/Forget, `revision = MAX(existing revision) + 1`, considerando
tombstones. Recovery Create cria novo recurso, sem reativar histórico nem criar
lineage de supersession para o checkpoint invalidado/esquecido. Esta redação
reflete a semântica já aprovada no handoff `10` e no ADR-0018.

---

## 27.2 Request

Exemplo:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "objective": {
    "text": "Implementar memória contextual persistente.",
    "support": {
      "mode": "all_required",
      "refs": [
        {
          "source_type": "session_entry",
          "source_id": "uuid"
        }
      ]
    }
  },
  "current_state": {
    "text": "PRD e Spec estão aprovados."
  },
  "approved_decisions": [
    {
      "text": "Episode será first-class."
    }
  ],
  "pending_work": [
    {
      "text": "Definir o Contract público."
    }
  ],
  "open_items": [],
  "blockers": [],
  "next_action": {
    "text": "Revisar o Contract."
  },
  "provenance": {
    "origin_kind": "assistant_generated",
    "source_system": "sofias-personal-assistant"
  }
}
```

---

## 27.3 Response

```http
201 Created
```

O servidor atribui:

```text
checkpoint_id
revision = 1 for a new stream; MAX(existing revision) + 1 otherwise
statement_ids
created_at
lifecycle = active
```

---

# 28. Current Checkpoint Get

```http
GET /api/v1/context/checkpoints/current
    ?scope=project:sofias-memory
    &context_key=feature:contextual-memory
```

Se existir:

```http
200 OK
```

Se nenhum ACTIVE existir:

```http
404 CURRENT_CHECKPOINT_NOT_FOUND
```

O endpoint MUST NOT retornar automaticamente checkpoint SUPERSEDED.

---

# 29. ContextCheckpoint Get

```http
GET /api/v1/context/checkpoints/{checkpoint_id}
```

FORGOTTEN retorna tombstone.

---

# 30. ContextCheckpoint List

```http
GET /api/v1/context/checkpoints
```

Filtros:

```text
scope
context_key
lifecycle
limit
cursor
```

`scope` é obrigatório.

---

# 31. ContextCheckpoint Supersede

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
```

O `{checkpoint_id}` representa explicitamente o checkpoint que o caller acredita ser current.

Headers:

```http
Idempotency-Key: ...
```

---

## 31.1 Preconditions

O target MUST:

- existir;
- estar ACTIVE;
- pertencer ao `scope/context_key` do replacement.

Caso contrário:

```http
409 CONTEXT_STATE_CONFLICT
```

---

## 31.2 Full Replacement

O request contém o **novo checkpoint completo**.

Não é patch/diff.

Exemplo:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "objective": {
    "text": "Implementar memória contextual persistente."
  },
  "current_state": {
    "text": "Contract aprovado e Architecture pendente."
  },
  "approved_decisions": [
    {
      "text": "Episode será first-class."
    },
    {
      "text": "Contextual Recall utilizará intents explícitas."
    }
  ],
  "pending_work": [
    {
      "text": "Definir Architecture."
    }
  ],
  "open_items": [],
  "blockers": [],
  "next_action": {
    "text": "Produzir Architecture."
  },
  "provenance": {
    "origin_kind": "assistant_generated",
    "source_system": "sofias-personal-assistant"
  }
}
```

---

## 31.3 Atomic Result

Successful operation:

```text
old ACTIVE      -> SUPERSEDED
new checkpoint  -> ACTIVE
new revision    -> old revision + 1
```

A transição é atômica.

Response:

```http
201 Created
```

---

# 32. Stale Writer Behaviour

Exemplo:

```text
A = ACTIVE

Writer 1:
A -> B

Writer 2:
tries A -> C
```

Resultado:

```text
B = ACTIVE
C = not created
```

Writer 2 recebe:

```http
409 CONTEXT_STATE_CONFLICT
```

A resposta SHOULD incluir o identificador do current checkpoint atual quando seguro e aplicável.

---

# 33. Checkpoint Invalidate

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/invalidate
```

Request:

```json
{
  "reason": "Checkpoint is no longer considered reliable."
}
```

State behaviour:

```text
ACTIVE      -> INVALIDATED
SUPERSEDED  -> INVALIDATED
INVALIDATED -> 200 state no-op
FORGOTTEN   -> conflict
```

Se ACTIVE for invalidado:

```text
no current checkpoint exists
```

Nenhum histórico é reativado automaticamente.

---

# 34. Checkpoint Forget

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/forget
```

State behaviour:

```text
ACTIVE      -> FORGOTTEN
SUPERSEDED  -> FORGOTTEN
INVALIDATED -> FORGOTTEN
FORGOTTEN   -> 200 state no-op
```

Se ACTIVE for esquecido:

```text
no automatic fallback
```

---

# 35. Checkpoint Mutation Restriction

Não existem:

```http
PATCH /context/checkpoints/{id}
PUT   /context/checkpoints/{id}
```

Cada mudança semântica cria uma nova revisão através de Supersede.

---

# 36. Idempotency Contract

Todas as mutations deste Contract MUST exigir:

```http
Idempotency-Key
```

Abrange:

- Episode Create;
- Episode Invalidate;
- Episode Forget;
- Checkpoint Create;
- Checkpoint Supersede;
- Checkpoint Invalidate;
- Checkpoint Forget.

---

## 36.1 Same Key + Same Request

MUST retornar o mesmo resultado lógico original.

Nenhum recurso duplicado é criado.

---

## 36.2 Same Key + Different Request

MUST retornar:

```http
409 IDEMPOTENCY_CONFLICT
```

---

## 36.3 Resolution Order

Idempotency replay MUST ser resolvido antes de rejeitar uma mutation por lifecycle atual.

Isso garante que:

```text
successful request
response lost
retry arrives after state changed by original request
```

continue reproduzindo o resultado original.

---

# 37. Contextual Recall Endpoint

```http
POST /api/v1/context/recall
```

---

# 38. Contextual Recall Request

Modelo:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "intent": "resume",
  "query": "Onde paramos?",
  "datasets": [],
  "include_evidence": false,
  "limits": {
    "memories": 8,
    "episodes": 8,
    "historical_checkpoints": 4,
    "knowledge": 8,
    "evidence": 12
  }
}
```

---

# 39. Recall Fields

## Required

```text
scope
intent
query
```

## Optional

```text
context_key
datasets
include_evidence
limits
```

Quando `context_key` for omitido:

```text
main
```

---

# 40. Recall Intents

Contract v1 suporta:

```text
resume
explain
history
context
```

---

# 41. `resume`

Objetivo:

```text
reconstruir estado atual de continuidade
```

O current ContextCheckpoint possui prioridade comportamental.

Caso não exista:

- recall não falha;
- `continuity_available=false`;
- warning é retornado.

---

# 42. `explain`

Objetivo:

```text
recuperar por que uma decisão/estado existe
```

Pode incluir:

- current Cognitive Memory;
- related Episodes;
- Evidence;
- Knowledge Memory quando solicitado.

---

# 43. `history`

Objetivo:

```text
recuperar evolução histórica
```

Pode incluir:

- Episodes;
- historical ContextCheckpoints;
- relevant semantic history.

Material histórico MUST ser identificado como histórico.

---

# 44. `context`

Objetivo:

```text
recuperar contexto geral seletivo do projeto
```

Pode combinar os diferentes planos dentro dos limites fornecidos.

---

# 45. Knowledge Memory in Contextual Recall

Não existe relação implícita entre:

```text
project:<key>
```

e `Dataset`.

Se Knowledge Memory precisar participar do Contextual Recall, o Consumer MUST fornecer explicitamente:

```json
{
  "datasets": [
    "docs",
    "architecture"
  ]
}
```

Quando `datasets` estiver vazio ou ausente, Contextual Recall MUST NOT inferir datasets por nome ou metadata.

---

# 46. Global Cognitive Memory

Contract v1 NÃO incorpora automaticamente:

```text
global
```

ao recall de:

```text
project:<key>
```

O Consumer pode consultar Global Cognitive Memory separadamente.

Auto-composition entre global + project fica fora do Contract v1.

---

# 47. Session Evidence

Contextual Recall MUST NOT realizar replay integral de Sessions.

SessionEntry participa quando:

- referenciada como Evidence;
- explicitamente expandida pelo recall.

---

# 48. `include_evidence`

Quando:

```json
{
  "include_evidence": false
}
```

o resultado retorna Evidence refs sem expansão textual completa.

Quando:

```json
{
  "include_evidence": true
}
```

o serviço MAY expandir conteúdo disponível das evidências internas, respeitando `limits.evidence`.

Evidence esquecida nunca é expandida.

---

# 49. Contextual Recall Response

Modelo conceitual:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "intent": "resume",
  "continuity_available": true,
  "current_checkpoint": {},
  "current_memories": [],
  "episodes": [],
  "historical_checkpoints": [],
  "knowledge_references": [],
  "evidence": [],
  "warnings": [],
  "degraded_sources": []
}
```

---

# 50. Recall Result Classification

Todo item retornado MUST permitir distinguir sua classe:

```text
current_semantic
historical_episode
current_checkpoint
historical_checkpoint
knowledge_reference
evidence
```

Um consumer nunca deve precisar inferir current/historical apenas pelo texto.

---

# 51. Recall Warnings

Contract v1 define warnings estáveis:

```text
NO_CURRENT_CHECKPOINT
PARTIAL_CONTEXT
EVIDENCE_UNAVAILABLE
AMBIGUOUS_CURRENT_TRUTH
```

Warnings não significam necessariamente que a operação falhou.

## 51.1 Persistence Lifecycle and Recall Eligibility

`persistence lifecycle != recall eligibility` quando support explícito estiver
atualmente insuficiente. Ordinary invalidation de uma Evidence pode deixar um
derivado fisicamente ACTIVE sem disparar destructive Forget propagation.
GET direto reflete o recurso real e availability atual, sem mutação durante read.
`resume`/`context` MUST excluir Episodes unsupported e statements unsupported do
contexto utilizável; sem `current_state` elegível, não retornar current checkpoint
e sinalizar `continuity_available=false`. Usar `EVIDENCE_UNAVAILABLE` e
`PARTIAL_CONTEXT`; `NO_CURRENT_CHECKPOINT` também se aplica à continuidade ausente.
`history` MAY mostrar conteúdo histórico elegível remanescente com lifecycle e
availability explícitos, sem apresentá-lo como current truth.
Após successful Forget, conteúdo que deveria ter sido scrubbed é invariant
failure: fail closed, não expor seu conteúdo semântico e não reparar durante read.

---

# 52. Recall Degradation

Se uma fonte opcional falhar e for possível retornar contexto parcial:

```http
200 OK
```

com:

```json
{
  "warnings": ["PARTIAL_CONTEXT"],
  "degraded_sources": ["knowledge"]
}
```

O serviço MUST NOT inventar conteúdo para substituir a fonte ausente.

Se o armazenamento autoritativo necessário à operação estiver indisponível:

```http
503
```

---

# 53. Bounded Recall

O serviço MUST aplicar limites máximos server-side mesmo quando o Consumer solicitar valores maiores.

O Contract garante bounded output.

Os valores máximos exatos pertencem à configuração/Architecture, não ao Contract v1.

---

# 54. No Final LLM Answer Requirement

Contextual Recall retorna evidências/contexto estruturado.

Ele MUST NOT exigir que um LLM gere resposta final.

Um consumidor pode utilizar o resultado em seu próprio ContextBuilder.

---

# 55. Forget Propagation Contract

Quando uma Evidence interna sofre Forget, o Sofias Memory deve aplicar a política de suporte aos derivados relacionados.

A garantia passa a valer quando a operação de Forget da fonte estiver concluída com sucesso.

---

# 56. Episode Evidence Loss

Se:

```text
support.mode = all_required
```

e uma Evidence é esquecida:

```text
Episode support becomes unavailable
```

O Episode MUST deixar de participar do recall normal.

O serviço MAY:

- invalidar o Episode;
- scrubbar conteúdo dependente;

conforme a política estrutural definida na Architecture.

O comportamento público obrigatório é:

> conteúdo não sustentado não permanece recuperável.

---

## 56.1 `any_sufficient`

Enquanto ao menos uma Evidence permanecer AVAILABLE:

```text
Episode MAY remain ACTIVE
```

A provenance/evidence response MUST refletir a fonte removida como indisponível ou removida conforme Forget semantics.

---

# 57. Checkpoint Evidence Loss

ContextStatement é avaliado individualmente.

Quando perde suporte:

```text
statement becomes unavailable
```

O conteúdo desse statement MUST deixar de ser retornado em recall.

---

## 57.1 Partial Checkpoint

Se outros statements permanecerem suportados, o checkpoint MAY continuar utilizável.

---

## 57.2 Structural Minimum

Um ACTIVE checkpoint só pode permanecer current se após invalidation de statements ainda possuir:

```text
current_state
```

válido e suportado segundo seu contrato.

Se `current_state` perder suporte e não puder permanecer:

```text
checkpoint -> INVALIDATED
```

e:

```text
no automatic historical fallback
```

---

# 58. Evidence Dependency Cycles

Um recurso contextual MUST NOT criar dependência de Evidence:

- direta sobre si próprio;
- indireta que forme ciclo.

Exemplo proibido:

```text
Episode A -> Episode B -> Episode A
```

A mutation deve falhar com:

```http
422 INVALID_EVIDENCE_CYCLE
```

---

# 59. Direct Forget vs Evidence Forget

Duas operações são semanticamente diferentes.

### Direct Forget

Caller esquece o próprio Episode/Checkpoint.

### Evidence Forget

Uma fonte referenciada é esquecida e seus derivados são reavaliados.

Ambos devem impedir resurrection de conteúdo.

---

# 60. No Restore

Contract v1 não oferece:

```text
unforget
restore forgotten Episode
restore forgotten Checkpoint
```

FORGOTTEN é terminal.

---

# 61. Error Envelope

Os endpoints seguem o error envelope padrão do Sofias Memory.

Exemplo conceitual:

```json
{
  "error": {
    "code": "CONTEXT_STATE_CONFLICT",
    "message": "Context checkpoint state changed.",
    "details": {}
  }
}
```

Mensagens textuais não são Contract estável.

`code` e semântica são.

---

# 62. Error Codes

## Authentication

```text
MISSING_API_KEY
INVALID_API_KEY
```

---

## Validation

```text
INVALID_REQUEST
INVALID_SCOPE
INVALID_CONTEXT_KEY
INVALID_EPISODE_KIND
INVALID_EVIDENCE_REFERENCE
INVALID_EVIDENCE_CYCLE
INVALID_SUPPORT_POLICY
```

HTTP:

```text
422
```

quando aplicável.

---

## Not Found

```text
EPISODE_NOT_FOUND
CHECKPOINT_NOT_FOUND
CURRENT_CHECKPOINT_NOT_FOUND
EVIDENCE_NOT_FOUND
```

HTTP:

```text
404
```

---

## Conflict

```text
IDEMPOTENCY_CONFLICT
CONTEXT_STATE_CONFLICT
EPISODE_STATE_CONFLICT
CHECKPOINT_STATE_CONFLICT
EVIDENCE_UNAVAILABLE
```

HTTP:

```text
409
```

---

## Dependency

```text
DEPENDENCY_UNAVAILABLE
```

HTTP:

```text
503
```

quando a operação não puder degradar com segurança.

---

# 63. Retry Semantics

## Safe to retry with same Idempotency-Key

Após:

- timeout;
- connection reset;
- unknown response state;
- `503` antes de resultado conhecido.

---

## Do not blind retry

Após:

```text
409 IDEMPOTENCY_CONFLICT
409 CONTEXT_STATE_CONFLICT
409 EPISODE_STATE_CONFLICT
409 CHECKPOINT_STATE_CONFLICT
422 ...
```

O caller deve primeiro resolver a causa.

---

# 64. Listing and Pagination

List endpoints MUST utilizar bounded pagination.

Modelo:

```text
limit
cursor
```

Offset pagination não faz parte do Contract obrigatório.

Ordenação padrão:

### Episodes

```text
occurred_at DESC
episode_id ASC
```

como tie-breaker determinístico.

### Checkpoints

```text
revision DESC
checkpoint_id ASC
```

dentro de `(scope, context_key)`.

---

# 65. Time Semantics

Todos os timestamps públicos gerados pelo servidor são UTC.

Formato:

```text
RFC 3339 / ISO 8601
```

Episode possui:

```text
occurred_at
```

definido pelo caller.

`occurred_at` MAY estar no passado.

Future timestamps além de tolerância operacional razoável SHOULD ser rejeitados segundo validação definida na implementação.

---

# 66. Revision Semantics

ContextCheckpoint `revision` é monotônica dentro de:

```text
(scope, context_key)
```

Exemplo:

```text
1
2
3
4
```

Revision nunca é reutilizada.

Invalidation ou Forget não reduz a sequência.

---

# 67. Tombstone Contract

FORGOTTEN resources MAY preservar somente o necessário para:

- identidade;
- lifecycle;
- timestamps;
- revision lineage;
- idempotency;
- structural audit.

Conteúdo semântico, Evidence recuperável e external provenance references sujeitos a Forget MUST ser scrubbed.

Checkpoint tombstone MUST preservar `checkpoint_id`, `scope`, `context_key`,
`revision`, `lifecycle`, `created_at`, `forgotten_at` e lineage/timestamps de
supersession ou invalidation quando aplicáveis. `scope` e `context_key` são
identidade estrutural do stream; não podem ser scrubbed. A constraint
`UNIQUE(scope, context_key, revision)` e a alocação MAX+1 incluem FORGOTTEN.
Essa retenção estrutural de Checkpoint difere do tombstone de Episode/Cognitive
Memory e não permite preservar statements, invalidation reason ou conteúdo textual.

---

# 68. Observability

A implementação SHOULD emitir métricas/eventos operacionais para:

```text
episode_created
episode_invalidated
episode_forgotten

checkpoint_created
checkpoint_superseded
checkpoint_invalidated
checkpoint_forgotten
checkpoint_conflict

context_recall
context_recall_partial
context_recall_no_checkpoint

evidence_invalidated
```

Nomes internos de métricas não fazem parte do Contract público.

---

# 69. Audit Boundary

Sofias Memory possui autoridade sobre:

- lifecycle do recurso;
- lineage de checkpoint;
- Evidence refs;
- provenance persistida;
- idempotency result;
- Forget result.

O Consumer possui autoridade sobre:

- motivo funcional pelo qual decidiu persistir;
- confirmação do usuário;
- Permission;
- uso do contexto;
- ações tomadas depois do recall.

---

# 70. Example — Project Continuity

Primeira interação:

```http
POST /api/v1/context/checkpoints
```

gera:

```text
checkpoint A
revision 1
ACTIVE
```

Nova Session consulta:

```http
POST /api/v1/context/recall
```

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "intent": "resume",
  "query": "Onde paramos?"
}
```

Resultado:

```json
{
  "continuity_available": true,
  "current_checkpoint": {
    "revision": 1,
    "current_state": {
      "text": "Spec aprovada."
    },
    "next_action": {
      "text": "Definir Contract."
    }
  }
}
```

---

# 71. Example — Decision History

Current semantic memory:

```text
A arquitetura atual usa B.
```

Historical Episode:

```text
Anteriormente foi escolhida A.
```

Contextual Recall `history` pode retornar ambos, classificados como:

```text
current_semantic
historical_episode
```

Não há conflito semântico obrigatório.

---

# 72. Example — Stale Checkpoint

Current:

```text
A
```

Writer 1:

```text
A -> B
```

Writer 2:

```text
A -> C
```

Writer 2 recebe:

```http
409 CONTEXT_STATE_CONFLICT
```

Current continua:

```text
B
```

---

# 73. Example — Forget Propagation

Episode:

```text
summary = "Arquitetura B foi escolhida."
support = all_required
Evidence = Memory X
```

Memory X é esquecida.

Após o Forget da Evidence ser concluído:

```text
Episode não pode continuar retornando
"Arquitetura B foi escolhida."
```

O serviço aplica a política de invalidation/scrub correspondente.

---

# 74. Example — Independent Evidence

Episode:

```text
support = any_sufficient

Evidence:
- SessionEntry A
- Document B
```

SessionEntry A deixa de estar disponível.

Document B permanece disponível.

O Episode pode continuar recuperável.

Seu Evidence state deve refletir a mudança.

---

# 75. Public Surface Summary

## Episodes

```http
POST /api/v1/episodes
GET  /api/v1/episodes
GET  /api/v1/episodes/{episode_id}
POST /api/v1/episodes/{episode_id}/invalidate
POST /api/v1/episodes/{episode_id}/forget
```

---

## Context Checkpoints

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

## Contextual Recall

```http
POST /api/v1/context/recall
```

---

# 76. Explicitly Forbidden Surface — v1

Não devem existir no Contract v1:

```http
PATCH /api/v1/episodes/{id}
PUT   /api/v1/episodes/{id}

PATCH /api/v1/context/checkpoints/{id}
PUT   /api/v1/context/checkpoints/{id}
```

Também não fazem parte:

```text
automatic promote
automatic distill
automatic reconcile
automatic restore
```

---

# 77. Backward Compatibility

Mudanças backward-compatible MAY incluir:

- novos campos opcionais em responses;
- novos Episode kinds que não alterem consumidores existentes;
- novas warning codes;
- novos Evidence source types;
- novas capabilities anunciadas separadamente.

Consumers SHOULD ignorar campos desconhecidos em responses quando não forem necessários ao contrato utilizado.

---

# 78. Breaking Changes

Exigem novo Contract version:

```text
contextual_project_memory = "2"
```

quando alterarem, entre outros:

- significado de Episode;
- significado de current checkpoint;
- lifecycle obrigatório;
- idempotency semantics;
- scope semantics;
- Evidence support semantics;
- trust boundary;
- required request fields de forma incompatível;
- error semantics necessários para reação do Consumer.

---

# 79. Deprecation

Um Contract version existente não deve desaparecer silenciosamente.

Deprecation futura deverá:

1. ser anunciada explicitamente;
2. preservar período de compatibilidade definido;
3. oferecer versão substituta;
4. não depender apenas de SemVer da aplicação.

---

# 80. Non-Goals

Este Contract não define:

- UI;
- ContextBuilder do Consumer;
- agent orchestration;
- task management;
- provider routing;
- permission workflows;
- automatic memory extraction;
- automatic contradiction resolution;
- graph projection;
- MemorySpace;
- tenancy;
- RBAC;
- LLM provider.

---

# 81. Normative Contract Summary

O Contract v1 estabelece:

```text
Episode
    = experiência histórica persistente

ContextCheckpoint
    = estado de continuidade versionado

Context Stream
    = (project scope, context_key)

Contextual Recall
    = recuperação estruturada e seletiva

Evidence
    = suporte rastreável para conteúdo derivado
```

As responsabilidades permanecem separadas:

```text
Sofias Memory
    stores
    validates
    versions
    recalls
    forgets

Consumer
    interprets
    decides
    authorizes
    composes
    executes
```

A regra central permanece:

> **Sofias Memory preserva e disponibiliza memória contextual; o Consumer interpreta essa memória e decide o que fazer com ela.**
