# Sofias Memory — Backlog Técnico Executável v0.4.0 Skills

**Release:** v0.4.0\
**Feature:** First-class durable procedural Skills\
**Status:** Proposed\
**Sequência:** SM-701..SM-706\
**Regra de execução:** executar uma task por vez; não antecipar dependências ou escopo de tickets posteriores.

## 1. Objetivo

O v0.4.0 introduz Skills como procedural memory first-class e durável, complementando a memória semântica (Datasets/Documents) e o contexto temporal (Sessions, ADR-0012) já existentes.

Ao final deste backlog, Sofias Memory deverá possuir:

- Skills e SkillRevisions persistentes em PostgreSQL, com revisão imutável e append-only;
- lifecycle `active <-> archived`;
- API de management com paginação e rollback de `current_revision` por ponteiro;
- import/export standalone de `SKILL.md`;
- resolução semântica via pgvector, respeitando progressive disclosure;
- `/skills` removido do forbidden-route contract, com `/agents`/`/proposals` preservados;
- preservação de compatibilidade com Datasets, Sessions, Forget, Dataset Delete e Neo4j.

Este backlog não implementa SkillRun, tool execution, tool authorization, Agent, ou qualquer forma de seleção/invocação automática de Skill.

---

# 2. Fontes normativas

Ordem de precedência específica deste release:

1. instrução explícita da task em execução;
2. `AGENTS.md`;
3. ADR-0013 — First-Class Durable Procedural Skills;
4. Feature Contract v0.4.0 — Skills;
5. ADRs anteriores aplicáveis, especialmente ADR-0002, ADR-0003 e ADR-0012;
6. contratos e testes existentes;
7. PRD original, exceto onde explicitamente amended pelo ADR-0013 (somente a exclusão de `/skills`; `/agents`/`/proposals` permanecem no baseline original).

O Feature Contract define a semântica pública detalhada. ADR-0013 define a mudança arquitetural e suas fronteiras. Este backlog define somente a ordem executável de implementação e os gates.

---

# 3. Invariantes de release

Durante SM-701..SM-706:

- PostgreSQL continua authoritative;
- nenhuma Skill ou SkillRevision é projetada para Neo4j;
- Skill nunca é authorization boundary;
- Skill nunca pertence a Dataset, Session ou Agent;
- `name` permanece textual, portable-subset, único e imutável;
- `skill_uuid` é a identidade estrutural UUID;
- SkillRevision nunca é editada in-place;
- revision number é inteiro monotônico por Skill, nunca timestamp;
- resolution embedding indexa apenas `name`+`description`+`tags`, nunca `procedure`;
- resolve nunca escolhe, nunca carrega procedure automaticamente, nunca cria SessionEntry/Query/SkillRun;
- `declared_tools`/`allowed-tools` importado nunca autorizam execução;
- import nunca faz upsert silencioso;
- Forget e Dataset Delete nunca removem Skill/SkillRevision;
- nenhuma transação PostgreSQL longa permanece aberta durante chamada ao embedding provider;
- não introduzir SkillRun, tool executor, Agent table, Redis, filesystem watcher, plugin system ou MCP.

---

# 4. Sequência

| Ticket | Entrega principal | Depende de | Status |
|---|---|---|---|
| SM-701 | Skill schema, domínio e persistence foundation | — | Proposed |
| SM-702 | Skill management + immutable revision API | SM-701 | Proposed |
| SM-703 | SKILL.md standalone import/export | SM-702 | Proposed |
| SM-704 | Semantic resolve + progressive disclosure | SM-701, SM-702 | Proposed |
| SM-705 | Lifecycle, compatibility e cross-feature hardening | SM-702, SM-703, SM-704 | Proposed |
| SM-706 | Docs, smoke e release gate v0.4.0 | SM-705 | Proposed |

SM-703 e SM-704 podem ser implementadas em qualquer ordem depois de suas dependências, mas não devem ser misturadas na mesma task.

---

# SM-701 — Skill schema, domínio e persistence foundation

