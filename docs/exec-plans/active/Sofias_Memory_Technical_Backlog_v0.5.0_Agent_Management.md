# Sofias Memory — Backlog Técnico Executável v0.5.0 Agent Management

**Release:** v0.5.0\
**Feature:** First-class durable Agent Management\
**Status:** Proposed\
**Sequência:** SM-801..SM-806\
**Regra de execução:** executar uma task por vez; não antecipar dependências ou escopo de tickets posteriores.

## 1. Objetivo

O v0.5.0 introduz Agent Management como identidade e perfil administrável durável, complementando a memória semântica (Datasets/Documents), o contexto temporal (Sessions, ADR-0012) e a memória procedural (Skills, ADR-0013) já existentes.

Ao final deste backlog, Sofias Memory deverá possuir:

- `Agent` persistente em PostgreSQL, com identidade `id`/`agent_uuid` + `name` imutável, mutável in place (sem `AgentRevision`);
- lifecycle `active <-> archived`, discovery/availability filter (não admission barrier);
- API de management com paginação (6 operações);
- `agent_skills`: associação explícita Agent↔Skill com pin opcional, endereçado publicamente por `pinned_revision` (integer), nunca por UUID interno;
- `agent_sessions`: associação explícita Agent↔Session, M:N, congelada como current management association — nunca provenance;
- `/agents` removido do forbidden-route contract, com `/proposals` preservado;
- preservação de compatibilidade com Datasets, Sessions, Skills, Forget, Dataset Delete e Neo4j.

Este backlog não implementa `AgentRevision`, agent runtime, tool execution, tool authorization, semantic Agent resolve, Agent-scoped Skill resolve, ou qualquer atribuição exata por operação (Query/PipelineRun/SessionEntry) a um Agent.

---

# 2. Fontes normativas

Ordem de precedência específica deste release:

1. instrução explícita da task em execução;
2. `AGENTS.md`;
3. ADR-0014 — First-Class Durable Agent Management;
4. Feature Contract v0.5.0 — Agent Management;
5. ADRs anteriores aplicáveis, especialmente ADR-0002, ADR-0003, ADR-0007, ADR-0012 e ADR-0013;
6. contratos e testes existentes;
7. PRD original, exceto onde explicitamente amended pelo ADR-0014 (somente a exclusão narrow de `/agents`; `/proposals` permanece no baseline original).

O Feature Contract define a semântica pública detalhada. ADR-0014 define a mudança arquitetural e suas fronteiras. Este backlog define somente a ordem executável de implementação e os gates.

---

# 3. Invariantes de release

Durante SM-801..SM-806:

- PostgreSQL continua authoritative;
- nenhum Agent, AgentSkill ou AgentSession é projetado para Neo4j;
- Agent nunca é authorization boundary;
- Agent nunca pertence a Dataset;
- `name` permanece textual, portable-subset, único e imutável;
- `agent_uuid` é a identidade estrutural UUID, sem coluna duplicada;
- não existe `AgentRevision` — Agent Profile é mutável in place;
- `agent_skills.pinned_revision_id` é sempre validado por FK composta contra `skill_revisions(skill_id, id)` — cross-Skill pinning é estruturalmente impossível;
- `pinned_revision` é sempre integer na API pública; `SkillRevision.id` nunca é exposto;
- `agent_sessions` é current explicit management association — nunca historical provenance; `Query.agent_id`/`PipelineRun.agent_id`/`SessionEntry.agent_id` nunca existem;
- Forget e Dataset Delete nunca removem Agent/AgentSkill/AgentSession;
- não introduzir AgentRun, tool executor, semantic Agent resolve, Agent-scoped Skill resolve, Redis, filesystem watcher, plugin system ou MCP.

---

# 4. Sequência

