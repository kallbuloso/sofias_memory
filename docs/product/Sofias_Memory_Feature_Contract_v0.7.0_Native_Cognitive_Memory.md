# Sofias Memory — Feature Contract v0.7.0: Native Cognitive Memory

**Status:** APPROVED / FROZEN FOR IMPLEMENTATION  
**Target release:** v0.7.0  
**Feature:** Native Cognitive Memory  
**Architecture authority:** ADR-0016 (`docs/adr/0016-native-cognitive-memory-model-and-lifecycle.md`, accepted)  
**Primary consumer contract:** `docs/product/Sofias_Memory_Integration_Contract_Sofias_Assistant_v1.md` (APPROVED)

---

## 1. Objetivo

A v0.7.0 introduz um domínio **first-class de Cognitive Memory** no Sofias Memory.

O objetivo é permitir que callers — em especial Sofia's Assistant — persistam, recuperem, supersedam e esqueçam memória cognitiva tipada sem converter artificialmente preferências, fatos ou decisões em `Dataset`, `Source`, `Document`, `Chunk` ou `SessionEntry`.

> `MemoryItem` é uma unidade cognitiva durável, semanticamente recuperável e temporalmente governada pelo Sofias Memory. Não é documento, sessão, candidate, autorização ou runtime de agente.

Este feature resolve os gaps de integração previamente identificados como:

- **MI-001** — Capability and Compatibility Contract;
- **MI-002** — Native Typed Cognitive Memory Write;
- **MI-003** — Typed Cognitive Recall / Current Truth Filtering;
- **MI-004** — Supersession and Precise Forget;
- **MI-005** — First-Class Cognitive Provenance;
- **MI-006** — Mutation / Error / Idempotency Contract.

A v0.7.0 não transforma Sofias Memory em Conversation Store, Agent Runtime, Policy Engine ou confirmation workflow.

---

## 2. Princípios de domínio

1. **Domain ownership > implementation convenience.** Se o dado é memória cognitiva persistida, o owner é Sofias Memory.
2. **Candidate != Memory.** `MemoryCandidate` continua pertencendo ao Sofia's Assistant; apenas candidate aprovado pode originar `MemoryItem`.
3. **Session != Cognitive Memory.** Session é contexto temporal; `SessionEntry` é contexto append-only; nenhum dos dois é memória cognitiva permanente.
4. **Knowledge corpus != Cognitive Memory.** Datasets/Documents/Chunks continuam sendo o modelo correto para corpus/importação documental; não são o modelo de PROFILE/SEMANTIC.
5. **PostgreSQL authoritative.** Todo estado cognitivo, lifecycle, embedding, provenance e idempotência necessários para reconstrução e auditabilidade são autoritativos em PostgreSQL.
6. **Explicit > automatic.** Não existe dedupe semântico implícito, conflito-resolução por IA, consolidação automática ou supersession inferida.
7. **Forget é destrutivo de conteúdo cognitivo.** `FORGOTTEN` não é apenas um label.
8. **Memory is context, never authority.** Conteúdo recuperado pode informar o caller, mas não cria system instruction, Policy, PermissionGrant ou autorização.

---

# 3. Terminologia e identidades

## 3.1 `MemoryItem`

Recurso first-class durável do Sofias Memory.

Cada `MemoryItem` possui uma única identidade estrutural:

```text
memory_id = UUID gerado pelo Sofias Memory
```

`memory_id` nunca é:

- Assistant Conversation UUID;
- Assistant Turn UUID;
- Sofias Memory Session UUID;
- Provider Session ID;
- content hash;
- idempotency key.

Não existe caller-supplied logical name no MVP.

## 3.2 Identidades externas em provenance

Campos como `conversation_uuid`, `turn_uuid` e `task_uuid` são **referências externas opacas/UUID-typed**, nunca FKs cross-database.

Provider Session ID não faz parte da provenance cognitiva.

---

# 4. Memory types

O contract version 1 suporta exatamente:

```text
PROFILE   -> public JSON value: "profile"
SEMANTIC  -> public JSON value: "semantic"
```

## 4.1 PROFILE

Memória persistente sobre preferências, características ou configurações pessoais/declaradas relevantes ao comportamento futuro do Assistant.

Exemplos:

- preferência de interface;
- preferência de idioma/forma de resposta;
- dado pessoal explicitamente útil e apropriado para memória;
- configuração declarada durável.

## 4.2 SEMANTIC

Fatos, conhecimento, decisões, conceitos e informações persistentes semanticamente úteis.

## 4.3 Explicitamente deferred

Não são valores aceitos pelo contrato v1:

```text
EPISODIC
PROCEDURAL
```

`PROCEDURAL` não é alias de Skill. Skills existentes permanecem um domínio separado (ADR-0013).

Requests com tipo fora de `profile|semantic` são `422 INVALID_REQUEST`.

---

# 5. Scope

Cognitive Memory não é Dataset-scoped.

O MVP suporta exatamente duas formas de scope:

```text
global
project:<key>
```

## 5.1 Normalização

O input é trimado nas bordas e então deve estar em forma canônica.

`global` é literal exato.

`project:<key>` exige:

```text
key length: 1..128
allowed: lowercase a-z, digits 0-9, dot, underscore, hyphen
first char: lowercase letter or digit
no whitespace
```

Regex conceitual:

```text
^[a-z0-9][a-z0-9._-]{0,127}$
```

Uppercase não é convertido silenciosamente; é rejeitado.

Matching é igualdade exata do scope canônico. Não existe wildcard, hierarquia, prefix matching, inheritance ou ACL implícita.

## 5.2 Não-tenancy

Scope organiza contexto cognitivo; não cria tenant, user, role ou authorization boundary.

---

# 6. Modelo público de `MemoryItem`

Campos conceituais:

| Campo | Owner / origem | Mutabilidade | Regra v0.7 |
|---|---|---|---|
| `memory_id` | Memory | imutável | UUID gerado pelo servidor |
| `memory_type` | caller | imutável | `profile|semantic` |
| `scope` | caller | imutável até Forget | `global|project:<key>` |
| `content` | caller | imutável até Forget | texto cognitivo normalizado |
| `lifecycle` | Memory | transições controladas | `active|superseded|forgotten` |
| `confidence` | caller | imutável | nullable, `[0,1]`; obrigatório para `inferred` |
| `valid_from` | caller | imutável | nullable UTC timestamp |
| `valid_until` | caller | imutável | nullable UTC timestamp; exclusivo |
| `created_at` | Memory | imutável | UTC server timestamp |
| `superseded_at` | Memory | system-managed | nullable |
| `superseded_by` | Memory | system-managed | nullable Memory UUID |
| `forgotten_at` | Memory | system-managed | nullable |
| `provenance` | caller + Memory validation | imutável, depois reduzível por Forget | first-class, §7 |

## 6.1 Content

`content` é uma unidade cognitiva, não documento.

Contrato:

```text
1..16384 Unicode chars após normalização
CRLF/CR -> LF
trim somente nas bordas
must contain non-whitespace
```

Não há HTML/Markdown semantics especiais. O texto é opaco para autorização e prompt-role semantics.

## 6.2 Confidence

`confidence` representa confiança epistêmica/extrativa declarada pelo caller no momento da criação.

- nullable = não informada;
- `0.0..1.0` quando presente;
- obrigatório para provenance `inferred`;
- não concede autoridade;
- não participa do ranking da v0.7;
- não é automaticamente recalculado pelo Memory.

## 6.3 Importance — decisão v0.7

`importance` **não entra no MVP v0.7**.

Racional: ainda não existe um contrato de retrieval/lifecycle que dê semântica estável e testável a esse valor. Persistir um número sem efeito congelado produziria metadata central sem domínio claro. Pode ser adicionado futuramente quando houver uma política explícita de ranking, decay ou consolidation.

## 6.4 Sem PATCH genérico

Não existe `PATCH /memories/{id}` no contract v1.

Correção de content, scope/type, confidence ou temporal validity ocorre por nova memória ou por **supersession**, preservando histórico em vez de mutação silenciosa.

---

# 7. First-class cognitive provenance

Cada MemoryItem possui exatamente uma `memory_provenance` record first-class em relação 1:1. Enquanto o item não está `FORGOTTEN`, essa row contém a provenance completa permitida pelo contrato. No Forget, **a mesma row é preservada e scrubbed atomicamente** conforme §16.2; ela nunca é apagada nem substituída por metadata solta.

Campos públicos:

```text
origin_kind
source_system
conversation_uuid?   # external UUID, no FK
turn_uuid?           # external UUID, no FK
task_uuid?           # external UUID, no FK
confirmation_ref?    # opaque external reference
source_ref?          # opaque external reference
observed_at?         # UTC timestamp
```

## 7.1 `origin_kind`

Valores contract v1:

```text
USER_ASSERTED         -> "user_asserted"
TOOL_OBSERVED         -> "tool_observed"
IMPORTED              -> "imported"
INFERRED              -> "inferred"
ASSISTANT_GENERATED    -> "assistant_generated"
```

## 7.2 `source_system`

Required para todos os origins.