## Objetivo

Criar a fundação persistente e de domínio de first-class Skills sem ainda expor a API pública de management.

## Escopo

Implementar:

### Skill

- migration `0014` criando `skills` — uma única coluna UUID de identidade (`id`, PK; nunca uma segunda coluna `skill_uuid` duplicando-a — Feature Contract §3, §4.1);
- constraint de unicidade em `name` (case-sensitive, subset portable já validado na camada de domínio);
- enum/coluna `status` (`active`/`archived`);
- `current_revision_id` como FK para `skill_revisions.id`, com criação atômica da revisão 1 na mesma transação do create de Skill (Feature Contract §4.4) — nullable somente na camada de schema, se necessário para quebrar o ciclo de FK durante a construção da transaction; nenhuma linha `Skill` committed pode ter `current_revision_id = NULL`, comprovado por teste de invariante;
- índices necessários para list/paginação e para o lookup de `name`.

### SkillRevision

- migration criando `skill_revisions` — `id` UUID PK interno (nunca exposto em URL pública, sempre endereçada por `{skill_uuid}/revisions/{revision}` — Feature Contract §3.3), FK para `skills`, `ON DELETE` policy consistente com "sem hard delete público de Skill" (nunca deve ser exercitada em operação normal, mas deve ter uma política explícita e testada, mesma disciplina de ADR-0007);
- unique constraint `(skill_id, revision)`;
- coluna pgvector para `resolution_embedding`, com dimensão compatível com `EMBEDDING_DIMENSIONS` (mesma disciplina já usada para chunks);
- `content_sha256` coluna + índice para detecção de reimportação/reenvio idêntico (Feature Contract §12.7 — hash do conteúdo semântico canônico, não dos bytes originais; SM-702/703 consomem isso para safe replay, mas a coluna e a função de canonicalização nascem aqui).

### Domínio

- função única e compartilhada de normalização/validação de `name` (mesmo padrão de `normalize_session_id`), usada por create e por import (SM-703);
- função única e compartilhada de canonicalização de conteúdo de revisão (Feature Contract §12.7: JSON UTF-8 canônico, chaves ordenadas, separadores estáveis, `procedure` normalizado para LF, `tags`/`declared_tools` ordenados) usada tanto pelo cálculo de `content_sha256` quanto, futuramente, pela geração determinística de export (SM-703);
- lógica de próximo `revision` monotônico, concurrency-safe, serializada por um lock por-Skill (Feature Contract §6.2) que também serializa `PATCH current_revision`/archive/restore — implementar o mecanismo de lock aqui, mesmo que as rotas que o exercitam só cheguem no SM-702+;
- repositories para `skills`/`skill_revisions`, sem SQL espalhado por routes/pipelines.

## Não fazer

- não expor nenhuma rota pública nesta task;
- não implementar resolve nesta task;
- não implementar import/export nesta task;
- não tocar `graph_outbox`, Neo4j, Recall, Session ou Remember/Cognify.

## Gate SM-701

A task só encerra quando:

- migration upgrade/downgrade é válida;
- schema guards refletem conscientemente as novas tabelas/colunas/FKs/constraints, incluindo a ausência de uma segunda coluna de identidade UUID duplicada;
- uniqueness de `name` é comprovada por teste, incluindo concorrência de criação;
- nenhuma Skill committed com `current_revision_id = NULL` é observável, comprovado por teste de invariante (incluindo o caminho de falha do embedding provider, simulado nesta camada de domínio);
- criação concorrente de revisão para a mesma Skill produz ordinais únicos e coerentes sob o lock por-Skill, comprovado por teste, cobrindo tanto conteúdo diferente (dois ordinais) quanto conteúdo idêntico (uma única revisão, replay detectável via `content_sha256`);
- a função de canonicalização produz o mesmo `content_sha256` para conteúdo semanticamente idêntico com formatação diferente (ordem de chaves, whitespace, CRLF vs LF, ordem de `tags`/`declared_tools`), comprovado por teste;
- `ON DELETE` policies estão cobertas por teste;
- normalização compartilhada de `name` possui testes de boundary (subset portable, hyphen leading/trailing, `--`, uppercase, limites de tamanho);
- nenhuma tabela ou coluna nova introduz `dataset_id`/`session_id`/`agent_id`/`owner_id`/`tenant_id`/`user_id`;
- suite existente permanece verde após atualização deliberada dos schema tests.

