# Sofias Memory — Implementation: SM-CTX-003 — Checkpoint Revision, Supersession and Concurrency

**Document:** `implementation/11-SF-Implementation-SM-CTX-003-Checkpoint-Revision-Concurrency.md`\
**Status:** APPROVED
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-003 — Checkpoint Revision, Supersession and Concurrency`

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

---

# 1. Objective

Implementar o terceiro vertical slice de Contextual Project Memory:

```text
SM-CTX-003 — Checkpoint Revision, Supersession and Concurrency
```

Ao final deste Slice, um Consumer deverá conseguir evoluir com segurança:

```text
ContextCheckpoint revision 1
        ↓
supersede
        ↓
ContextCheckpoint revision 2
        ↓
supersede
        ↓
ContextCheckpoint revision 3
```

preservando:

- histórico;
- lineage;
- exatamente um ACTIVE por Context Stream;
- idempotent replay;
- stale-writer detection;
- atomicidade;
- comportamento determinístico sob concorrência.

O principal valor entregue é:

> dois callers não podem alterar silenciosamente o mesmo estado contextual e sobrescrever o trabalho um do outro.

---

# 2. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-003
```

Decisões locais, reversíveis e compatíveis com o baseline podem ser tomadas autonomamente.

Não alterar decisões APPROVED.

---

# 3. Required Entry State

SM-CTX-003 só pode ser executado depois de:

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
```

com Human Validation correspondente quando exigida.

---

## 3.1 SM-CTX-001 MUST provide

```text
Episode first-class
generic contextual_memory_idempotency
contextual operation enum
PostgreSQL authority
transactional idempotency pattern
```

O ledger MUST estar resource-generic conforme a versão emendada do handoff `09`.

---

## 3.2 SM-CTX-002 MUST provide

```text
ContextCheckpoint
ContextStatement
ContextCheckpoint provenance
(scope, context_key) Context Stream identity
revision
ACTIVE lifecycle
INVALIDATED lifecycle
FORGOTTEN lifecycle
single-ACTIVE database constraint
current lookup
initial/current creation
direct invalidate
direct Forget
```

---

# 4. Resolved Dependency / Required Entry Semantics — Re-establishing Current State

A semântica abaixo já foi incorporada e APPROVED no handoff `10` de SM-CTX-002.
O executor de SM-CTX-003 verifica a capability entregue pelo Slice anterior;
não deve corrigir novamente nem reexecutar SM-CTX-002 durante este Slice.

O baseline aprovado estabelece simultaneamente que:

```text
ACTIVE invalidated/forgotten
    ↓
stream has no current checkpoint
```

e que:

```text
historical checkpoint is NEVER automatically reactivated
```

Isso é correto.

Porém, `supersede` exige um ACTIVE checkpoint como alvo.

Logo, depois de:

```text
ACTIVE -> INVALIDATED
```

ou:

```text
ACTIVE -> FORGOTTEN
```

não existe recurso válido que possa ser alvo de `/supersede`.

Sem outro mecanismo, o Context Stream ficaria permanentemente sem possibilidade de recuperar um current checkpoint.

Essa era a lacuna original em relação à Architecture, que prevê ausência temporária de current até o Consumer criar novo estado válido. O handoff `10` já a resolveu com recovery Create.

---

## 4.1 Approved SM-CTX-002 Entry Semantics

`POST /api/v1/context/checkpoints` deve semanticamente significar:

> create a new ACTIVE checkpoint when the Context Stream currently has no ACTIVE checkpoint.

Isso inclui dois casos.

### New stream

Nenhum checkpoint histórico existe:

```text
revision = 1
```

### Existing stream with no ACTIVE

Existe histórico, mas nenhum ACTIVE:

```text
revision = max(existing revision) + 1
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

Não reativar revision 1.

---

## 4.2 Conflict semantics remain unchanged

Se existir ACTIVE:

```text
POST /context/checkpoints
    -> 409 CONTEXT_STATE_CONFLICT
```

O caller deverá usar:

```text
POST /context/checkpoints/{current_id}/supersede
```

para evolução normal.

---

## 4.3 Why this is not automatic rollback

Criar uma nova revision exige ação explícita do Consumer e payload completo.

Não ocorre:

```text
historical checkpoint -> ACTIVE
```

automaticamente.

O histórico permanece imutável.

---

## 4.4 Revision monotonicity

Revision é monotônica por:

```text
(scope, context_key)
```

e nunca reutilizada.

Portanto:

```text
1 ACTIVE
1 INVALIDATED
1 FORGOTTEN
```

não permitem uma nova revision `1`.

A próxima sempre é:

```text
MAX(revision) + 1
```

---

## 4.5 Resolved Amendment — Verify at Entry

O handoff `10` já reflete esta regra. Na entrada de SM-CTX-003, verificar testes
e completion evidence de recovery Create de SM-CTX-002. Se a implementação não
entregar a capability aprovada, reportar dependency failure, sem ampliar CTX-003.

Essa correção:

- não altera a intenção do produto;
- não cria endpoint novo;
- não reativa histórico;
- não muda a arquitetura;
- resolve uma lacuna entre Contract e Architecture.

---

# 5. Scope

Implementar neste Slice:

