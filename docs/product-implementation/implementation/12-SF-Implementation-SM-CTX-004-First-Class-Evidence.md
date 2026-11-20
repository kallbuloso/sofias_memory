# Sofias Memory — Implementation: SM-CTX-004 — First-Class Evidence and Deterministic Support

**Document:** `implementation/12-SF-Implementation-SM-CTX-004-First-Class-Evidence.md`\
**Status:** APPROVED
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-004 — First-Class Evidence and Deterministic Support`

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

---

# 1. Objective

Implementar:

```text
SM-CTX-004 — First-Class Evidence and Deterministic Support
```

Ao final deste Slice, o Sofias Memory deverá conseguir responder estruturalmente:

```text
De onde veio esta informação?
```

para:

```text
Episode
ContextStatement
```

e também determinar, sem LLM:

```text
Esta informação ainda possui suporte suficiente?
```

O fluxo conceitual passa a ser:

```text
SessionEntry / MemoryItem / Episode / Source / Document / Chunk
                         │
                         │ EvidenceRef
                         ▼
                 derived contextual fact
                         │
              deterministic support
```

Este Slice cria a infraestrutura necessária para que o próximo Slice consiga implementar:

```text
Forget source
    ↓
reevaluate derived support
    ↓
prevent resurrection
```

sem inferência heurística.

---

# 2. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-004
```

Decisões locais e reversíveis podem ser tomadas autonomamente.

Não alterar decisões APPROVED.

---

# 3. Required Entry State

Antes de executar SM-CTX-004:

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
```

com os respectivos Human Validation gates concluídos quando exigidos.

---

## 3.1 SM-CTX-001 MUST provide

```text
Episode
Episode lifecycle
Episode provenance
Episode direct Forget
generic contextual idempotency
```

---

## 3.2 SM-CTX-002 MUST provide

```text
ContextCheckpoint
ContextStatement
ContextCheckpoint provenance
Context Stream identity
direct checkpoint Forget
```

---

## 3.3 SM-CTX-003 MUST provide

```text
checkpoint revisions
atomic supersession
linear lineage
stale writer protection
```

---

# 4. Fundamental Distinction — Provenance vs Evidence

Esta distinção é obrigatória.

```text
Provenance
    = where/how this resource was produced

Evidence
    = which authoritative resources explicitly support a semantic claim
```

Exemplo:

```text
Episode provenance:
    source_system = sofias-personal-assistant
    conversation_ref = conversation:42
```

não significa:

```text
conversation:42 is Evidence supporting the Episode
```

Somente um:

```text
EvidenceRef
```

explicitamente declarado cria dependency semantics.

---

## 4.1 Consequence

Nunca inferir Evidence automaticamente a partir de:

- `source_system`;
- `conversation_ref`;
- `turn_ref`;
- Session association;
- scope;
- matching text;
- semantic similarity;
- temporal proximity;
- Dataset association.

---

## 4.2 Why this matters

No SM-CTX-005, Forget propagation dependerá do grafo explícito de Evidence.

Se provenance fosse implicitamente dependency, Forget poderia apagar contexto que nunca declarou depender daquela informação.

Isso é proibido.

---

# 5. No Retroactive Dependency

Episodes e Checkpoints criados antes do SM-CTX-004 permanecem válidos.

Ausência de Evidence significa:

```text
no explicit support dependency declared
```

e não:

```text
unsupported
invalid
forgotten
```

Não executar backfill automático.

Não tentar reconstruir Evidence a partir de provenance antiga.

---

# 6. Scope

Implementar neste Slice:

## Domain

- `EvidenceSourceType`;
- `EvidenceAvailability`;
- `SupportMode`;
- `EvidenceRef`;
- deterministic support evaluation;
- Episode support;
- ContextStatement support;
- source resolvers;
- dependency-cycle protection.

## Persistence

- canonical Evidence identity;
- Episode ↔ Evidence links;
- ContextStatement ↔ Evidence links;
- support mode persistence;
- indexes/constraints.

## Existing APIs extended

- Episode create/result;
- Checkpoint create/result;
- Checkpoint supersede/result.

## Internal behavior

- source validation;
- current availability resolution;
- deterministic support evaluation;
- cycle prevention.

## Tests

- every Evidence source type;
- `all_required`;
- `any_sufficient`;
- missing/unavailable/forgotten sources;
- provenance/Evidence isolation;
- cycles;
- pre-CTX-004 compatibility.

---

# 7. Explicit Out of Scope

MUST NOT implementar ainda:

```text
automatic Forget propagation
transitive invalidation after source Forget
automatic scrubbing of derived Episodes
automatic scrubbing of ContextStatements
Contextual Recall
Knowledge composition
automatic Evidence discovery
semantic Evidence inference
LLM support evaluation
Evidence scoring
Evidence confidence aggregation
cross-project Evidence inference
Evidence management API
Evidence PATCH
```

Esses itens pertencem ao SM-CTX-005+ ou estão DEFERRED.

---

# 8. No Public Evidence Resource API

Não criar endpoints como:

```http
POST /api/v1/evidence
PATCH /api/v1/evidence/{id}
DELETE /api/v1/evidence/{id}
```

Evidence é parte do suporte declarado por recursos contextuais.

A identidade canônica pode ser first-class internamente sem criar um novo management resource público.

---

# 9. Evidence Source Types

Implementar exatamente:

```text
session_entry
memory_item
episode
source
document
chunk
```

Não adicionar neste Slice:

```text
dataset
relation
entity
query
checkpoint
context_statement
skill
agent
url
external_web_page
```

---

# 10. `source` Means Source

No Contract:

```text
source_type = "source"
```

significa o recurso Knowledge:

```text
Source
```

Não renomear publicamente para:

```text
knowledge_source
```

mesmo que o nome interno seja mais explícito.

A ambiguidade é resolvida pelo Contract.

---

# 11. Evidence Reference Shape

Request conceitual:

```json
{
  "source_type": "episode",
  "source_id": "uuid"
}
```

Result conceitual:

```json
{
  "evidence_id": "uuid",
  "source_type": "episode",
  "source_id": "uuid",
  "availability": "available"
}
```

EvidenceRef MUST NOT duplicar:

- source text;
- Episode summary;
- MemoryItem content;
- SessionEntry content;
- Chunk text.

No public request, support usa `refs`; no result, usa `evidence`, com
`evidence_id` e `availability`, exatamente como Contract `05` §§11–13.
Não aceitar `evidence` como alias de request por causa de exemplo conceitual.

---

# 12. Evidence Identity

Identidade canônica:

```text
(source_type, source_id)
```

Exemplo:

```text
("episode", UUID-X)
```

é diferente de:

```text
("memory_item", UUID-X)
```

mesmo que UUID seja textual/estruturalmente igual.

---

# 13. Canonical Evidence Storage

Criar tabela conceitual:

```text
contextual_evidence_refs
├── id UUID PK
├── source_type evidence_source_type NOT NULL
├── source_id UUID NOT NULL
└── created_at TIMESTAMPTZ NOT NULL
```

Constraint:

```text
UNIQUE(source_type, source_id)
```

Não criar uma Evidence row por link.

Um source contextual referenciado várias vezes deve reutilizar a identidade canônica.

---

# 14. Polymorphic Reference

`source_id` não recebe FK direta porque o target pode estar em:

```text
session_entries
memory_items
episodes
sources
documents
chunks
```

Não criar:

- seis nullable FK columns;
- generic `resources` table;
- trigger polimórfico complexo.

Validade é garantida por:

```text
EvidenceResolver
```

e testes de integração.

---

# 15. Availability

Valores públicos:

```text
available
unavailable
forgotten
```

---

## 15.1 AVAILABLE

O source existe e continua elegível para servir como Evidence segundo o lifecycle do domínio correspondente.

---

## 15.2 UNAVAILABLE

O source:

- não existe;
- não é atualmente elegível;
- foi invalidado;
- está fora do estado authoritative aplicável;

mas não há semântica suficiente para classificá-lo como Forget explícito.

---

## 15.3 FORGOTTEN

O source possui evidência authoritative de Forget/destructive deletion.

Exemplo:

```text
MemoryItem.lifecycle = forgotten
Episode.lifecycle = forgotten
Source.status = deleted through Forget
```

---

# 16. Availability Must Not Become Stale Authority

Uma `availability` persistida/cached não pode se tornar fonte de verdade por conveniência.

A autoridade continua sendo o domínio original.

Exemplo incorreto:

```text
EvidenceRef.availability = available

