# Sofias Memory — Backlog Técnico Executável v0.7.0 Native Cognitive Memory

**Release:** v0.7.0  
**Feature:** Native Cognitive Memory  
**Status:** APPROVED — IMPLEMENTATION NOT STARTED  
**Baseline:** `main` @ `c8520bc3a2d380ff142507146fecd30fd3927efb`  
**Sequência:** SM-1001..SM-1006  
**Architecture authority:** ADR-0016 — Native Cognitive Memory Model and Lifecycle (`accepted`)  
**Product authority:** Feature Contract v0.7.0 — Native Cognitive Memory (`APPROVED / FROZEN FOR IMPLEMENTATION`)  
**Cross-repository authority:** Sofias Memory ↔ Sofia's Assistant Integration Contract v1 (`APPROVED`)  
**Regra de execução:** executar um gate por vez; não antecipar escopo de gates posteriores; nenhuma implementação começa antes da aprovação humana deste backlog.

---

# 1. Objetivo

A v0.7.0 adiciona ao Sofias Memory um domínio nativo e first-class de **Cognitive Memory** para persistir e recuperar `MemoryItem` tipado sem distorcer Dataset/Source/Document/Chunk ou Session/SessionEntry.

Ao final deste backlog, Sofias Memory deverá oferecer:

```text
PROFILE | SEMANTIC MemoryItem
global | project:<key> scope
first-class cognitive provenance
ACTIVE | SUPERSEDED | FORGOTTEN lifecycle
synchronous idempotent Create/Get
typed cognitive Recall
atomic Supersession
destructive precise Forget
machine-readable compatibility negotiation
real PostgreSQL + pgvector authority
Automatic Serialized Migration Bootstrap from Alembic 0017 forward
```

A implementação continua sendo um **modular monolith** e segue a direção arquitetural existente:

```text
api -> services -> domain -> ports -> infrastructure
```

Cognitive Memory é um domínio novo do Sofias Memory; não é uma extensão semântica de `/remember`, knowledge `/recall`, legacy `/forget`, SessionEntry, Skill ou Agent Profile.

---

# 2. Fontes normativas e precedência

Para SM-1001..SM-1006, a precedência é:

1. instrução explícita da task/gate em execução;
2. `AGENTS.md` / `CLAUDE.md`;
3. ADR-0016 — Native Cognitive Memory Model and Lifecycle;
4. Feature Contract v0.7.0 — Native Cognitive Memory;
5. Integration Contract Sofias Memory ↔ Sofia's Assistant v1;
6. ADR-0015 — Automatic Serialized Migration Bootstrap;
7. ADRs anteriores aplicáveis, especialmente:
   - ADR-0002 — PostgreSQL Source of Truth / Neo4j Rebuildable Projection;
   - ADR-0003 — Single Static API Key;
   - ADR-0006 — pgvector 3072 / cosine / full-precision authority;
   - ADR-0007 — PostgreSQL enums/FKs/delete policies;
   - ADR-0009 — idempotency/retry/error precedents onde semanticamente reutilizáveis;
   - ADR-0012 — Session é temporal context, não permanent Cognitive Memory;
   - ADR-0013 — Skills continuam domínio procedural separado;
   - ADR-0014 — Agent Profile continua management resource separado;
8. contratos, migrations e testes existentes do repositório;
9. PRD original apenas onde não foi amended pelos contratos/ADRs acima.

O backlog define **ordem executável, gates e evidências**. Ele não pode reinterpretar contratos congelados.

---

# 3. Baseline e preflight obrigatório

Antes de SM-1001:

```text
branch = main
HEAD = c8520bc3a2d380ff142507146fecd30fd3927efb
worktree = clean
origin/main = HEAD
Alembic head = 0017
canonical app version = 0.6.0
v0.6.0 = RELEASED
```

Se `main` tiver avançado legitimamente antes da execução, o executor deve:

1. registrar o novo HEAD;
2. provar que os commits adicionais não contradizem ADR-0016/Feature Contract/Integration Contract;
3. usar o HEAD novo como baseline;
4. parar somente se houver conflito normativo real.

Os três contratos aprovados e este backlog devem ser materializados no repositório em um **docs-only freeze commit antes do primeiro código**, sem alterar seu conteúdo normativo. Esse commit documental não conta como gate de implementação.

---

# 4. Invariantes globais da release

Durante toda a v0.7:

- PostgreSQL continua authoritative para `MemoryItem`, provenance, lifecycle, embeddings, lineage e cognitive idempotency evidence.
- Neo4j não participa de Cognitive Memory v0.7.
- Nenhum `(:MemoryItem)` e nenhum Cognitive Memory `graph_outbox` event é criado.
- `memory_items`, `memory_provenance` e `cognitive_memory_idempotency` são domínios/tabelas próprias; não criar fake Dataset/Source/Document/Chunk.
- `Session` e `SessionEntry` permanecem contexto temporal; não backfillar nem reinterpretar como MemoryItem.
- Apenas `PROFILE` e `SEMANTIC` são tipos aceitos.
- `EPISODIC` e `PROCEDURAL` permanecem deferred.
- Scope aceita somente `global` e `project:<key>` com matching exato; não introduzir tenancy/ACL genérico.
- `confidence` é first-class; `importance` não existe no v0.7.
- Não existe PATCH de MemoryItem.
- Não existe semantic dedupe, content-hash identity, auto-upsert ou conflict-resolution AI.
- Create/Supersede são síncronos; não criam `PipelineRun`/`PipelineStep`.
- Nenhuma transaction PostgreSQL permanece aberta durante chamada externa de embedding.
- Cognitive embeddings usam o contrato de ADR-0006: authoritative `VECTOR(3072)`, cosine; v0.7 usa recall exato, sem novo ANN/HNSW obrigatório.
- `FORGOTTEN` é destrutivo: content/embedding/scope/validity/confidence/external provenance refs deixam de ser recuperáveis.
- `memory_provenance` permanece 1:1 após Forget, scrubbed para somente `origin_kind + source_system`.
- `source_system` é system slug; nunca resource reference.
- Cognitive idempotency request digest é keyed/non-reversible; raw/unkeyed request/content hash é proibido.
- Idempotency correctness é database-authoritative sob `UNIQUE` + locking/serialization.
- `FORGOTTEN -> FORGOTTEN` é resource-state idempotent no-op inclusive com nova key, depois da semântica normal da key.
- Historical current truth é avaliada em `as_of`; lifecycle presente isoladamente nunca decide current truth histórico.
- APIs v0.6 permanecem semanticamente backward compatible.
- `/info` só recebe extensão aditiva.
- Todos os novos endpoints usam `X-API-Key`, `SuccessEnvelope`/`ErrorEnvelope`, request correlation e sanitização já existentes.
- Não vazar content, vectors, full provenance refs, HMAC secret, SQL, stack trace ou provider internals em logs/errors.
- Toda migration nova segue ADR-0015 e precisa ser segura para automatic serialized bootstrap.

---

# 5. Sequência de gates

