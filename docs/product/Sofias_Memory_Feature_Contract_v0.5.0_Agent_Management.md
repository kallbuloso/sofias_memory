# Sofias Memory — Feature Contract v0.5.0: Agent Management

**Status:** Implemented\
**Target release:** v0.5.0\
**Feature:** First-class durable Agent Management

---

## 1. Objetivo

O v0.5.0 introduz **Agent Management first-class e durável** no Sofias Memory.

> An Agent in Sofias Memory is a durable declarative Agent Profile identity and management resource. It is not an executing agent process.

Agent não é:

- agent runtime;
- LLM orchestration engine;
- conversation engine;
- provider/model runtime selector;
- provider session manager;
- tool executor;
- tool authorization engine;
- security principal;
- Dataset;
- Skill;
- Session (mas associa-se explicitamente a ambos).

Sofias Memory armazena, administra, associa e devolve Agent Profiles. O caller — especialmente Sofia's Assistant — decide qual Agent conduz um turno, qual Skill usar, e é responsável por qualquer snapshot de runtime necessário.

PostgreSQL permanece como fonte de verdade.

---

## 2. Princípios

### 2.1 Agent é identidade e perfil durável, não runtime

```text
Agent
    ↓
identidade declarativa administrável

Sofia's Assistant / caller externo
    ↓
loop de execução, seleção de provider/model, tools, conversação
```

### 2.2 Agent Profile é mutável, sem revisioning

Diferente de Skill, não existe `AgentRevision`. Atualizações de perfil são in-place. Ver §10 e §14 para as non-guarantees decorrentes.

### 2.3 Associações são explícitas e não são autorização nem provenance

`Agent ↔ Skill` e `Agent ↔ Session` são junções administrativas explícitas. Nenhuma das duas concede execução, autorização, ou atribuição histórica precisa de operação.

### 2.4 Archive é discovery/availability filter, não admission barrier

Sofias Memory nunca executa um Agent; portanto não pode nem deve afirmar que archive interrompe qualquer execução externa. Archive apenas retira o Agent da listagem default e não bloqueia nenhuma operação de management/associação.

### 2.5 Explicit is better than automatic

Não existem no v0.5.0:

- seleção automática de Agent;
- seleção automática de Skill por um Agent;
- criação automática de Agent;
- AgentRun ou qualquer rastreamento de execução;
- resolve semântico de Agent;
- resolve semântico de Skill escopado por Agent;
- atribuição automática de Query/PipelineRun/SessionEntry a um Agent.

### 2.6 Compatibilidade é obrigatória

`/agents` deixa de ser um prefixo proibido **somente quando SM-802 for implementado** (§16). `/proposals` continua proibido. Nenhuma outra superfície do contrato existente muda de comportamento.

---

# 3. Identidade do Agent

Cada Agent possui exatamente duas identidades, nunca três:

- `id`: UUID PRIMARY KEY interno, gerado pelo sistema. Exposto publicamente como `agent_uuid` — não existe uma coluna `agent_uuid` separada duplicando `id`. Mesmo padrão de `Session.id`/`session_uuid` (ADR-0012) e `Skill.id`/`skill_uuid` (ADR-0013).
- `name`: identificador lógico portable, externo, imutável.

## 3.1 Formato de `name`

```text
1..64 chars
lowercase a-z
digits 0-9
single hyphens between groups

regex: ^[a-z0-9]+(-[a-z0-9]+)*$
```

Inválidos: uppercase, leading hyphen, trailing hyphen, double hyphen, spaces, underscores. `name` é validado, nunca silenciosamente normalizado (nenhum lowercasing automático de uppercase — uppercase é rejeitado).

`name` é:

- globally unique dentro da instância;
- imutável após criação;
- **não** um caller-supplied external key textual (diferente de `Session.session_id`) — Agent é um recurso originado pelo Sofias Memory, não uma correlation key externa preexistente.

Rename não é uma operação suportada. Novo `name` = novo Agent.

Duplicate create:

```text
409 INVALID_REQUEST
```

nunca upsert.

## 3.2 `agent_uuid` (= `id`) vs `name`

```text
agent_uuid  → serialização pública de Agent.id; identidade estrutural, usada em
              toda a API de management/associação (path parameters)
name        → identidade lógica portable, imutável, usada em create/list/get
```

---

# 4. Agent Profile — campos