## Public behavior

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
```

## Domain

- atomic supersession;
- linear revision;
- checkpoint lineage;
- expected-current semantics;
- stale writer detection;
- concurrency conflict;
- replay precedence;
- superseded lifecycle.

## Persistence

- lineage constraints;
- revision guarantees;
- locks;
- unique current protection;
- optional additional indexes required by concurrency.

## Tests

- normal supersession;
- multi-revision chain;
- same-key concurrency;
- different-key concurrency;
- stale writers;
- transaction rollback;
- Supersede vs Invalidate;
- Supersede vs Forget;
- current uniqueness;
- restart durability.

---

# 6. Explicit Out of Scope

MUST NOT implementar neste Slice:

```text
Evidence
support policies
Evidence cycles
Forget propagation
Contextual Recall
Knowledge integration
checkpoint embeddings
semantic checkpoint search
automatic merge
three-way merge
automatic conflict resolution
LLM conflict resolution
distributed locks
Redis locks
Neo4j
PipelineRun
automatic checkpoint creation
```

Não implementar SM-CTX-004+ antecipadamente.

---

# 7. Core Invariant

Para cada:

```text
(scope, context_key)
```

deve existir:

```text
0 or 1 ACTIVE checkpoint
```

Nunca:

```text
2 ACTIVE checkpoints
```

nem mesmo sob concorrência real.

---

# 8. Normal Evolution

Fluxo esperado:

```text
revision 1
ACTIVE
   │
   │ supersede
   ▼
SUPERSEDED
               revision 2
               ACTIVE
```

Depois:

```text
revision 1 SUPERSEDED
revision 2 ACTIVE
```

Nova evolução:

```text
revision 1 SUPERSEDED
revision 2 SUPERSEDED
revision 3 ACTIVE
```

---

# 9. Supersede Is Full Replacement

Supersede não é PATCH.

O novo checkpoint é construído integralmente a partir do payload do Consumer.

Não herdar silenciosamente:

- objective;
- current_state;
- decisions;
- pending work;
- blockers;
- next action;
- provenance.

Se o Consumer deseja preservar determinado conteúdo, deve enviá-lo novamente.

---

# 10. Scope and Context Identity Are Immutable

Supersede nunca move um checkpoint para outro Context Stream.

Replacement MUST pertencer a:

```text
same scope
same context_key
```

do target.

Usar o payload do Contract `05` §31: `scope` deve ser fornecido e o `context_key`
resolvido conforme seu default público deve corresponder exatamente ao target.
Não inventar herança do context_key do target que substitua o default `main`.

Nunca permitir:

```text
project:a/main
    ↓ supersede
project:b/main
```

ou:

```text
project:a/main
    ↓ supersede
project:a/feature:x
```

---

# 11. Expected Current Semantics

O path:

```http
/context/checkpoints/{checkpoint_id}/supersede
```

representa também:

```text
expected_current_checkpoint_id = checkpoint_id
```

O caller está afirmando:

> acredito que este checkpoint ainda é o current state.

O serviço deve verificar essa premissa.

---

# 12. Successful Supersession

Para:

```text
A = ACTIVE revision 3
```

request válido produz:

```text
A.lifecycle       = superseded
A.superseded_at   = now
A.superseded_by   = B.id

B.lifecycle       = active
B.revision        = 4
```

Tudo na mesma transaction.

---

# 13. Replacement Identity

Replacement recebe:

```text
new checkpoint UUID
new ContextStatement UUIDs
new provenance row
new created_at
```

Não reutilizar IDs do checkpoint antigo.

---

# 14. Statement Identity

Mesmo que um statement tenha exatamente o mesmo texto da revision anterior:

```text
old statement_id != new statement_id
```

Cada checkpoint é snapshot independente.

Não implementar statement dedupe.

---

# 15. New Provenance

Replacement recebe provenance da nova operação.

Não copiar provenance antiga silenciosamente.

Isso permite distinguir:

```text
who/what established revision 2
```

de:

```text
who/what established revision 3
```

---

# 16. Revision Allocation

Para supersession normal:

```text
replacement.revision = current.revision + 1
```

Não calcular revision por timestamp.

Não reutilizar revision.

---

# 17. Linear Lineage

Lineage deve ser linear por Context Stream.

Exemplo:

```text
A -> B -> C -> D
```

Não permitir:

```text
A -> B
A -> C
```

como dois replacements válidos.

Nem:

```text
A -> C
B -> C
```

como dois predecessores do mesmo replacement.

---

# 18. `superseded_by`

`superseded_by` preserva identidade estrutural do replacement.

FK:

```text
context_checkpoints.id
ON DELETE RESTRICT
```

Hard delete público continua inexistente.

---

# 19. No Self Supersession

Database constraint:

```text
superseded_by IS NULL
OR superseded_by <> id
```

---

# 20. Single Direct Predecessor

Adicionar proteção equivalente a:

```text
UNIQUE(superseded_by)
WHERE superseded_by IS NOT NULL
```

para impedir que dois checkpoints apontem para o mesmo replacement.

---

# 21. Stream Consistency

A aplicação deve garantir que predecessor e replacement possuem:

```text
same scope
same context_key
```

Uma FK simples não garante isso.

Não criar trigger complexo apenas para esse vínculo se service + transaction + testes estruturais forem suficientes.

---

# 22. ACTIVE Lifecycle Shape

ACTIVE MUST possuir:

```text
superseded_at = NULL
superseded_by = NULL
invalidated_at = NULL
forgotten_at = NULL
```

---

# 23. SUPERSEDED Lifecycle Shape

SUPERSEDED MUST possuir:

```text
superseded_at != NULL
superseded_by != NULL
invalidated_at = NULL
forgotten_at = NULL
```

e preservar:

- statements;
- provenance;
- scope;
- context_key;
- revision.

SUPERSEDED é histórico válido, não tombstone.

---

# 24. INVALIDATED Historical Checkpoint

Contract permite posteriormente invalidar um checkpoint que já foi SUPERSEDED.

Portanto:

```text
SUPERSEDED -> INVALIDATED
```

é válido.

Nesse caso, preservar lineage:

```text
superseded_at
superseded_by
```

mesmo com:

```text
lifecycle = invalidated
```

Assim é possível saber:

> este checkpoint foi historicamente substituído e depois marcado como não confiável.

---

# 25. INVALIDATED Current Checkpoint

Quando ACTIVE é invalidado diretamente:

```text
superseded_at = NULL
superseded_by = NULL
invalidated_at != NULL
```

Logo, INVALIDATED pode possuir ou não lineage de supersession.

DB constraints devem aceitar ambos os shapes coerentes.

---

# 26. FORGOTTEN Historical Checkpoint

Quando um SUPERSEDED posteriormente é FORGOTTEN:

```text
lifecycle = forgotten
```

pode preservar:

```text
superseded_at
superseded_by
```

como lineage estrutural.

Semantic content permanece scrubbed.

---

# 27. No New Revision by Mutation

Revision nunca é alterada in-place.

Não fazer:

```text
checkpoint.revision += 1
```

no mesmo row.

Cada revision é novo checkpoint.

---

# 28. Supersede Endpoint

Public surface:

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
```

