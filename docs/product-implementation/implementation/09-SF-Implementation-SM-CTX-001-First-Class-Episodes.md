# Sofias Memory — Implementation: SM-CTX-001 — First-Class Episodes

**Document:** `implementation/09-SF-Implementation-SM-CTX-001-First-Class-Episodes.md`\
**Status:** APPROVED\
**Date:** 2026-11-18\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-001 — First-Class Episodes`

**Approved baseline:**
- `../01-SF-Discovery-Memoria-Contextual-Por-Projeto.md`
- `../02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md`
- `../03-SF-PRD-Memoria-Contextual-Por-Projeto.md`
- `../04-SF-Spec-Memoria-Contextual-Por-Projeto.md`
- `../05-SF-Contract-Memoria-Contextual-Por-Projeto.md`
- `../06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`
- `../07-SF-ADRs-Memoria-Contextual-Por-Projeto.md`
- `../08-SF-Technical-Backlog-Memoria-Contextual-Por-Projeto.md`

---

# 1. Objective

Implementar o primeiro vertical slice de Contextual Project Memory:

```text
SM-CTX-001 — First-Class Episodes
```

Ao final deste Slice, um Consumer deverá conseguir:

```text
create Episode
    ↓
persist historical experience
    ↓
restart Sofias Memory
    ↓
retrieve Episode
    ↓
invalidate
    ↓
forget
```

sem:

- criar `MemoryItem`;
- criar `SessionEntry`;
- criar `PipelineRun`;
- criar `graph_outbox`;
- depender de Neo4j.

O valor realmente utilizável ao fim do Slice é:

> preservar explicitamente uma experiência histórica relevante como recurso first-class e recuperá-la de forma durável após restart.

---

# 2. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-001
```

Durante este Slice, decisões locais e reversíveis podem ser tomadas autonomamente.

Não alterar decisões APPROVED.

---

# 3. Repository Baseline Observed

Antes da implementação, considerar o seguinte estado real do repositório.

## 3.1 Alembic head

O migration head atual observado durante o planejamento é:

```text
0018
```

Arquivo:

```text
migrations/versions/0018_create_native_cognitive_memory.py
```

Portanto, salvo mudança concorrente no repositório antes da implementação, o Slice deverá iniciar com:

```text
Revision ID: 0019
Revises: 0018
```

Quando a implementação começar, MUST verificar o head real.

Se tiver mudado, usar o head real e registrar isso no relatório.

---

## 3.2 Native Cognitive Memory é a principal referência de implementação

Usar como padrões de engenharia, não como domínio a ser copiado integralmente:

```text
sofias_memory/services/cognitive_memory.py
sofias_memory/services/cognitive_memory_recall.py

sofias_memory/schemas/memories.py

sofias_memory/api/routes/memories.py

sofias_memory/infrastructure/postgres/models/memory_item.py
sofias_memory/infrastructure/postgres/models/memory_provenance.py
sofias_memory/infrastructure/postgres/models/cognitive_memory_idempotency.py

sofias_memory/infrastructure/postgres/repositories/memory_items.py
sofias_memory/infrastructure/postgres/repositories/memory_provenance.py
sofias_memory/infrastructure/postgres/repositories/cognitive_memory_idempotency.py

sofias_memory/infrastructure/postgres/unit_of_work.py

sofias_memory/domain/cognitive_memory_idempotency.py
```

Os padrões importantes já existentes são:

- external embedding call antes da PostgreSQL transaction;
- PostgreSQL como autoridade;
- exact pgvector cosine retrieval;
- Unit of Work explícito;
- repositories sem regras de produto;
- routes sem SQL/Cypher;
- lifecycle protegido também por DB constraints;
- idempotency race resolvida por `UNIQUE` + savepoint;
- request digest HMAC;
- tombstone destrutivo;
- nenhuma dependência de Neo4j.

---

# 4. Scope

Implementar neste Slice:

## Domain

- `EpisodeKind`;
- `EpisodeLifecycle`;
- contextual mutation operation identity;
- Episode scope validation;
- optional `context_key`;
- Episode provenance validation;
- canonical Episode embedding text;
- contextual idempotency digest.

## Persistence

- `episodes`;
- `episode_provenance`;
- `contextual_memory_idempotency`;
- PostgreSQL constraints;
- pgvector embedding;
- repositories;
- Unit of Work integration.

## Service

- create;
- get;
- list;
- semantic retrieval interno;
- invalidate;
- direct Forget;
- idempotent replay.

## HTTP

```http
POST /api/v1/episodes
GET  /api/v1/episodes
GET  /api/v1/episodes/{episode_id}
POST /api/v1/episodes/{episode_id}/invalidate
POST /api/v1/episodes/{episode_id}/forget
```

## Tests

- domain;
- migration;
- service;
- HTTP;
- PostgreSQL integration;
- restart durability;
- idempotency race;
- lifecycle;
- exact cosine retrieval;
- absence of graph side effects.

---

# 5. Explicit Out of Scope

MUST NOT implementar neste Slice:

- Evidence;
- `support`;
- `all_required`;
- `any_sufficient`;
- Evidence cycles;
- `ContextCheckpoint`;
- `ContextStatement`;
- Contextual Recall API;
- Knowledge integration;
- SessionEntry expansion;
- Forget propagation para derivados;
- Episode-to-Episode Evidence;
- Neo4j projection;
- graph outbox;
- `PipelineRun`;
- automatic Episode extraction;
- Session distillation;
- automatic promotion;
- automatic contradiction resolution;
- MemorySpace;
- full Contract v1 advertisement.

Se uma boa ideia exigir qualquer item acima:

```text
DEFERRED
```

---

# 6. Important Contract Boundary

O Slice implementa apenas a parte de Episode do Contract v1.

A aplicação MUST NOT ainda anunciar:

```json
{
  "contracts": {
    "contextual_project_memory": "1"
  }
}
```

O Contract completo só será anunciado em `SM-CTX-008`.

Também não adicionar comportamento que faça Consumers assumirem suporte completo a Contextual Project Memory.

---

# 7. Episode Semantics

`Episode` representa:

```text
what happened
```

e não:

```text
what is currently true
```

Exemplo válido:

```text
Em novembro decidimos modelar Episode como recurso first-class.
```

Esse Episode pode continuar histórico mesmo que decisões posteriores mudem.

Episode MUST NOT:

- supersede `MemoryItem`;
- criar `MemoryItem`;
- tornar seu conteúdo current semantic truth;
- ser tratado como SessionEntry.

---

# 8. Episode Kinds

Implementar exatamente os kinds aprovados no Contract v1:

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

Sugestão:

```python
class EpisodeKind(StrEnum):
    DECISION = "decision"
    INVESTIGATION = "investigation"
    EXPERIMENT = "experiment"
    INCIDENT = "incident"
    DISCOVERY = "discovery"
    CHANGE = "change"
    OUTCOME = "outcome"
    LEARNING = "learning"
    MILESTONE = "milestone"
```

Não adicionar outros kinds neste Slice.

---

# 9. Episode Lifecycle

Implementar:

```text
active
invalidated
forgotten
```

State machine:

```text
ACTIVE
  ├── INVALIDATED
  └── FORGOTTEN

INVALIDATED
  └── FORGOTTEN

FORGOTTEN
  └── terminal
```

Não existe:

```text
superseded
restore
unforget
```

para Episode.

---

# 10. Scope Semantics

Episode MUST pertencer a:

```text
project:<key>
```

Não aceitar:

```text
global
```

para Episode.

A gramática de project key deverá permanecer consistente com Cognitive Memory:

```text
project:[a-z0-9][a-z0-9._-]{0,127}
```

Não duplicar regras de validação sem necessidade.

Pode reutilizar os primitives atuais de scope desde que:

```text
global
```

seja explicitamente rejeitado para Episode.

---

# 11. `context_key`

Episode MAY possuir:

```text
context_key
```

mas ele é opcional.

Diferença importante:

```text
Episode sem context_key
    = relevante ao Project Scope como um todo
```

Não transformar Episode sem `context_key` em:

```text
context_key = main
```

automaticamente.

O default `main` será importante para Checkpoints/Recall posteriormente, não para Episode project-wide.

Quando informado, aplicar a gramática aprovada:

```text
[a-z0-9][a-z0-9._:-]{0,127}
```

---

# 12. Provenance

Episode deve possuir provenance first-class.

Criar preferencialmente:

```text
episode_provenance
```

como relação 1:1 com `episodes`.

Essa abordagem é consistente com Cognitive Memory e evita transformar provenance em JSON opaco.

---

# 13. Episode Provenance Public Shape

Implementar o Contract v1, não copiar cegamente o schema cognitivo.

Request conceitual:

```json
{
  "origin_kind": "user_asserted",
  "source_system": "sofias-personal-assistant",
  "conversation_ref": "conversation:42",
  "turn_ref": "turn:18"
}
```

Campos:

```text
origin_kind       required
source_system     required
conversation_ref  optional
turn_ref          optional
```

Não adicionar Provider Session IDs.

---

# 14. Origin Kinds

Reutilizar os valores já aprovados:

```text
user_asserted
tool_observed
imported
inferred
assistant_generated
```

Não renomear ou refatorar `CognitiveMemoryOriginKind` neste Slice apenas para obter um nome mais genérico.

Se for possível reutilizar o enum existente com segurança:

```text
reuse
```

é preferível a modificar contratos existentes.

---

# 15. Confidence

Episode MAY possuir:

```text
confidence
```

com:

```text
0.0 <= confidence <= 1.0
```

Quando:

```text
origin_kind = inferred
```

`confidence` é obrigatório.

Confidence não representa:

- authority;
- permission;
- current truth.

---

# 16. Episode Public Shape

O recurso público não esquecido deverá possuir conceitualmente:

```text
episode_id
scope
context_key
kind
summary
situation
reason
outcome
occurred_at
lifecycle
confidence
provenance
created_at
invalidated_at
forgotten_at
```

Não expor embedding.

---

# 17. Required Create Fields

Para `POST /api/v1/episodes`:

```text
scope
kind
summary
occurred_at
provenance
```

Optional:

```text
context_key
situation
reason
outcome
confidence
```

`support` pertence ao SM-CTX-004 e não deve ser implementado agora.

---

# 18. Text Validation

Não introduzir limites públicos arbitrários não aprovados pelo Contract.

No mínimo:

- `summary` MUST NOT ser blank;
- optional textual fields, quando presentes, MUST NOT ser blank;
- normalize whitespace apenas se compatível com os padrões já usados pelo projeto.

Não adicionar limites pequenos por conveniência.

O request body global já fornece um boundary operacional.

Se durante implementação surgir necessidade técnica comprovada de novo limite público, isso é mudança de Contract e deve voltar para decisão humana.

---

# 19. Time Semantics

`occurred_at`:

- é caller-supplied;
- é timezone-aware;
- normalizado para UTC;
- representa quando o acontecimento ocorreu;
- é diferente de `created_at`.

`created_at`:

- é server-generated;
- representa quando o Episode foi persistido.

Não confundir os dois.

---

# 20. Canonical Embedding Text

O embedding deverá ser reconstruível deterministicamente apenas a partir do Episode.

Formato recomendado:

```text
Summary: <summary>
Situation: <situation>
Reason: <reason>
Outcome: <outcome>
```

Campos ausentes são omitidos.

Exemplo:

```text
Summary: Episode foi escolhido como agregado first-class.
Situation: Foram avaliadas duas estratégias.
Reason: Historical experience possui lifecycle diferente de current truth.
Outcome: MemoryItem permanece profile|semantic.
```

Não incluir no embedding:

- `episode_id`;
- scope;
- context_key;
- timestamps;
- provenance;
- confidence;
- lifecycle;
- idempotency key.

---

# 21. Embedding Boundary

Reutilizar:

```text
OpenAIEmbeddingClient
```

e o provider já configurado.

Não criar provider próprio.

A chamada externa deve ocorrer:

```text
before PostgreSQL transaction
```

seguindo `CognitiveMemoryService`.

Se embedding falhar:

```text
no Episode persists
```

---

# 22. PostgreSQL Migration

Criar uma migration aditiva.

Baseline observado:

```text
0018
```

Quando a execução começar, verificar o head real.

A migration deverá criar somente estruturas necessárias a SM-CTX-001.

---

# 23. Proposed Tables

## 23.1 `episodes`

Estrutura conceitual:

```text
episodes
├── id UUID PK
├── scope TEXT nullable for forgotten tombstone
├── context_key TEXT nullable
├── kind episode_kind nullable/retained according to tombstone design
├── summary TEXT nullable
├── situation TEXT nullable
├── reason TEXT nullable
├── outcome TEXT nullable
├── occurred_at TIMESTAMPTZ nullable for forgotten tombstone
├── embedding VECTOR(3072) nullable
├── lifecycle episode_lifecycle NOT NULL
├── confidence REAL nullable
├── created_at TIMESTAMPTZ NOT NULL
├── invalidated_at TIMESTAMPTZ nullable
├── invalidation_reason TEXT nullable
├── replacement_episode_id UUID nullable
└── forgotten_at TIMESTAMPTZ nullable
```

A implementação deverá preservar a semântica forte de Forget.

---

## 23.2 `episode_provenance`

Relação 1:1.

Conceitualmente:

```text
episode_provenance
├── episode_id UUID PK/FK
├── origin_kind
├── source_system
├── conversation_ref nullable
└── turn_ref nullable
```

External refs deverão poder ser scrubbed no Forget.

---

## 23.3 `contextual_memory_idempotency`

O ledger pertence a **Contextual Project Memory**, não especificamente a Episodes.

Ele será criado no SM-CTX-001 porque Episodes são o primeiro consumidor, mas deve nascer preparado para mutations heterogêneas de Slices posteriores.

Estrutura conceitual:

```text
contextual_memory_idempotency
├── id UUID PK
├── idempotency_key TEXT UNIQUE
├── operation contextual_memory_operation
├── request_digest CHAR(64)
├── target_resource_id UUID nullable
├── result_resource_id UUID nullable
└── created_at TIMESTAMPTZ
```

Não criar:

```text
target_episode_id
result_episode_id
```

porque isso tornaria o ledger fisicamente específico de Episode e obrigaria remodelagem imediata em SM-CTX-002.

`idempotency_key` no shape físico é lookup identity protegida, não a header em texto puro: cumprir `06` §28, que proíbe raw key persistida. Preservar UNIQUE sobre representação não reversível e estável da chave, separada do request_digest HMAC. Não copiar silenciosamente a retenção de raw key do ledger cognitivo existente: código é referência de engenharia, não override da decisão contextual. Testar no PostgreSQL que raw key e raw request não persistem. Não introduzir novo secret; usar a chave existente com domain separation (§33).

---

## 23.4 Heterogeneous resource identity

`target_resource_id` e `result_resource_id` referenciam a identidade estrutural do recurso contextual associado à operação.

O tipo do recurso é determinado por:

```text
operation
```

Exemplos:

```text
episode_create
episode_invalidate
episode_forget

checkpoint_create
checkpoint_supersede
checkpoint_invalidate
checkpoint_forget
```

Não adicionar FK desses dois campos diretamente a:

```text
episodes
```

porque futuramente também apontarão para:

```text
context_checkpoints
```

A validade resource-specific é garantida pelo service/domain da operação correspondente.

---

## 23.5 Why no polymorphic FK

PostgreSQL não oferece uma FK simples que aponte condicionalmente para tabelas diferentes.

Não criar:

- duplicate target columns por resource type;
- generic contextual_resources table só para resolver FK;
- trigger complexo;
- inheritance table artificial.

A combinação:

```text
operation
+
resource UUID
+
request digest
+
service validation
+
UNIQUE(idempotency_key)
```

é suficiente para o ledger.

---

# 24. Contextual Operation Enum

Criar o enum contextual completo aprovado para evitar alteração desnecessária do PostgreSQL enum a cada Slice.

Valores:

```text
episode_create
episode_invalidate
episode_forget

checkpoint_create
checkpoint_supersede
checkpoint_invalidate
checkpoint_forget
```

No SM-CTX-001 somente estas operações serão executáveis:

```text
episode_create
episode_invalidate
episode_forget
```

Os valores de checkpoint existem apenas como schema readiness.

Não implementar Checkpoints ainda.

---

# 25. DB Lifecycle Constraints

As invariantes devem ser estruturalmente testáveis.

## ACTIVE

Deve possuir payload necessário:

```text
scope
kind
summary
occurred_at
embedding
```

e:

```text
invalidated_at = NULL
forgotten_at = NULL
```

---

## INVALIDATED

Mantém payload histórico.

Deve possuir:

```text
invalidated_at != NULL
forgotten_at = NULL
```

---

## FORGOTTEN

Deve possuir:

```text
forgotten_at != NULL
```

e não poderá manter conteúdo recuperável.

Deverão ser scrubbed no mínimo:

```text
scope
context_key
summary
situation
reason
outcome
embedding
confidence
occurred_at
invalidation_reason
conversation_ref
turn_ref
```

Avaliar se `kind` precisa ser mantido para structural audit.

A preferência é:

> não manter dado semântico que não seja necessário ao tombstone.

Preservar apenas estrutura necessária, como:

```text
episode_id
lifecycle
created_at
invalidated_at if historically applicable
replacement_episode_id if structurally necessary
forgotten_at
minimal provenance
```

---

# 26. Provenance Tombstone

Seguir o princípio já utilizado em Cognitive Memory:

- external refs são scrubbed;
- structural provenance mínima pode permanecer.

Não copiar os requisitos cognitivos de origin-specific refs que não existem no Episode Contract.

---

# 27. Exact Semantic Retrieval

Adicionar repository/service capability interna para future Contextual Recall.

Não criar novo endpoint público neste Slice.

Query normal:

```text
ACTIVE Episodes
exact scope match
optional context_key filtering when requested
embedding IS NOT NULL
```

Ranking:

```text
cosine relevance DESC
occurred_at DESC
episode_id ASC
```

Usar exact pgvector cosine.

Não criar:

- HNSW;
- IVFFlat;
- Neo4j retrieval.

---

# 28. Listing

`GET /api/v1/episodes` deve suportar filtros do Contract:

```text
scope        required
context_key  optional
kind         optional
lifecycle    optional
limit        optional/bounded
cursor       optional
```

Quando `lifecycle` não for informado:

```text
active only
```

---

# 29. Listing Order

Ordenação pública:

```text
occurred_at DESC
episode_id ASC
```

para Episodes que possuem conteúdo.

Para tombstones consultados explicitamente por lifecycle, manter ordenação determinística coerente.

O filtro obrigatório de scope não reconstrói scope scrubbed: Episode FORGOTTEN não corresponde ao project scope anterior. Tombstone continua recuperável por ID/replay, sem index paralelo que retenha a associação apagada. Não preservar scope apenas para fazê-lo aparecer no listing por projeto.

Não depender de unordered SELECT.

---

# 30. Pagination

Implementar bounded pagination desde o início.

Não criar framework genérico de paginação.

Uma solução local simples para Episodes é suficiente.

O cursor deve ser opaco para o Consumer.

A implementação poderá utilizar keyset pagination baseada nos campos de ordenação.

Não utilizar offset como requisito do Contract.

---

# 31. Create Idempotency

Ao contrário do endpoint atual de Cognitive Memory, o Contract contextual exige:

```http
Idempotency-Key
```

em toda mutation.

Portanto:

```text
missing Idempotency-Key
```

deve falhar.

Não tornar a header opcional apenas para copiar `POST /memories`.

---

# 32. Idempotency Digest

Criar primitives contextual-specific.

Canonical domain deve ser diferente do Cognitive Memory domain.

Exemplo:

```text
sofias-memory.contextual-memory.idempotency
```

A identidade canônica deve conter:

```text
operation
target_resource_id when operation targets a resource
semantic payload
```

Para Episode Create, o semantic payload cobre:

```text
scope
context_key
kind
summary
situation
reason
outcome
occurred_at
confidence
provenance
```

Para Episode Invalidate:

```text
target_resource_id = episode_id
reason
replacement_episode_id
```

Para Episode Forget:

```text
target_resource_id = episode_id
```

Não incluir:

- embedding;
- generated UUID;
- created_at;
- transport metadata;
- secret.

---

# 33. HMAC Secret

Não introduzir um novo secret obrigatório neste Slice sem necessidade.

Reutilizar o secret já disponível:

```text
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

para computar o HMAC contextual, desde que:

- canonical domain seja diferente;
- ledger seja separado;
- digest seja HMAC-SHA-256;
- nenhum raw semantic payload seja persistido no ledger.

Isso evita criar nova dependência operacional apenas por naming.

Não renomear o setting existente neste Slice.

Uma eventual generalização de configuração pode ser DEFERRED.

---

# 34. Reserved Idempotency Namespace

Manter o contrato global:

```text
sys:
```

reservado.

Não criar segunda regra.

---

# 35. Authoritative Idempotency Race

Seguir o padrão comprovado por Cognitive Memory:

```text
read-only pre-check optional
      ↓
external embedding when required
      ↓
short transaction
      ↓
UNIQUE idempotency claim
      ↓
winner writes
loser resolves winner
```

A correctness boundary é PostgreSQL `UNIQUE`, não o pre-check.

O resolver deve interpretar o resultado através de:

```text
operation
result_resource_id
```

e não por tipo de coluna específico de Episode.

---

# 36. Create Transaction

Fluxo recomendado:

```text
validate request
normalize
compute digest
optional pre-check
embed canonical text

BEGIN

create Episode
create EpisodeProvenance
claim contextual idempotency:
    operation = episode_create
    target_resource_id = NULL
    result_resource_id = episode.id

COMMIT
```

Race recovery deve usar savepoint como no Cognitive Memory pattern quando necessário.

Nenhum orphan Episode/provenance pode permanecer de uma race perdida.

---

# 37. Get

```http
GET /api/v1/episodes/{episode_id}
```

## ACTIVE

Retorna conteúdo.

## INVALIDATED

Retorna conteúdo e lifecycle `invalidated`.

## FORGOTTEN

Retorna tombstone.

Não retornar semantic payload esquecido.

---

# 38. Invalidate

Endpoint:

```http
POST /api/v1/episodes/{episode_id}/invalidate
```

Header obrigatório:

```http
Idempotency-Key
```

Request:

```json
{
  "reason": "The original episode was found to be inaccurate.",
  "replacement_episode_id": null
}
```

Neste Slice:

- não criar replacement automaticamente;
- validar `replacement_episode_id` quando fornecido;
- replacement deve existir;
- não pode estar forgotten;
- deve pertencer ao mesmo Project Scope.

---

# 39. Invalidate Lifecycle

```text
ACTIVE
    -> INVALIDATED

INVALIDATED
    -> state no-op / replay-safe result

FORGOTTEN
    -> EPISODE_STATE_CONFLICT
```

O `reason` precisa ser preservado de forma auditável.

Campo dedicado:

```text
invalidation_reason
```

é apropriado.

Não usar log como única persistência do motivo.

Idempotency ledger:

```text
operation = episode_invalidate
target_resource_id = episode_id
result_resource_id = episode_id
```

---

# 40. Forget

Endpoint:

```http
POST /api/v1/episodes/{episode_id}/forget
```

Header obrigatório:

```http
Idempotency-Key
```

Lifecycle:

```text
ACTIVE      -> FORGOTTEN
INVALIDATED -> FORGOTTEN
FORGOTTEN   -> state no-op
```

No SM-CTX-001, Forget é somente direto.

Não propagar para dependentes ainda.

Isso será SM-CTX-005 após Evidence existir.

Ledger:

```text
operation = episode_forget
target_resource_id = episode_id
result_resource_id = episode_id
```

---

# 41. Forget Transaction

Forget deve:

```text
lock Episode
resolve idempotency before state conflict
scrub semantic payload
scrub external provenance refs
set lifecycle=forgotten
set forgotten_at
persist contextual idempotency result
commit
```

Não executar embedding/provider call.

---

# 42. No Hard Delete

Não criar DELETE público.

Episode forgotten permanece como tombstone mínimo.

---

# 43. Repository Layer

Criar apenas repositories concretos necessários.

Prováveis:

```text
EpisodeRepository
EpisodeProvenanceRepository
ContextualMemoryIdempotencyRepository
```

Não criar:

```text
GenericMemoryRepository
GenericLifecycleRepository
GenericContextRepository
GenericContextualResourceRepository
```

O ledger é genérico quanto ao tipo do recurso referenciado; isso não implica criar repository genérico para os próprios recursos.

---

# 44. Unit of Work

Adicionar os repositories contextuais ao `PostgresUnitOfWork`.

Manter o padrão existente:

```text
lazy property guarded by active UoW
```

Garantir `_clear()` completo.

---

# 45. Models Registration

Atualizar exports/registries necessários para que:

- SQLAlchemy metadata conheça os models;
- migrations/tests importem corretamente;
- nenhum import cycle novo seja criado.

Seguir padrão atual de:

```text
sofias_memory/infrastructure/postgres/models/
```

e:

```text
repositories/
```

---

# 46. Service Layer

Criar service explícito:

```text
EpisodeService
```

Responsável por:

```text
create
get
list
semantic_search
invalidate
forget
```

Se semantic search justificar separação posterior, refatorar quando SM-CTX-006 realmente precisar.

Não criar antecipadamente vários services sem segunda necessidade concreta.

---

# 47. HTTP Layer

Criar:

```text
sofias_memory/api/routes/episodes.py
```

Routes devem:

- autenticar pelo mecanismo existente;
- validar headers/body;
- construir/injetar service;
- chamar service;
- mapear envelope;
- não executar SQL;
- não executar pgvector query;
- não conter lifecycle logic.

---

# 48. Schemas

Criar:

```text
sofias_memory/schemas/episodes.py
```

Prováveis schemas:

```text
EpisodeProvenanceCreateRequest
EpisodeProvenanceResult
EpisodeCreateRequest
EpisodeResult
EpisodeInvalidateRequest
EpisodeListResult
```

Manter:

```python
ConfigDict(extra="forbid")
```

quando consistente com os schemas existentes.

---

# 49. Error Codes Required in CTX-001

Implementar os códigos necessários deste Contract:

```text
INVALID_REQUEST
INVALID_SCOPE
INVALID_CONTEXT_KEY
INVALID_EPISODE_KIND