| Ticket | Entrega principal | Depende de | Migration | Status |
|---|---|---|---|---|
| SM-801 | Agent persistence/domain foundation | — | 0015 | Proposed |
| SM-802 | Agent management API | SM-801 | — | Proposed |
| SM-803 | Agent ↔ Skill association | SM-802 | 0016 | Proposed |
| SM-804 | Agent ↔ Session explicit association | SM-802 | 0017 | Proposed |
| SM-805 | Lifecycle, concurrency e cross-feature hardening | SM-802, SM-803, SM-804 | — | Proposed |
| SM-806 | Docs, smoke e release gate v0.5.0 | SM-805 | — | Proposed |

---

# SM-801 — Agent persistence/domain foundation

## Objetivo

Criar a fundação persistente e de domínio de first-class Agent Management sem ainda expor a API pública.

## Escopo

Implementar:

### Agent

- migration `0015` (revises `0014`) criando `agents` — uma única coluna UUID de identidade (`id`, PK; nunca uma segunda coluna `agent_uuid` duplicando-a — Feature Contract §3);
- constraint de unicidade em `name`, `CHECK` de comprimento (`<=64`) e `CHECK` de charset portable (`^[a-z0-9]+(-[a-z0-9]+)*$`), mesma disciplina de `skills.name`;
- `display_name` (`TEXT NULL`, `CHECK` `<=120`), `description` (`TEXT NULL`, `CHECK` `<=1024`), `instructions` (`TEXT NULL`, `CHECK` `<=65536` e nonblank quando non-null), `metadata` (`JSONB NOT NULL DEFAULT '{}'`);
- enum/coluna `status` (`active`/`archived`), `created_at`/`updated_at`/`archived_at`;
- índices necessários para list/paginação e para o lookup de `name`.

### Domínio

- função única e compartilhada de normalização/validação de `name`, análoga em disciplina a `normalize_session_id`/à validação de `Skill.name`, usada por create (SM-802) — sem normalização silenciosa de uppercase, apenas rejeição;
- validação de `display_name` (trim, non-empty when non-null, `<=120`), `description` (`1..1024`), `instructions` (CRLF/CR→LF, nonblank, `<=65536`, sem trim destrutivo de whitespace significativo persistido);
- repository para `agents`, sem SQL espalhado por routes/pipelines.

## Não fazer

- não expor nenhuma rota pública nesta task;
- não implementar `agent_skills`/`agent_sessions` nesta task (SM-803/SM-804);
- não tocar `graph_outbox`, Neo4j, Recall, Session, Skill ou Remember/Cognify;
- não implementar `PATCH`/archive/restore como comportamento observável via API (a lógica de domínio pode existir, mas sem rota).

## Gate SM-801

A task só encerra quando:

- migration `0015` upgrade/downgrade é válida (`0014 → 0015` e volta);
- schema guards refletem conscientemente a nova tabela/colunas/constraints, incluindo a ausência de uma segunda coluna de identidade UUID duplicada;
- uniqueness de `name` é comprovada por teste, incluindo concorrência de criação (exatamente um sucesso, demais falham por unique violation);
- charset portable e limite de tamanho de `name` são comprovados por teste de boundary (uppercase, leading/trailing hyphen, `--`, tamanho exato aceito, tamanho+1 rejeitado);
- limites de `display_name`/`description`/`instructions` são comprovados por teste de boundary, incluindo `instructions` nonblank-when-non-null e normalização CRLF/CR→LF;
- nenhuma tabela ou coluna nova introduz `dataset_id`/`session_id`/`tenant_id`/`user_id`/`owner_id`/`model`/`provider`/`temperature`/`max_tokens`/`provider_session_id`/`api_key`/`tool_credentials`;
- suite existente permanece verde após atualização deliberada dos schema tests.

---

# SM-802 — Agent management API

## Objetivo

Expor a API pública de management sobre a fundação do SM-801, incluindo criação/leitura/paginação/`PATCH`/archive/restore, e realizar o amendment estreito de `/agents` autorizado por ADR-0014.

## Endpoints

Implementar:

```text
POST   /api/v1/agents
GET    /api/v1/agents
GET    /api/v1/agents/{agent_uuid}
PATCH  /api/v1/agents/{agent_uuid}
POST   /api/v1/agents/{agent_uuid}/archive
POST   /api/v1/agents/{agent_uuid}/restore
```

## Create

`POST /api/v1/agents` cria o Agent (payload `name` required, `display_name`/`description`/`instructions`/`metadata` opcionais). `409`/`INVALID_REQUEST` se `name` já existir em qualquer status. Nunca aceita `status`/`agent_uuid`/`created_at`/`updated_at`/`archived_at` (Feature Contract §9).

## List

`GET /api/v1/agents` pagina (`limit` default 50/max 100, `offset` default 0), filtra por `status`. **Default `status=active`** (Feature Contract §9 — decisão consciente, diferente de Dataset/Session/Skill). Item de lista é `AgentListItem` leve (sem `instructions`).

## Detail

`GET /api/v1/agents/{agent_uuid}` devolve o Agent Profile completo (incluindo `instructions`), para `active` e `archived`.

## PATCH

Aceita estritamente `display_name`/`description`/`instructions`/`metadata`. Rejeita `name`/`status`/`agent_uuid` (`extra=forbid`). Exige ao menos um campo. `null` explícito limpa `display_name`/`description`/`instructions`. `metadata` substitui o objeto inteiro (sem deep merge); `metadata=null` é `422`/`INVALID_REQUEST`. Esta semântica replica deliberadamente `SessionUpdateRequest` (`sofias_memory/schemas/sessions.py`).

## Archive/restore

Discovery/availability filter, não admission barrier — Feature Contract §7/§11: toda operação de management permanece disponível em um Agent `archived`. Primeiro archive define `archived_at`/avança `updated_at`; archive repetido é no-op exato (`archived_at`/`updated_at` inalterados). Simétrico para restore. Idempotentes.

## Amendment de rotas proibidas

Atualizar `tests/contract/test_openapi_forbidden_routes.py` para remover `/agents` de `FORBIDDEN_PATH_PREFIXES`, preservando `/proposals`. Atualizar `AGENTS.md` §12 (rotas proibidas) e §6 (repository tree, adicionar `routes/agents.py`) **e o `CLAUDE.md` correspondente na raiz** (mesma disciplina de sincronização que SM-702 já aplicou para `/skills`), com o amendment mínimo já congelado no Feature Contract §21.2.

## Não fazer

- não implementar `agent_skills`/`agent_sessions` nesta task (SM-803/SM-804);
- não implementar semantic Agent resolve;
- não implementar hard delete.

## Gate SM-802

A task só encerra quando:

- os 6 endpoints acima estão implementados com envelopes/paginação/erros no padrão existente;
- `name`/`display_name`/`description`/`instructions` respeitam os limites congelados no Feature Contract §4, comprovado por teste de boundary;
- criação concorrente com `name` colidente resulta em exatamente um sucesso e um `409`, comprovado por teste real PostgreSQL, sem `IntegrityError`/500 vazado ao caller;
- `PATCH` rejeita `name`/`status`, exige ao menos um campo, aplica `null`-clears para campos de texto simples e rejeita `metadata=null`, comprovado por teste;
- archive/restore são comprovadamente idempotentes byte-a-byte em `archived_at`/`updated_at` (repetição não avança nenhum dos dois), comprovado por teste;
- toda operação de management (`GET`, `PATCH`) funciona sobre um Agent `archived`, comprovado por teste;
- `GET /agents` sem filtro devolve apenas `active` por default, comprovado por teste; `GET /agents?status=archived` devolve os arquivados;
- `/agents` sai do forbidden-route contract sem reabrir `/proposals`, comprovado pelo teste de contrato atualizado;
- `AGENTS.md` e `CLAUDE.md` estão sincronizados quanto a `/agents`, comprovado por revisão manual do diff;
- OpenAPI gerado documenta os 6 endpoints e os shapes corretos (`AgentListItem` sem `instructions`, detail completo);
- suite existente permanece verde.