Header obrigatório:

```http
Idempotency-Key
```

Status de sucesso:

```http
201 Created
```

---

# 29. Replacement Payload

O payload deve representar snapshot completo.

Conceitualmente:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "objective": {
    "text": "..."
  },
  "current_state": {
    "text": "..."
  },
  "approved_decisions": [],
  "pending_work": [],
  "open_items": [],
  "blockers": [],
  "next_action": {
    "text": "..."
  },
  "provenance": {
    "origin_kind": "assistant_generated",
    "source_system": "sofias-personal-assistant"
  }
}
```

Usar o shape público exato definido pelo Contract/Schemas de SM-CTX-002.

`current_state` continua obrigatório.

---

# 30. Response

O resultado deve identificar claramente o replacement criado.

No mínimo:

```text
new checkpoint_id
scope
context_key
new revision
lifecycle=active
statements
provenance
created_at
```

Se o Contract público aprovado definir wrapper contendo old/replacement, obedecer ao Contract.

Não inventar segunda representação apenas para este Slice.

---

# 31. Idempotency Canonical Request

O digest de Supersede deve incluir:

```text
operation = checkpoint_supersede
target_resource_id = checkpoint_id
full replacement semantic payload
```

Não incluir:

- generated replacement UUID;
- generated statement IDs;
- created_at;
- resulting revision;
- transport metadata.

---

# 32. Replay Precedence

Esta é uma invariante crítica.

Para mesma:

```text
Idempotency-Key
+
same semantic request
```

o replay MUST ser resolvido antes de avaliar se o target ainda está ACTIVE.

Motivo:

```text
request 1:
A ACTIVE -> B ACTIVE
A becomes SUPERSEDED

response lost

request 1 retry:
target A is now SUPERSEDED
```

O resultado correto é:

```text
replay B
```

e não:

```text
CONTEXT_STATE_CONFLICT
```

---

# 33. Authoritative Operation Ordering

Para mutation com Idempotency-Key:

```text
BEGIN

claim/check idempotency key
    ↓
same-key existing?
    ├── same request -> replay winner
    └── different request -> IDEMPOTENCY_CONFLICT

only new winner continues
    ↓
lock target checkpoint
    ↓
validate target/current state
    ↓
perform supersession
    ↓
persist result_resource_id
    ↓
COMMIT
```

Idempotency claim precede lifecycle validation.

---

# 34. Why Target Lock Cannot Come First

Este fluxo é incorreto:

```text
lock A
check ACTIVE
then resolve idempotency
```

porque same-key retry após a primeira execução veria:

```text
A = SUPERSEDED
```

e poderia produzir falso state conflict.

Não implementar nessa ordem.

---

# 35. Generic Contextual Ledger

Usar:

```text
contextual_memory_idempotency
```

produzido pelo SM-CTX-001.

Supersede entry:

```text
operation = checkpoint_supersede
target_resource_id = old_checkpoint_id
result_resource_id = new_checkpoint_id
```

Não criar ledger adicional de checkpoint.

---

# 36. Same-Key Concurrency

Cenário:

```text
Caller A
Caller B

same target
same Idempotency-Key
same payload
```

Resultado:

```text
exactly one replacement
same result returned to both callers
```

Não podem existir:

```text
revision 2
revision 3
```

causadas pelo replay da mesma operação.

---

# 37. Same-Key Different Request

Cenário:

```text
same Idempotency-Key
same target
different replacement payload
```

Resultado:

```text
409 IDEMPOTENCY_CONFLICT
```

Mesmo se target já tiver sido superseded.

Idempotency conflict tem precedência sobre lifecycle conflict para key já vinculada.

---

# 38. Different-Key Concurrency

Cenário:

```text
A ACTIVE