EPISODE_NOT_FOUND

IDEMPOTENCY_CONFLICT
EPISODE_STATE_CONFLICT

DEPENDENCY_UNAVAILABLE
```

Não implementar Evidence errors ainda.

Mensagens humanas podem variar.

`code` e semântica devem permanecer estáveis.

---

# 50. Embedding Failure

Falha do provider deve produzir dependency failure consistente com padrões existentes.

Regra:

```text
embedding failure
    -> Episode not persisted
```

Não criar Episode com:

```text
embedding = NULL
```

exceto FORGOTTEN tombstone.

---

# 51. Graph Isolation

CTX-001 não toca:

```text
graph_outbox
Neo4j
graph reconciliation
```

Adicionar teste que prove que Episode writes não produzem graph events.

Não é necessário iniciar Neo4j para testes específicos do domínio Episode.

---

# 52. Pipeline Isolation

Episode mutations são síncronas.

MUST NOT criar:

```text
PipelineRun
PipelineStep
```

Adicionar regression assertion onde útil.

---

# 53. Cognitive Memory Isolation

Episode Create MUST NOT criar:

```text
MemoryItem
MemoryProvenance
```

Não reutilizar a tabela cognitiva como storage de Episode.

---

# 54. Session Isolation

Episode Create MUST NOT criar:

```text
Session
SessionEntry
```

Uma futura Evidence poderá apontar para SessionEntry, mas isso não pertence ao Slice atual.

---

# 55. Migration Tests

Criar teste unitário da migration no padrão atual.

Validar:

- `revision`;
- `down_revision`;
- enums;
- tables;
- columns;
- constraints;
- indexes;
- contextual operation enum;
- generic idempotency resource columns;
- ausência de FK específica de Episode no generic ledger;
- no graph schema;
- downgrade reversível para estrutura criada neste Slice.

---

# 56. Unit Tests

Cobrir comportamento que realmente possui lógica.

No mínimo:

## Domain

- Episode kind values;
- lifecycle values;
- scope rejects global;
- scope accepts canonical project scope;
- context_key validation;
- inferred requires confidence;
- confidence bounds;
- canonical embedding text.

## Idempotency

- canonical request stable;
- contextual domain separated from cognitive domain;
- same semantic request → same digest;
- changed request → changed digest;
- target resource participates in target operations;
- resource UUID alone does not identify operation;
- reserved `sys:` detection.

## Lifecycle

- valid transitions;
- invalidated no-op;
- forgotten terminal;
- tombstone serialization.

---

# 57. Repository Tests

Com PostgreSQL real, validar:

- insert/get;
- list;
- filtering;
- deterministic ordering;
- cursor pagination;
- exact cosine search;
- lifecycle filtering;
- row locking where needed;
- generic contextual idempotency ledger persistence.

---

# 58. HTTP Tests

Cobrir:

```text
POST create
GET by id
GET list
POST invalidate
POST forget
```

Também:

- missing Idempotency-Key;
- invalid scope;
- invalid kind;
- invalid context_key;
- not found;
- state conflict;
- idempotency conflict;
- forgotten tombstone.

---

# 59. PostgreSQL Integration Test Gate

Criar env flag dedicado seguindo padrão atual.

Exemplo:

```text
SOFIAS_MEMORY_RUN_CONTEXTUAL_EPISODES_POSTGRES_TESTS
```

e database URL dedicado equivalente.

Não reutilizar banco de desenvolvimento.

Se o repositório possuir convenção mais apropriada no momento da implementação, seguir a convenção existente.

---

# 60. Idempotency Race Test

Obrigatório testar concorrência real.

Cenário:

```text
same Idempotency-Key
same request
two concurrent callers
```

Resultado:

```text
exactly one Episode
exactly one contextual idempotency claim
both callers resolve same logical Episode
```

Também testar:

```text
same key
different request
```

Resultado:

```text
IDEMPOTENCY_CONFLICT
```

E provar que o ledger não depende de FK específica para `episodes`.

---

# 61. Restart Durability

Teste/smoke obrigatório:

```text
create Episode
close app/process boundary
new app/service instance
get Episode
```

O objetivo é provar persistência real, não apenas ORM identity map.

---

# 62. Semantic Retrieval Test

Criar múltiplos ACTIVE Episodes com fake embeddings controláveis.

Executar semantic search interno.

Validar:

```text
relevance DESC
occurred_at DESC
episode_id ASC
```

Também validar exclusão de:

```text
invalidated
forgotten
other scope
```

do retrieval normal.

---

# 63. No ANN

Não criar:

```text
HNSW
IVFFlat
```

neste Slice.

Exact cosine é baseline aprovado.

---

# 64. Logging and Privacy

Não logar por padrão:

- summary;
- situation;
- reason;
- outcome;
- invalidation reason;
- Idempotency-Key;
- request digest secret input.

Structured logs podem incluir:

```text
operation
episode_id
lifecycle
```

e identifiers não sensíveis quando necessários.

Se já existir log-privacy pattern de Cognitive Memory, reutilizá-lo.

---

# 65. OpenAPI

Episode endpoints devem aparecer corretamente no OpenAPI.

Adicionar contract tests que provem:

- métodos corretos;
- ausência de PATCH;
- ausência de PUT;
- ausência de DELETE.

Não adicionar ainda ContextCheckpoint/Contextual Recall routes.

---

# 66. `/api/v1/info`

Não anunciar ainda:

```text
contextual_project_memory = "1"
```

em CTX-001.

Se a implementação precisar modificar `/info` por algum motivo técnico, isso exige justificativa explícita no relatório.

---

# 67. Documentation During Slice

Atualizar apenas documentação necessária para refletir comportamento já implementado.

Não reescrever toda Wiki ainda.

Mudanças maiores de documentação de Contract v1 ficam principalmente para SM-CTX-008.

Pode ser necessário atualizar:

- development test flags;
- API docs parciais;
- CHANGELOG conforme política atual.

Não declarar Contextual Project Memory completa.

---

# 68. Expected File Impact

A lista abaixo é indicativa, não uma obrigação literal.

Possíveis novos arquivos:

```text
migrations/versions/<next>_create_contextual_episodes.py