Forma canônica:

```text
1..64 chars
lowercase a-z / 0-9 / hyphen
^[a-z0-9]+(-[a-z0-9]+)*$
```

Exemplo principal: `sofias-assistant`.

`confirmation_ref` e `source_ref`, quando presentes, são strings opacas trimadas, 1..255 chars, nunca content blobs. UUID fields são UUIDs válidos; timestamps são timezone-aware e normalizados para UTC.

`source_system` é **somente um system slug** (por exemplo `sofias-assistant`). Ele nunca identifica Conversation, Turn, Task, URL, document, tool execution, import object ou qualquer resource instance. Essas referências, quando permitidas, pertencem exclusivamente aos campos externos específicos acima.

## 7.3 Regras mínimas por origin

| Origin | Obrigatório além de `source_system` |
|---|---|
| `user_asserted` | ao menos `turn_uuid` ou `source_ref` |
| `tool_observed` | `source_ref` + `observed_at` |
| `imported` | `source_ref` |
| `inferred` | ao menos `turn_uuid`, `task_uuid` ou `source_ref`; `confidence` obrigatório |
| `assistant_generated` | ao menos `turn_uuid`, `task_uuid` ou `source_ref` |

O Integration Contract do Sofia's Assistant pode impor regras mais estritas sem alterar este contrato de serviço.

## 7.4 Confirmation boundary

`confirmation_ref` é apenas evidence externa de que um workflow de confirmação já ocorreu.

Sofias Memory:

- não cria candidate;
- não persiste `PENDING/PENDING_CONFIRMATION/APPROVED/REJECTED`;
- não pede confirmação;
- não concede permission/authority;
- não valida o significado de `confirmation_ref` fora de formato/tamanho.

---

# 8. Lifecycle

Estados públicos:

```text
ACTIVE       -> "active"
SUPERSEDED   -> "superseded"
FORGOTTEN    -> "forgotten"
```

Allowed transitions:

```text
ACTIVE      -> SUPERSEDED
ACTIVE      -> FORGOTTEN
SUPERSEDED  -> FORGOTTEN
FORGOTTEN   -> FORGOTTEN   # safe idempotent replay, no further mutation
```

Forbidden:

```text
SUPERSEDED -> ACTIVE
FORGOTTEN  -> ACTIVE
FORGOTTEN  -> SUPERSEDED
SUPERSEDED -> SUPERSEDED via a second replacement
```

Restore/unforget não existe no contract v1.

## 8.1 ACTIVE

Pode representar current truth, sujeito às regras temporais de §9.

`ACTIVE` não significa necessariamente "válido agora": um item pode estar fora de `valid_from/valid_until` e permanecer lifecycle `active`.

## 8.2 SUPERSEDED

Histórico cognitivo preservado e legível por `GET`; não é current truth após `superseded_at`.

SUPERSEDED != FORGOTTEN.

## 8.3 FORGOTTEN

Tombstone de identidade/lifecycle. Conteúdo cognitivo não permanece recuperável.

---

# 9. Temporal semantics e current truth

`valid_from` é inclusivo. `valid_until` é exclusivo.

Quando ambos existem:

```text
valid_until > valid_from
```

Não há job que altere lifecycle quando `valid_until` passa.

## 9.1 Current truth em um instante T

Um item é current truth em `T` somente se:

```text
lifecycle != FORGOTTEN
created_at <= T
(superseded_at is null OR T < superseded_at)
(valid_from is null OR valid_from <= T)
(valid_until is null OR T < valid_until)
content ainda existe
```

Isso permite reconstruir truth histórico de um item superseded antes de `superseded_at`.

**Forget tem precedência sobre history:** um item `FORGOTTEN` nunca volta em recall, mesmo quando `as_of` é anterior a `forgotten_at`, porque o conteúdo já foi destruído.

## 9.2 Não existe unicidade semântica global

Sofias Memory não tenta provar que dois conteúdos representam a mesma proposição. Dois `ACTIVE` items semanticamente contraditórios podem coexistir se o caller os criar separadamente.

Supersession é explícita por `memory_id`; conflict-resolution semântico automático é deferred.

---

# 10. PostgreSQL, pgvector e Neo4j

## 10.1 PostgreSQL authoritative

PostgreSQL armazena autoritativamente:

- MemoryItem;
- lifecycle e timestamps;
- cognitive provenance;
- full-precision embedding;
- supersession lineage;
- idempotency evidence necessária.

## 10.2 Embedding

Cada item não-forgotten persistível para recall possui embedding compatível com ADR-0006:

```text
VECTOR(3072)
cosine metric
```