MemoryItem later becomes forgotten

resolver skips MemoryItem because cached Evidence says available
```

Isso é proibido.

---

# 17. Preferred Availability Model

A abordagem preferida neste Slice é:

```text
contextual_evidence_refs
    stores identity

EvidenceResolver
    computes current availability
```

na operação que necessita da informação.

Se a implementação optar por persistir `availability` como last-observed state, então:

- ela é cache/observation;
- MUST ser revalidada;
- não pode substituir source lifecycle;
- transient failure não pode ser convertido em persistent `unavailable`;
- `forgotten` somente pode ser marcado com evidence authoritative.

O Implementation Report deve registrar qual estratégia foi usada.

---

# 18. Transient Failure Is Not Evidence Unavailability

Erro transitório de:

- database;
- transaction;
- connection;
- internal dependency;

não significa:

```text
availability = unavailable
```

Isso é:

```text
DEPENDENCY_UNAVAILABLE
```

ou internal failure apropriada.

Não corromper estado semântico devido a falha operacional.

---

# 19. SupportMode

Implementar:

```text
all_required
any_sufficient
```

Não adicionar:

```text
majority
weighted
quorum
llm_evaluated
```

---

# 20. No-Support State

Um recurso pode não possuir support declarado.

Representação:

```text
support = null
```

ou equivalente conforme Contract.

Isso significa:

```text
independent assertion / no explicit evidence dependency
```

Não significa:

```text
support mode with zero evidence
```

---

# 21. Empty Support Is Invalid

Isto é inválido:

```json
{
  "mode": "all_required",
  "refs": []
}
```

e:

```json
{
  "mode": "any_sufficient",
  "refs": []
}
```

Se não há Evidence:

```text
support must be absent/null
```

---

# 22. `all_required`

Para Evidence:

```text
A
B
C
```

support é suficiente somente quando:

```text
A AVAILABLE
AND B AVAILABLE
AND C AVAILABLE
```

Se qualquer uma estiver:

```text
UNAVAILABLE
FORGOTTEN
```

o conjunto não possui suporte suficiente.

---

# 23. `any_sufficient`

Support é suficiente quando:

```text
at least one EvidenceRef is AVAILABLE
```

Exemplo:

```text
A FORGOTTEN
B AVAILABLE
C UNAVAILABLE