---

# SM-702 — Skill management + immutable revision API

## Objetivo

Expor a API pública de management sobre a fundação do SM-701, incluindo criação/leitura/paginação/archive/restore/rollback de `current_revision`.

## Endpoints

Implementar:

```text
POST   /api/v1/skills
GET    /api/v1/skills
GET    /api/v1/skills/{skill_uuid}
POST   /api/v1/skills/{skill_uuid}/revisions
GET    /api/v1/skills/{skill_uuid}/revisions
GET    /api/v1/skills/{skill_uuid}/revisions/{revision}
PATCH  /api/v1/skills/{skill_uuid}
POST   /api/v1/skills/{skill_uuid}/archive
POST   /api/v1/skills/{skill_uuid}/restore
```

## Create

`POST /api/v1/skills` cria a Skill e sua revisão 1 atomicamente (payload mínimo `name`+`description`+`procedure`). `409`/`INVALID_REQUEST` se `name` já existir em qualquer status. Falha do embedding provider antes da persistência resulta em zero linhas de `Skill`/`SkillRevision` (Feature Contract §4.4/§7.1).

## Revisions

`POST .../revisions` cria uma nova SkillRevision e atualiza `current_revision_id` na mesma transação, sob o lock por-Skill (SM-701). `201` para conteúdo semanticamente novo; `200` (safe replay, Feature Contract §9.1) se o `content_sha256` já corresponder a uma revisão existente da mesma Skill — nesse caso `current_revision_id` **não** é alterado. `GET .../revisions` lista metadata (sem `procedure`). `GET .../revisions/{revision}` devolve o conteúdo completo, endereçado pelo inteiro `revision`, nunca pelo `id` interno da linha.

## PATCH e rollback

`PATCH /api/v1/skills/{skill_uuid}` aceita estritamente `current_revision`. Rejeita `name`. Rejeita qualquer campo de conteúdo de revisão. `current_revision` apontando para uma revisão inexistente da própria Skill é `422`/`INVALID_REQUEST`. Concorrência entre `PATCH` (rollback) e criação de nova revisão é linearizada pelo lock por-Skill (Feature Contract §6.5) — cobrir ambas as ordens de intercalação em teste.

## Archive/restore

Discovery filter, não admission barrier sobre escrita — Feature Contract §5 e §17: `GET`, criar revisão, `PATCH current_revision` e `export` continuam funcionando normalmente em uma Skill `archived`; apenas `resolve` (SM-704) a exclui. Restore preserva `current_revision_id` inalterado. Idempotentes.

## Esta é a única rota pública de rotas proibidas afetada

Atualizar `tests/contract/test_openapi_forbidden_routes.py` para remover `/skills` de `FORBIDDEN_PATH_PREFIXES`, preservando `/agents` e `/proposals`. Atualizar `AGENTS.md` §12 (rotas proibidas) e §6 (repository tree, adicionar `routes/skills.py`) **e o `CLAUDE.md` correspondente na raiz** (Feature Contract §21.2 — os dois arquivos devem sair desta task sincronizados, não apenas `AGENTS.md`) com o amendment mínimo já anunciado no Feature Contract.

## Não fazer

- não implementar resolve nesta task (SM-704);
- não implementar import/export nesta task (SM-703);
- não expor `procedure` em list/discovery.

## Gate SM-702

A task só encerra quando:

- os 8 endpoints acima estão implementados com envelopes/paginação/erros no padrão existente;
- criação concorrente com `name` colidente resulta em exatamente um sucesso e um `409`, comprovado por teste real PostgreSQL;
- rollback de `current_revision` é comprovado por teste (criar revisão 2, apontar de volta para 1, revisão 2 permanece intacta e legível);
- safe replay de conteúdo idêntico devolve `200`/revisão existente e nunca altera `current_revision_id`, comprovado por teste;
- concorrência entre nova revisão e rollback explícito é comprovada nas duas ordens de intercalação, sem lost update, comprovado por teste real PostgreSQL;
- `PATCH` rejeita `name` e conteúdo de revisão, comprovado por teste;
- toda operação de management (`GET`, criar revisão, `PATCH`) funciona sobre uma Skill `archived`, comprovado por teste;
- `/skills` sai do forbidden-route contract sem reabrir `/agents`/`/proposals`, comprovado pelo teste de contrato atualizado;
- `AGENTS.md` e `CLAUDE.md` estão sincronizados quanto a `/skills`, comprovado por revisão manual do diff;
- OpenAPI gerado documenta os 8 endpoints e os shapes corretos, incluindo `declared_tools` como `list[string]` e `metadata` como `map<string,string>`;
- suite existente permanece verde.

---

# SM-703 — SKILL.md standalone import/export

## Objetivo

Implementar a superfície de interoperabilidade com o Agent Skills format, mantendo a API estruturada como recurso autoritativo (Feature Contract §12). As rotas, o envelope de export, e todos os mappings de campo já estão congelados pelo Feature Contract — esta task é implementação, não decisão de produto.

## Escopo

Rotas (já congeladas, Feature Contract §12.4/§12.5 — não redecidir):

```text
POST /api/v1/skills/import
POST /api/v1/skills/{skill_uuid}/revisions/import
GET  /api/v1/skills/{skill_uuid}/revisions/{revision}/export
```

Implementar:

- parsing do subset portable (`name`/`description`/`license`/`compatibility`/`metadata`/corpo Markdown), com os limites de §12.8;
- mapping determinístico `allowed-tools` (string separada por espaço) ↔ `declared_tools` (`list[string]`), Feature Contract §12.2 — reutilizar a mesma lógica de import/export em ambas as direções, sem caminho de código separado por rota;
- mapping determinístico `tags` ↔ `metadata["sofias-memory.tags"]` (JSON array serializado como string), Feature Contract §12.6;
- preservação de `allowed-tools` como metadata descritiva, nunca autorização (Feature Contract §12.2);
- reutilização da função de canonicalização/`content_sha256` do SM-701 (não reimplementar);
- serialização determinística de export dentro do `SuccessEnvelope` padrão (`format`/`content`/`content_sha256`, Feature Contract §12.5) — nunca uma resposta raw fora do envelope.

## Import

- `POST /skills/import`: `name` inexistente → cria nova Skill + revisão 1 (equivalente a `POST /skills`); `name` já existente (qualquer status) → `409`/`INVALID_REQUEST`, nunca upsert;
- `POST /skills/{skill_uuid}/revisions/import`: cria nova SkillRevision na Skill alvo; frontmatter `name` divergente do `Skill.name` alvo → `422`/`INVALID_REQUEST`, nunca renomeação/redirecionamento implícito;
- conteúdo idêntico (mesmo `content_sha256`, via a função canônica do SM-701) → safe replay (Feature Contract §9.1): `200`, revisão existente, `current_revision_id` inalterado — mesma regra e mesmo código usados por `POST .../revisions` estruturado (SM-702), não uma segunda implementação.

## Export

- determinístico sobre o **conteúdo semanticamente persistido**: mesmo `revision` sempre produz o mesmo `content`/`content_sha256`, dado o mesmo conteúdo persistido (Feature Contract §12.7) — nunca uma promessa de bytes idênticos ao arquivo originalmente importado.

## Não fazer

- não implementar bundles/scripts/references/assets;
- não usar Source object storage;
- não implementar resolve nesta task;
- não introduzir uma terceira rota de import/export além das três congeladas acima;
- não reimplementar canonicalização/hash/safe-replay separadamente do SM-701/SM-702.

## Gate SM-703

A task só encerra quando:

- import via `POST /skills/import` de um `SKILL.md` novo cria Skill + revisão 1, comprovado por teste;
- import via `POST /skills/{skill_uuid}/revisions/import` para uma Skill existente cria revisão explícita, nunca upsert, comprovado por teste;
- import de revisão com `name` divergente do frontmatter é rejeitado com `422`, comprovado por teste;
- reimportação idêntica (mesmo `content_sha256`) resolve como safe replay (`200`, revisão existente, `current_revision_id` inalterado), comprovado por teste, em ambas as rotas de import;
- export é semanticamente determinístico: `export(import(x))` produz o mesmo conteúdo canônico que `x` representa, para o subset portable — sem exigir igualdade de bytes com `x`, comprovado por teste;
- `allowed-tools` ↔ `declared_tools` e `tags` ↔ `metadata["sofias-memory.tags"]` fazem round-trip corretamente e deterministicamente, comprovado por teste;
- `allowed-tools`/`declared_tools` importado nunca é interpretado como autorização em nenhum caminho de código, comprovado por teste;
- `metadata` permanece `map<string,string>` em toda a superfície pública, comprovado por teste de shape;
- suite existente permanece verde.

---

# SM-704 — Semantic resolve + progressive disclosure

## Objetivo

Implementar `POST /api/v1/skills/resolve` sobre pgvector, respeitando o boundary de metadata-only da resolution embedding.

## Escopo

- geração de `resolution_embedding` a partir de `name`+`description`+`tags` no momento de criação/atualização de revisão (SM-701/702 já persistem a coluna; esta task popula e consome);
- endpoint `resolve` com `query`/`top_k` (default 5, máximo 20);
- filtro `active`-only;
- ranking por similaridade, sem threshold oculto, `score` decrescente com direção fixa "maior = mais similar" (Feature Contract §10);
- sem matches → `200`/`matches=[]`, nunca `404`/erro;
- shape de resposta = metadata de progressive disclosure (Feature Contract §11), nunca `procedure`.

## Transaction boundary

Nenhuma transação PostgreSQL permanece aberta durante a chamada ao embedding provider — mesma disciplina de Cognify/Session resolution. Falha do provider durante create/revision falha atomicamente, sem Skill/SkillRevision parcial.

## Não fazer

- não escolher uma Skill em nome do caller;
- não carregar `procedure` automaticamente;
- não criar SessionEntry;
- não criar Query de Recall;
- não criar SkillRun (não existe);
- não executar tool;
- não alterar Recall nem Session Context RAG.

## Gate SM-704

A task só encerra quando:

- `resolve` devolve ranking correto contra um fixture real de múltiplas Skills, comprovado por teste PostgreSQL real, com `score` decrescente e direção "maior = mais similar" verificada;
- query sem nenhum match retorna `200`/`matches=[]`, comprovado por teste;
- Skills `archived` nunca aparecem em `resolve`, comprovado por teste;
- resposta nunca contém `procedure`, comprovado por teste de shape;
- `top_k` respeita default/máximo;
- falha do embedding provider durante create/revision não deixa Skill/SkillRevision parcial, comprovado por teste;
- nenhuma SessionEntry/Query/PipelineRun é criada como efeito colateral de `resolve`, comprovado por teste;
- suite existente permanece verde.

---

# SM-705 — Lifecycle, compatibility e cross-feature hardening

## Objetivo

Provar que Skills coexistem corretamente com Dataset, Session, Forget, Dataset Delete e Neo4j, fechando os invariantes cross-feature do Feature Contract.

## Cenários obrigatórios