Embedding nunca é exposto pela API.

## 10.3 Retrieval v0.7

O contract v1 usa **cosine similarity exata sobre o vetor full-precision autoritativo** depois dos filtros estruturais/temporais.

A v0.7 não exige um novo HNSW/halfvec ANN index para Cognitive Memory. Escala que justifique ANN pode ser tratada em decisão posterior, preservando ADR-0006.

## 10.4 Neo4j — explicitamente não usado no MVP

Cognitive Memory v0.7 **não é projetada em Neo4j**.

Consequências:

- typed cognitive recall não depende de Neo4j;
- create/supersede/forget não criam `graph_outbox` para Cognitive Memory;
- não existe `(:MemoryItem)` no graph contract v0.7.

Se uma release futura adicionar cognitive graph projection, ADR-0002 volta a ser obrigatório: PostgreSQL authoritative, transactional `graph_outbox`, projection rebuildable e delete projection no Forget.

---

# 11. Compatibility negotiation — MI-001

`GET /api/v1/info` mantém todos os campos atuais e ganha, aditivamente:

```json
{
  "api_contract_version": "1",
  "contracts": {
    "cognitive_memory": "1"
  },
  "capabilities": [
    "cognitive_memory.write",
    "cognitive_memory.get",
    "cognitive_memory.recall",
    "cognitive_memory.supersede",
    "cognitive_memory.forget"
  ]
}
```

## 11.1 Semântica

- `version` existente continua significando release version da aplicação.
- `api_contract_version` é machine contract, não SemVer da aplicação.
- `contracts.cognitive_memory="1"` identifica a major version deste contrato.
- `capabilities` anuncia operações implementadas, não health/readiness momentâneo.
- ausência do contract/capability é tratada pelo consumer como feature indisponível; nunca inferir suporte a partir de `version >= 0.7.0`.
- breaking change de Cognitive Memory exige novo contract major; mudanças aditivas compatíveis podem permanecer em contract `1`.


# 11.2 API surface e success contract

| Operação | Endpoint | Success | Response data |
|---|---|---:|---|
| Create | `POST /api/v1/memories` | `201` | `MemoryItem` |
| Get | `GET /api/v1/memories/{memory_id}` | `200` | `MemoryItem` ou tombstone |
| Recall | `POST /api/v1/memories/recall` | `200` | `MemoryRecallResult` |
| Supersede | `POST /api/v1/memories/{memory_id}/supersede` | `200` | old superseded + replacement |
| Forget | `POST /api/v1/memories/{memory_id}/forget` | `200` | forgotten tombstone |

Todos os successes usam o `SuccessEnvelope` existente. Replay idempotente preserva o mesmo logical outcome e o mesmo success status da operação original.

---

# 12. Native typed write — `POST /api/v1/memories`

Operação síncrona.

Request conceitual:

```json
{
  "memory_type": "profile",
  "scope": "global",
  "content": "Prefere interfaces em teal.",
  "valid_from": null,
  "valid_until": null,
  "provenance": {
    "origin_kind": "user_asserted",
    "source_system": "sofias-assistant",
    "conversation_uuid": "...",
    "turn_uuid": "...",
    "confirmation_ref": "...",
    "observed_at": "..."
  }
}
```

Response: HTTP `201`, `SuccessEnvelope[MemoryItem]`.

## 12.1 Ordering obrigatório

```text
validate + canonicalize
        ↓
idempotency committed-outcome pre-check (optimization only)
        ↓
external embedding call
        ↓
short PostgreSQL transaction
    authoritative Idempotency-Key claim/check (UNIQUE + locking/serialization)
    if loser: replay winner for same request, or conflict for different request
    if winner: insert MemoryItem
               insert provenance
               persist idempotency outcome
        ↓
commit
        ↓
response
```

Nenhuma transaction PostgreSQL permanece aberta durante chamada externa de embedding.

Embedding failure antes do commit significa: **nenhum MemoryItem criado**.

O pre-check anterior ao embedding é apenas uma otimização para evitar trabalho externo quando já existe outcome committed. Ele **não é a race authority**. Depois do embedding, a transaction deve fazer um claim/check autoritativo da `Idempotency-Key` protegido por `UNIQUE` e locking/serialization PostgreSQL equivalente. Se dois requests chegaram após um pre-check simultaneamente vazio, somente um pode ganhar o claim; o loser aguarda/observa o outcome committed do winner e:

- mesma key + mesmo keyed request digest -> replaya o winner;
- mesma key + digest diferente -> `409 IDEMPOTENCY_CONFLICT`.

Nenhuma mutação do recurso ocorre antes desse claim autoritativo.