=> still supported
```

---

# 24. Deterministic Evaluation Only

Support evaluator não usa:

- LLM;
- embedding;
- semantic similarity;
- confidence;
- heurística textual;
- provenance narrative.

Entrada:

```text
support mode
+
Evidence availability states
```

Saída:

```text
supported / unsupported
```

deterministicamente.

---

# 25. Evidence Ordering

Evidence é semanticamente um:

```text
set
```

não uma lista ordenada.

Portanto:

```text
[A, B]
```

e:

```text
[B, A]
```

representam o mesmo support set.

Responses devem usar ordenação determinística, por exemplo:

```text
source_type ASC
source_id ASC
```

---

# 26. Duplicate Evidence

Request:

```text
[A, A]
```

não deve gerar dois links.

Preferência:

```text
reject duplicate EvidenceRefs
```

como `INVALID_REQUEST`, porque isso detecta erro do caller.

Canonical storage continua protegido por UNIQUE.

---

# 27. Episode Support

Adicionar support opcional a Episode.

Create conceitual:

```json
{
  "scope": "project:sofias-memory",
  "kind": "decision",
  "summary": "...",
  "occurred_at": "...",
  "provenance": {
    "origin_kind": "assistant_generated",
    "source_system": "sofias-personal-assistant"
  },
  "support": {
    "mode": "all_required",
    "refs": [
      {
        "source_type": "session_entry",
        "source_id": "..."
      },
      {
        "source_type": "memory_item",
        "source_id": "..."
      }
    ]
  }
}
```

---

# 28. Existing Episode Compatibility

Request sem:

```text
support
```

continua válido.

Não tornar Evidence obrigatória retroativamente.

---

# 29. Episode Persistence

Adicionar conceitualmente:

```text
episodes.support_mode nullable
```

e link table:

```text
episode_evidence_refs
├── episode_id
├── evidence_ref_id
└── PK/UNIQUE(episode_id, evidence_ref_id)
```

Quando:

```text
support_mode IS NULL
```

o Episode não possui support dependency declarada.

---

# 30. ContextStatement Support

Evidence de Checkpoint é **statement-level**.

Não associar Evidence apenas ao ContextCheckpoint inteiro.

Exemplo:

```text
current_state
    supported by Episode A

next_action
    supported by SessionEntry B

objective
    no explicit support
```

---

# 31. Why Statement-Level

Um checkpoint pode continuar parcialmente válido se apenas uma informação perder suporte.

Essa granularidade será necessária no SM-CTX-005.

Não modelar:

```text
checkpoint -> one giant evidence set
```

porque isso destruiria a possibilidade de scrub parcial.

---

# 32. ContextStatement Persistence

Adicionar:

```text
context_checkpoint_statements.support_mode nullable
```

e:

```text
context_statement_evidence_refs
├── statement_id
├── evidence_ref_id
└── PK/UNIQUE(statement_id, evidence_ref_id)
```

---

# 33. Statement Public Shape

Passa conceitualmente de:

```json
{
  "statement_id": "...",
  "text": "..."
}
```

para:

```json
{
  "statement_id": "...",
  "text": "...",
  "support": {
    "mode": "all_required",
    "evidence": [
      {
        "source_type": "episode",
        "source_id": "...",
        "availability": "available"
      }
    ]
  }
}
```

`support` continua opcional.

---

# 34. Checkpoint Create Extension

`POST /context/checkpoints` passa a aceitar support em cada statement.

Não alterar:

- revision semantics;
- current semantics;
- Context Stream semantics;
- create race behavior.

---

# 35. Checkpoint Supersede Extension

`POST /context/checkpoints/{id}/supersede` também passa a aceitar support em cada statement do replacement.

Como Supersede é full replacement:

```text
Evidence support also is full replacement
```

Nada é herdado silenciosamente.

---

# 36. No Evidence Mutation In-Place

Não criar:

```http
PATCH /episodes/{id}/evidence
PATCH /context/statements/{id}/evidence
```

Episode permanece semanticamente immutable.

Checkpoint muda por nova revision.

---

# 37. Existing Resources Are Not Backfilled

Episodes e ContextStatements criados antes do CTX-004:

```text
support = null
```

Não inferir Evidence.

Não criar migration data backfill usando provenance.

---

# 38. Episode Evidence Source — SessionEntry

Resolver:

```text
session_entry
```

contra:

```text
session_entries.id
```

### AVAILABLE

row existe.

### IMPORTANT

Se parent Session estiver:

```text
archived
```

SessionEntry continua:

```text
AVAILABLE
```

Session archive não é Forget.

---

# 39. Missing SessionEntry

Se row não existir:

```text
UNAVAILABLE
```

Não classificar automaticamente como FORGOTTEN porque SessionEntry não possui Forget tombstone próprio no baseline atual.

---

# 40. Evidence Source — MemoryItem

Resolver contra:

```text
memory_items
```

### AVAILABLE

```text
ACTIVE
SUPERSEDED
```

SUPERSEDED continua sendo historical Evidence válida.

### FORGOTTEN

```text
lifecycle = forgotten
```

### UNAVAILABLE

row inexistente ou estado estruturalmente inválido.

---

# 41. Evidence Source — Episode

Resolver contra:

```text
episodes
```

### AVAILABLE

```text
ACTIVE
```

### UNAVAILABLE

```text
INVALIDATED
```

Um Episode explicitamente invalidado não deve continuar sustentando novas claims.

### FORGOTTEN

```text
FORGOTTEN
```

---

# 42. Evidence Source — Source

Resolver contra Knowledge:

```text
sources
```

### AVAILABLE

Source authoritative/usable segundo seu lifecycle.

Normalmente:

```text
SourceStatus.ACTIVE
```

### FORGOTTEN

Source possui authoritative state de deletion/Forget.

Normalmente:

```text
SourceStatus.DELETED
```

### UNAVAILABLE

Estados como:

```text
pending
processing
failed
deleting
```

quando não satisfazem o requisito de Evidence authoritative.

---

# 43. Evidence Source — Document

Document availability deve considerar não apenas existência da row, mas o estado authoritative associado.

Um Document normalmente é AVAILABLE quando:

- row existe;
- Source correspondente continua ACTIVE;
- Document continua authoritative/active para seu generation contract.

Se Source foi forgotten/deleted:

```text
FORGOTTEN
```

Se row existe mas não é mais authoritative/current:

```text
UNAVAILABLE
```

Não usar mera existência física como prova de Evidence válida.

---

# 44. Evidence Source — Chunk

Mesma regra de Document.

AVAILABLE exige que o Chunk continue authoritative/eligible segundo:

- Source;
- Document;
- Dataset generation;
- `is_active`.

Se a Source foi Forgotten:

```text
FORGOTTEN
```

Se existe fisicamente mas ficou fora da geração authoritative:

```text
UNAVAILABLE
```

---

# 45. Dataset Lifecycle Is Resolver Context

Embora `dataset` não seja EvidenceSourceType v1, resolver de:

```text
source
document
chunk
```

pode precisar consultar Dataset lifecycle/generation para determinar availability.

Isso não transforma Dataset em EvidenceRef.

---

# 46. Source Resolver Interface

Criar abstraction interna estreita.

Exemplo conceitual:

```python
class EvidenceResolver(Protocol):
    async def resolve(
        self,
        source_type: EvidenceSourceType,
        source_id: UUID,
    ) -> EvidenceResolution:
        ...