---

# SM-803 — Agent ↔ Skill association

## Objetivo

Implementar a associação explícita Agent↔Skill com pin opcional, sobre a fundação do SM-801/SM-802 e sobre Skills já existente (v0.4.0). Toda a semântica já está congelada pelo Feature Contract §11 — esta task é implementação, não decisão de produto.

## Escopo

Migration `0016` (revises `0015`) criando `agent_skills`:

```text
agent_skills
    agent_id              UUID NOT NULL
    skill_id               UUID NOT NULL
    pinned_revision_id     UUID NULL
    created_at             TIMESTAMPTZ NOT NULL

    PRIMARY KEY (agent_id, skill_id)
```

FKs (já congeladas, Feature Contract §11.2 — não redecidir):

```text
agent_id  → agents.id  ON DELETE CASCADE
skill_id  → skills.id  ON DELETE RESTRICT
(skill_id, pinned_revision_id) → skill_revisions(skill_id, id)  ON DELETE RESTRICT
```

Reutilizar a candidate key `skill_revisions(skill_id, id)` já criada por SM-701/ADR-0013 para o FK deferrable de `Skill.current_revision_id`. Nenhuma FK independente sobre `pinned_revision_id` isolado.

Endpoints:

```text
GET    /api/v1/agents/{agent_uuid}/skills
PUT    /api/v1/agents/{agent_uuid}/skills/{skill_uuid}
DELETE /api/v1/agents/{agent_uuid}/skills/{skill_uuid}
```

## PUT

Payload `pinned_revision: int | null`; omitido é equivalente a `null` (follow current). `revision >= 1` quando integer. Pinned revision inexistente na Skill alvo → `422`. Cross-Skill revision → `422` na API (defendido também pelo composite FK). Agent/Skill inexistentes → `404`. Idempotente: `200` sempre, nunca `409` por associação já existente, nunca duplica row.

## GET

Management/disclosure list — inclui Skills `archived` associadas. Shape mínimo: `skill_uuid`/`name`/`status`/`current_revision`/`pinned_revision`/`effective_revision`/`description`/`tags`/`declared_tools`/`compatibility`/`association_created_at`. Nunca `procedure`/`resolution_embedding`/`SkillRevision.id`/`content_sha256`. `effective_revision = pinned_revision` quando pin não-nulo, senão `= current_revision`; `description`/`tags`/`declared_tools`/`compatibility` retornados são os da `effective_revision`.

## DELETE

Idempotente. Agent existente + associação inexistente → `204`. Associação existente → `204`. Nunca modifica `Skill`/`SkillRevision`.

## Não fazer

- não implementar `POST /agents/{uuid}/skills/resolve` nesta ou em nenhuma task do v0.5.0;
- não implementar endpoint inverso Skill→Agents;
- não modificar `skills`/`skill_revisions` schema.

## Gate SM-803

A task só encerra quando:

- os 3 endpoints acima estão implementados com envelopes/erros no padrão existente;
- o composite FK `(skill_id, pinned_revision_id) → skill_revisions(skill_id, id)` comprovadamente **rejeita cross-Skill pinning** por teste de integração real PostgreSQL (tentativa de pin com revisão de outra Skill falha na constraint, não apenas na validação de aplicação);
- deletar diretamente uma `SkillRevision` correntemente pinada por qualquer `agent_skills` é rejeitada (`ON DELETE RESTRICT`), comprovado por teste de integração real PostgreSQL — nunca resulta em `pinned_revision_id = NULL`;
- `pinned_revision_id IS NULL` acompanha `Skill.current_revision_id` ao vivo (mudar `current_revision_id` via rollback/nova revisão muda o `effective_revision` observado sem mutar a linha de `agent_skills`), comprovado por teste;
- `pinned_revision_id` não-nulo ignora mudanças posteriores de `current_revision_id` (pin sobrevive a rollback/nova revisão da Skill), comprovado por teste;
- re-pin e unpin (`pinned_revision: null` explícito) funcionam via `PUT` idempotente, comprovado por teste;
- `PUT` duplicado (mesmo payload) e `PUT` concorrente com payloads diferentes convergem para uma única linha determinística, sem `409`, comprovado por teste real PostgreSQL;
- Skill `archived`/`restore` preserva a associação e continua visível em `GET .../skills`, comprovado por teste;
- Agent `archived` preserva a associação e todas as operações de gestão continuam permitidas, comprovado por teste;
- `GET .../skills` nunca inclui `procedure`, `resolution_embedding`, `SkillRevision.id` interno, ou `content_sha256`, comprovado por teste de shape;
- nenhum evento `graph_outbox` é criado por nenhuma operação desta task, comprovado por teste;
- suite existente permanece verde.