## 12.2 Não é PipelineRun

Create de Cognitive Memory não cria PipelineRun/PipelineStep e não depende do durable worker.

Justificativa: é uma pequena mutação síncrona cujo único trabalho externo obrigatório no MVP é embedding; não existe stage durável que justifique fila/pipeline.

---

# 13. Get — `GET /api/v1/memories/{memory_id}`

Retorna o item identificado, incluindo `active`, `superseded` ou tombstone `forgotten`.

Missing UUID conhecido sintaticamente mas inexistente:

```text
404 MEMORY_NOT_FOUND
```

## 13.1 Forgotten response

GET de tombstone é permitido para preservar stable identity/idempotency/lifecycle evidence, mas retorna material cognitivo destruído como `null`/redacted conforme §16.

---

# 14. Typed cognitive recall — `POST /api/v1/memories/recall`

Este endpoint é separado de `POST /api/v1/recall`, que continua sendo knowledge recall.

Request contract v1:

```text
query                 required, 1..8192 chars após normalização
memory_types          optional, default [profile, semantic]
scopes                required, 1..16 canonical scopes
top_k                 optional, default 10, range 1..50
as_of                 optional, default server now UTC; future timestamps rejected
include_superseded    optional, default false
min_relevance         optional cosine similarity threshold [-1,1]
```

Não existe implicit current project; caller sempre escolhe scopes explicitamente.

## 14.1 Default safe behavior

- FORGOTTEN nunca retorna;
- current truth é avaliada em `as_of` conforme §9;
- `include_superseded=false` significa **current truth em `as_of`**, não `lifecycle == ACTIVE` no presente: um item hoje `SUPERSEDED` **é elegível** quando `created_at <= as_of < superseded_at` e as demais regras temporais de §9 são satisfeitas;
- a implementação não pode aplicar um filtro ingênuo `lifecycle = ACTIVE` antes da avaliação histórica;
- `include_superseded=true` permite também histórico superseded que já não era current em `as_of`, quando elegível pela validade temporal, e cada result sinaliza `is_current_truth=false` quando aplicável;
- itens criados depois de `as_of` não retornam;
- `as_of` futuro é `422 INVALID_REQUEST`;
- scope/type filters são exatos.

## 14.2 Embedding e transaction ordering

Query embedding ocorre antes da query PostgreSQL e sem transaction longa aberta.

Provider failure retorna `503 DEPENDENCY_UNAVAILABLE` sem fallback lexical silencioso.

## 14.3 Ranking

`relevance` é cosine similarity entre query embedding e authoritative full embedding.

Ordenação total e determinística:

```text
1. relevance DESC
2. created_at DESC
3. memory_id ASC
```

`confidence` não altera ranking v0.7.

Response HTTP `200`, `SuccessEnvelope[MemoryRecallResult]`.

Response conceitual:

```json
{
  "items": [
    {
      "memory": { "...": "typed MemoryItem" },
      "relevance": 0.91,
      "is_current_truth": true
    }
  ]
}
```

Memory content retornado é evidence/context; não possui prompt-role privilegiado.

---

# 15. Atomic supersession — `POST /api/v1/memories/{memory_id}/supersede`

Supersession substitui um item `ACTIVE` por exatamente um replacement `ACTIVE`.

Replacement herda obrigatoriamente do old item:

```text
memory_type
scope
```

Esses dois campos não são mutáveis via supersession. Mudança de type/scope exige uma operação conceitualmente nova, não reescrita silenciosa de lineage.

Payload de replacement contém:

```text
content
confidence?
valid_from?
valid_until?
provenance   # nova provenance da correção/substituição
```

## 15.1 Ordering

```text
validate replacement
committed-outcome idempotency pre-check (optimization only)
prepare replacement embedding outside transaction
        ↓
short authoritative transaction:
    authoritative Idempotency-Key claim/check (UNIQUE + locking/serialization)
    if loser: resolve/replay winner before evaluating target lifecycle
    if winner:
        lock old MemoryItem row
        require old.lifecycle == ACTIVE
        create replacement ACTIVE
        old.lifecycle = SUPERSEDED
        old.superseded_at = now
        old.superseded_by = replacement.memory_id
        persist idempotency outcome
commit
```

## 15.2 Race guarantee

Duas supersessions concorrentes do mesmo old item **não podem** produzir dois replacements válidos.

Row-level serialization/locking ou mecanismo PostgreSQL equivalente é obrigatório.