```

Não criar plugin registry.

Resolvers são first-party e fechados.

---

# 47. Resolution Result

Internamente deve distinguir pelo menos:

```text
exists
availability
```

e, se necessário:

```text
resource lifecycle
```

Não retornar conteúdo completo salvo se realmente necessário.

Evidence validation precisa de identity/lifecycle, não de hydration semântica integral.

---

# 48. Avoid N+1 Where Material

Ao validar vários refs do mesmo type, repository poderá implementar batch lookup.

Não criar abstração sofisticada prematuramente.

Mas evitar dezenas de queries individuais se o request já fornece collections bounded.

---

# 49. Request Bounds

Support Evidence arrays devem possuir server-side bound.

Não inventar limite minúsculo.

Se o Contract não define valor exato, escolher limite operacional razoável e documentá-lo.

O objetivo é impedir payload abusivo e dependency graphs enormes.

Não transformar isso em novo Product decision salvo se o limite afetar uso legítimo previsto.

---

# 50. Evidence Validation on Create

Para criar novo derived contextual content:

```text
every referenced EvidenceRef must exist
and must currently be AVAILABLE
```

Não aceitar nova derivação sustentada por:

```text
UNAVAILABLE
FORGOTTEN
```

---

## 50.1 Explicit Evidence Project Isolation

Para Episode ou ContextStatement de checkpoint em `project:X`:

- `episode` Evidence MUST pertencer ao mesmo `project:X`;
- `memory_item` Evidence com scope `project:Y` exige `Y == X`;
- MemoryItem `global` MAY ser usado somente quando explicitamente referenciado;
- SessionEntry, Source, Document e Chunk não adquirem ownership de projeto.

Validar no authoritative write path de Episode Create, Checkpoint Create e
Supersede. Cross-project Episode/project-Memory refs são rejeitadas com
`422 INVALID_EVIDENCE_REFERENCE`, sem owner, links ou successful ledger parcial.
Isso não é ACL e não permite auto-inclusão de Global Cognitive Memory no Recall.
Não exigir mesmo context_key para Episode Evidence: a boundary aqui é o projeto.

---

# 51. `any_sufficient` Does Not Permit Bad Refs on Creation

Mesmo em:

```text
any_sufficient
```

não aceitar request novo contendo refs inexistentes/forgotten apenas porque outra Evidence está AVAILABLE.

Motivo:

> caller está declarando um support set explícito; referências inválidas devem ser detectadas, não escondidas.

`any_sufficient` governa perda posterior de suporte, não tolerância a request malformado.

---

# 52. No Hidden Evidence Fetching

Se caller informa:

```text
Episode A
```

Sofias Memory valida Episode A.

Não deve buscar semanticamente outros Episodes para “fortalecer” a claim.

---

# 53. Evidence Cycle Rule

Dependency graph de Evidence MUST ser acíclico.

Principal caso v1:

```text
Episode -> Episode
```

Não permitir:

```text
A -> A
```

nem:

```text
A -> B -> C -> A
```

---

# 54. Why Cycles Are Forbidden

SM-CTX-005 precisará avaliar support transitivamente.

Cycle produziria:

```text
A is supported because B
B is supported because A
```

sem Evidence independente.

Isso é semanticamente inválido.

---

# 55. Practical Creation Property

Como Episode não possui PATCH e normalmente referencia Episodes já existentes, o graph tende naturalmente a apontar:

```text
newer Episode -> older Episode
```

o que já reduz a possibilidade de cycle.

Ainda assim, cycle validation deve existir como invariant defensiva.

---

# 56. Cycle Detection

Implementar cycle detection deterministicamente.

Pode usar:

- recursive CTE PostgreSQL;
- traversal service-side bounded;
- outra solução simples adequada ao volume.

Não usar Neo4j.

---

# 57. Self Reference

Se tecnicamente for possível um caller fornecer o ID do recurso em criação através de algum future/internal path:

```text
self evidence
```

deve ser rejeitada.

No public Create com server-generated UUID pode ser estruturalmente impossível, mas o invariant deve ser protegido no domínio.

---

# 58. Checkpoint Statements Cannot Form Evidence Cycles in v1

`context_statement` não é EvidenceSourceType.

Logo:

```text
ContextStatement -> ContextStatement
```

não existe no Contract v1.

Não criar suporte para isso.

---

# 59. Evidence Canonicalization

Para cada incoming EvidenceRef:

```text
resolve source
validate availability
get-or-create contextual_evidence_ref
link to owner
```

`get-or-create` precisa ser race-safe.

Constraint:

```text
UNIQUE(source_type, source_id)
```

é authority.

---

# 60. Concurrent Canonical Evidence Creation

Dois requests concorrentes referenciando o mesmo source devem convergir para:

```text
one contextual_evidence_refs row
```

Não depender de:

```text
SELECT then INSERT
```

sem conflict handling.

---

# 61. Support Persistence Invariants

Service deve garantir:

```text
support_mode IS NULL
    => zero Evidence links
```

e:

```text
support_mode IS NOT NULL
    => at least one Evidence link