Caller 1:
key = K1
A -> B

Caller 2:
key = K2
A -> C
```

Resultado obrigatório:

```text
one winner
one stale conflict
```

Possível resultado:

```text
B ACTIVE
C does not exist
```

ou:

```text
C ACTIVE
B does not exist
```

dependendo de quem vencer.

Nunca:

```text
B ACTIVE
C ACTIVE
```

---

# 39. Stale Writer Error

O loser recebe:

```text
409 CONTEXT_STATE_CONFLICT
```

O response SHOULD incluir quando disponível:

```text
expected_checkpoint_id
current_checkpoint_id
scope
context_key
```

sem expor conteúdo semântico desnecessário.

---

# 40. Stale Request Does Not Create Revision

Se target não é current:

```text
no replacement checkpoint
no statements
no provenance row
no durable idempotency winner for failed new-key operation
```

A transaction deve rollback integralmente.

---

# 41. Row Lock

Repository deve oferecer operação equivalente a:

```text
get_by_id_for_update(checkpoint_id)
```

usando:

```sql
SELECT ... FOR UPDATE
```

para lifecycle-mutating operations.

---

# 42. Lock Scope

Não criar lock global de projeto.

Não criar lock externo.

Serializar somente recursos necessários.

Independent Context Streams devem conseguir evoluir independentemente.

---

# 43. Partial Unique Constraint Remains Safety Net

Mesmo com row lock, manter:

```text
UNIQUE(scope, context_key)
WHERE lifecycle='active'
```

A constraint é defesa estrutural.

Não confiar exclusivamente em service code.

---

# 44. Supersession Transaction Order

Como partial unique ACTIVE não é deferrable, uma implementação segura pode executar dentro da mesma transaction:

```text
1. lock old ACTIVE
2. old.lifecycle = SUPERSEDED
3. old.superseded_at = now
4. establish old.superseded_by/new identity as appropriate
5. flush state needed to release ACTIVE uniqueness
6. insert replacement ACTIVE
7. insert statements
8. insert provenance
9. complete lineage
10. persist idempotency outcome
11. commit
```

A ordem física exata pode variar.

Invariante pública:

> old transition + replacement creation são atômicos.

---

# 45. Failure After Old Transition

Se qualquer operação falhar depois de old ter sido marcado SUPERSEDED mas antes do commit:

```text
ROLLBACK
```

deve restaurar:

```text
old = ACTIVE
replacement = absent
```

Não pode existir stream sem current devido a partial supersession failure.

---

# 46. Failure After Replacement Insert

Se replacement for inserido e posteriormente ocorrer erro antes do commit:

```text
ROLLBACK
```

deve eliminar replacement e restaurar old ACTIVE.

---

# 47. No External Provider

Supersession de checkpoint não exige:

- embedding;
- LLM;
- remote API.

Toda operação permanece dentro de short PostgreSQL transaction.

---

# 48. Revision Sequence

Teste:

```text
A revision 1
B revision 2
C revision 3
D revision 4
```

Revision deve crescer estritamente.

Não exigir ausência matemática de gaps causada por transações rollback internas; porém nenhuma revision commitada pode repetir ou retroceder.

No fluxo normal linear:

```text
replacement.revision = current.revision + 1
```

---

# 49. Re-establishing After Missing Current

A semântica já resolvida na seção 4 pertence ao endpoint Create de SM-CTX-002.

Exemplo:

```text
revision 1 ACTIVE
    ↓ invalidate

revision 1 INVALIDATED

create
    ↓

revision 2 ACTIVE
```

Depois SM-CTX-003 pode evoluir normalmente:

```text
revision 2 ACTIVE
    ↓ supersede