- winner: `200`/created replacement;
- same idempotency key + same semantic request: replay do winner, mesmo que o próprio winner já tenha tornado o old item `SUPERSEDED`;
- **same-operation replay tem precedência sobre lifecycle conflict**: o loser do claim não executa primeiro `old.lifecycle == ACTIVE`; ele resolve o ledger e replaya o winner;
- uma operação realmente distinta, com nova key, depois que old deixou de ser ACTIVE: `409 MEMORY_STATE_CONFLICT`.

Replacement pode futuramente ser superseded, formando uma chain linear. Response HTTP `200` devolve o old item já `superseded` e o replacement `active`.

---

# 16. Precise Forget — `POST /api/v1/memories/{memory_id}/forget`

Forget atua **somente** sobre o `memory_id` exato.

Não chama Source/Dataset Forget e não faz cascade semântico para predecessor/replacement.

Allowed:

```text
ACTIVE     -> FORGOTTEN
SUPERSEDED -> FORGOTTEN
FORGOTTEN  -> FORGOTTEN  # resource-state idempotent no-op, inclusive com nova key
```

Missing -> `404 MEMORY_NOT_FOUND`. Success/replay -> HTTP `200` com o tombstone.

`FORGOTTEN -> FORGOTTEN` é idempotência **do estado do recurso**, não apenas replay da mesma key. Portanto um novo Forget válido com uma nova `Idempotency-Key` sobre um item já forgotten também retorna `200` com o tombstone e não causa nova mutação. A semântica normal da key continua tendo precedência: se a key já existir, same-key/same-request replaya seu outcome e same-key/different-request continua `409 IDEMPOTENCY_CONFLICT`.

## 16.1 Destruição obrigatória

Ao concluir Forget, não podem permanecer recuperáveis normalmente:

```text
content
embedding
scope value/project key
confidence
valid_from / valid_until
external provenance references:
  conversation_uuid
  turn_uuid
  task_uuid
  confirmation_ref
  source_ref
  observed_at
```

No v0.7 não existe cognitive Neo4j projection; portanto não há graph content a apagar. Uma futura projection deverá ser removida via graph_outbox no mesmo lifecycle operation.

## 16.2 Tombstone permitido

Pode permanecer exclusivamente:

```text
memory_id
memory_type
lifecycle = FORGOTTEN
created_at
superseded_at?        # se já havia ocorrido
superseded_by?        # lineage identity, sem conteúdo
forgotten_at
provenance.origin_kind
provenance.source_system
idempotency evidence mínima (§17)
```

`memory_provenance` permanece fisicamente/logicamente 1:1 com `MemoryItem`, mas é scrubbed **na mesma transaction do Forget** para exatamente:

```text
origin_kind   = valor original
source_system = valor original (system slug only)
conversation_uuid = NULL
turn_uuid         = NULL
task_uuid         = NULL
confirmation_ref  = NULL
source_ref        = NULL
observed_at       = NULL
```

`source_system` nunca pode conter uma resource reference escondida; continua sujeito ao slug contract de §7.2 antes e depois do Forget.

Nada além disso deve ser retido por conveniência.

Tombstone não é elegível para embedding, recall, semantic search ou content hydration.

---

# 17. Idempotency — MI-006

As mutações cognitivas seguem a semântica já estabelecida pelo projeto para `Idempotency-Key`.

Aplica-se a:

```text
POST /memories
POST /memories/{id}/supersede
POST /memories/{id}/forget
```

Header é opcional para callers genéricos; o Integration Contract exige seu uso pelo Sofia's Assistant.

## 17.1 Regras

Mesma key + mesma operação semântica normalizada:

```text
replay seguro do mesmo outcome lógico
```

Mesma key + request semanticamente diferente:

```text
409 IDEMPOTENCY_CONFLICT
```

`sys:` permanece namespace reservado para mecanismos internos, seguindo o contrato existente.

Content hash **não** é identidade de MemoryItem e nunca causa dedupe/upsert automático.

## 17.2 Race authority e claim transacional

Para mutações com `Idempotency-Key`, o ledger possui uma constraint `UNIQUE` autoritativa sobre a identidade da key definida por este contract. O serviço pode fazer um read-only pre-check antes de embedding, mas correctness não depende dele.

Depois de qualquer embedding externo e **dentro da short PostgreSQL transaction**, o request deve:

1. tentar claim/check autoritativo da key;
2. serializar com qualquer request concorrente da mesma key via `UNIQUE`/locking PostgreSQL;
3. se perder o claim, aguardar/ler o winner committed;
4. comparar o keyed request digest;
5. replayar o winner se same-request, ou retornar `409 IDEMPOTENCY_CONFLICT` se different-request;
6. somente o winner do claim pode executar a mutação de domínio.