```

Não criar trigger complexo apenas para cross-table cardinality se service + tests forem suficientes.

---

# 62. Evidence Link Deletion

Quando owner semantic content é removido:

## Episode direct Forget

Os outgoing Evidence links usados para sustentar o próprio Episode podem ser removidos/scrubbed.

Porém Evidence refs **de outros recursos apontando para esse Episode** devem permanecer resolvíveis.

O Episode tombstone continua existindo e o resolver retornará:

```text
FORGOTTEN
```

---

## Checkpoint direct Forget

ContextStatements são eliminados conforme CTX-002.

Links dos statements podem usar:

```text
ON DELETE CASCADE
```

para desaparecer junto com os statements.

Canonical EvidenceRef rows compartilhadas não devem ser apagadas se ainda forem referenciadas por outros recursos.

---

# 63. Inbound vs Outbound Dependency

Distinção:

```text
Episode A -> Evidence X
```

A possui outgoing dependency para X.

Se Episode B declara:

```text
Episode B -> Episode A
```

A possui inbound dependent B.

Direct Forget de A pode remover seu próprio outgoing support, mas precisa continuar estruturalmente identificável como source forgotten para B.

Essa distinção será essencial no SM-CTX-005.

---

# 64. No Cascading Forget Yet

Mesmo que o grafo agora exista:

```text
A -> B -> C
```

SM-CTX-004 não propaga Forget.

Exemplo:

```text
Forget C
```

pode fazer resolver de B indicar perda de suporte, mas não deverá ainda automaticamente:

- invalidate B;
- scrub B;
- cascade to A.

Essa mutation automática pertence ao SM-CTX-005.

---

# 65. Support Evaluation Is Available Now

Embora a propagação ainda não exista, service interno deve conseguir calcular:

```text
is_supported(resource)
```

a partir do estado atual dos refs.

Isso será reutilizado pelo próximo Slice.

---

# 66. Episode GET

Quando Episode possui support, GET deve retornar:

- support mode;
- Evidence identities;
- current availability.

Não precisa retornar conteúdo integral das Evidence sources.

---

# 67. Checkpoint GET

ContextStatements com support devem retornar Evidence refs e availability.

Statements sem support continuam simples.

---

# 68. Read-Time Availability

GET de um resource supported deve refletir disponibilidade atual do source, não uma observação antiga aceita cegamente.

Isso prepara o sistema para:

```text
source changes state
    ↓
GET reflects changed evidence availability
```

mesmo antes da propagation mutation do CTX-005.

---

# 69. Supported Resource Does Not Auto-Mutate on GET

Mesmo se GET detectar:

```text
support no longer sufficient
```

SM-CTX-004 não deve transformar silenciosamente lifecycle durante read.

GET é read.

SM-CTX-005 implementará convergence mutation apropriada.

---

# 70. Optional Support Status in Result

Se o Contract já prevê um campo derivado de sufficiency, usar o shape aprovado.

Caso contrário:

- retornar Evidence availability conforme Contract;
- manter support evaluator interno;
- não inventar public field apenas por conveniência.

---

# 71. Idempotency Digest — Episode

Episode create canonical digest deve passar a incluir support quando presente.

Canonical support:

```text
mode
Evidence set sorted canonically by (source_type, source_id)
```

A ordem original da Evidence array não deve alterar digest.

---

# 72. Backward Idempotency Compatibility

Quando `support` for omitido:

```text
canonical form should preserve the pre-SM-CTX-004 semantic request
```

Não adicionar um artificial:

```json
{"support": null}
```

ao canonical representation se isso quebrar replay de requests preexistentes com o mesmo ledger.

A implementação deve considerar compatibility do digest.

---

# 73. Checkpoint Idempotency Digest

Create/Supersede digest passa a incluir support por ContextStatement.

Para cada statement:

```text
text
support mode if present
canonical sorted Evidence set
```

Statement server-generated ID não entra no digest.

---

# 74. Evidence Source Validation and Idempotent Replay

Replay já committed deve ser resolvido pelo ledger antes de revalidar Evidence atual quando necessário para preservar semântica de idempotência.

Exemplo:

```text
Create Episode with Evidence A
operation commits
A later becomes unavailable
same request retry with same Idempotency-Key
```

O retry deve replayar o outcome original.

Não reinterpretar o mesmo operation replay como uma nova create request.

---

# 75. New Request Revalidates Evidence

Nova Idempotency-Key:

```text
same semantic payload
```

é uma nova operation.

Evidence deve ser validada no estado atual.

Não usar o sucesso histórico de outra key como autorização.

---

# 76. Migration Strategy

Criar migration aditiva posterior ao head real de CTX-003.

Não congelar revision number agora.

Antes de implementar:

```bash
uv run alembic heads
```

---

# 77. Proposed Migration Content

Criar:

```text
evidence_source_type enum
support_mode enum

contextual_evidence_refs
episode_evidence_refs
context_statement_evidence_refs
```

Alterar:

```text
episodes
    add support_mode nullable

context_checkpoint_statements
    add support_mode nullable
```

Se persistir availability observation:

```text
evidence_availability enum
```

pode ser necessário.

Mas availability cached não se torna authority.

---

# 78. Suggested Constraints

## Canonical Evidence

```text
UNIQUE(source_type, source_id)
```

## Episode link

```text
PRIMARY/UNIQUE(episode_id, evidence_ref_id)
```

## Statement link

```text
PRIMARY/UNIQUE(statement_id, evidence_ref_id)
```

## Support mode

enum fechado:

```text
all_required
any_sufficient
```

---

# 79. FK Delete Policies

## Episode link → Episode

Preferir:

```text
ON DELETE CASCADE
```

somente como structural hard-delete safety.

Hard delete público continua inexistente.

## Episode link → EvidenceRef

```text
ON DELETE RESTRICT
```

ou política equivalente que impeça dangling links.

## Statement link → Statement

```text
ON DELETE CASCADE
```

porque ContextStatement pode ser removido no Forget.

---

# 80. Canonical Evidence Garbage

Não é obrigatório implementar garbage collection de EvidenceRef órfã neste Slice.

Uma canonical row sem links:

```text
small structural metadata
```

não é semantic content duplicado.

GC pode ser feita deterministicamente se simples, mas não ampliar o Slice por isso.

---

# 81. No Graph Outbox

Nenhum Evidence write contextual produz:

```text
graph_outbox
```

Contextual Evidence continua PostgreSQL-only.

---

# 82. No Neo4j

Cycle detection:

```text
PostgreSQL/service-side
```

Nunca Neo4j.

Não criar Episode graph projection.

---

# 83. No PipelineRun

Evidence validation/linking ocorre dentro da mutation contextual síncrona.

Não criar PipelineRun.

---

# 84. Transaction Boundary — Episode Create

Fluxo conceitual:

```text
validate request
canonicalize support
optional idempotency pre-check
embedding call outside transaction