Campos conceituais:

```text
id            UUID PK, exposto publicamente como agent_uuid
name          (public portable identity, unique, immutable)
display_name  optional, nullable, mutable — human label, não identidade
description   optional, nullable, mutable
instructions  optional, nullable, mutable — texto declarativo opaco
metadata      dict[str, JSONValue], mutable, default {}
status        (active | archived)
created_at
updated_at
archived_at
```

## 4.1 `display_name`

```text
optional
nullable
max 120 Unicode chars
trim de whitespace nas bordas
non-empty quando non-null (após trim)
```

Label humano mutável. Não é identidade.

## 4.2 `description`

```text
optional
nullable
1..1024 Unicode chars quando presente
```

Descrição humana declarativa.

## 4.3 `instructions`

```text
optional
nullable
1..65536 Unicode chars quando presente
deve conter ao menos um caractere não-whitespace
normalização CRLF/CR → LF
sem trim destrutivo de whitespace significativo já persistido
```

Texto declarativo opaco. Sofias Memory:

```text
armazena
devolve
```

mas nunca:

```text
interpreta
injeta em Recall
injeta em Session Context
usa para construir prompts
executa
```

Mesma postura de não-interpretação que ADR-0013 já estabeleceu para `Skill.procedure`.

## 4.4 `metadata`

Tipo público: `dict[str, JSONValue]` — natureza geral de `Session.metadata`, **não** o `map<string, string>` restrito do SKILL.md (§12.3 do Feature Contract v0.4.0 não se aplica aqui).

Default: `{}`.

---

# 5. Campos explicitamente fora do Agent

Nenhuma destas colunas pertence a `agents`:

```text
dataset_id
session_id
tenant_id
user_id
owner_id

model
provider
temperature
max_tokens

provider_session_id
conversation_id

api_key
tool_credentials
tool_permissions
```

Agent nunca é security principal (§13).

---

# 6. Sem AgentRevision

```text
AgentRevision does not exist in v0.5.
```

Agent Profile é mutável in place via `PATCH` (§9). Consequências deliberadas, ver §14 (Historical non-guarantees) para a lista completa.

`AgentRevision` não é introduzida apenas para resolver essa lacuna — seria complexidade por simetria com Skill, sem uma invariante concreta que a justifique (ver ADR-0014 §Alternatives Rejected).

---

# 7. Lifecycle

Estados possíveis de `Agent.status`:

```text
active
archived
```

Archive é um **discovery/availability filter**, não um admission barrier (diferença deliberada em relação a Session archive — ver ADR-0014).

Enquanto `archived`, continuam permitidos exatamente como se `active`:

```text
GET detail
PATCH profile
associate Skill / update pin
remove Skill association
associate Session
remove Session association
read Skills (GET .../skills)
read Sessions (GET .../sessions)
restore
```

Archive não apaga associações. Restore não recria associações — elas nunca foram removidas.

Archive/restore são idempotentes.

## 7.1 Archive

Primeiro archive:

```text
status = archived
archived_at = now
updated_at advances
```

Archive repetido:

```text
no-op
archived_at unchanged
updated_at unchanged
```

## 7.2 Restore

Primeiro restore:

```text
status = active
archived_at = null
updated_at advances
```

Restore repetido:

```text
no-op
updated_at unchanged
```

## 7.3 Sem hard delete público

Não existe endpoint de delete físico de Agent no v0.5.0.

---

# 8. Sofia's Assistant boundary

```text
Sofias Memory owns:
  durable Agent Profile
  semantic memory
  Memory Sessions
  Skills
  Agent<->Skill associations
  Agent<->Session associations

Sofia's Assistant / external caller owns:
  Conversation
  Turn lifecycle
  provider session
  provider/model selection
  temperature/max_tokens/runtime settings
  tool execution
  tool authorization
  deciding which Agent drives a turn
  deciding which Skill is used
  historical runtime snapshotting
```

Nenhuma tabela `conversations` é criada no Sofias Memory.

---

# 9. API — Agent management

Mesmo envelope/erro/paginação das APIs existentes (Dataset/Session/Skill).

## `POST /api/v1/agents`

Request:

```text
name           required
display_name   optional
description    optional
instructions   optional
metadata       optional, default {}
```

Nunca aceita:

```text
status
agent_uuid
created_at
updated_at
archived_at
```

`201` com o Agent criado.