Para Supersede, essa resolução ocorre **antes** de avaliar `old.lifecycle == ACTIVE`. Assim um retry concorrente da própria operação que venceu não é transformado em falso `MEMORY_STATE_CONFLICT` pelo estado `SUPERSEDED` produzido pelo winner.

Forget não exige embedding, mas usa a mesma ordem claim/check -> resource lock/state handling. Uma nova key contra um recurso já `FORGOTTEN` é um novo request válido cujo efeito é o no-op de estado definido em §16.

## 17.3 Persistência mínima

A implementação deve manter evidence suficiente para replay/conflict após commit concorrente sem persistir cópia extra do body cognitivo.

O ledger deve reter somente um **keyed, non-reversible request digest** e as identidades do outcome. O canonical request body/content não pode ser duplicado no ledger.

O digest **não pode** ser SHA-256/raw hash, checksum ou qualquer hash unkeyed do request/content. Deve usar HMAC (por exemplo HMAC-SHA-256) com secret server-side de alta entropia, ou construção equivalente resistente a offline dictionary guessing. A key do digest não é persistida junto ao ledger/digest e deve permanecer disponível/estável durante toda a vida de qualquer ledger entry para a qual replay idempotente ainda seja garantido. O valor não é API-visible e não serve como Memory identity ou semantic dedupe key.

Após Forget, esse keyed digest é a única exceção permitida de dado derivado do request original necessária à garantia de idempotência.

---

# 18. Error contract

Todos os errors usam o `ErrorEnvelope` existente e nunca vazam SQL, stack trace, provider payload interno ou Neo4j internals.

Novos stable error codes mínimos:

```text
MEMORY_NOT_FOUND
MEMORY_STATE_CONFLICT
```

Reutilizados:

```text
INVALID_REQUEST
IDEMPOTENCY_CONFLICT
RESERVED_IDEMPOTENCY_KEY_NAMESPACE
MISSING_API_KEY
INVALID_API_KEY
DEPENDENCY_UNAVAILABLE
CONFIGURATION_ERROR
INTERNAL_ERROR
```

Semântica HTTP:

| HTTP | Semântica |
|---|---|
| 400 | reserved idempotency namespace ou request contraditório não-Pydantic |
| 401 | missing/invalid API key |
| 404 | `MEMORY_NOT_FOUND` |
| 409 | idempotency ou lifecycle/concurrency conflict |
| 422 | field/schema validation, `INVALID_REQUEST` |
| 503 | embedding/dependency unavailable ou application not operational |
| 500 | sanitized unexpected internal error |

Não existe blind retry para `409` ou `422`.

---

# 19. Concurrency invariants

1. Create concorrente com mesma idempotency key converge via claim `UNIQUE` autoritativo: same-request replaya um único winner; different-request conflita; não cria duas memórias por replay.
2. Supersession concorrente do mesmo old item produz no máximo um replacement a partir daquele old item; same-operation replay resolve o ledger antes de lifecycle e replaya o winner.
3. Forget concorrente é idempotente e converge para um tombstone; um novo Forget com nova key sobre `FORGOTTEN` também é `200` no-op, sujeito primeiro à semântica normal de colisão da própria key.
4. Forget vs Supersede é serializado sobre o target row:
   - Forget primeiro -> Supersede observa FORGOTTEN e falha `409`;
   - Supersede primeiro -> old torna-se SUPERSEDED; Forget posterior pode esquecer **somente o old**, replacement permanece independente.
5. Nenhuma external embedding call ocorre com row/database transaction mantida aberta.
6. Não há distributed transaction com embedding provider.

---

# 20. Privacy e data minimization

- Cognitive content e embedding são dados potencialmente sensíveis.
- Logs não incluem `content`, embedding, external provenance refs completas ou Idempotency-Key completa.
- OpenAPI/examples não usam dados pessoais reais.
- Forget remove conteúdo e referências externas conforme §16.
- Recall nunca retorna FORGOTTEN.
- Não existe endpoint para "unforget".
- Provenance externa não cria foreign key nem cópia de Conversation/Turn content.
- Memory não armazena Provider Session ID.

---

# 21. Migrations e interação com v0.6

A v0.7 usará Alembic normalmente a partir do head atual `0017`.

A implementação poderá criar uma ou mais revisions posteriores para as tabelas/constraints necessárias a Cognitive Memory.

Todas as migrations automáticas devem obedecer ADR-0015:

- resumable a partir de qualquer committed revision alcançada antes de interrupção;
- nenhuma operação automática de `stamp`, downgrade ou repair;
- startup serialized/fail-closed;
- `DATABASE_MIGRATION_MODE=auto|verify_only` preservado.