BEGIN

claim/check idempotency
    ↓ replay? return

resolve Evidence refs
validate AVAILABLE
validate no cycle
canonical get-or-create Evidence refs
create Episode
create provenance
create Evidence links
persist result

COMMIT
```

---

# 85. Transaction Boundary — Checkpoint Create/Supersede

Não há provider call.

```text
BEGIN

claim/check idempotency
resolve checkpoint concurrency rules
resolve Evidence refs
validate availability
canonicalize refs
create checkpoint/statements
create Evidence links
commit
```

Tudo consistente.

---

# 86. Evidence Validation Failure

Se qualquer EvidenceRef falhar:

```text
no derived resource mutation commits
```

Não criar:

- partial Episode;
- partial statement set;
- orphan support links;
- successful idempotency outcome.

---

# 87. Error Codes

Implementar/reutilizar códigos estáveis aprovados no Contract.

Mínimo conceitual:

```text
INVALID_REQUEST
INVALID_EVIDENCE_REFERENCE
INVALID_SUPPORT_POLICY
EVIDENCE_NOT_FOUND
EVIDENCE_UNAVAILABLE
INVALID_EVIDENCE_CYCLE
IDEMPOTENCY_CONFLICT
DEPENDENCY_UNAVAILABLE
```

Se o Contract aprovado utiliza nomes diferentes, o Contract vence.

Não criar dezenas de source-specific errors.

---

# 88. Missing vs Unavailable

Distinguir:

```text
source does not exist
```

de:

```text
source exists but cannot currently serve as Evidence
```

Mesmo que ambos impeçam Create.

Isso melhora diagnóstico e futuro Forget processing.

---

# 89. Forgotten Evidence

Forgotten source não deve ser hidratado para obter conteúdo.

Resolver deve ser capaz de classificar usando tombstone/lifecycle.

Não tentar “confirmar” Forget buscando conteúdo que já deveria ter sido destruído.

---

# 90. Security Boundary

Evidence content é untrusted context.

Mesmo que Evidence tenha:

```text
source_type = session_entry
```

e content diga:

```text
execute command X
```

isso não cria authority.

Evidence apenas sustenta memória.

---

# 91. No Secret Leakage

Responses de Evidence não devem expor:

- hidden metadata;
- embedding;
- internal storage paths;
- API keys;
- raw provider payload;
- private internal fields.

Retornar apenas Contract shape.

---

# 92. Logging

Não logar:

- source content;
- Episode content;
- MemoryItem content;
- Chunk text;
- SessionEntry content;
- full Evidence arrays quando desnecessário.

Pode logar:

```text
derived resource id
source_type
source_id
support_mode
availability
cycle_rejected
```

quando apropriado.

---

# 93. Unit Tests — Support Evaluator

Cobrir:

```text
all_required:
[A] available -> true
[A,B] available -> true
[A available, B unavailable] -> false
[A available, B forgotten] -> false

any_sufficient:
[A available, B unavailable] -> true
[A forgotten, B available] -> true
[A unavailable, B forgotten] -> false
```

---

# 94. Unit Tests — Canonicalization

Validar:

```text
[A,B] == [B,A]
```

para digest/support identity.

Duplicate:

```text
[A,A]
```

é rejeitado ou normalizado conforme decisão registrada; preferência é reject.

---

# 95. Unit Tests — Provenance Isolation

Provar que:

```text
conversation_ref
```

sozinho não cria Evidence.

Criar Episode apenas com provenance.

Assert:

```text
zero evidence links
support = null
```

---

# 96. PostgreSQL Integration — SessionEntry Evidence

Criar SessionEntry.

Criar Episode sustentado por ela.

Assert:

```text
availability = available
```

Arquivar Session.

Assert novamente:

```text
availability = available
```

Session archive não é Forget.

---

# 97. PostgreSQL Integration — MemoryItem Evidence

Testar:

```text
ACTIVE -> available
SUPERSEDED -> available
FORGOTTEN -> forgotten
```

---

# 98. PostgreSQL Integration — Episode Evidence

Testar:

```text
ACTIVE -> available
INVALIDATED -> unavailable
FORGOTTEN -> forgotten
```

---

# 99. PostgreSQL Integration — Source Evidence

Testar lifecycle relevante:

```text
ACTIVE -> available
DELETED/forgotten -> forgotten
non-authoritative state -> unavailable
```

conforme estado real do domínio Source.

---

# 100. PostgreSQL Integration — Document Evidence

Validar:

- current authoritative Document;
- inactive/non-current Document;
- forgotten Source ancestor.

---

# 101. PostgreSQL Integration — Chunk Evidence

Validar:

- current active Chunk;
- old generation/inactive Chunk;
- forgotten Source ancestor.

---

# 102. Canonical Evidence Race

Dois creates concorrentes usando o mesmo:

```text
(source_type, source_id)
```

devem produzir:

```text
one contextual_evidence_refs row
```

mesmo quando owners são diferentes.

---

# 103. Episode Dependency Chain

Criar:

```text
Episode A
Episode B supported by A
Episode C supported by B
```

Validar links:

```text
C -> B -> A
```

sem Neo4j.

---

# 104. Cycle Test

Se uma internal/test path conseguir construir tentativa cíclica:

```text
A -> B -> C -> A
```

a operação deve falhar:

```text
INVALID_EVIDENCE_CYCLE
```

sem partial write.

---

# 105. Checkpoint Statement Evidence

Incluir testes PostgreSQL/HTTP em Episode Create, Checkpoint Create e Supersede:
owner `project:a` com Episode Evidence `project:b` falha; MemoryItem
`project:b` também falha; refs do mesmo projeto são aceitas. MemoryItem `global`
explicitamente referenciado é aceito, sem participação automática no Recall.
SessionEntry/Knowledge Evidence não exige project ownership inventado.

Criar checkpoint:

```text
current_state -> Episode A
next_action   -> SessionEntry B
objective     -> no support
```

Confirmar links independentes por statement.

---

# 106. Supersede Evidence Replacement

Revision 1:

```text
current_state supported by Episode A
```

Revision 2 replacement:

```text
current_state supported by Episode B
```

Resultado:

```text
revision 1 retains A historical support
revision 2 uses B
```

Não mover links da revision antiga.

---

# 107. Full Replacement Support

Se revision 1 tinha support e revision 2 omite:

```text
revision 2 support = null
```

Não herdar Evidence.

---

# 108. Existing Episode Compatibility Test

Criar Episode no shape CTX-001 sem support.

Após CTX-004:

```text
GET succeeds
support = null/absent per Contract
```

Não classificar como unsupported.

---

# 109. Existing Checkpoint Compatibility Test

Mesma regra.

Checkpoints anteriores sem Evidence continuam utilizáveis.

---

# 110. Direct Forget Episode With Dependents

Criar:

```text
A
B supported by A
```

Forget A.

No SM-CTX-004:

- A torna-se forgotten;
- B ainda não é automaticamente mutated;
- resolver de Evidence para B deve indicar A como forgotten;
- support evaluator deve dizer B sem suporte suficiente quando `all_required`.

A mutation de B ocorrerá apenas no SM-CTX-005.

---

# 111. Direct Forget Checkpoint Statement

Ao esquecer um checkpoint:

- statements são removidos;
- statement Evidence links desaparecem;
- canonical EvidenceRef compartilhada continua se usada em outro lugar.

---

# 112. No Accidental Cascades

FKs não podem fazer:

```text
Forget/Delete source resource
    -> database cascade deletes contextual derived resource