revision 2 SUPERSEDED
revision 3 ACTIVE
```

---

# 50. Re-establishing Does Not Create Lineage to Invalidated History

Quando `POST /context/checkpoints` cria novo ACTIVE porque não existia current:

```text
old invalidated checkpoint.superseded_by remains NULL
```

A nova revision não é semanticamente um supersede do checkpoint invalidado.

Eles compartilham apenas:

```text
scope
context_key
revision sequence
```

---

# 51. Why Revision Still Continues

Revision identifica evolução temporal do Context Stream, não apenas supersession edges.

Portanto:

```text
rev1 INVALIDATED
rev2 ACTIVE
```

é correto mesmo sem:

```text
rev1 -> rev2 superseded_by
```

---

# 52. Supersede Target Must Be ACTIVE

Nova operation key sobre target:

```text
SUPERSEDED
INVALIDATED
FORGOTTEN
```

resulta:

```text
409 CONTEXT_STATE_CONFLICT
```

Exceto:

```text
same-key / same-request replay
```

que retorna outcome original pela regra de idempotência.

---

# 53. Missing Target

Target inexistente:

```text
404 CHECKPOINT_NOT_FOUND
```

Não criar idempotency outcome bem-sucedido.

---

# 54. Supersede vs Invalidate — Invalidate Wins

Cenário concorrente:

```text
A ACTIVE
```

Invalidate obtém lock e commit primeiro:

```text
A INVALIDATED
```

Supersede depois observa que A não é ACTIVE:

```text
409 CONTEXT_STATE_CONFLICT
```

Nenhum replacement é criado.

---

# 55. Supersede vs Invalidate — Supersede Wins

Supersede vence:

```text
A SUPERSEDED
B ACTIVE
```

Invalidate posterior de A é permitido pelo Contract:

```text
A INVALIDATED
B ACTIVE
```

Preservar:

```text
A.superseded_at
A.superseded_by = B
```

Invalidar A histórico não afeta B.

---

# 56. Supersede vs Forget — Forget Wins

Forget em A commit primeiro:

```text
A FORGOTTEN
```

Supersede posterior:

```text
409 CONTEXT_STATE_CONFLICT
```

Nenhum replacement.

---

# 57. Supersede vs Forget — Supersede Wins

Supersede primeiro:

```text
A SUPERSEDED
B ACTIVE
```

Forget posterior de A:

```text
A FORGOTTEN
B ACTIVE
```

Replacement B permanece independente.

Não existe cascade neste Slice.

---

# 58. Forget Does Not Follow Lineage

Forget de predecessor:

```text
A
```

não esquece:

```text
B
```

automaticamente.

Forget propagation contextual baseado em Evidence será implementado no SM-CTX-005.

Lineage de checkpoint não é Evidence.

---

# 59. Invalidation Does Not Follow Lineage

Invalidar predecessor histórico não invalida replacement.

Cada checkpoint possui lifecycle próprio.

---

# 60. Full Replacement and Empty Collections

Se replacement envia:

```text
pending_work = []
```

isso significa:

```text
new revision has no pending work
```

Não herdar itens antigos.

---

# 61. Omitted Optional Scalar

Se campo opcional como:

```text
objective
next_action
```

for omitido no full replacement:

```text
new revision does not contain it
```

Não copiar do predecessor.

---

# 62. No Diff Storage

Não persistir apenas mudanças entre revisions.

Cada checkpoint é snapshot completo.

Não exigir reconstrução:

```text
rev1 + patch2 + patch3
```

para hidratar rev3.

---

# 63. Historical Retrieval

Depois de supersession:

```text
GET by ID old
```

deve retornar old checkpoint:

```text
lifecycle=superseded
```

com conteúdo preservado.

---

# 64. List History

Quando lifecycle histórico for solicitado, listar revisions antigas segundo:

```text
revision DESC
checkpoint_id ASC
```

Superseded checkpoints devem aparecer.

---

# 65. Current Lookup

Após supersession:

```text
GET /context/checkpoints/current
```

deve retornar apenas replacement.

Nunca old.

---

# 66. Restart Durability

Após:

```text
A -> B -> C
```

reiniciar aplicação.

Deve permanecer:

```text
A SUPERSEDED
B SUPERSEDED
C ACTIVE
```

com lineage e revisions intactas.

---

# 67. Database Changes

Espera-se que SM-CTX-002 já tenha criado a maioria das colunas necessárias.

SM-CTX-003 pode precisar de migration aditiva para:

- lineage indexes;
- missing lineage constraints;
- check constraints;
- uniqueness sobre `superseded_by`.

Não congelar migration number neste documento.

Usar head real no momento da execução.

---

# 68. Expected Constraints

No mínimo, o schema final após CTX-003 deve proteger estruturalmente:

```text
UNIQUE(scope, context_key, revision)
```

```text
UNIQUE(scope, context_key)
WHERE lifecycle='active'
```

```text
UNIQUE(superseded_by)
WHERE superseded_by IS NOT NULL
```

e:

```text
superseded_by IS NULL OR superseded_by <> id
```

---

# 69. Lifecycle Constraint Compatibility

Não escrever constraint que torne impossível:

```text
SUPERSEDED -> INVALIDATED
```

ou:

```text
SUPERSEDED -> FORGOTTEN
```

com lineage preservada.

Isso é particularmente importante.

Uma constraint excessivamente rígida como:

```text
lifecycle != superseded
    => superseded_by MUST be NULL