`409 INVALID_REQUEST` se `name` já existir.

## `GET /api/v1/agents`

Paginação:

```text
limit default 50, max 100
offset default 0
```

Filtro `status`: `active` | `archived`.

**Default `status = active`** — decisão consciente, diferente das listagens de management de Dataset/Session/Skill (que retornam todos os status por default). Para Agent, a listagem principal é também a superfície mínima de disponibilidade/discovery do v0.5.0: como Sofias Memory nunca executa um Agent, `GET /agents` sem filtro é o único sinal de "quais Agents estão correntemente disponíveis para uso administrativo." `GET /agents?status=archived` lista os arquivados.

Item de lista — `AgentListItem` (leve, sem `instructions`):

```text
agent_uuid
name
display_name
description
status
created_at
updated_at
archived_at
```

Não há semantic Agent resolve (§17).

## `GET /api/v1/agents/{agent_uuid}`

Full management Agent Profile:

```text
agent_uuid
name
display_name
description
instructions
metadata
status
created_at
updated_at
archived_at
```

Funciona para `active` e `archived`.

`404 INVALID_REQUEST` se não existir.

## `PATCH /api/v1/agents/{agent_uuid}`

Campos mutáveis exclusivamente:

```text
display_name
description
instructions
metadata
```

Nunca:

```text
name
status
agent_uuid
```

`status` muda somente via `archive`/`restore`.

```text
extra = forbid
ao menos um campo obrigatório
```

Explicit null:

```text
display_name = null → clear
description  = null → clear
instructions = null → clear
```

`metadata`:

```text
fornecido → substitui o objeto inteiro (sem deep merge)
metadata = null → 422 INVALID_REQUEST
```

Esta semântica acompanha deliberadamente `SessionUpdateRequest` (`sofias_memory/schemas/sessions.py`): campo omitido não é alterado, `null` explícito limpa campos de texto simples, `metadata` nunca aceita `null` e nunca faz merge parcial.

## `POST /api/v1/agents/{agent_uuid}/archive`

Ver §7.1. Idempotente.

## `POST /api/v1/agents/{agent_uuid}/restore`

Ver §7.2. Idempotente.

---

# 10. PostgreSQL model — `agents`

Schema conceitual:

```text
agents
    id            UUID PK
    name          TEXT NOT NULL UNIQUE
    display_name  TEXT NULL
    description   TEXT NULL
    instructions  TEXT NULL
    metadata      JSONB NOT NULL DEFAULT '{}'
    status        agent_status NOT NULL DEFAULT 'active'
    created_at    TIMESTAMPTZ NOT NULL
    updated_at    TIMESTAMPTZ NOT NULL
    archived_at   TIMESTAMPTZ NULL
```

PostgreSQL deve reforçar pelo menos:

```text
name max length (64)
name portable regex (^[a-z0-9]+(-[a-z0-9]+)*$)
display_name max length (120)
description max length (1024)
instructions max length (65536)
instructions nonblank when non-null
```

A migration nasce em SM-801, não neste documento. Migration planejada: `0015`, revises `0014` (a migration `0014` já pertence a Skills; ADR-0014 não tem relação numérica com ela — ver §16.3).

---

# 11. Agent ↔ Skill

## 11.1 Conceito

```text
Agent
  ↕ associação explícita, com pin opcional
Skill
```

Não altera:

```text
Skill.id
Skill.name
SkillRevision
revision numbering
Skill.current_revision_id
Skill content immutability
```

Não adiciona `agent_id` a `skills` ou `skill_revisions`.

## 11.2 Schema

```text
agent_skills
    agent_id             UUID NOT NULL
    skill_id             UUID NOT NULL
    pinned_revision_id   UUID NULL
    created_at           TIMESTAMPTZ NOT NULL

    PRIMARY KEY (agent_id, skill_id)
```

FKs:

```text
agent_id
    → agents.id
    ON DELETE CASCADE

skill_id
    → skills.id
    ON DELETE RESTRICT

(skill_id, pinned_revision_id)
    → skill_revisions(skill_id, id)
    ON DELETE RESTRICT
```

Reutiliza a candidate key já existente `skill_revisions(skill_id, id)` (criada por ADR-0013 para o FK deferrable de `Skill.current_revision_id`). Nenhuma FK independente é criada apenas sobre `pinned_revision_id`.