---

# SM-804 — Agent ↔ Session explicit association

## Objetivo

Implementar a associação explícita Agent↔Session, congelada como **current explicit management association** — nunca como Agent provenance — sobre a fundação do SM-801/SM-802 e sobre Sessions já existente (v0.3.0). Toda a semântica já está congelada pelo Feature Contract §12 — esta task é implementação, não decisão de produto.

## Escopo

Migration `0017` (revises `0016`) criando `agent_sessions`:

```text
agent_sessions
    agent_id      UUID NOT NULL
    session_id    UUID NOT NULL
    created_at    TIMESTAMPTZ NOT NULL

    PRIMARY KEY (agent_id, session_id)
```

FKs (já congeladas, Feature Contract §12.2 — não redecidir):

```text
agent_id    → agents.id    ON DELETE CASCADE
session_id  → sessions.id  ON DELETE CASCADE
```

`created_at` significa exclusivamente "quando a associação corrente foi criada" — nunca "primeira participação histórica".

Endpoints:

```text
GET    /api/v1/agents/{agent_uuid}/sessions
PUT    /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
DELETE /api/v1/agents/{agent_uuid}/sessions/{session_uuid}
```

## PUT

Idempotent ensure-association, sem payload de runtime/provenance. Agent/Session inexistentes → `404`. Create/replay → `200`, nunca duplica row. Associar a uma Session `archived` continua permitido (management metadata, não nova atividade contextual — não é bloqueado pelo admission barrier de ADR-0012). Nunca cria `SessionEntry`/`Query`/`PipelineRun`.

## GET

Management list: `session_uuid`/`session_id`/`name`/`status`/`association_created_at`. Nunca retorna transcript nem `SessionEntries`.

## DELETE

Idempotente. Agent existente + associação inexistente → `204`. Associação existente → `204`. Permitido justamente porque `agent_sessions` não é historical audit.

## Não fazer

- **não adicionar `agent_id` a `queries`, `pipeline_runs` ou `session_entries`** — nenhuma coluna de atribuição por operação é criada nesta ou em nenhuma task do v0.5.0;
- não implementar endpoint inverso Session→Agents;
- não descrever, em código, testes, ou comentário, `Query`/`PipelineRun → Session → agent_sessions` como identificação do Agent que originou a operação — essa afirmação é falsa sob cardinalidade M:N (Feature Contract §12.3) e não deve aparecer em nenhum artefato desta task.

## Gate SM-804

A task só encerra quando:

- os 3 endpoints acima estão implementados com envelopes/erros no padrão existente;
- cardinalidade M:N é comprovada por teste real PostgreSQL: mesmo Agent associado a duas Sessions distintas, e mesma Session associada a dois Agents distintos, simultaneamente;
- `PUT` duplicado é idempotente (`200`, sem nova linha), comprovado por teste;
- `DELETE` de associação inexistente é idempotente (`204`, sem erro), comprovado por teste;
- Session `archived` preserva a associação e `GET .../sessions` continua retornando-a, comprovado por teste — e o teste também comprova que a associação em si não dispara nenhuma escrita de `SessionEntry`/`Query`/`PipelineRun`, preservando o admission barrier de ADR-0012 intacto;
- Agent `archived` preserva a associação, comprovado por teste;
- **nenhuma coluna nova aparece em `queries`, `pipeline_runs` ou `session_entries`**, comprovado por schema guard test;
- um teste explícito comprova que, com uma Session associada a dois Agents distintos, nenhuma query/consulta sobre os dados persistidos permite determinar qual dos dois Agents está associado a uma `Query`/`PipelineRun` específica daquela Session (prova direta do invariant de não-atribuição, Feature Contract §12.3/§23-25);
- `created_at` de `agent_sessions` reflete apenas a criação da associação corrente (uma remoção seguida de nova associação produz um `created_at` novo, distinto do original), comprovado por teste;
- nenhum evento `graph_outbox` é criado por nenhuma operação desta task, comprovado por teste;
- suite existente permanece verde.

---

# SM-805 — Lifecycle, concurrency e cross-feature hardening

## Objetivo

Provar, com infraestrutura real (PostgreSQL e, onde aplicável, Neo4j), todos os invariants de concorrência e de não-interferência cross-feature ainda não cobertos individualmente por SM-801..SM-804 — mesmo papel que SM-705 exerceu para Skills (v0.4.0) e SM-606 para Sessions (v0.3.0).

## Escopo

### Forget / Dataset Delete non-interference

- Forget `source`, `dataset` e `everything` preservam `agents`/`agent_skills`/`agent_sessions` completamente intactos (contagem de linhas antes/depois idêntica), comprovado por teste real PostgreSQL, mesmo padrão de SM-606/SM-705;
- Dataset Delete idem.

### Neo4j / graph_outbox non-interference

- nenhum evento `graph_outbox` com `aggregate_type` relacionado a Agent é criado por nenhuma operação de Agent Management, comprovado por teste;
- teste real Neo4j comprova zero labels/relationship types relacionados a Agent (`(:Agent)`, `(:AgentProfile)`, `(:AgentSkill)` ou equivalentes) após um ciclo completo de criação/associação/archive/restore, mesmo padrão de prova que SM-705 usou para Skills.

### Lifecycle matrices

- Agent archive/restore: matriz completa de operações permitidas (`GET`, `PATCH`, associar/remover Skill, associar/remover Session, restore) comprovada com o Agent `archived`;
- Skill archive interaction: associação `agent_skills` sobrevive a archive/restore da Skill, comprovado em conjunto com a matriz acima;
- Session archive interaction: associação `agent_sessions` sobrevive a archive da Session, e a associação em si nunca dispara nova atividade contextual, comprovado em conjunto.

### Concurrency matrix (Feature Contract §19)

- duplicate Agent name creation: exatamente um sucesso, demais `409`, zero `IntegrityError`/500 vazado;
- `PATCH` vs archive/restore concorrentes sobre o mesmo Agent: linearizável, sem lost update, via `FOR UPDATE` de linha única;
- `agent_skills` duplicate association / concurrent pin updates: convergência determinística, sem duplicata;
- `agent_sessions` duplicate association: convergência via unicidade de PK;
- Agents diferentes nunca se serializam desnecessariamente entre si (prova de que o lock é por-Agent, não global).

## Não fazer

- não introduzir nenhum endpoint novo;
- não alterar nenhuma decisão de schema já congelada por SM-801/SM-803/SM-804 — esta task prova, não redesenha.

## Gate SM-805

A task só encerra quando:

- todos os itens de escopo acima estão cobertos por teste real PostgreSQL (e real Neo4j onde aplicável), sem mocks de infraestrutura para as provas centrais;
- Forget/Dataset Delete preservação é comprovada nos três escopos de Forget (`source`/`dataset`/`everything`) e em Dataset Delete;
- zero `graph_outbox` e zero label/relationship Neo4j para Agent é comprovado, não apenas assumido pela ausência de código de projeção;
- a matriz de concorrência completa do Feature Contract §19 está coberta;
- suite completa (unit/integration/contract/security) permanece verde.