```

seria incorreta.

---

# 70. Timestamp Semantics

`superseded_at` representa:

> quando aquele checkpoint deixou de ser current por supersession.

`created_at` do replacement é independente.

Não usar `created_at` como prova da transição.

---

# 71. Clock Ordering

Não depender da hipótese:

```text
replacement.created_at > old.created_at
```

como mecanismo de correctness.

Revision e lifecycle são autoritativos.

---

# 72. Repository Layer

Extender:

```text
ContextCheckpointRepository
```

com primitivas necessárias.

Prováveis:

```text
get_by_id_for_update()
get_current()
get_current_for_update() when useful
get_latest_revision()
add()
```

Não criar repository específico apenas para supersession.

---

# 73. Service Layer

Extender:

```text
ContextCheckpointService
```

com:

```text
supersede()
```

Manter regras de:

- lifecycle;
- concurrency;
- idempotency;
- revision;

fora da route.

---

# 74. No Generic Revision Engine

Não criar:

```text
GenericRevisionEngine
GenericSupersessionService
RevisionGraph
VersionedAggregateFramework
```

apenas porque Checkpoint e Cognitive Memory possuem conceitos de supersession.

As semânticas são diferentes.

---

# 75. Schema Layer

Adicionar request público necessário para Supersede.

Pode reutilizar base interna de fields do checkpoint create para evitar duplicação de validators.

Não aceitar campos de mutation parcial.

---

# 76. Route Layer

Adicionar somente:

```http
POST /context/checkpoints/{checkpoint_id}/supersede
```

Não adicionar:

```http
PATCH
PUT
```

---

# 77. Error Codes

Este Slice usa:

```text
CHECKPOINT_NOT_FOUND
CONTEXT_STATE_CONFLICT
IDEMPOTENCY_CONFLICT
INVALID_REQUEST
```

e erros comuns já existentes.

Não criar código diferente como:

```text
STALE_CHECKPOINT
```

se Contract estabilizou `CONTEXT_STATE_CONFLICT`.

---

# 78. Conflict Details

Para stale writer, incluir detalhes seguros quando possível:

```json
{
  "expected_checkpoint_id": "...",
  "current_checkpoint_id": "..."
}
```

Se stream atualmente não possui ACTIVE:

```text
current_checkpoint_id = null
```

é aceitável.

---

# 79. Idempotency Failure Rollback

Different-key stale request pode ter tentado inserir sua claim no ledger antes de descobrir state conflict.

Essa claim MUST ser rollbackada junto com a transaction.

Não persistir um outcome bem-sucedido para operação que falhou.

---

# 80. Same-Key Replay Outcome

O ledger deve possuir informação suficiente para hidratar o replacement original:

```text
result_resource_id
```

Resolver resource type a partir de:

```text
operation=checkpoint_supersede
```

---

# 81. Deleted Resource Assumption

Não existe hard delete público de checkpoint.

Logo, um `result_resource_id` de supersession idempotente deve continuar resolvível pelo menos como:

- ACTIVE;
- SUPERSEDED;
- INVALIDATED;
- FORGOTTEN tombstone.

Isso permite replay mesmo depois de lifecycle posterior do replacement.

---

# 82. Replay After Replacement Lifecycle Changed

Exemplo:

```text
A -> B
B later -> C
```

Retry tardio da idempotency key original de:

```text
A -> B
```

deve resolver a mesma operação original.

Não criar C novamente.

Replay resolve B em seu estado atual/lifecycle-safe. B FORGOTTEN retorna somente tombstone; statements removidos por propagation não são recompostos. Não armazenar snapshot semântico no ledger nem ressuscitar payload histórico.

Não criar nova revision.

---

# 83. No Historical Mutation by Supersede

Supersede só aceita ACTIVE target.

Não permitir:

```text
supersede revision 1
```

quando revision 3 é current.

Isso é stale mutation.

---

# 84. Cross-Stream Concurrency

Operações sobre:

```text
(project:a, feature:x)
```

e:

```text
(project:a, feature:y)
```

não devem bloquear-se globalmente sem necessidade.

Nenhum project-wide mutex.

---

# 85. Cross-Project Concurrency

Mesma regra para:

```text
project:a
project:b
```

Sem global lock.

---

# 86. PostgreSQL Integration Tests

Obrigatórios:

- rev1 -> rev2;
- rev1 -> rev2 -> rev3;
- lineage;
- single ACTIVE;
- historical list;
- current lookup;
- same-key race;
- same-key/different-payload;
- different-key race;
- stale retry;
- rollback after old transition;
- rollback after replacement insert;
- Supersede vs Invalidate;
- Supersede vs Forget;
- restart durability;
- independent stream concurrency.

---

# 87. Same-Key Race Test

Usar PostgreSQL real.

Cenário:

```text
A ACTIVE

two concurrent supersede calls
same key
same payload
```

Assert:

```text
one replacement row
one new revision
same replacement id returned
A superseded exactly once
```

---

# 88. Different-Key Race Test

```text
A ACTIVE

caller 1 K1
caller 2 K2
```

Assert:

```text
exactly one succeeds
exactly one CONTEXT_STATE_CONFLICT
exactly one replacement
exactly one ACTIVE
```

---

# 89. Stale Writer Sequential Test

```text
A ACTIVE

A -> B succeeds

try A -> C with new key
```

Expected:

```text
409 CONTEXT_STATE_CONFLICT
```

and:

```text
C does not exist
```

---

# 90. Same-Key Replay Sequential Test

```text
A -> B using K1
retry A -> B using K1
```

Expected:

```text
B replayed
```

não:

```text
CONTEXT_STATE_CONFLICT
```

---

# 91. Same-Key Different-Payload Sequential Test

```text
A -> B using K1 payload P1
retry K1 with payload P2
```

Expected:

```text
IDEMPOTENCY_CONFLICT
```

não state conflict.

---

# 92. Transaction Failure Injection — Before Replacement

Injetar erro depois de old transition ter sido preparado/flushed.

Após rollback:

```text
old ACTIVE
no replacement
```

---

# 93. Transaction Failure Injection — After Replacement

Injetar erro depois de replacement/statements terem sido inseridos, antes de commit.

Após rollback:

```text
old ACTIVE
replacement absent
statements absent
provenance absent
```

---

# 94. Supersede vs Invalidate Test

Cobrir ambas as ordens.

## Invalidate first

```text
A INVALIDATED
supersede fails
```

## Supersede first

```text
A SUPERSEDED -> INVALIDATED
B remains ACTIVE
```

---

# 95. Supersede vs Forget Test

Cobrir ambas as ordens.

## Forget first

```text
A FORGOTTEN
supersede fails
```

## Supersede first

```text
A SUPERSEDED -> FORGOTTEN
B remains ACTIVE
```

---

# 96. Recovery Create Test

Verificar a capability já aprovada e entregue por CTX-002:

```text
rev1 ACTIVE
rev1 INVALIDATED
create new current
```

Assert:

```text
rev1 INVALIDATED
rev2 ACTIVE
```

Depois:

```text
rev2 -> rev3
```

Assert:

```text
rev3 ACTIVE
```

Isso prova continuidade após ausência de current.

---

# 97. Forgotten Current Recovery Test

```text
rev1 ACTIVE
forget rev1
create current
```

Expected:

```text
rev1 FORGOTTEN
rev2 ACTIVE
```

Sem reativação.

---

# 98. Revision Uniqueness Race

Quando dois callers tentam criar successor do mesmo ACTIVE:

```text
only one revision N+1
```

Nunca:

```text
two rows with revision N+1
```

e nunca:

```text
N+1 and N+2
```

para os dois writers concorrentes sobre o mesmo expected current.

O loser não “tenta novamente” automaticamente contra o novo current.

---

# 99. No Blind Retry

`CONTEXT_STATE_CONFLICT` não dispara retry automático dentro do Memory.

O Consumer decide:

```text
read current
review/merge
retry explicitly
```

Sofias Memory não faz semantic merge.

---

# 100. Why No Automatic Merge

Dois checkpoints podem representar decisões humanas incompatíveis.

Exemplo:

```text
Writer A:
next_action = implement schema