Não existe backfill automático de:

- Document/Chunk para MemoryItem;
- SessionEntry para MemoryItem;
- Skill para SEMANTIC/PROFILE;
- Agent Profile para MemoryItem.

---

# 22. Backward compatibility

Permanecem semanticamente intactos:

```text
/api/v1/remember
/api/v1/recall             # knowledge recall
/api/v1/forget             # Source/Dataset/Everything semantic-memory pipeline
/api/v1/sessions
/api/v1/skills
/api/v1/agents
Dataset/Source/Document/Chunk contracts
```

`GET /api/v1/info` só recebe campos aditivos (§11).

Nenhum caller existente é obrigado a adotar Cognitive Memory.

---

# 23. Deferred / non-goals v0.7

Explicitamente fora deste release:

- EPISODIC memory model;
- PROCEDURAL runtime ou reinterpretar Skills;
- automatic semantic dedupe;
- automatic conflict-resolution AI;
- advanced consolidation/decay;
- importance-based ranking;
- Memory prefetch;
- graph-heavy Cognitive Recall;
- Cognitive Memory Neo4j projection;
- Feedback/Improve integration;
- Agent Profiles no Assistant ContextBuilder;
- offline sync;
- multi-user ACL/tenancy;
- generic plugin architecture;
- hard delete de tombstone;
- full recall audit log dentro do Memory;
- content editing in-place.

---

# 24. Acceptance criteria

O feature somente pode ser considerado Implemented quando, no mínimo:

1. `/info` negocia contract/capabilities de forma machine-readable e aditiva.
2. `MemoryItem` existe como domínio/tabela própria, sem Dataset/Source/Document/Chunk fake.
3. tipos `profile|semantic` e scopes `global|project:<key>` são validados deterministically.
4. provenance first-class é persistida e devolvida; após Forget a row 1:1 permanece e é scrubbed atomicamente para `origin_kind` + `source_system`, com todas as external refs `NULL`.
5. create é síncrono, idempotente e não usa PipelineRun.
6. embedding externa ocorre fora de transaction PostgreSQL.
7. typed recall usa PostgreSQL+pgvector, current-truth/temporal filters e ranking determinístico; `as_of=T` com `include_superseded=false` inclui um item hoje SUPERSEDED quando ele era current truth em T, sem filtro ingênuo pelo lifecycle presente.
8. knowledge `/recall` antigo permanece semanticamente inalterado.
9. supersession cria replacement + transiciona old atomicamente, resiste a corrida e dá precedência a same-operation idempotent replay sobre o state conflict produzido pelo próprio winner.
10. precise Forget aceita ACTIVE/SUPERSEDED, trata FORGOTTEN como resource-state idempotent no-op inclusive com nova key, e torna content/embedding/provenance externa irrecuperáveis.
11. FORGOTTEN nunca aparece em typed recall, inclusive com `as_of` histórico.
12. GET de forgotten devolve somente tombstone permitido.
13. `Idempotency-Key` same/same replaya; same/different retorna explicit conflict; race é resolvida por authoritative transactional claim/check sob `UNIQUE`, e request fingerprints persistidos são keyed/non-reversible, nunca raw/unkeyed content hashes.
14. error responses usam `ErrorEnvelope` e stable codes sem leaks internos.
15. não há Cognitive Memory em Neo4j/outbox no MVP.
16. migrations posteriores a `0017` passam pelo bootstrap ADR-0015 em fresh DB e upgrade DB.
17. endpoints existentes permanecem backward compatible.
18. o Cross-repository Integration Contract v1 é testável pelo Sofia's Assistant sem persistir IDs internos do Memory por conveniência.

---

# 25. Decisões aprovadas e congeladas

A revisão humana aprovou explicitamente as seguintes escolhas, agora congeladas para o backlog técnico da v0.7:

1. **`confidence` entra; `importance` fica deferred.**
2. **Cognitive Memory v0.7 não usa Neo4j** e typed recall começa com cosine exato sobre `VECTOR(3072)`.
3. **Forget destrói scope, content, embedding, validity e provenance externa**, mantendo tombstone mínimo + provenance scrubbed `origin_kind/source_system` + keyed/non-reversible idempotency digest interno.
4. **Não existe PATCH de MemoryItem**; correção é por supersession.
5. **Não existe semantic dedupe/conflict AI**; supersession é sempre explícita por `memory_id`.

Este Feature Contract está aprovado e congelado para derivação do backlog técnico da v0.7; isso não autoriza implementação antes do gate humano explicitamente separado.