O composite FK garante, no nível do banco:

- **cross-Skill pin é estruturalmente impossível** — a linha nunca pode referenciar uma `SkillRevision` de outra Skill;
- **`RESTRICT`, nunca `SET NULL`** — deletar uma `SkillRevision` correntemente pinada é rejeitado; nunca converte silenciosamente `pinned` em `follow-current`.

Migration planejada: `0016`, revises `0015`.

## 11.3 Pin semantics (banco)

```text
pinned_revision_id IS NULL
    → follow Skill.current_revision_id (live)

pinned_revision_id IS NOT NULL
    → use exactly that SkillRevision
```

## 11.4 Public pin identity

`SkillRevision.id` continua interno e nunca aparece na API (mesmo princípio de §3.3 do Feature Contract v0.4.0). O payload público de Agent↔Skill usa `pinned_revision` como **integer revision number**:

```json
{ "pinned_revision": 3 }
```

Follow-current:

```json
{ "pinned_revision": null }
```

O servidor resolve `(skill_uuid, revision number) → SkillRevision.id` internamente. `pinned_revision_id` (UUID) nunca é recebido nem devolvido publicamente.

## 11.5 API

```text
GET    /api/v1/agents/{agent_uuid}/skills
PUT    /api/v1/agents/{agent_uuid}/skills/{skill_uuid}
DELETE /api/v1/agents/{agent_uuid}/skills/{skill_uuid}
```

Não é authorization. Não executa Skill. Não chama embedding provider.

### `PUT /api/v1/agents/{agent_uuid}/skills/{skill_uuid}`

Payload:

```text
pinned_revision: int | null
```

`pinned_revision` omitido é **equivalente a `null`** (decisão normativa: omitido = null = follow current).

Validação:

```text
revision >= 1 quando integer
```

- Pinned revision inexistente na Skill alvo → `422 INVALID_REQUEST`.
- Cross-Skill revision → `422 INVALID_REQUEST` na API, com defesa adicional do composite FK no PostgreSQL.
- Agent inexistente → `404 INVALID_REQUEST`.
- Skill inexistente → `404 INVALID_REQUEST`.
- Idempotent set/upsert: `200`, nunca cria duplicata, nunca `409` por associação já existente. Repetir o mesmo estado → `200`, sem mudança observável; pin diferente → `200`, associação atualizada.

### `GET /api/v1/agents/{agent_uuid}/skills`

Management/disclosure list, **não** semantic resolve. Inclui Skills `archived` associadas.

Shape mínimo por item:

```text
skill_uuid
name
status

current_revision
pinned_revision
effective_revision

description
tags
declared_tools
compatibility

association_created_at
```

Nunca inclui:

```text
procedure
resolution_embedding
internal revision id (SkillRevision.id)
content_sha256
```

`effective_revision`:

```text
pinned_revision != null   → effective_revision = pinned_revision
pinned_revision == null   → effective_revision = current_revision
```

`description`/`tags`/`declared_tools`/`compatibility` retornados no item são os da `effective_revision`. Isso permite ao caller avaliar as Skills associadas sem carregamento automático de `procedure`.

Ainda não existe `POST /agents/{uuid}/skills/resolve` (§17).

### `DELETE /api/v1/agents/{agent_uuid}/skills/{skill_uuid}`

Idempotente.

- Agent inexistente → `404 INVALID_REQUEST`.
- Agent existente + associação inexistente → `204`.
- Associação existente → `204`.

Nenhuma operação de associação modifica `Skill`/`SkillRevision`.

## 11.6 Interação de lifecycle

Skill `archived`:

```text
association remains
GET Agent Skills continua retornando o item
status = archived
pin permanece
effective_revision permanece resolvível para management
```

Skill `restore`: a mesma associação permanece — nunca foi removida.

Agent `archived`: mesmo comportamento — gestão de associação permanece permitida.

---

# 12. Agent ↔ Session

## 12.1 Conceito — congelado como associação, não provenance

```text
current explicit management association
```

**NÃO**:

```text
historical provenance
audit event
turn attribution
Query attribution
PipelineRun attribution
```

Cardinalidade: `Agent M:N Session`. A mesma Session pode possuir vários Agents associados ao mesmo tempo. O mesmo Agent pode possuir várias Sessions.

## 12.2 Schema