sofias_memory/domain/contextual_memory.py
sofias_memory/domain/contextual_idempotency.py

sofias_memory/schemas/episodes.py

sofias_memory/api/routes/episodes.py

sofias_memory/services/episodes.py

sofias_memory/infrastructure/postgres/models/episode.py
sofias_memory/infrastructure/postgres/models/episode_provenance.py
sofias_memory/infrastructure/postgres/models/contextual_memory_idempotency.py

sofias_memory/infrastructure/postgres/repositories/episodes.py
sofias_memory/infrastructure/postgres/repositories/episode_provenance.py
sofias_memory/infrastructure/postgres/repositories/contextual_memory_idempotency.py
```

Arquivos existentes provavelmente afetados:

```text
sofias_memory/domain/__init__.py
sofias_memory/domain/enums.py

sofias_memory/infrastructure/postgres/models/__init__.py
sofias_memory/infrastructure/postgres/repositories/__init__.py
sofias_memory/infrastructure/postgres/unit_of_work.py

sofias_memory/app.py
sofias_memory/schemas/common.py
```

e testes.

Não force exatamente essa estrutura se o código atual indicar uma organização mais coerente.

---

# 69. Small Refactors Allowed

Durante CTX-001 são permitidos refactors locais como:

- extrair helper de constraint-name handling;
- reutilizar canonical HMAC utility;
- compartilhar embedding Protocol;
- reduzir duplicação trivial.

Somente se:

- não alterarem public Contract;
- não mudarem arquitetura;
- não ampliarem escopo;
- permanecerem pequenos.

Não transformar CTX-001 em refactor geral de Cognitive Memory.

---

# 70. Findings Classification

Durante implementação, classificar findings:

## BLOCKER

Impede o Slice.

Exemplos:

- Contract impossível de implementar sem mudança;
- migration inconsistente;
- lifecycle não pode ser garantido;
- idempotency race incorreta;
- Forget vaza payload;
- regression crítica.

## IMPORTANT

Afeta diretamente qualidade/objetivo do Slice, mas pode ter correção localizada.

## MINOR

Não impede objetivo.

## DEFERRED

Fora do Slice.

Não manter CTX-001 aberto indefinidamente por MINOR/DEFERRED.

---

# 71. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- alterar Contract público;
- aceitar `global` para Episode;
- mudar lifecycle;
- alterar kinds;
- mudar semantics de Forget;
- criar novo required secret;
- introduzir Redis/message broker;
- usar Neo4j;
- criar PipelineRun;
- alterar Cognitive Memory contract;
- migration destrutiva;
- adicionar automatic extraction;
- transformar generic contextual idempotency em recurso-specific novamente;
- criar generic resource table apenas para obter FK no ledger.

Não decidir esses itens silenciosamente.

---

# 72. Implementation Order

A ordem recomendada é:

```text
1. domain primitives
2. migration + ORM
3. generic contextual idempotency foundation
4. repositories + UoW
5. schemas
6. service create/get
7. HTTP create/get
8. idempotency race hardening
9. list + pagination
10. semantic retrieval internal
11. invalidate
12. forget/tombstone
13. contract/OpenAPI protection
14. PostgreSQL integration tests
15. restart validation
16. no-graph/no-pipeline regression
17. Human Validation
```

A ordem pode ser ajustada localmente se facilitar integração real mais cedo.

---

# 73. Acceptance Criteria

O Slice somente pode ser declarado tecnicamente concluído quando todos forem satisfeitos.

## AC-001.1

É possível criar Episode ACTIVE.

## AC-001.2

Episode recebe UUID server-side.

## AC-001.3

`occurred_at` é independente de `created_at`.

## AC-001.4

Episode persiste após restart.

## AC-001.5

Episode pode ser listado por scope.

## AC-001.6

Episode pode ser filtrado por:

```text
context_key
kind
lifecycle
```

## AC-001.7

Normal listing exclui INVALIDATED/FORGOTTEN.

## AC-001.8

Invalidate realiza:

```text
ACTIVE -> INVALIDATED
```

## AC-001.9

Forget realiza:

```text
ACTIVE | INVALIDATED -> FORGOTTEN
```

## AC-001.10

FORGOTTEN não retorna conteúdo semântico.

## AC-001.11

Não existe PATCH/PUT/DELETE público.

## AC-001.12

Same key + same request não cria Episode adicional.

## AC-001.13

Same key + different request produz `IDEMPOTENCY_CONFLICT`.

## AC-001.14

Episode write não produz `graph_outbox`.

---

# 74. Required Test Commands

Executar ao menos:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

além das suites PostgreSQL opt-in específicas do Slice.

Se a suíte global for excessivamente longa ou depender de infraestrutura indisponível, registrar exatamente:

- o que foi executado;
- o que não foi;
- por quê.

Não declarar PASS de teste não executado.

---

# 75. Human Validation

Após os testes automatizados, executar contra a API real.

## Step A — Create

Criar um Episode:

```json
{
  "scope": "project:contextual-memory-validation",
  "context_key": "feature:v1",
  "kind": "decision",
  "summary": "Episode foi escolhido como agregado first-class.",
  "situation": "Foram avaliadas alternativas de modelagem.",
  "reason": "Historical experience possui semântica diferente de current truth.",
  "outcome": "MemoryItem continua profile|semantic.",
  "occurred_at": "2026-11-18T20:00:00Z",
  "provenance": {
    "origin_kind": "user_asserted",
    "source_system": "manual-validation",
    "conversation_ref": "sm-ctx-001",
    "turn_ref": "create"
  }
}
```

com:

```http
Idempotency-Key: sm-ctx-001-create-001
```

Confirmar:

```text
201
lifecycle=active
```

---

## Step B — Replay

Repetir a mesma request com a mesma key.

Confirmar:

```text
same episode_id
no duplicate
```

---

## Step C — Restart

Reiniciar Sofias Memory.

Executar:

```http
GET /api/v1/episodes/{episode_id}
```

Confirmar conteúdo idêntico.

---

## Step D — List

Listar pelo scope.

Confirmar presença do Episode.

---

## Step E — Invalidate

Invalidar com nova Idempotency-Key.

Confirmar:

```text
lifecycle=invalidated
```

Confirmar ausência no listing ACTIVE padrão.

---

## Step F — Forget

Esquecer o Episode.

Confirmar:

```text
lifecycle=forgotten
```

e tombstone sem:

```text
scope
context_key
summary
situation
reason
outcome
embedding
confidence
occurred_at
invalidation_reason
conversation_ref
turn_ref
```

quando aplicável ao shape público.

---

## Step G — Regression

Confirmar que:

- Cognitive Memory continua funcionando;
- Sessions continuam funcionando;
- nenhum PipelineRun foi criado;
- nenhum graph_outbox contextual foi criado.

---

# 76. Slice Exit Criteria

SM-CTX-001 estará pronto para Human Validation/closeout quando:

```text
objective implemented
acceptance criteria satisfied
required tests pass
migration verified
restart persistence verified
idempotency race verified
generic contextual ledger verified
tombstone verified
no graph side effect
no PipelineRun side effect
no BLOCKER remains
```

---

# 77. Expected Implementation Report

O executor deverá retornar um relatório compacto neste formato:

```text
SM-CTX-001 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- migration:
- previous head:
- new head:
- tables/enums:
- constraints:
- contextual idempotency shape:

Verified
- unit:
- integration:
- HTTP:
- race:
- generic contextual ledger:
- restart:
- graph isolation:
- pipeline isolation:

Acceptance Criteria
- AC-001.1 PASS/FAIL
- AC-001.2 PASS/FAIL
...
- AC-001.14 PASS/FAIL

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

Não declarar o Slice DONE apenas porque testes unitários estão verdes.

---

# 78. Handoff to Executor

## Objective

Implementar `SM-CTX-001 — First-Class Episodes`.

## Approved baseline

Os documentos `01` a `08` em:

```text
docs/product-implementation/
```

são baseline APPROVED.

Este documento é o plano de execução do Slice.

## Scope

Somente Episodes first-class:

```text
create
get
list
semantic retrieval internal
invalidate
forget
provenance
idempotency
durability
```

## Infrastructure produced for later slices

SM-CTX-001 também estabelece a fundação compartilhada:

```text
contextual_memory_idempotency
```

que MUST ser resource-generic e reutilizável pelos Checkpoints de SM-CTX-002+.

Isso é infraestrutura compartilhada aprovada, não implementação antecipada de Checkpoint.

## Out of Scope

Tudo relacionado a:

```text
Evidence
Checkpoints
Contextual Recall
Forget propagation
automatic distillation
Neo4j projection
```

## Expected result

Ao final:

> uma experiência histórica pode ser persistida explicitamente, recuperada após restart, invalidada e esquecida de forma segura, sem interferir nos domínios existentes, e a fundação de idempotência contextual fica pronta para ser reutilizada pelos Slices seguintes sem remodelagem imediata.