---

# SM-806 — Docs, smoke e release gate v0.5.0

## Objetivo

Fechar o release: documentação, smoke real end-to-end, quality gates completos, e o gate formal GATE-v0.5.0.

## Documentação

- `README.md`, `AGENTS.md` (amendment final de rotas proibidas e repository tree, já iniciado em SM-802 — revisão final de consistência), `docs/api.md` (nova família de endpoints), `docs/development.md`/`docs/operations.md` se aplicável, `CHANGELOG.md`;
- Feature Contract `Status` só é promovido de `Proposed` para `Implemented` após os gates abaixo passarem, mesmo padrão de SM-607/SM-706;
- `.github/workflows/integration.yml`: adicionar explicitamente as suítes de integração de Agent Management (opt-in flags dedicados, mesmo padrão dos 9 flags de Skills).

## Smoke real

Cobrir, via API pública real (sem atalho direto em PostgreSQL exceto para inspeção/cleanup):

- create Agent → `PATCH` profile → archive → restore, comprovando disponibilidade de management durante archive;
- associar Skill sem pin → resolver `effective_revision` como `current_revision` → pin explícito → rollback de `current_revision` na Skill → comprovar que `effective_revision` da associação pinada não mudou;
- associar Session → comprovar que nenhuma `SessionEntry`/`Query`/`PipelineRun` foi criada pela associação em si;
- Forget/Dataset Delete smoke provando não-interferência com Agent/AgentSkill/AgentSession;
- Neo4j check provando ausência de labels/relationship types de Agent;
- `graph_outbox` check provando ausência de categoria relacionada a Agent.

## Quality gate

Executar:

```text
ruff
format/check
mypy
pytest (unit/contract/security/integration)
migration/schema gates (0014 → 0017, fresh-install e upgrade)
smoke v0.5.0
runtime-only pip-audit
Bandit HIGH-severity blocking gate
release consistency
```

conforme tooling oficial do repositório. Não mascarar testes existentes, não reduzir cobertura contratual e não excluir suites para obter gate verde.

## Version bump

`APP_VERSION`/canonical version `0.4.0 → 0.5.0` — somente nesta task, como parte do release commit, nunca antes.

## GATE-v0.5.0

O release somente pode ser marcado como concluído quando:

- SM-801..SM-805 estiverem aprovadas;
- migrations `0015`/`0016`/`0017` estiverem validadas em banco real (fresh-install e upgrade, `0014 → 0017`);
- API pública estiver coerente com o Feature Contract v0.5.0 (12 operações, nem mais nem menos);
- ADR-0014 estiver respeitado;
- `/agents` estiver fora do forbidden-route contract e `/proposals` permanecer proibido, comprovado pelo teste de contrato;
- o composite FK de `agent_skills` estiver comprovado (cross-Skill pin rejeitado, pinned revision delete rejeitado) por teste real PostgreSQL;
- a non-guarantee de atribuição por operação (`agent_sessions` não identifica Agent de Query/PipelineRun) estiver comprovada por teste explícito;
- `declared_tools`/associação Agent↔Skill nunca-autorização estiver comprovada;
- Forget/Dataset Delete/Session/Skill/Neo4j compatibility estiver comprovada;
- suite completa estiver verde;
- smoke real estiver verde;
- documentação de v0.5.0 estiver atualizada, incluindo `integration.yml`.

Após esse gate, nenhum trabalho de `AgentRevision`, semantic Agent resolve, Agent-scoped Skill resolve, ou atribuição exata por operação deve ser incluído retroativamente no v0.5.0 — esses itens permanecem non-goals explícitos (Feature Contract §14/§22) até um requisito futuro separadamente escopado.

O próximo release funcional planejado permanece separado.