- Forget (source/dataset/everything) não afeta nenhuma Skill/SkillRevision existente, comprovado por teste real PostgreSQL;
- Dataset Delete não afeta nenhuma Skill/SkillRevision existente, comprovado por teste real PostgreSQL;
- nenhuma linha em `graph_outbox` é criada por qualquer operação de Skill, comprovado por teste;
- nenhum nó/relacionamento Skill aparece em Neo4j após qualquer operação de Skill, comprovado por teste real Neo4j;
- Session e SessionEntry permanecem sem qualquer coluna ou referência a Skill;
- archive de Skill remove de `resolve` mas preserva `GET`/`GET revisions`/`PATCH current_revision`/criar nova revisão/`export`, comprovado por teste — incluindo criar uma nova revisão *enquanto* a Skill está `archived` e confirmar que ela permanece `archived` e ainda excluída de `resolve`;
- restore de Skill archived reabilita `resolve` preservando exatamente o `current_revision_id` que a Skill tinha antes do archive (restore nunca reseta o ponteiro), comprovado por teste;
- archive/restore são idempotentes, comprovado por teste;
- concorrência de criação de Skill com `name` colidente sob carga real (não apenas unit) converge para um único vencedor;
- matriz completa de concorrência por-Skill (Feature Contract §6) sob carga real: (a) duas criações de revisão com conteúdo diferente → dois ordinais distintos, o último committed é `current`; (b) duas criações de revisão com conteúdo idêntico → uma única revisão persistida, a segunda resolve como safe replay; (c) nova revisão concorrente com rollback explícito, nas duas ordens de intercalação → resultado determinístico sem lost update, conforme Feature Contract §6.5.

## Não fazer

- não implementar nenhum item de §22 do Feature Contract (SkillRun, Agent, etc.);
- não implementar seleção automática de Skill em Recall.

## Gate SM-705

A task só encerra quando todos os cenários acima estão cobertos por teste de integração real e a suite completa permanece verde.

---

# SM-706 — Docs, smoke e release gate v0.4.0

## Objetivo

Fechar o release: documentação, smoke real end-to-end, quality gates completos, e o gate formal GATE-v0.4.0.

## Documentação

- `README.md`, `AGENTS.md` (amendment final de rotas proibidas e repository tree), `docs/api.md` (nova família de endpoints), `docs/development.md`/`docs/operations.md` se aplicável, `CHANGELOG.md`;
- Feature Contract `Status` só é promovido a `Implemented` após os gates abaixo passarem, mesmo padrão de SM-607.

## Smoke real

Cobrir, via API pública real (sem atalho direto em PostgreSQL exceto para inspeção/cleanup):

- create Skill → create revision → rollback current_revision → archive → resolve (deve sumir) → restore → resolve (deve reaparecer);
- import SKILL.md → export → comparação determinística;
- Forget/Dataset Delete smoke provando não-interferência;
- Neo4j check provando ausência de labels/relationship types de Skill;
- `graph_outbox` check provando ausência de categoria relacionada a Skill.

## Quality gate

Executar:

```text
ruff
format/check
mypy
pytest (unit/contract/security/integration)
migration/schema gates
smoke v0.4.0
runtime-only pip-audit
Bandit HIGH-severity blocking gate
release consistency
```

conforme tooling oficial do repositório. Não mascarar testes existentes, não reduzir cobertura contratual e não excluir suites para obter gate verde.

## GATE-v0.4.0

O release somente pode ser marcado como concluído quando:

- SM-701..SM-705 estiverem aprovadas;
- migration estiver validada em banco real (fresh-install e upgrade);
- API pública estiver coerente com o Feature Contract;
- ADR-0013 estiver respeitado;
- `/skills` estiver fora do forbidden-route contract e `/agents`/`/proposals` permanecerem proibidos, comprovado pelo teste de contrato;
- progressive disclosure estiver comprovada (nenhum endpoint de discovery/list/resolve vaza `procedure`);
- import/export determinístico estiver comprovado;
- `declared_tools`/`allowed-tools` nunca-autorização estiver comprovada;
- Forget/Dataset Delete/Session/Neo4j compatibility estiver comprovada;
- suite completa estiver verde;
- smoke real estiver verde;
- documentação de v0.4.0 estiver atualizada.

Após esse gate, nenhum trabalho de Agent Management (v0.5) deve ser incluído retroativamente no v0.4.0.

O próximo release funcional planejado permanece separado.