```

Forget semantics são de aplicação e serão implementadas explicitamente.

---

# 113. Evidence Resolver Must Be Reusable

Projetar resolver para reutilização no:

```text
SM-CTX-005
SM-CTX-006
SM-CTX-007
```

sem transformá-lo em framework genérico.

A abstraction deve continuar first-party e fechada.

---

# 114. Expected File Impact

Possíveis novos arquivos:

```text
sofias_memory/domain/contextual_evidence.py

sofias_memory/services/contextual_evidence.py

sofias_memory/infrastructure/postgres/models/contextual_evidence_ref.py
sofias_memory/infrastructure/postgres/models/episode_evidence_ref.py
sofias_memory/infrastructure/postgres/models/context_statement_evidence_ref.py

sofias_memory/infrastructure/postgres/repositories/contextual_evidence.py
```

Alterações esperadas:

```text
episodes model/service/schema
context checkpoint statement model/service/schema
PostgresUnitOfWork
domain exports
model exports
repository exports
tests
migration
```

Não force nomes exatos se o código real indicar estrutura local melhor.

---

# 115. Migration Tests

Validar:

- migration chain;
- enum values;
- canonical Evidence table;
- UNIQUE source identity;
- Episode links;
- Statement links;
- support_mode columns;
- FK policies;
- downgrade;
- nenhum Neo4j schema;
- nenhum graph_outbox type.

---

# 116. Contract/OpenAPI Tests

Validar que existing endpoints agora aceitam support conforme Contract.

Continuam inexistindo:

```http
POST /api/v1/evidence
PATCH /api/v1/evidence/*
DELETE /api/v1/evidence/*
POST /api/v1/context/recall
```

---

# 117. `/api/v1/info`

Ainda MUST NOT anunciar:

```text
contextual_project_memory = "1"
```

O Contract v1 continua incompleto.

---

# 118. Human Validation

Executar usando API real.

## Step A — Seed sources

Criar:

```text
SessionEntry S
MemoryItem M
Episode A
```

---

## Step B — Episode with `all_required`

Criar Episode B:

```text
support:
    all_required
    [S, M, A]
```

Confirmar:

```text
3 EvidenceRefs
all availability = available
supported = true internally
```

---

## Step C — Episode with `any_sufficient`

Criar Episode C:

```text
support:
    any_sufficient
    [M, A]
```

---

## Step D — Checkpoint field-level support

Criar checkpoint:

```text
objective
    no support

current_state
    support = Episode B

next_action
    support = SessionEntry S
```

Confirmar associações distintas.

---

## Step E — Session archive

Arquivar Session de S.

Confirmar:

```text
SessionEntry S remains available Evidence
```

---

## Step F — Invalidate Episode A

Confirmar:

```text
EvidenceRef(A).availability = unavailable
```

e:

```text
all_required B -> internally unsupported
any_sufficient C remains supported if M is available
```

Sem mutation automática de B/C.

---

## Step G — Forget MemoryItem M

Confirmar:

```text
EvidenceRef(M).availability = forgotten
```

Sem automatic propagation ainda.

---

## Step H — Existing unsupported resources

Confirmar que GET de B/C:

- não inventa Evidence;
- reflete availability atual;
- não altera lifecycle por efeito colateral.

---

## Step I — Regression

Confirmar:

- Episode without support continua válido;
- Checkpoint without support continua válido;
- Cognitive Memory continua funcional;
- Sessions continuam funcionais;
- Knowledge Recall continua funcional;
- nenhum graph_outbox contextual;
- nenhum PipelineRun contextual.

---

# 119. Acceptance Criteria

## AC-004.1

Episode pode referenciar SessionEntry.

## AC-004.2

Episode pode referenciar MemoryItem.

## AC-004.3

Episode pode referenciar outro Episode.

## AC-004.4

ContextStatement pode possuir Evidence própria.

## AC-004.5

Evidence inexistente é rejeitada.

## AC-004.6

Evidence forgotten/unavailable é rejeitada para novo conteúdo derivado.

## AC-004.7

`all_required` é suportado deterministicamente.

## AC-004.8

`any_sufficient` é suportado deterministicamente.

## AC-004.9

Self-reference é rejeitada quando tecnicamente possível.

## AC-004.10

Cycle indireto é rejeitado.

## AC-004.11

Evidence não copia integralmente source content.

## AC-004.12

Mesmo source ref é canonicalizado/reutilizado.

## AC-004.13

Evidence em checkpoints é statement-level.

---

# 120. Additional Acceptance Criteria

## AC-004.14

Provenance não cria Evidence implicitamente.

## AC-004.15

Recursos preexistentes sem support continuam válidos.

## AC-004.16

Session archive não torna SessionEntry Evidence unavailable.

## AC-004.17

MemoryItem SUPERSEDED continua disponível como historical Evidence.

## AC-004.18

Episode INVALIDATED deixa de ser Evidence disponível.

## AC-004.19

Forget de source é refletido como `forgotten`, não como generic `unavailable`, quando lifecycle authoritative permite distinguir.

## AC-004.20

Read-time Evidence resolution não muta derived lifecycle.

## AC-004.21

Evidence order não altera semantic identity/digest.

## AC-004.22

Nenhuma Evidence API independente foi criada.

## AC-004.23

Nenhuma operação cria graph_outbox.

## AC-004.24

Nenhuma operação cria PipelineRun.

## AC-004.25

Episode Evidence e project-scoped MemoryItem Evidence pertencem ao mesmo projeto
do owner; referências cross-project são rejeitadas atomicamente nas três mutations.

## AC-004.26

Global MemoryItem Evidence exige referência explícita; não há auto-inclusão no
Contextual Recall nem ownership de projeto inventado para SessionEntry/Knowledge.

---

# 121. Required Test Commands

Executar:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

mais suites PostgreSQL opt-in aplicáveis.

Quando houver migration:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Não declarar PASS de teste não executado.

---

# 122. Findings Classification

## BLOCKER

Exemplos:

- provenance cria dependency implícita;
- Evidence refs duplicam semantic content;
- support evaluator depende de LLM;
- stale availability é tratada como authority;
- cycle não é detectado;
- Session archive é confundido com Forget;
- forgotten Evidence é aceita em nova derivação;
- statement-level Evidence é perdida.

## IMPORTANT

Afeta correctness/objetivo do Slice.

## MINOR

Não impede valor entregue.

## DEFERRED

Pertence a SM-CTX-005+ ou feature futura.

---

# 123. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- tornar Evidence obrigatória para todo Episode;
- tornar Evidence obrigatória para todo ContextStatement;
- inferir Evidence automaticamente;
- adicionar novos source types públicos;
- transformar Dataset em EvidenceSourceType;
- criar public Evidence management API;
- usar Neo4j;
- usar LLM para support;
- alterar support modes;
- mudar provenance semantics;
- alterar Forget behavior antes do SM-CTX-005;
- criar cross-project Evidence automática.

---

# 124. Implementation Order

Ordem recomendada:

```text
1. verify CTX-001..003 completion
2. domain enums/primitives
3. migration
4. canonical Evidence ORM/repository
5. EvidenceResolver
6. source-specific resolvers
7. deterministic SupportEvaluator
8. Episode support persistence
9. Episode API/schema integration
10. ContextStatement support persistence
11. checkpoint create integration
12. checkpoint supersede integration
13. cycle detection
14. idempotency canonicalization updates
15. resolver lifecycle tests
16. canonical Evidence race tests
17. compatibility regression
18. Human Validation
```

---

# 125. Slice Exit Criteria

SM-CTX-004 está pronto para closeout quando:

```text
AC-004.1..26 satisfied
all Evidence source types validated
support evaluation deterministic
provenance/evidence separation proven
canonical Evidence race proven
cycle protection proven
preexisting resources remain compatible
Session archive semantics proven
no automatic propagation
no graph side effects
no PipelineRun side effects
no BLOCKER remains
```

---

# 126. Expected Implementation Report

O executor deverá retornar:

```text
SM-CTX-004 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- previous migration head:
- new migration head:
- tables/enums:
- constraints/indexes:
- availability strategy:

Dependencies verified
- SM-CTX-001:
- SM-CTX-002:
- SM-CTX-003:

Evidence resolvers
- session_entry:
- memory_item:
- episode:
- source:
- document:
- chunk:

Verified
- unit:
- migration:
- PostgreSQL integration:
- all_required:
- any_sufficient:
- canonical Evidence race:
- cycle detection:
- provenance isolation:
- Session archive:
- compatibility:
- graph isolation:
- pipeline isolation:

Acceptance Criteria
- AC-004.1 PASS/FAIL
...
- AC-004.26 PASS/FAIL

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

Não avançar automaticamente para SM-CTX-005.

---

# 127. Handoff to Executor

## Objective

Implementar:

```text
SM-CTX-004 — First-Class Evidence and Deterministic Support
```

## Required entry conditions

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
```

## Core invariant

```text
Provenance != Evidence
```

Somente Evidence explicitamente declarada cria dependency semantics.

## Scope

```text
EvidenceRef
canonical Evidence identity
source resolvers
Evidence availability
all_required
any_sufficient
Episode support
ContextStatement support
cycle prevention
deterministic support evaluation
```

## Out of Scope

```text
automatic Forget propagation
Contextual Recall
Knowledge composition
automatic Evidence inference
LLM support evaluation
Evidence management API
Neo4j
```

## Expected result

Ao final:

> Episodes e ContextStatements podem declarar explicitamente de quais recursos dependem, o Sofias Memory consegue verificar deterministicamente se essas evidências continuam disponíveis, e essa informação está pronta para alimentar Forget propagation sem depender de inferência, similarity ou LLM.