Writer B:
next_action = redesign contract
```

Sofias Memory não possui autoridade para decidir qual vence.

Logo:

```text
detect conflict
do not resolve conflict
```

---

# 101. No LLM Conflict Resolution

Não chamar LLM para:

- merge;
- compare;
- choose winner;
- rewrite snapshot.

Conflito é estrutural.

---

# 102. OpenAPI

Após este Slice deverá existir:

```http
POST /api/v1/context/checkpoints/{checkpoint_id}/supersede
```

e continuar inexistindo:

```http
PATCH /api/v1/context/checkpoints/{id}
PUT   /api/v1/context/checkpoints/{id}
POST  /api/v1/context/recall
```

---

# 103. `/api/v1/info`

Ainda MUST NOT anunciar:

```text
contextual_project_memory = "1"
```

Contract continua incompleto.

---

# 104. Graph Isolation

Supersession não:

- grava graph_outbox;
- toca Neo4j;
- depende de Neo4j.

---

# 105. Pipeline Isolation

Supersession não cria:

- PipelineRun;
- PipelineStep.

É synchronous mutation.

---

# 106. Session Isolation

Checkpoint supersession não:

- cria Session;
- modifica Session;
- exige Session ativa;
- cria SessionEntry.

---

# 107. Episode Isolation

Checkpoint supersession não altera Episodes.

---

# 108. Cognitive Memory Isolation

Checkpoint supersession não altera MemoryItems.

---

# 109. Knowledge Isolation

Checkpoint supersession não consulta nem altera Datasets.

---

# 110. Logging

Não logar:

- checkpoint semantic text;
- Idempotency-Key;
- full payload.

Pode logar:

```text
operation
target_checkpoint_id
replacement_checkpoint_id
scope
context_key
old_revision
new_revision
conflict=true|false
```

quando seguro.

---

# 111. Metrics / Observability

Se o projeto já possuir mechanism apropriado, é útil observar:

```text
checkpoint_supersede_success
checkpoint_supersede_conflict
checkpoint_idempotency_replay
```

Não criar telemetry subsystem novo apenas para isso.

---

# 112. Human Validation

Executar usando API real.

## Step A — Create revision 1

```text
revision=1
ACTIVE
```

---

## Step B — Read in two clients

Ambos guardam:

```text
checkpoint_id=A
```

---

## Step C — Client 1 supersedes

```text
A -> B
```

Expected:

```text
A SUPERSEDED
B ACTIVE
B.revision=2
```

---

## Step D — Client 2 stale supersede

Tentar:

```text
A -> C
```

com nova key.

Expected:

```text
409 CONTEXT_STATE_CONFLICT
```

Confirmar:

```text
C does not exist
```

---

## Step E — Replay

Repetir exatamente a operação original:

```text
A -> B
```

com a mesma Idempotency-Key do Client 1.

Expected:

```text
same B
```

---

## Step F — Third revision

```text
B -> D
```

Expected:

```text
A SUPERSEDED
B SUPERSEDED
D ACTIVE revision=3
```

---

## Step G — Restart

Reiniciar aplicação.

Confirmar:

```text
GET current -> D
history -> D/B/A
```

---

## Step H — Invalidate old history

Invalidar B.

Confirmar:

```text
B INVALIDATED
B.superseded_by = D
D remains ACTIVE
```

---

## Step I — Forget oldest

Forget A.

Confirmar:

```text
A FORGOTTEN
D remains ACTIVE
```

---

## Step J — Recovery after no current

Em outro stream:

```text
revision 1 ACTIVE
invalidate revision 1
```

Confirmar ausência de current.

Criar novo current explicitamente.

Expected:

```text
revision 1 INVALIDATED
revision 2 ACTIVE
```

---

# 113. Acceptance Criteria

## AC-003.1

A → B gera:

```text
A = SUPERSEDED
B = ACTIVE
```

## AC-003.2

B recebe:

```text
revision = A.revision + 1
```

## AC-003.3

Supersession é atômica.

## AC-003.4

Não existe estado commitado com dois ACTIVE.

## AC-003.5

Writer stale recebe:

```text
CONTEXT_STATE_CONFLICT
```

## AC-003.6

Writer stale não cria recurso.

## AC-003.7

Current vencedor permanece inalterado após stale conflict.

## AC-003.8

Retry da mutation concluída retorna mesmo outcome lógico.

## AC-003.9

Retry não cria revision adicional.

## AC-003.10

Full replacement não herda silenciosamente statements omitidos.

## AC-003.11

Historical checkpoints permanecem listáveis.

## AC-003.12

Invalidation/Forget de current não reativa superseded.

---

# 114. Additional Concurrency Invariants

Além dos ACs:

```text
same-key replay precedes lifecycle conflict
different-key race has exactly one winner
supersede vs invalidate is serialized
supersede vs forget is serialized
no distributed lock
no automatic semantic merge
```

---

# 115. Required Test Commands

Executar conjunto aplicável:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

Mais suite PostgreSQL opt-in dedicada.

Se houver migration:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Não declarar teste não executado como PASS.

---

# 116. Findings Classification

## BLOCKER

Exemplos:

- dois ACTIVE sob race;
- same-key retry vira state conflict;
- stale writer cria replacement;
- partial supersession persiste;
- revision duplicada;
- automatic rollback para historical state.

## IMPORTANT

Afeta diretamente correctness do Slice.

## MINOR

Não impede valor prometido.

## DEFERRED

Fora do Slice.

---

# 117. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- mudar expected-current semantics;
- permitir last-write-wins;
- permitir PATCH;
- introduzir automatic merge;
- criar distributed lock;
- usar Redis;
- alterar lifecycle;
- permitir cross-stream supersession;
- adicionar Evidence antecipadamente;
- alterar public Contract;
- mudar revision semantics.

---

# 118. Expected File Impact

Prováveis alterações:

```text
sofias_memory/services/context_checkpoints.py
sofias_memory/schemas/context_checkpoints.py
sofias_memory/api/routes/context_checkpoints.py
sofias_memory/infrastructure/postgres/repositories/context_checkpoints.py
sofias_memory/infrastructure/postgres/models/context_checkpoint.py
sofias_memory/domain/contextual_memory.py
tests/
```

Possível migration aditiva:

```text
migrations/versions/<next>_harden_context_checkpoint_supersession.py
```

somente se constraints/indexes adicionais forem necessários.

---

# 119. Implementation Order

Ordem recomendada:

```text
1. verify SM-CTX-001/002 completion
2. verify existing SM-CTX-002 recovery-create capability and completion evidence
3. inspect actual checkpoint schema
4. harden lineage constraints
5. repository locking primitives
6. supersede request schema
7. service idempotency claim ordering
8. atomic supersession
9. HTTP route
10. normal revision tests
11. same-key race
12. different-key stale race
13. rollback failure injection
14. invalidate cross-race
15. forget cross-race
16. restart durability
17. OpenAPI regression
18. Human Validation
```

---

# 120. Slice Exit Criteria

SM-CTX-003 está pronto para closeout quando:

```text
AC-003.1..12 satisfied
single ACTIVE proven under race
revision lineage proven
same-key replay precedence proven
stale writer rejection proven
transaction rollback proven
supersede/invalidate race proven
supersede/forget race proven
restart durability proven
no graph side effect
no PipelineRun side effect
no BLOCKER remains
```

---

# 121. Expected Implementation Report

O executor deverá retornar:

```text
SM-CTX-003 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- previous migration head:
- new migration head:
- schema changes:
- lineage constraints:
- current uniqueness:

Dependencies verified
- SM-CTX-001:
- SM-CTX-002:
- existing recovery-create capability verified:
- contextual idempotency ledger:

Verified
- unit:
- integration:
- HTTP:
- normal supersession:
- same-key race:
- different-key race:
- stale writer:
- rollback:
- supersede vs invalidate:
- supersede vs forget:
- restart:
- graph isolation:
- pipeline isolation:

Acceptance Criteria
- AC-003.1 PASS/FAIL
- AC-003.2 PASS/FAIL
- AC-003.3 PASS/FAIL
- AC-003.4 PASS/FAIL
- AC-003.5 PASS/FAIL
- AC-003.6 PASS/FAIL
- AC-003.7 PASS/FAIL
- AC-003.8 PASS/FAIL
- AC-003.9 PASS/FAIL
- AC-003.10 PASS/FAIL
- AC-003.11 PASS/FAIL
- AC-003.12 PASS/FAIL

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

Não avançar automaticamente para SM-CTX-004.

---

# 122. Handoff to Executor

## Objective

Implementar:

```text
SM-CTX-003 — Checkpoint Revision, Supersession and Concurrency
```

## Required entry conditions

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
```

e a capability de criação de novo current após ausência de ACTIVE já deve estar
entregue por SM-CTX-002. Aqui ela é pré-condição verificada, não correção pendente.

## Scope

Somente:

```text
Checkpoint Supersede
Revision evolution
Linear lineage
Expected-current semantics
Stale writer detection
Atomic concurrency
Idempotent replay
Cross-race correctness
```

## Out of Scope

```text
Evidence
Forget propagation
Contextual Recall
Knowledge composition
checkpoint embeddings
automatic merge
LLM conflict resolution
Neo4j
distributed locks
```

## Expected result

Ao final:

> ContextCheckpoint evolui através de revisions lineares, com exatamente um current state por stream, sem last-write-wins silencioso e com conflito explícito quando um Consumer tenta gravar a partir de contexto obsoleto.