| Gate | Ticket | Entrega principal | Depende de | Migration impact | Status |
|---|---|---|---|---|---|
| G1 | SM-1001 | Foundation — domain/schema/provenance/idempotency primitives | docs freeze | `0018` | DONE |
| G2 | SM-1002 | Compatibility negotiation + synchronous Create/Get | SM-1001 | nenhuma nova esperada | DONE |
| G3 | SM-1003 | Typed Cognitive Recall + temporal/current-truth query | SM-1002 | nenhuma nova esperada | TODO |
| G4 | SM-1004 | Atomic Supersession + destructive precise Forget | SM-1003 | nenhuma nova esperada | TODO |
| G5 | SM-1005 | Real-PG concurrency/security/privacy/integration hardening | SM-1004 | somente corretiva se inevitável | TODO |
| G6 | SM-1006 | Release prep + exact-SHA GATE-v0.7.0 + publication/closeout | SM-1005 | valida `0017 -> 0018+` | TODO |

**Regra:** se um gate posterior revelar necessidade de schema não prevista em `0018`, criar revision linear posterior (`0019+`). Não reescrever uma migration já aterrissada/aplicada em gate anterior.

---

# SM-1001 — Foundation: Native Cognitive Memory domain, schema and idempotency primitives

## Objetivo

Criar a fundação estrutural do domínio sem expor ainda a API pública `/memories`: tipos/normalização, modelos PostgreSQL, repositories/UoW necessários, migration pós-`0017`, provenance 1:1, authoritative embedding column e cognitive idempotency ledger.

Este gate deve tornar as invariantes de domínio **estruturalmente testáveis no PostgreSQL** antes de construir endpoints.

## Arquivos/domínios prováveis

Prováveis novos/alterados:

```text
sofias_memory/domain/
    cognitive_memory.py
    cognitive_memory_scope.py
    cognitive_memory_idempotency.py

sofias_memory/infrastructure/postgres/models/
    memory_item.py
    memory_provenance.py
    cognitive_memory_idempotency.py

sofias_memory/infrastructure/postgres/repositories/
    memory_items.py
    memory_provenance.py
    cognitive_memory_idempotency.py

sofias_memory/infrastructure/postgres/unit_of_work.py
sofias_memory/infrastructure/postgres/types.py
sofias_memory/config.py

migrations/versions/
    0018_create_native_cognitive_memory.py

tests/unit/
tests/integration/

.env.example
compose.yaml
deploy/easypanel/compose.yaml
```

Os nomes finais de módulos podem seguir a convenção real do repositório; os três nomes de tabela congelados por ADR-0016 permanecem:

```text
memory_items
memory_provenance
cognitive_memory_idempotency
```

## Escopo técnico

### A. `memory_items`

Representar no mínimo:

```text
id UUID PK                     -> public memory_id
memory_type                    -> profile | semantic
scope nullable after Forget
content nullable after Forget
embedding VECTOR(3072) nullable after Forget
lifecycle                      -> active | superseded | forgotten
confidence nullable
valid_from nullable UTC
valid_until nullable UTC
created_at UTC
superseded_at nullable UTC
superseded_by nullable self-FK
forgotten_at nullable UTC
```

Database constraints devem tornar testáveis:

- `confidence IS NULL OR 0 <= confidence <= 1`;
- `valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from`;
- non-forgotten exige `scope`, `content`, `embedding`;
- `SUPERSEDED` exige `superseded_at` + `superseded_by`;
- `FORGOTTEN` exige `forgotten_at` e exige `scope/content/embedding/confidence/valid_from/valid_until IS NULL`;
- `superseded_by != id`;
- um replacement possui no máximo um direct predecessor, preservando lineage linear;
- tombstone pode preservar `superseded_at/superseded_by` quando a memória já havia sido superseded antes do Forget.

`memory_type`, `lifecycle` e enums equivalentes devem seguir ADR-0007.

Não criar ANN/HNSW para Cognitive Memory neste release.

### B. `memory_provenance`

Relação obrigatória 1:1 com `memory_items`, contendo:

```text
memory_id PK/FK
origin_kind
source_system
conversation_uuid nullable
turn_uuid nullable
task_uuid nullable
confirmation_ref nullable
source_ref nullable
observed_at nullable
```

Implementar validation/constraints compatíveis com as regras por `origin_kind` do Feature Contract.

As regras de refs obrigatórias por origin valem para a **provenance completa de item não-FORGOTTEN**. Não criar um `CHECK` local em `memory_provenance` que torne impossível o scrub aprovado (`origin_kind + source_system`, refs externas NULL) depois de Forget. Como lifecycle/confidence vivem em `memory_items`, as regras contextuais por origin devem ser application-enforced + real-PostgreSQL integration-tested, salvo se houver uma constraint relacional simples que preserve explicitamente o tombstone scrubbed sem trigger/complexidade desnecessária.

### C. `cognitive_memory_idempotency`

Ledger dedicado, independente de `PipelineRun`, com:

```text
idempotency_key UNIQUE
operation identity
keyed request digest
target/result Memory identities suficientes para replay
created_at
```

O ledger:

- nunca armazena request body/content;
- nunca usa content hash como identity/dedupe;
- deve permitir `create`, `supersede`, `forget`;
- usa claim autoritativo por `UNIQUE`;
- persiste outcome na mesma transaction da mutação de domínio;
- um rollback não pode deixar claim órfão committed.

### D. keyed digest secret

Introduzir secret dedicado de processo para HMAC, preferencialmente congelado como:

```text
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

Requisitos:

- `SecretStr`;
- sem valor default inseguro;
- documentação exige material aleatório de alta entropia;
- secret nunca aparece em `/info`, logs, errors, OpenAPI examples ou persisted ledger;
- usar HMAC-SHA-256 ou equivalente com domain separation estável;
- canonical semantic request inclui operation + path target quando aplicável + payload canonicalizado;
- digest persistido não é API-visible;
- não reutilizar `API_KEY` como digest key: rotação de API credential não deve destruir a capacidade de validar ledger histórico;
- config parity deve permanecer consistente em `.env.example`, Compose e EasyPanel.

Se o projeto já possuir antes da implementação uma primitive de secret durável com exatamente essas propriedades, ela pode ser reutilizada mediante prova; não criar alias duplicado.

## Invariantes

- Nenhuma API `/memories` ainda é necessária neste gate.
- Nenhum PipelineRun.
- Nenhum Neo4j/outbox.
- Embedding armazenado full precision `VECTOR(3072)`.
- `memory_provenance` não vira JSON metadata.
- `source_system` valida apenas system slug.
- Scope normalization é centralizada e reutilizável, nunca duplicada por endpoints.
- Content normalization é centralizada.
- Todas as timestamps persistidas são timezone-aware/UTC conforme convenções do projeto.

## Dependências

- docs freeze aprovado;
- ADR-0006/0007;
- ADR-0015;
- Alembic head inicial `0017`.

## Testes obrigatórios

### Unit/domain

Provar:

- `profile|semantic` only;
- canonical scope `global|project:<key>`;
- uppercase/whitespace/invalid project key rejection;
- content normalization CRLF/CR -> LF + edge trim;
- content limits;
- provenance origin validations;
- confidence bounds and inferred-confidence requirement;
- HMAC canonicalization determinística;
- same semantic request + same secret -> same digest;
- same semantic request + different secret -> different digest;
- digest não é raw SHA-256 do canonical body;
- reserved `sys:` key validation reutilizada/compatível.

### Migration/model

Migration tests devem provar:

- `0017 -> 0018` linear;
- fresh upgrade cria exatamente as três tabelas esperadas;
- enums/checks/FKs/UNIQUE existem;
- `memory_items.embedding` é `VECTOR(3072)`;
- no HNSW/ANN Cognitive Memory index;
- provenance 1:1;
- idempotency key `UNIQUE`;
- state-invalid rows são recusadas pelo PostgreSQL;
- self-supersession é recusada;
- dois predecessors para o mesmo replacement são recusados;
- downgrade behavior segue política real do projeto, sem inventar support não contratado.

### Real PostgreSQL

Em DB dedicado:

```text
upgrade 0017 -> 0018
insert ACTIVE válido
insert SUPERSEDED válido
reject malformed lifecycle rows
insert FORGOTTEN tombstone válido
reject forgotten row retaining cognitive payload
provenance 1:1 enforcement
idempotency UNIQUE enforcement
```

## Migration impact

**Sim.** Revision esperada:

```text
0018_create_native_cognitive_memory.py
down_revision = 0017
```

A migration deve ser PostgreSQL-transactional/resumable conforme ADR-0015. Nenhum `stamp`, autocommit DDL não-idempotente ou side-channel repair.

## Concurrency / Security / Privacy obligations

- `UNIQUE(idempotency_key)` é a autoridade de race, não um read-before-write.
- Ledger não contém plaintext cognitive request.
- HMAC key não é persistida no PostgreSQL.
- Secret não entra em telemetry/log/fingerprint de forma reversível.
- DB constraints impedem estado forgotten com payload cognitivo residual.
- External provenance refs não são FK cross-database.

## Definition of Done

SM-1001 só fecha quando:

1. migration `0018` é linear, limpa e passa em PostgreSQL real;
2. três tabelas authoritative existem com constraints/fks corretos;
3. domain normalizers/validators têm testes exaustivos;
4. HMAC digest primitive cumpre o contrato keyed/non-reversible;
5. config parity do novo secret está verde;
6. nenhuma rota `/memories` pública foi antecipada;
7. Neo4j/graph_outbox/PipelineRun permanecem intocados;
8. `uv lock --check`, ruff, format, mypy e suites existentes permanecem verdes.

## SM-1001 — Evidência de fechamento (DONE)

```text
Baseline:                main @ 2490e32833358c27d4b4b2ce0f99c27d9d087ce2 (CI #72 SUCCESS)
Implementation SHA:      230e06ba56d0f522c56e36bffe1cf35572a446e8
CI fix SHA:              bd8c32d4262c57521fc6466a8c6d309a02229fa2 (fix(ci): supply
                          COGNITIVE_IDEMPOTENCY_HMAC_KEY to the release consistency gate)
Closeout SHA:            83c3992c092d8c50ee401608b75fad9ff64e362e (docs(v0.7): close SM-1001)
Alembic head:             0018 (down_revision = 0017)

Final closeout CI:
CI #75
run 34779407916
SUCCESS
head_sha = 83c3992c092d8c50ee401608b75fad9ff64e362e
```

Tabelas criadas (real PostgreSQL, verificado via introspecção direta):

```text
memory_items                    -- id, memory_type, scope, content, embedding
                                    VECTOR(3072), lifecycle, confidence,
                                    valid_from/until, created_at,
                                    superseded_at/by, forgotten_at
memory_provenance                -- 1:1 (memory_id é PK+FK), origin_kind,
                                    source_system, external refs, observed_at
cognitive_memory_idempotency     -- idempotency_key UNIQUE, operation,
                                    request_digest, target/result memory ids
```

Constraints estruturais provadas contra PostgreSQL real (não apenas em unit
tests com spy): `confidence` bounds, `validity_window_ordered`,
`no_self_supersession`, `content_not_blank`/`content_max_length`,
`scope_grammar`, `non_forgotten_requires_cognitive_payload`,
`active_requires_clean_lineage`, `superseded_requires_lineage_markers`,
`forgotten_requires_tombstone_shape`, partial `UNIQUE(superseded_by)`
(lineage linear), `UNIQUE(idempotency_key)`, `request_digest_hex`,
`operation_target_shape`. Nenhum ANN/HNSW index foi criado.

Domain primitives: `sofias_memory/domain/cognitive_memory.py` (content
normalization, confidence, provenance-by-origin, source_system slug),
`cognitive_memory_scope.py` (`global`/`project:<key>` grammar),
`cognitive_memory_idempotency.py` (canonical request + HMAC-SHA-256 keyed
digest; `sys:` reserved namespace proven equal to
`services.pipeline_submission`'s contract).

Testes:

```text
unit (domain + migration/model):  111 tests (test_cognitive_memory*.py,
                                   test_cognitive_memory_scope.py,
                                   test_cognitive_memory_idempotency.py,
                                   test_native_cognitive_memory_migration.py)
migration/model:                  10/10 passed (OperationSpy-based)
real PostgreSQL (dedicated):      16/16 passed
                                   (test_native_cognitive_memory_postgres_integration.py,
                                   env SOFIAS_MEMORY_RUN_POSTGRES_COGNITIVE_MEMORY_TESTS=1)
full unit/contract/security:      2403 passed
real-infra regression sweep:      227 passed, 0 failed (all opt-in
                                   PostgreSQL+Neo4j suites except one
                                   testcontainers-only gate unavailable in
                                   this environment)
migration round-trip:             0017 -> 0018 -> 0017 -> 0018 (upgrade/
                                   downgrade/re-upgrade) verified against
                                   real PostgreSQL, plus the full
                                   empty-database gate
                                   (test_postgres_migration_gate.py, updated
                                   for the new head)
```

Quality gates: `uv lock --check`, `ruff check`, `ruff format --check`,
`mypy sofias_memory scripts`, `git diff --check` all green at the final
HEAD.

CI: implementation SHA `230e06b` failed once (`Settings / .env.example /
Compose parity + version consistency` — `docker compose config` required
the new `COGNITIVE_IDEMPOTENCY_HMAC_KEY` that the job's env block did not
yet supply); fixed and re-pushed as `bd8c32d`, both jobs green
(`CI` run `34779224743`).

Escopo confirmado intocado: nenhuma rota `/memories` pública, nenhum
`PipelineRun`/`PipelineStep`, nenhum evento `graph_outbox`, nenhum node
Neo4j — provado por teste de integração dedicado
(`test_no_pipeline_run_or_graph_outbox_row_is_created`) e por leitura
literal da migration (nenhuma referência a essas tabelas fora do
docstring).

Ripple conhecido e documentado: `COGNITIVE_IDEMPOTENCY_HMAC_KEY` é um novo
secret obrigatório (`SecretStr`, sem default), seguindo o precedente de
`API_KEY`/`NEO4J_PASSWORD`/`LLM_API_KEY` — isso exigiu adicionar o valor de
teste em todo fixture/local helper que já construía `Settings` (35 arquivos
de teste unitário + 3 de contract/security), sem alterar o comportamento
desses testes.

---

# SM-1002 — Compatibility negotiation + synchronous Create/Get

## Objetivo

Expor a primeira superfície pública de Cognitive Memory: negotiation em `/info`, `POST /api/v1/memories` e `GET /api/v1/memories/{memory_id}`, reutilizando envelopes/auth/error/idempotency existentes e mantendo o write síncrono fora de PipelineRun.

## Arquivos/domínios prováveis

```text
sofias_memory/api/routes/
    info.py
    memories.py

sofias_memory/schemas/
    memories.py
    common.py              # apenas stable error codes necessários

sofias_memory/services/
    cognitive_memory.py
    cognitive_idempotency.py

sofias_memory/ports/       # se necessário para embedding seam
sofias_memory/infrastructure/postgres/repositories/*
sofias_memory/app.py
sofias_memory/api/openapi_responses.py

tests/unit/
tests/contract/
tests/integration/
docs/api.md                # apenas quando gate implementar superfície pública
```

## Escopo técnico

### A. `/info`

Adicionar **sem remover/renomear campos existentes**:

```text
api_contract_version = "1"
contracts["cognitive_memory"] = "1"
```

Capabilities são **progressivas durante os gates de implementação** e anunciam somente operações realmente disponíveis naquele HEAD:

```text
SM-1002:
  cognitive_memory.write
  cognitive_memory.get

SM-1003 adiciona:
  cognitive_memory.recall

SM-1004 adiciona:
  cognitive_memory.supersede
  cognitive_memory.forget
```

No GATE-v0.7.0 final, as cinco capabilities congeladas pelo Feature Contract devem estar presentes.

Capabilities anunciam contract implementado, não health momentâneo; um HEAD intermediário nunca pode anunciar uma operação que sua rota/service ainda não implementa.

A release version não é negotiation authority.

### B. Create

Implementar:

```text
POST /api/v1/memories
201 SuccessEnvelope[MemoryItem]
```

Ordering obrigatório:

```text
validate/canonicalize
optional committed-outcome pre-check
external embedding call
short PostgreSQL transaction:
    authoritative key claim/check
    winner inserts MemoryItem + provenance + outcome
    loser replays same digest OR conflicts different digest
commit
response
```

Sem `Idempotency-Key`, cada request válido é uma create independente; content igual não deduplica.

Embedding failure antes do commit cria **zero** MemoryItem/provenance/ledger outcome.

### C. Get

Implementar:

```text
GET /api/v1/memories/{memory_id}
200 MemoryItem/tombstone
404 MEMORY_NOT_FOUND
```

O schema público deve ser lifecycle-safe: campos cognitivos que serão destruídos no futuro precisam suportar tombstone sem criar outro response shape incompatível.

## Invariantes

- Create não cria `PipelineRun`.
- Nenhuma transaction aberta durante embedding.
- provenance é persisted no mesmo commit do item.
- API nunca expõe embedding.
- MemoryCandidate/confirmation lifecycle não existe no Memory.
- no PATCH.
- no semantic dedupe.
- scope/type são imutáveis após create, exceto scrub destrutivo de scope no Forget futuro.
- `/info` change é estritamente aditiva.

## Dependências

- SM-1001;
- existing embedding provider/port;
- existing auth/envelope/error machinery.

## Testes obrigatórios

### API/contract/OpenAPI

Provar:

- `/info` mantém todos os campos v0.6 e adiciona contract/capabilities;
- capabilities são estáveis/canônicas;
- Create = 201;
- Get = 200;
- missing = `404 MEMORY_NOT_FOUND`;
- all new routes require `X-API-Key`;
- Pydantic validation = `422 INVALID_REQUEST`;
- `sys:` idempotency key = stable 400/error code;
- OpenAPI documents all request/response schemas and `Idempotency-Key`;
- custom human-doc filtering of `Idempotency-Key` continua coerente com o novo número de rotas; nenhum teste hardcoded de “seis rotas” pode ficar stale;
- nenhum endpoint v0.6 muda schema/status semantics.

### Service/unit

Provar:

- provider call happens before UoW transaction;
- provider error -> no commit;
- same key/same request sequential replay -> same `memory_id`, same 201 logical outcome;
- same key/different request -> 409;
- absent key + same content twice -> two different MemoryItems;
- provenance rule failures;
- inferred without confidence fails;
- `USER_ASSERTED` does not synthesize `confidence=1.0`.

### Real PostgreSQL

Provar:

- `SM-1002` `/info` anuncia somente `write|get`, nunca capabilities futuras;
- create persists one item + exactly one provenance;
- embedding is full vector and never returned;
- ledger stores digest/outcome only;
- Get hydrates authoritative PostgreSQL state;
- no graph_outbox row is created;
- no PipelineRun/PipelineStep row is created.

## Migration impact

Nenhuma migration nova é esperada além de `0018`.

Se o gate descobrir requisito de schema ausente, parar para classificar se é:
- correção legítima do design já congelado -> criar `0019+`, nunca reescrever `0018`;
- mudança de contrato -> blocker arquitetural, não corrigir silenciosamente.

## Concurrency / Security / Privacy obligations

- O pre-check não é race authority.
- Keyed digest nunca é retornado.
- `Idempotency-Key` completa nunca é logada.
- content/provenance refs não entram em logs.
- auth/error behavior é idêntico à disciplina existente.
- no provider payload interno em `503`.

## Definition of Done

1. `/info`, Create e Get implementados e documentados;
2. Create é síncrono e retry-safe;
3. Create/Get passam em PostgreSQL real;
4. OpenAPI/contract tests congelam a surface;
5. legacy APIs v0.6 passam sem mudanças semânticas;
6. no PipelineRun/Neo4j/outbox;
7. CI padrão verde.

## SM-1002 — Evidência de fechamento (DONE)

```text
Baseline:                main @ 2d0c2d6cc4488ecc60f56fcbfd0b648e90e6d7ae
                          (SM-1001 evidence correction, CI SUCCESS)
Implementation SHA:      87765a8ec6ed6dc73446eb74d1504d47e219ebb6
Alembic head:            0018 (down_revision = 0017) -- unchanged, no new migration
```

Rotas públicas novas:

```text
GET  /api/v1/info        -- additive: api_contract_version, contracts,
                             capabilities (write/get only at this HEAD)
POST /api/v1/memories    -- 201 SuccessEnvelope[MemoryItem], synchronous
GET  /api/v1/memories/{memory_id}  -- 200 MemoryItem / 404 MEMORY_NOT_FOUND
```

Capabilities anunciadas neste HEAD: `cognitive_memory.write`,
`cognitive_memory.get` -- exatamente as duas, nenhuma antecipada.

Create ordering provado (real PostgreSQL, `BlockingEmbeddingClient` +
session-factory call counter): zero sessões PostgreSQL abertas enquanto o
embedding está bloqueado; a authoritative transaction só abre depois do
retorno do provider.

Idempotência: pre-check committed (read-only) antes do embedding; claim
autoritativo via `UNIQUE(idempotency_key)` dentro de um `SAVEPOINT` que
also contém o insert de `MemoryItem`+`MemoryProvenance`, de forma que um
loser não deixa nenhuma linha órfã. Prova de corrida real com
`asyncio.Barrier` (duas chamadas concorrentes, mesma key/mesmo request,
sem `sleep`): convergem para um único `memory_id`, uma única
`memory_provenance`, uma única linha de ledger.

Testes novos (contagem exata via `pytest --collect-only`):

```text
schemas (test_memories_schemas.py):                28
routes, service faked (test_memories_routes.py):   10
domain timestamp/validity-window additions
  (test_cognitive_memory.py):                       8
/info capability negotiation (test_info.py):        2
OpenAPI route/shape contract
  (test_openapi_forbidden_routes.py):               3
real PostgreSQL/HTTP
  (test_cognitive_memory_create_get_postgres_integration.py):
                                                    11/11 passed, incluindo
                                                    o teste de corrida com
                                                    barrier
full unit/contract/security:                       2528 passed, 582 skipped
SM-1001 real-PG regression:                        47 passed, 3 skipped
security suite:                                    32 passed
```

Escopo confirmado intocado: nenhum `PipelineRun`/`PipelineStep`/
`graph_outbox` criado por Create (contagem antes/depois idêntica, teste
dedicado); nenhum node Neo4j; nenhum `PATCH`/`recall`/`supersede`/`forget`
de Cognitive Memory implementado; `/api/v1/recall`, `/api/v1/remember`,
`/api/v1/forget`, `/api/v1/sessions`, `/api/v1/skills`, `/api/v1/agents`
permanecem semanticamente inalterados (full regression verde).

Achado corrigido durante a implementação (documentação, não decisão
técnica): as docstrings dos enums `CognitiveMemoryType`/
`CognitiveMemoryLifecycle`/`CognitiveMemoryOriginKind`/
`CognitiveMemoryOperation` (`sofias_memory/domain/enums.py`) continham
marcadores `ADR-0016`/`ADR-0013` que, ao serem expostos pela primeira vez
publicamente via `/memories`, vazavam para o OpenAPI (`enum.description`
gerado pelo docstring da classe). Reescritas sem marcador interno,
preservando o significado; nenhuma tag de rota faltava metadata (`memories`
adicionada a `TAG_METADATA`).

Quality gates: `uv lock --check`, `ruff check`, `ruff format --check`,
`mypy sofias_memory scripts`, `git diff --check` todos verdes na
implementation SHA.

CI implementation: run `34782479814`, head_sha
`87765a8ec6ed6dc73446eb74d1504d47e219ebb6`, conclusion SUCCESS.

---

# SM-1003 — Typed Cognitive Recall and historical current-truth semantics

## Objetivo

Implementar `POST /api/v1/memories/recall` como retrieval separado do knowledge `/recall`, usando query embedding + PostgreSQL/pgvector exato, filtros de tipo/scope/tempo e ranking total determinístico.

## Arquivos/domínios prováveis

```text
sofias_memory/api/routes/memories.py
sofias_memory/schemas/memories.py
sofias_memory/services/cognitive_memory_recall.py
sofias_memory/infrastructure/postgres/repositories/memory_items.py
sofias_memory/ports/embedding.py ou seam já existente

tests/unit/test_cognitive_memory_recall*.py
tests/contract/
tests/integration/test_cognitive_memory_recall_postgres_integration.py
docs/api.md
```

## Escopo técnico

Request contract:

```text
query                 required
memory_types          optional default [profile, semantic]
scopes                required 1..16
top_k                 default 10, range 1..50
as_of                 optional, default server now UTC
include_superseded    default false
min_relevance         optional [-1,1]
```

### Query ordering

```text
validate/canonicalize request
external query embedding
PostgreSQL query
hydrate typed results
deterministic sort/limit
response
```

Nenhuma long transaction durante query embedding.

### Current truth predicate

Para `include_superseded=false`, eligibility em `T=as_of` exige:

```text
lifecycle != FORGOTTEN
content/embedding exist
created_at <= T
(superseded_at IS NULL OR T < superseded_at)
(valid_from IS NULL OR valid_from <= T)
(valid_until IS NULL OR T < valid_until)
scope/type exact match
```

**Proibição:** não aplicar `lifecycle = ACTIVE` como filtro de current truth histórico. Um item atualmente SUPERSEDED deve retornar se era current truth em `T`.

Para `include_superseded=true`, permitir histórico superseded elegível pelas regras de existência/validity e marcar `is_current_truth` corretamente.

### Ranking

Cosine similarity sobre authoritative full `VECTOR(3072)`:

```text
relevance DESC
created_at DESC
memory_id ASC
```

`confidence` não altera ranking.

Sem ANN/HNSW, sem Neo4j, sem fallback lexical silencioso.

## Invariantes

- typed recall != legacy knowledge recall;
- legacy `POST /api/v1/recall` permanece intocado semanticamente;
- FORGOTTEN nunca retorna, nem para `as_of < forgotten_at`;
- future `as_of` = 422;
- scope matching é exato;
- returned memory é evidence/context, nunca authority;
- no full recall audit log novo no Memory;
- no SessionEntry injection automática.

## Dependências

- SM-1002;
- authoritative embedding schema SM-1001.

## Testes obrigatórios

### Unit/query builder

Cobrir matriz temporal:

```text
ACTIVE current now
ACTIVE before valid_from
ACTIVE at valid_from
ACTIVE at valid_until (exclusive -> false)
SUPERSEDED queried before superseded_at -> current truth true
SUPERSEDED queried at/after superseded_at -> false when include_superseded=false
SUPERSEDED historical -> included when include_superseded=true
created_after_as_of -> excluded
FORGOTTEN -> excluded for every as_of
```

### Ranking

Com vectors controlados, provar:

- cosine exact ordering;
- `min_relevance`;
- deterministic ties por `created_at`, depois UUID;
- top_k;
- memory_types/scopes filters.

### Provider/error

- query embedding before DB transaction/query UoW lifecycle apropriado;
- provider unavailable -> `503 DEPENDENCY_UNAVAILABLE`;
- no lexical fallback.

### OpenAPI/contract

- endpoint separado;
- typed result includes `memory`, `relevance`, `is_current_truth`;
- embedding não aparece;
- legacy `/recall` schema/examples permanecem inalterados.

### Real PostgreSQL + pgvector

Em DB dedicado com `vector` real:

- inserir fixtures ACTIVE/SUPERSEDED/FORGOTTEN;
- executar SQL real de cosine;
- provar especialmente:
  `currently SUPERSEDED + as_of before superseded_at + include_superseded=false -> returned/current`;
- provar FORGOTTEN nunca retorna;
- provar order total.

## Migration impact

Nenhuma migration nova esperada.

Não criar ANN index “por performance preventiva”. Se explain/query plan revelar necessidade futura, documentar como future work; não expandir v0.7.

## Concurrency / Security / Privacy obligations

- Recall é read-only.
- Nenhum content de item fora do scope solicitado pode aparecer por hydration bug.
- Nenhum FORGOTTEN pode reaparecer via stale embedding/secondary query.
- Provider/query errors permanecem sanitized.
- Não logar query embedding.

## Definition of Done

1. typed recall completo conforme contract v1;
2. `/info` adiciona `cognitive_memory.recall` somente neste gate e continua sem anunciar `supersede|forget`;
3. historical `as_of` semantics comprovadas em PostgreSQL real;
4. exact cosine + stable tie-break comprovados;
5. FORGOTTEN exclusion comprovada;
6. legacy knowledge recall regression verde;
7. OpenAPI/contract tests verdes;
8. nenhuma dependência Neo4j/ANN introduzida.

---

# SM-1004 — Atomic Supersession and destructive precise Forget

## Objetivo

Completar o lifecycle mutável do domínio com duas operações atômicas:

```text
POST /api/v1/memories/{memory_id}/supersede
POST /api/v1/memories/{memory_id}/forget
```

Supersession deve manter uma única lineage current replacement sob concorrência. Forget deve destruir material cognitivo e preservar apenas tombstone/provenance scrubbed/idempotency evidence permitidos.

## Arquivos/domínios prováveis

```text
sofias_memory/api/routes/memories.py
sofias_memory/schemas/memories.py
sofias_memory/services/cognitive_memory.py
sofias_memory/services/cognitive_idempotency.py
sofias_memory/infrastructure/postgres/repositories/memory_items.py
sofias_memory/infrastructure/postgres/repositories/memory_provenance.py
sofias_memory/infrastructure/postgres/repositories/cognitive_memory_idempotency.py

tests/unit/
tests/contract/
tests/integration/
docs/api.md
```

## Escopo técnico — Supersession

Replacement:

- exige old `ACTIVE`;
- herda `memory_type` + `scope`;
- recebe novo content/confidence/validity/provenance;
- embedding do replacement é calculado fora da transaction.

Transaction:

```text
authoritative idempotency claim/check
if loser:
    resolve committed winner first
    same digest -> replay
    different digest -> 409
if winner:
    SELECT/lock old row
    require ACTIVE
    insert replacement ACTIVE + provenance
    old -> SUPERSEDED
    old.superseded_at = now
    old.superseded_by = replacement.id
    persist outcome
commit
```

Same-operation replay tem precedência sobre state conflict criado pelo próprio winner.

## Escopo técnico — Forget

Forget exato por `memory_id`.

Ordering:

```text
validate key/request
authoritative idempotency claim/check
if existing key -> replay/conflict normally
if fresh claim:
    lock target row
    missing -> 404
    ACTIVE/SUPERSEDED:
        scrub memory cognitive payload
        scrub provenance external refs
        lifecycle -> FORGOTTEN
        forgotten_at = now
    FORGOTTEN:
        no-op de estado
    persist this key's outcome
commit
return tombstone
```

Destruir atomicamente:

```text
content = NULL
embedding = NULL
scope = NULL
confidence = NULL
valid_from = NULL
valid_until = NULL

memory_provenance:
conversation_uuid = NULL
turn_uuid = NULL
task_uuid = NULL
confirmation_ref = NULL
source_ref = NULL
observed_at = NULL
```

Preservar somente:

```text
memory_id
memory_type
lifecycle
created_at
superseded_at?
superseded_by?
forgotten_at
origin_kind
source_system
minimal cognitive idempotency evidence
```

`source_system` continua slug, não resource ref.

## Invariantes

- Ao concluir este gate, `/info` passa a anunciar `cognitive_memory.supersede` e `cognitive_memory.forget`; nenhuma capability é anunciada antes de sua implementação.
- Supersession nunca é `POST new + later mutate old`.
- Um old item gera no máximo um direct replacement.
- Distinct second supersede after winner -> `409 MEMORY_STATE_CONFLICT`.
- same-key/same-request retry -> original winner.
- Forget old superseded item não esquece replacement.
- Forget active item não cria replacement.
- `FORGOTTEN -> FORGOTTEN` com nova key = 200 no-op + tombstone.
- Same already-bound key semantics rodam antes do state no-op.
- Forget nunca chama legacy Source/Dataset `/forget`.
- no unforget.
- no graph_outbox.

## Dependências

- SM-1003.

## Testes obrigatórios

### Supersession

- happy path old ACTIVE -> old SUPERSEDED + replacement ACTIVE;
- inheritance de type/scope;
- replacement provenance própria;
- old non-ACTIVE + distinct operation -> 409;
- same-key sequential replay after old already superseded -> original 200 outcome;
- same key/different request -> idempotency 409;
- replacement pode posteriormente ser superseded, formando chain linear;
- no two direct predecessors for one replacement.

### Forget

- ACTIVE -> FORGOTTEN;
- SUPERSEDED -> FORGOTTEN preserving lineage identity;
- FORGOTTEN + new key -> 200 same tombstone/no second destructive mutation;
- FORGOTTEN + same prior key/same request -> replay;
- reused key/different request -> 409 before state no-op;
- missing -> 404;
- GET after Forget returns tombstone only;
- typed recall after Forget excludes item even with historical `as_of`;
- provenance row still exists exactly once with only origin/source;
- legacy Source/Dataset Forget is not invoked.

### PostgreSQL atomicity

Fault injection/rollback must prove no state where:

- replacement committed but old still ACTIVE;
- old SUPERSEDED but replacement missing;
- memory content cleared but provenance external refs remain;
- ledger outcome committed without its corresponding domain outcome.

## Migration impact

Nenhuma migration nova esperada.

Qualquer missing constraint discovered aqui deve become `0019+`, not rewrite `0018`.

## Concurrency / Security / Privacy obligations

- row-level target serialization obrigatório;
- idempotency claim resolution ocorre antes de Supersede lifecycle validation;
- Forget scrubbing e provenance scrubbing no mesmo transaction;
- no sensitive content in ledger/logs;
- no stale embedding remains after Forget;
- no accidental cascading Forget to replacement;
- no semantic lineage inference.

## Definition of Done

1. Supersede e Forget routes/contracts implementados;
2. atomicity provada com PostgreSQL real;
3. resource-state Forget idempotency provada;
4. privacy scrub completo provado;
5. same-operation Supersede replay precedence provada;
6. typed recall/GET refletem lifecycle corretamente;
7. nenhum Neo4j/outbox/legacy Forget coupling;
8. suites anteriores verdes.

---

# SM-1005 — Integration, concurrency, security and privacy hardening

## Objetivo

Executar o hardening adversarial da feature completa contra infraestrutura real e congelar as propriedades que não podem depender de scheduling acidental, mocks ou sleeps.

Este gate não adiciona produto novo; ele prova SM-1001..SM-1004 e corrige defeitos dentro do contrato congelado.

## Arquivos/domínios prováveis

```text
tests/integration/
    test_cognitive_memory_postgres_integration.py
    test_cognitive_memory_concurrency_postgres_integration.py
    test_cognitive_memory_privacy_postgres_integration.py
    test_cognitive_memory_migration_bootstrap_integration.py

tests/contract/
tests/security/
tests/unit/

.github/workflows/integration.yml
docs/development.md
scripts/ test helpers somente se justificados
```

Production code só muda se os testes reais revelarem defeito dentro do contrato aprovado.

## Dependências

- SM-1004 completo;
- PostgreSQL/pgvector real;
- existing Integration workflow.

## Invariantes

- este gate não adiciona nova feature pública;
- production code só muda para corrigir defeito comprovado dentro dos contratos congelados;
- races são provadas por sincronização determinística, nunca por timing probabilístico;
- PostgreSQL continua única autoridade da prova de concorrência;
- privacy proof consulta estado committed real, não apenas responses;
- Cognitive Memory continua sem Neo4j/outbox/PipelineRun;
- legacy APIs v0.6 permanecem semanticamente intactos.

## Testes obrigatórios

### Concurrency test discipline

**Proibido usar `sleep()` como mecanismo de sincronização/correção de race.**

Usar:

```text
asyncio.Event
threading.Event
barriers
instrumented repository/provider seams
PostgreSQL row/advisory/unique-lock observability
```

Sleep pode existir apenas como timeout de segurança externo do teste, nunca como prova de ordering.

### Matriz mínima de races — real PostgreSQL

### Create / idempotency

1. same key + same request, dois participantes após pre-check:
   - exatamente um claim winner;
   - exatamente um MemoryItem;
   - exatamente uma provenance;
   - ambos convergem ao mesmo `memory_id`;
   - um único logical 201 outcome.
2. same key + different request:
   - no máximo um commit de domínio;
   - loser = `409 IDEMPOTENCY_CONFLICT`.
3. provar que embedding pode ocorrer concorrentemente antes do claim sem quebrar correctness.
4. provar que nenhuma DB transaction fica aberta durante embedding barrier.

### Supersession

1. same key + same request concorrente:
   - exatamente um replacement;
   - loser replaya winner;
   - zero falso `MEMORY_STATE_CONFLICT`.
2. distinct keys contra mesmo old:
   - um success;
   - um `MEMORY_STATE_CONFLICT`;
   - exatamente um replacement.
3. no branch/duplicate lineage.

### Forget

1. same key concorrente -> um tombstone/outcome convergente;
2. different fresh keys concorrentes sobre ACTIVE -> ambos podem retornar 200 lógico, estado final único FORGOTTEN, sem data resurrection;
3. new key sobre já FORGOTTEN -> 200 no-op;
4. same bound key/different request -> 409 precede no-op.

### Forget vs Supersede

Com barriers, provar ambos os orderings:

```text
Forget lock/commit primeiro
-> Supersede observa FORGOTTEN -> 409

Supersede lock/commit primeiro
-> old SUPERSEDED + replacement ACTIVE
-> Forget(old) esquece somente old
-> replacement permanece ACTIVE
```

### Privacy hardening obrigatório

Usar sentinels únicos e consultar PostgreSQL diretamente após Forget.

Provar ausência em todas as authoritative Cognitive Memory surfaces:

```text
memory_items.content is NULL
memory_items.embedding is NULL
memory_items.scope is NULL
confidence/validity NULL
memory_provenance external refs NULL
origin_kind/source_system preserved
cognitive_memory_idempotency has no raw body/content/external refs
```

Adicionalmente:

- keyed digest != raw SHA-256/request hash;
- digest muda com HMAC key diferente;
- HMAC secret nunca aparece em row/API/log;
- captured logs não contêm sensitive content, full Idempotency-Key ou external provenance sentinel;
- GET tombstone não contém material destruído;
- recall `as_of` anterior ao Forget ainda não consegue recuperar conteúdo;
- nenhuma Cognitive Memory row/event existe em Neo4j/graph_outbox.

### Compatibility/backward regression

Rodar a suite completa para provar semanticamente intactos:

```text
/remember
/recall
/forget
/sessions
/skills
/agents
/datasets
/runs
/health
/info legacy fields
```

OpenAPI:

- novos endpoints presentes;
- legacy endpoint schemas/statuses intactos;
- `ErrorEnvelope` universal;
- `Idempotency-Key` canonical docs corretos;
- forbidden PATCH ausente;
- EPISODIC/PROCEDURAL ausentes dos enums;
- no Cognitive Neo4j APIs.

### Real PostgreSQL migration/bootstrap proof

Criar dedicated integration DB, por exemplo:

```text
sofias_memory_cognitive_memory_test
```

e cobrir:

### Fresh

```text
pristine DB
DATABASE_MIGRATION_MODE=auto
startup
0018+ aplicado automaticamente
/health/live continua disponível durante bootstrap
/health/ready só fica ready após schema current
```

Para provar liveness **durante** migration quando `0018` for rápida demais para uma observação determinística, reutilizar/instrumentar o harness sancionado de ADR-0015 para manter o bootstrap controladamente in-flight. Nunca inserir `sleep`, lock artificial ou atraso em uma migration de produção somente para tornar o teste observável.

### Upgrade from v0.6

```text
DB em genuine Alembic 0017
current v0.7 code/image
auto bootstrap
0017 -> 0018+
ready
Cognitive Memory CRUD/recall smoke
legacy data preservada
```

### verify_only

```text
DB em 0017
DATABASE_MIGRATION_MODE=verify_only
nenhuma auto-migration
not-ready até operator migration
```

### Concurrent starters

Contra DB em 0017, provar disciplina ADR-0015 continua válida com a nova migration; não criar migration path alternativo.

### Workflow integration

Adicionar a suite real ao workflow manual `Integration (real PostgreSQL + Neo4j)` ou nome vigente, usando flags opt-in/dedicated DB conforme convenção do projeto.

Mesmo sem Cognitive Neo4j, o workflow existente pode manter Neo4j para regressões legadas; os novos tests não podem depender dele.

## Concurrency / Security / Privacy obligations

### Security gates

Além de tests funcionais:

- auth obrigatório em todos os novos endpoints;
- no secret/content leak;
- invalid UUID/request fails safely;
- no SQL/stack/provider detail in errors;
- Bandit HIGH gate;
- runtime dependency audit conforme tooling oficial;
- HMAC key handling com `SecretStr`;
- `/info` não expõe HMAC configuration value.

## Migration impact

Nenhuma migration de produto nova esperada.

Se hardening revelar defeito estrutural real, uma migration corretiva linear `0019+` é permitida; deve ser testada fresh + 0018/0017 upgrade e documentada. Mudança de contrato exige parar.

## Definition of Done

SM-1005 fecha somente quando:

1. race matrix real passa sem sleeps como synchronization;
2. privacy proof pós-Forget passa por inspeção direta do PostgreSQL;
3. HMAC/non-reversible digest properties passam;
4. migration/bootstrap fresh + v0.6 upgrade + verify_only passam;
5. backward compatibility total v0.6 passa;
6. OpenAPI/contract/security suites passam;
7. manual Integration workflow contém e executa os novos testes;
8. no production defect conhecido permanece aberto;
9. no scope creep para deferred features.

---

# SM-1006 — Release prep, GATE-v0.7.0 and publication

## Objetivo

Preparar, validar e publicar v0.7.0 somente depois que SM-1001..SM-1005 estiverem aprovados e verdes, seguindo a disciplina exact-SHA já estabelecida em v0.5/v0.6.

## Arquivos/domínios prováveis

```text
pyproject.toml
uv.lock
CHANGELOG.md
README.md
docs/api.md
docs/operations.md
docs/deployment/
docs/development.md
docs/product/Sofias_Memory_Feature_Contract_v0.7.0_Native_Cognitive_Memory.md
docs/product/Sofias_Memory_Integration_Contract_Sofias_Assistant_v1.md
docs/exec-plans/active/Sofias_Memory_Technical_Backlog_v0.7.0_Native_Cognitive_Memory.md
.env.example
compose.yaml
deploy/easypanel/compose.yaml
.github/workflows/ci.yml                 # somente se gate oficial exigir ajuste
.github/workflows/integration.yml        # normalmente já wired por SM-1005
.github/workflows/release.yml            # somente se consistência oficial exigir ajuste
```

Nenhum production domain/service novo pertence a este gate; qualquer necessidade desse tipo significa que um gate anterior não foi realmente concluído.

## Invariantes

- nenhuma tag antes de CI + manual Integration verdes na mesma release SHA;
- nenhuma tag é movida após publicação;
- Feature Contract só vira `Implemented` depois de implementation gates comprovados;
- ADR-0016 permanece `accepted`;
- Integration Contract permanece `APPROVED`;
- release gate não introduz funcionalidade nova;
- migration head final é validado, não alterado por conveniência de release;
- Automatic Serialized Migration Bootstrap é o único caminho automático de schema upgrade;
- post-release closeout ocorre depois da tag e não muda o target da tag.

## Dependências

```text
SM-1001 PASS
SM-1002 PASS
SM-1003 PASS
SM-1004 PASS
SM-1005 PASS
```

Nenhuma tag/release é criada antes disso.

## Release-prep scope

- canonical version `0.6.0 -> 0.7.0`;
- `uv.lock`/config/version parity conforme tooling do projeto;
- CHANGELOG `[0.7.0]`;
- README/docs/api/docs/operations/deployment docs necessárias;
- documentação do novo `COGNITIVE_IDEMPOTENCY_HMAC_KEY`;
- Feature Contract status:
  `APPROVED / FROZEN FOR IMPLEMENTATION -> Implemented`
  somente após gates de implementação comprovados;
- ADR-0016 permanece `accepted`;
- Integration Contract permanece `APPROVED`;
- backlog passa a DONE somente após publication/closeout.

## Testes obrigatórios

### GATE-v0.7.0 — local/CI obrigatório

No exact release candidate SHA:

```text
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory scripts
uv run pytest tests/unit tests/contract tests/security
real PostgreSQL integration suites
existing legacy integration suites
runtime pip-audit
Bandit HIGH blocking gate
release consistency check
git diff --check
```

### Cognitive release acceptance

Antes da tag, comprovar:

- `/info` negotiation v1;
- Create/Get;
- exact typed Recall;
- historical current truth;
- Supersession atomic;
- precise destructive Forget;
- keyed idempotency;
- no sensitive residue pós-Forget;
- no Neo4j/outbox Cognitive Memory;
- no PipelineRun Cognitive write;
- no PATCH;
- no EPISODIC/PROCEDURAL;
- no semantic dedupe/importance;
- backward APIs v0.6 intactos.

### Automatic Serialized Migration Bootstrap release validation

A release image deve passar, no exact release SHA:

### Fresh-image smoke

```text
empty PostgreSQL
v0.7 image
DATABASE_MIGRATION_MODE=auto
automatic migration through current head (0018+)
liveness during bootstrap
readiness after schema current
Cognitive Memory smoke
```

Se a migration real concluir rápido demais para observar deterministicamente o estado intermediário, a prova D33/liveness deve vir do harness controlado já aceito para ADR-0015 executado contra a mesma release SHA; a migration de produção não pode ganhar atraso artificial para satisfazer o teste.

### Genuine v0.6 upgrade smoke

Preparar database compatível com release v0.6.0 em `0017`, com fixture legacy representativa.

Subir imagem v0.7.0:

```text
auto migration 0017 -> current v0.7 head
legacy rows preserved
ready
Create/Recall/Forget Cognitive smoke
```

Nenhum `alembic upgrade head` manual no caminho `auto`.

### verify_only smoke

Em schema `0017`, v0.7 `verify_only` não migra automaticamente e permanece not-ready conforme ADR-0015.

### Exact-SHA release discipline

Ordem:

```text
release-prep commit(s)
        ↓
CI green
        ↓
manual Integration green
        ↓
freeze release_sha
        ↓
exact-SHA migration/image smoke green
        ↓
annotated tag v0.7.0 -> release_sha
        ↓
Release workflow green
        ↓
verify GitHub Release + GHCR
        ↓
post-release docs closeout commit
        ↓
closeout CI green
```

Tag nunca é movida para o post-release closeout commit.

### Publication verification

Verificar:

```text
tag v0.7.0 -> exact release SHA
GitHub Release stable / not prerelease
GHCR ghcr.io/kallbuloso/sofias-memory:0.7.0
OCI version = 0.7.0
OCI revision = release SHA
OCI source = repository canonical URL
```

## Migration impact

Nenhuma migration criada neste gate.

Este gate valida todas as revisions v0.7 (`0018+`) por fresh install e upgrade desde genuine `0017`.

## Concurrency / Security / Privacy obligations

Release não pode ser tagueada se qualquer um estiver sem prova:

- idempotency race;
- supersession race;
- forget/supersede race;
- keyed digest security;
- destructive Forget;
- external provenance scrub;
- no sensitive logs;
- no forgotten historical recall;
- migration serialization.

## Definition of Done

SM-1006 só fecha quando:

1. exact release SHA tem CI verde;
2. manual Integration está verde na mesma SHA;
3. release-image fresh + v0.6 upgrade + verify_only smokes passam;
4. version/release consistency = 0.7.0;
5. annotated tag `v0.7.0` aponta exatamente ao release SHA;
6. Release workflow = success;
7. GitHub Release/GHCR/OCI labels verificados;
8. post-release closeout marca:
   - SM-1001..SM-1006 DONE;
   - GATE-v0.7.0 PASSED;
   - v0.7.0 RELEASED;
9. exec plan é movido `active/ -> completed/` conforme convenção;
10. CI do closeout final = success.

---

# 6. Test matrix consolidada da release

A implementação não está completa sem evidência para todos os grupos:

| Área | Unit | Contract/OpenAPI | Real PostgreSQL | Release image |
|---|---:|---:|---:|---:|
| Domain/type/scope/content/provenance validation | sim | sim | sim | smoke |
| Cognitive HMAC/idempotency | sim | sim | sim + races | smoke |
| `/info` negotiation | sim | sim | sim | sim |
| Create/Get | sim | sim | sim | sim |
| Typed Recall / pgvector exact | sim | sim | sim | sim |
| Historical current truth | sim | sim | sim | sim |
| Supersession | sim | sim | sim + races | sim |
| Precise Forget | sim | sim | sim + privacy | sim |
| Backward compatibility v0.6 APIs | regressão | regressão | regressão | upgrade smoke |
| ADR-0015 migration bootstrap | existente + nova | n/a | fresh/upgrade/verify | obrigatório |

---

# 7. Explicit non-goals / forbidden scope

SM-1001..SM-1006 não podem introduzir:

```text
Cognitive Memory em Neo4j
Cognitive graph_outbox
EPISODIC
PROCEDURAL
Skill reinterpretation
semantic dedupe
automatic semantic conflict resolution
importance
Memory PATCH
generic tenancy
generic ACL/RBAC
Memory prefetch
advanced consolidation/decay
Feedback/Improve Cognitive integration
Agent Profile ContextBuilder integration
offline sync
generic plugin architecture
Sofia's Assistant Slice 05 implementation
```

O Integration Contract é um contrato consumidor; nenhum código do Sofia's Assistant pertence a este backlog.

---

# 8. Estratégia de commits/gates

Cada SM é uma unidade significativa e pode produzir um ou poucos commits semanticamente coerentes.

Regras:

- não empilhar seis tasks em um único commit;
- não fragmentar uma mesma invariância em dezenas de microcommits obrigatórios;
- correções descobertas dentro do gate podem ser feitas antes de fechá-lo;
- cada gate só é marcado DONE após sua própria suite + regressão relevante verde;
- CI failure in-scope é corrigido antes do closeout do gate;
- não iniciar o próximo gate com finding conhecido do atual.

Sugestões de commits principais:

```text
SM-1001  feat(memory): add native cognitive memory foundation
SM-1002  feat(api): add cognitive memory create and get
SM-1003  feat(memory): add typed cognitive recall
SM-1004  feat(memory): add supersession and precise forget
SM-1005  test(v0.7): harden cognitive memory integration
SM-1006  chore(release): prepare v0.7.0
```

Closeout documental por gate é opcional conforme precedente; o release final exige closeout pós-publicação.

---

# 9. Gate de aprovação humana deste documento

Este backlog é **design/execution planning only**.

Neste momento estão autorizados somente:

- revisão humana;
- amendments no backlog;
- após aprovação, docs-only freeze commit dos três contratos + backlog.

Ainda não estão autorizados:

```text
código
migration 0018
config changes
API changes
workflow changes
version bump
tag
release
```

A implementação começa somente após aprovação explícita deste Technical Backlog / Exec Plan.

---

# 10. Estado esperado após aprovação, antes de implementação

```text
Feature Contract v0.7.0 = APPROVED / FROZEN FOR IMPLEMENTATION
ADR-0016 = accepted
Integration Contract v1 = APPROVED
Technical Backlog v0.7.0 = APPROVED
implementation = NOT STARTED
first executable gate = SM-1001
```