```text
agent_sessions
    agent_id      UUID NOT NULL
    session_id    UUID NOT NULL
    created_at    TIMESTAMPTZ NOT NULL

    PRIMARY KEY (agent_id, session_id)
```

FKs:

```text
agent_id
    → agents.id
    ON DELETE CASCADE

session_id
    → sessions.id
    ON DELETE CASCADE
```

Migration planejada: `0017`, revises `0016`.

`created_at` significa **exclusivamente**:

```text
when the CURRENT association was created
```

Não significa:

```text
first-ever participation
historical participation
provenance event
```

`DELETE` remove essa current association fact — permitido e idempotente precisamente porque esta tabela é association state, não historical audit. Re-associação futura cria um novo `created_at`.

## 12.3 Sem provenance por operação — invariante crítico

```text
Query.agent_id does not exist.
PipelineRun.agent_id does not exist.
SessionEntry.agent_id does not exist.
```

v0.5.0 não persiste:

```text
per-Query Agent attribution
per-PipelineRun Agent attribution
per-SessionEntry Agent attribution
per-turn Agent attribution
```

**Nunca afirmar** que `Query -> Session -> agent_sessions` identifica o Agent que originou a operação. Se uma Session tiver Agents A e B associados, não é possível inferir qual dos dois originou uma Query ou PipelineRun específico a partir dos dados persistidos. Esta é uma non-guarantee deliberada do v0.5.0 (ver §14), não uma lacuna de implementação.

## 12.4 API

```text
GET    /api/v1/agents/{agent_uuid}/sessions
PUT    /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
DELETE /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
```

Sem endpoint inverso Session→Agents no v0.5.0. Pode ser adicionado futuramente sem redesign de schema, se houver necessidade concreta.

### `PUT /api/v1/agents/{agent_uuid}/sessions/{session_uuid}`

Idempotent ensure-association. Sem payload de runtime/provenance (corpo vazio ou ausente).

- Agent inexistente → `404 INVALID_REQUEST`.
- Session inexistente → `404 INVALID_REQUEST`.
- Create/replay → `200`, nunca duplica row.

Associar a uma Session `archived` continua permitido — é management metadata, não nova atividade contextual de Session (não é bloqueado pelo admission barrier de ADR-0012, porque não é `SessionEntry`/Recall/Remember). Nunca cria `SessionEntry`, `Query`, ou `PipelineRun`.

### `GET /api/v1/agents/{agent_uuid}/sessions`

Management list. Retorna pelo menos:

```text
session_uuid
session_id
name
status
association_created_at
```

Não retorna transcript. Não retorna `SessionEntries` implicitamente.

### `DELETE /api/v1/agents/{agent_uuid}/sessions/{session_uuid}`

Idempotente.

- Agent inexistente → `404 INVALID_REQUEST`.
- Agent existente + associação inexistente → `204`.
- Associação existente → `204`.

## 12.5 Interação de lifecycle

Session `archived`:

```text
existing association remains
GET Agent Sessions continua retornando o item
association create/remove permanece management-allowed
```

Nunca interpretar isso como bypass do Session admission barrier (ADR-0012) — `SessionEntry`/Recall/Remember continuam obedecendo ADR-0012 normalmente. Associação de Agent não altera a semântica de admission de Session.

Agent `archived`: mesmo comportamento — gestão de associação permanece permitida.

---

# 13. Security boundary

Sofias Memory mantém exatamente:

```text
X-API-Key static application key (ADR-0003)
```

Agent não é:

```text
user
tenant
role
security principal
credential holder
```

Nenhum per-Agent credential. Nenhum permission/ACL. `Agent ↔ Skill` nunca autoriza `Skill.declared_tools` — o campo permanece descritivo, inalterado por associação com qualquer Agent.

---

# 14. Historical non-guarantees

Seção explícita, para que futuros tickets não tratem a ausência destas garantias como bug:

```text
no AgentRevision
no historical Agent Profile snapshots
no historical Agent<->Session event ledger
no exact per-operation Agent attribution (Query/PipelineRun/SessionEntry)
no runtime/provider snapshot
```

Detalhado:

- **Agent Profile updates são mutáveis in place.** Não há versionamento histórico de `display_name`/`description`/`instructions`/`metadata`.
- **`Agent ↔ Session` associa apenas identidade do Agent.** Nunca captura um snapshot do Agent Profile no momento da associação.
- **Sofias Memory não pode reconstruir** quais valores históricos de `display_name`/`description`/`instructions`/`metadata` estavam ativos durante uma Session, Query, PipelineRun, ou turno de conversação externa passados.
- **Reprodutibilidade histórica de Agent Profile/runtime não é uma garantia do v0.5.0.**
- **`agent_sessions` não é um ledger de eventos** — apenas o estado de associação corrente, com `created_at` da associação corrente.
- **Nenhuma atribuição exata por operação** (`Query`/`PipelineRun`/`SessionEntry`) é persistida.

O caller externo (Sofia's Assistant) é responsável por qualquer snapshot necessário para reproduzir comportamento histórico.

---

# 15. PostgreSQL / Neo4j / graph_outbox

`agents`, `agent_skills`, `agent_sessions`:

```text
PostgreSQL authoritative
```

Nunca:

```text
graph_outbox event
Neo4j node
Neo4j relationship
Neo4j property projection
```

Nenhum `(:Agent)`, `(:AgentProfile)`, ou `(:AgentSkill)` é criado. Neo4j permanece projeção reconstruível de semantic knowledge — Agent é estado operacional/de identidade, não conhecimento semântico de grafo (mesmo raciocínio de ADR-0012 e ADR-0013).

---

# 16. Forget / Dataset Delete

Forget (`source`, `dataset`, `everything`) nunca remove:

```text
Agent
AgentSkill (agent_skills)
AgentSession (agent_sessions)
```

Dataset Delete idem.

Agent não possui `dataset_id`. Deletar memória semântica não altera Agent Profile ou suas associações.

---

# 17. Semantic resolve — exclusões

v0.5.0 não adiciona:

```text
Agent embeddings
POST /agents/resolve
POST /agents/{uuid}/skills/resolve
```

`GET /agents/{uuid}/skills` é uma management/disclosure list, não resolve semântico. O caller externo decide se e como escolher uma Skill dentre as associadas.

---

# 18. Explicitly forbidden Agent routes

Nunca criados nesta release:

```text
/agents/{uuid}/run
/agents/{uuid}/execute
/agents/{uuid}/chat
/agents/{uuid}/respond
/agents/{uuid}/complete
/agents/{uuid}/invoke
/agents/{uuid}/tools

/agents/resolve
/agents/{uuid}/skills/resolve

/proposals
```

Nenhum alias/synonym endpoint que esconda semântica de execução é criado.

---

# 19. Concurrency contract

## 19.1 Duplicate Agent create

Múltiplas requests concorrentes com o mesmo `name`:

```text
exactly one create succeeds
outras → 409
0 raw IntegrityError/500 vazando ao caller
```

## 19.2 `PATCH` vs `archive`/`restore`

Por Agent:

```text
single Agent row FOR UPDATE
linearizable
no lost update
```

Sem lock global.

## 19.3 Duplicate `agent_skills` association

Converge para uma única row (PK `(agent_id, skill_id)`).

## 19.4 Concurrent pin updates

O último `PUT` serializado determina o pin corrente. Nunca cria row duplicada.

## 19.5 `agent_sessions` association

Unicidade de PK resolve ensures concorrentes. Sem lock global.

## 19.6 Agents diferentes

Mutações no Agent A nunca serializam desnecessariamente o Agent B.

---

# 20. Public surface total

```text
Agent management     6 operações
Agent<->Skill         3 operações
Agent<->Session       3 operações
---------------------------------
total                12 operações
```

Nenhum endpoint além destes deve ser implementado por SM-801..SM-806 salvo amendment explícito deste Feature Contract.

---

# 21. Legacy compatibility

## 21.1 Rotas proibidas

O contrato atual (`AGENTS.md` §12, `CLAUDE.md`, PRD §11.3) proíbe `/agents`. ADR-0014 autoriza a remoção **futura** e **estreita** de `/agents` — não desta task de definição normativa, mas de SM-802.

Até SM-802 ser implementado, `/agents` **continua corretamente presente** no forbidden-route contract e em `AGENTS.md`/`CLAUDE.md`. Este documento não altera o estado atual do teste de contrato nem de `AGENTS.md`/`CLAUDE.md`.

`/proposals` continua proibido, sem alteração, antes e depois de SM-802.

## 21.2 `AGENTS.md` / `CLAUDE.md`

Amendment mínimo esperado (executado em SM-802, não aqui):

```text
Agent Profile management/associations = allowed
agent runtime / tool execution         = still forbidden
```

O amendment deve tocar `AGENTS.md` e `CLAUDE.md` juntos, mesma disciplina que SM-702 já aplicou para `/skills`.

## 21.3 PRD original

`docs/product/Sofias_Memory_PRD_SPECS.md` continua descrevendo o baseline original, sem reescrita. ADR-0014 é o amendment específico de Agent Management, assim como ADR-0013 foi o amendment específico de Skills.

## 21.4 Numeração — migration vs ADR

A migration `0014` já pertence a Skills (`0014_create_skills_foundation.py`) e não tem relação com ADR-0014. As migrations planejadas para v0.5.0 são `0015` (SM-801, `agents`), `0016` (SM-803, `agent_skills`), `0017` (SM-804, `agent_sessions`) — numeração de Alembic revision, independente da numeração de ADR.

---

# 22. Out of scope — v0.5.0

Explicitamente fora deste release:

```text
AgentRevision
agent runtime
LLM orchestration
conversation runtime
provider/model runtime selection
provider session management
tool execution
tool authorization
tool credentials
AgentRun
SkillRun
proposals
self-improvement/evaluation loop
automatic learning
Agent semantic resolve
Agent-scoped Skill semantic resolve
per-operation Agent provenance (Query/PipelineRun/SessionEntry)
Session→Agents inverse listing endpoint
Conversation storage
Neo4j projection de Agent
multi-agent orchestration
```

---

# 23. Invariants obrigatórios

1. PostgreSQL é authoritative para `agents`, `agent_skills` e `agent_sessions`.
2. Agent nunca pertence a Dataset; nenhuma das três tabelas possui `dataset_id`.
3. `name` é globally unique, imutável, e segue o subset portable (`^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 chars).
4. `agent_uuid` é a serialização pública de `Agent.id` — não existe coluna `agent_uuid` separada.
5. Rename de Agent não é suportado; novo `name` é um novo Agent.
6. Não existe `AgentRevision`; Agent Profile é mutado in place via `PATCH`.
7. `PATCH /agents/{uuid}` nunca aceita `name` ou `status`.
8. `instructions`, `description`, `metadata` são armazenados opacos; Sofias Memory nunca os interpreta, injeta em Recall/Session Context, ou usa para construir prompts.
9. Nenhum campo `model`/`provider`/`temperature`/`max_tokens`/`provider_session_id`/tool-credential existe em `agents`.
10. Agent archive é discovery/availability filter, não admission barrier: toda operação de management/associação permanece disponível em um Agent `archived`.
11. Archive/restore de Agent são idempotentes; archive/restore repetidos não alteram `archived_at`/`updated_at`.
12. Não existe hard delete público de Agent.
13. `agent_skills` associa exatamente um Agent a uma Skill via PK composta `(agent_id, skill_id)`; `PUT` é idempotente (upsert), nunca `409` por associação já existente.
14. `agent_skills.pinned_revision_id` é nullable; `NULL` significa "segue `Skill.current_revision_id` ao vivo"; um valor significa "usa exatamente aquela `SkillRevision`" até ser explicitamente alterado.
15. `agent_skills` garante, via FK composta `(skill_id, pinned_revision_id) → skill_revisions(skill_id, id)`, que uma revisão pinada sempre pertence à mesma Skill da linha de associação — cross-Skill pinning é estruturalmente impossível.
16. Deletar uma `SkillRevision` correntemente pinada por qualquer linha de `agent_skills` é rejeitado (`ON DELETE RESTRICT`) — uma associação pinada nunca é silenciosamente convertida em "follow current".
17. `pinned_revision` é sempre um integer (revision number) na API pública; `SkillRevision.id` (UUID) nunca é recebido ou devolvido publicamente.
18. Remover uma associação `agent_skills`/`agent_sessions` é idempotente; remover uma associação inexistente não é erro.
19. Skill `archived` nunca faz uma associação `agent_skills` desaparecer de `GET /agents/{uuid}/skills`.
20. Agent `archived` nunca remove associações `agent_skills`/`agent_sessions`.
21. Session `archived` nunca remove associações `agent_sessions`.
22. `agent_sessions` é current explicit management association state, não historical provenance/audit — `DELETE` é permitido e idempotente precisamente por isso.
23. `agent_sessions.created_at` significa exclusivamente "quando a associação corrente foi criada", nunca "primeira participação histórica".
24. `Query.agent_id`, `PipelineRun.agent_id` e `SessionEntry.agent_id` não existem em v0.5.0. Nenhuma das três tabelas ganha coluna relacionada a Agent.
25. v0.5.0 não persiste atribuição exata por Query, por PipelineRun, por SessionEntry, ou por turno a um Agent específico. Quando uma Session possui mais de um Agent associado, nenhuma inferência de qual Agent originou uma operação específica é possível a partir dos dados persistidos.
26. Sofias Memory não pode reconstruir valores históricos de Agent Profile ativos durante uma Session/Query/PipelineRun passados; reprodutibilidade histórica de runtime não é garantia do v0.5.0.
27. Associações `agent_skills`/`agent_sessions` nunca são autorização; `declared_tools` de uma Skill associada permanece metadata descritiva, inalterada pela associação.
28. Nenhum evento `graph_outbox` é criado para Agent ou suas associações; nenhum label/relationship type Neo4j é criado para Agent.
29. Forget (qualquer escopo) nunca remove `agents`, `agent_skills` ou `agent_sessions`.
30. Dataset Delete nunca remove `agents`, `agent_skills` ou `agent_sessions`.
31. Não existe semantic/embedding-based Agent resolve em v0.5.0.
32. Não existe Agent-scoped Skill semantic resolve (`POST /agents/{uuid}/skills/resolve`) em v0.5.0; `GET /agents/{uuid}/skills` é uma lista de management simples.
33. `/agents` é removido do forbidden-route contract somente quando SM-802 for implementado; até lá permanece proibido.
34. `/proposals` permanece proibido, sem alteração, antes e depois de SM-802.
35. Nenhum endpoint `/agents/.../run|execute|chat|respond|complete|invoke|tools` é criado.
36. Criação de Agent com `name` duplicado é sempre `409`, nunca upsert silencioso.
37. Não existe `AgentProposal`, `SkillProposal`, mutação automática de Agent Profile, ou loop de aprendizado/avaliação em v0.5.0.
38. Escopo de lock para operações de Agent é limitado a `FOR UPDATE` de uma única linha em `agents` (PATCH/archive/restore); nenhum lock cross-table ou global é introduzido.

---

# 24. Critério de conclusão do v0.5.0

A feature é considerada concluída quando:

- `agents`, `agent_skills` e `agent_sessions` possuem persistência PostgreSQL e lifecycle aprovado, com a identidade única `id`/`agent_uuid` (§3) comprovada em schema/testes;
- a API de management (6 operações, §9) funciona com paginação e contratos estáveis, incluindo o default `status=active` de `GET /agents` (§9);
- criação de Agent é concurrency-safe (§19.1), incluindo zero `IntegrityError`/500 vazado ao caller;
- `PATCH`/archive/restore são concurrency-safe (§19.2) e idempotentes (§7.1, §7.2), comprovado por teste real PostgreSQL;
- `Agent ↔ Skill` (3 operações, §11.5) funciona com pin opcional, `effective_revision` correto, e a matriz de concorrência de §19.3–§19.4 comprovada por teste real PostgreSQL;
- o composite FK de `agent_skills` comprovadamente rejeita cross-Skill pinning e rejeita deleção de revisão pinada, por teste de integração real (não apenas verificação de constraint isolada);
- `Agent ↔ Session` (3 operações, §12.4) funciona com cardinalidade M:N comprovada (mesma Session/dois Agents; mesmo Agent/duas Sessions) e a matriz de concorrência de §19.5 comprovada;
- um teste explícito comprova que uma Session com dois Agents associados não permite nenhuma atribuição de Agent específico a uma Query/PipelineRun individual (§12.3, §23 invariant 25);
- Forget/Dataset Delete/Neo4j compatibility está comprovada por teste, incluindo prova de zero label/relationship type Neo4j para Agent;
- `/agents` é removido do forbidden-route contract sem reabrir `/proposals`;
- documentação (`AGENTS.md`, `CLAUDE.md`, `docs/api.md`, README) reflete a nova superfície, sincronizada entre `AGENTS.md` e `CLAUDE.md`;
- suite completa (unit/integration/contract/security) está verde;
- smoke real end-to-end está verde;
- nenhum item de §22 foi implementado antecipadamente.
