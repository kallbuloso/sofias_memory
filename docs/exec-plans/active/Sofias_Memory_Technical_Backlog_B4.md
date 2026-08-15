# Sofias Memory — Backlog Técnico Executável B4

**Documento:** Backlog técnico — B4 Core Memory  
**Escopo:** B4 núcleo funcional de memória sobre B0–B3  
**Status:** Em execução; SM-401..SM-419 concluídas; SM-420 é a próxima task
**Pré-requisitos:** GATE-B3 PASSED; ADR-0008 accepted; `AGENTS.md`; `docs/product/Sofias_Memory_PRD_SPECS.md`  
**Regra:** executar uma task por vez, respeitando dependências, contratos congelados e gates.  
**Base de reconstrução:** estado real do repositório em `bd426c99d4f1bc96922b00bb62e2598f545306c1`.  
**Baseline Cognee:** `topoteretes/cognee` v1.4.1, commit `38eece5bbb0cb9f5706fed908abd16dba0f5505e`.

---

## 1. Objetivo deste backlog

B4 representa o núcleo funcional de memória construído sobre as fundações B0–B3.
`GATE-B4` significa que o Core Memory funcional síncrono está concluído. Ele não é
ainda o gate final do MVP operacional, porque B5 continua necessário para execução
assíncrona real, worker e lifecycle operacional de pipelines.

B0–B2 estabeleceram aplicação, contratos básicos, PostgreSQL e schema autoritativo.
B3 estabeleceu Neo4j como projeção reconstruível por outbox/rebuild, sem worker de
polling. B4 usa essas fundações para entregar os fluxos de produto executáveis
sincronamente pelo mecanismo atual:

- remember text/file/url;
- cognify com chunking, embeddings, extração estruturada, summaries e projeção;
- recall chunks/summaries/rag/graph/hybrid/triplets;
- feedback durável;
- improve explícito;
- forget;
- graph/provenance read-only;
- datasets operacionais;
- gates reais de memória fim a fim.

B4 não implementa o pipeline worker completo. Queue claiming, polling, scheduling,
`wait=false` real, heartbeat, stale recovery, retry/cancel assíncronos e lifecycle
operacional de worker pertencem ao futuro B5.

---

## 2. Fontes de verdade e precedência

Para toda task B4, respeitar a precedência definida no `AGENTS.md`:

1. instrução explícita do usuário na task atual;
2. `AGENTS.md` mais específico;
3. `AGENTS.md` raiz;
4. ADR aceito;
5. `docs/product/Sofias_Memory_PRD_SPECS.md`;
6. contratos, migrations e testes versionados;
7. Cognee upstream congelado.

ADRs relevantes:

- ADR-0002 — PostgreSQL source of truth + Neo4j rebuildable projection;
- ADR-0004 — OpenAI-compatible only;
- ADR-0006 — pgvector 3072/halfvec;
- ADR-0007 — PostgreSQL enums/FK/delete policies;
- ADR-0008 — Neo4j Projection and Rebuild Contract.

---

## 3. Invariantes B4

Nunca introduzir, nem temporariamente:

- users, owner_id, tenant_id, roles, permissions, ACL ou multitenancy;
- provider registry para LLM, embeddings, vector DB ou graph DB;
- LiteLLM, Instructor, BAML, Redis, Celery, Qdrant, LanceDB ou Kuzu;
- conhecimento exclusivo no Neo4j;
- dual-write PostgreSQL + Neo4j;
- transação distribuída;
- Cypher arbitrário por API;
- worker/polling completo antes de B5;
- crawler, OCR, imagens, áudio, vídeo, PPTX, XLSX ou ZIP no MVP B4;
- auto-improve invisível;
- rebuild destrutivo implícito no startup/readiness;
- logs com API keys, documentos completos, embeddings, prompts completos ou secrets.

PostgreSQL continua a fonte de verdade. Neo4j continua projeção reconstruível e
descartável. Conteúdo e evidências retornados por APIs públicas devem ser hidratados
do PostgreSQL.

---

## 4. Boundary B4 / B5

B4 pode executar fluxos síncronos explícitos usando os serviços atuais e `wait=true`
quando o contrato da API possuir esse campo:

- `POST /api/v1/remember`;
- `POST /api/v1/remember/file`;
- `POST /api/v1/remember/url`;
- `POST /api/v1/cognify`;
- `POST /api/v1/recall`;
- `POST /api/v1/feedback`;
- `POST /api/v1/improve`;
- `POST /api/v1/forget`, quando implementado;
- operações administrativas explícitas de dataset/graph/provenance, quando implementadas.

B5 fica reservado para:

- polling worker;
- queue claiming com `FOR UPDATE SKIP LOCKED`;
- pipeline scheduling;
- execução assíncrona real para `wait=false`;
- lifecycle operacional de runs/retry/cancel ligado ao worker;
- heartbeat, stale recovery e cancellation cooperativo;
- lock operacional por dataset;
- retry por etapa.

ADR-0008 distingue explicitamente o consumer unitário/B3 do worker futuro/B5; essa
fronteira permanece válida em B4.

Portanto `GATE-B4` valida o core funcional síncrono. O MVP operacional final ainda
depende de B5 para transformar esses casos de uso em execução assíncrona robusta.

---

## 5. Ordem completa de execução B4

```text
B4
SM-401  Remember text ingest
  ↓
SM-402  Remember file TXT/Markdown
  ↓
SM-403  Cognify chunking + embeddings
  ↓
SM-404  Structured knowledge extraction
  ↓
SM-405  Document summary + embedding
  ↓
SM-406  Recall chunks + RAG
  ↓
SM-407  Cognify → outbox → Neo4j projection
  ↓
SM-408  Graph/triplets/hybrid recall
  ↓
SM-409  JSON/CSV/HTML file ingestion
  ↓
SM-410  PDF/DOCX file ingestion
  ↓
SM-411  HTTPS URL ingestion
  ↓
SM-412  Summaries recall
  ↓
SM-413  Feedback recording
  ↓
SM-414  Improve feedback weights
  ↓
SM-415  Improve relation embeddings
  ↓
SM-416  Improve entity duplicate candidate detection
  ↓
SM-417  Improve safe entity merge
  ↓
SM-418  Improve graph reconciliation / projection repair
  ↓
SM-419  Improve summaries stage
  ↓
SM-420  Complete graph_reconciliation hygiene, centrality and change report
  ↓
SM-421  Dataset management API
  ↓
SM-422  Forget source and source memory
  ↓
SM-423  Forget dataset and everything
  ↓
SM-424  Graph/provenance read-only API
  ↓
GATE-B4
```

SM-401..SM-419 estão DONE. SM-420 é a próxima task. SM-421..SM-424 seguem TODO.
GATE-B4 segue TODO.

---

# 6. Histórico B4 reconstruído — DONE

## SM-401 — Remember text ingest baseado no core do `cognee.add()`

**Status:** DONE  
**Commit:** `aa28d23f308f9f81e7a7b156ff56d67272fc72b5` — `feat: implement text remember ingestion`  
**Prioridade:** P0  
**PRD:** FR-020; writes com `PipelineRun`

### Resultado

Implementou `POST /api/v1/remember` para texto direto, dataset `main` lazy, storage local
controlado, hashes original/normalizado, `Source`, `Document`, deduplicação, `force`,
`Idempotency-Key` e `PipelineRun`.

### Não incluiu

Arquivo, URL, chunking, embeddings, LLM, Neo4j, cognify ou worker.

---

## SM-402 — Remember file TXT/Markdown

**Status:** DONE  
**Commit:** `913f71350c37ec6b79c2c9a6476bde3da70b0ebe` — `feat: add text file remember ingestion`  
**Prioridade:** P0  
**PRD:** FR-020

### Resultado

Estendeu `POST /api/v1/remember/file` para `.txt`, `.md` e `.markdown`, reutilizando o
fluxo SM-401 de storage, hashes, dedup, `force`, idempotência e `PipelineRun`.

### Não incluiu

JSON, CSV, HTML, PDF, DOCX, URL, chunking, embeddings, LLM ou worker.

---

## SM-403 — Cognify core: chunking + embeddings reais

**Status:** DONE  
**Commit:** `ec5986dca3e3ad78a89806b4b620b19cd31f264f` — `feat: implement cognify chunking and embeddings`  
**Prioridade:** P0  
**PRD:** FR-030; FR-040; FR-050

### Resultado

Implementou `POST /api/v1/cognify` até chunks + embeddings reais OpenAI-compatible:
chunking determinístico, offsets reais, hashes versionados, `Chunk` persistido com
`embedding` e `lexical`, `Document.token_count` e transições `Source`.

### Não incluiu

Extração de entidades/relações, summaries, Neo4j, outbox, graph recall ou worker.

---

## SM-404 — Structured knowledge extraction + PostgreSQL persistence

**Status:** DONE  
**Commit:** `6c0e148415231334ce89d891c663095c7a49dd93` — `feat: add structured knowledge extraction`  
**Prioridade:** P0  
**PRD:** FR-050

### Resultado

Adicionou extração estruturada por chunk, schema Pydantic, repair único, prompts versionados,
chunk summary em metadata, `Entity`, `EntityMention`, `Relation` e `RelationEvidence`
autoritativos no PostgreSQL. Inclui correção pré-commit do checkpoint para rejeitar
self-relations.

### Não incluiu

Document summary, Neo4j projection, graph outbox, recall ou improve.

---

## SM-405 — Document Summary + embedding persistido

**Status:** DONE  
**Commit:** `b5efae0408db559a11e6ed52256de916de89dba6` — `feat: add document summary generation`  
**Prioridade:** P0  
**PRD:** FR-050; summaries; embeddings

### Resultado

Criou document summary a partir dos chunk summaries, structured output validado,
embedding real do summary, `Summary(target_type=document, level=0)` e marker operacional
em `Document.metadata_`.

### Não incluiu

Dataset summary, entity summary, cluster summary, Neo4j/outbox ou recall de summaries.

---

## SM-406 — Recall Chunks + RAG

**Status:** DONE  
**Commit:** `997eb14be8ca16a6adf05a91ed2548f6a2a2db3b` — `feat: add recall retrieval and rag`  
**Prioridade:** P0  
**PRD:** FR-060

### Resultado

Implementou `POST /api/v1/recall` para `mode=chunks` e `mode=rag` com embedding da query,
vector search PostgreSQL, lexical search PostgreSQL, RRF, contexto, referências,
resposta RAG opcional e auditoria em `Query`.

### Não incluiu

Summaries, graph, hybrid, triplets, Neo4j retrieval, improve ou worker.

---

## SM-407 — Cognify → Transactional Outbox → Neo4j Projection

**Status:** DONE  
**Commit:** `7b74391451624c149feac7ea8f41f8fea334376c` — `feat: project cognify graph to neo4j`  
**Prioridade:** P0  
**PRD:** FR-050; FR-110  
**ADR:** ADR-0008

### Resultado

Ligou cognify à `graph_outbox` transacional e ao drain explícito de projeção. Reutilizou
builders de `ProjectionCommand`, serialização `to_payload`, batch processor por dataset
e projeção de `entity`, `chunk`, `entity_mention`, `relation` e `chunk_next`.

### Não incluiu

Worker/polling, stale recovery, graph recall ou rebuild automático.

---

## SM-408 — Graph, Triplets and Hybrid Recall

**Status:** DONE  
**Commit:** `5010cda6e62174cebd9f45df4123cdcfe0228056` — `feat: add graph and hybrid recall`  
**Prioridade:** P0  
**PRD:** FR-060; FR-110

### Resultado

Estendeu recall com `mode=graph`, `mode=triplets` e `mode=hybrid`, usando chunks RRF como
seeds, Neo4j read-only para IDs técnicos, hidratação PostgreSQL, entidades/relações no
schema público, summaries de documentos em hybrid e resposta LLM quando aplicável.

### Não incluiu

Entity/relation/triplet embeddings para retrieval, graph write, Cypher arbitrário ou worker.

---

## SM-409 — Add JSON, CSV and HTML file ingestion

**Status:** DONE  
**Commit:** `72feb7e62b2d167271b1b01e4e90b739d52c887a` — `feat: add json csv and html file ingestion`  
**Prioridade:** P0  
**PRD:** FR-020

### Resultado

Estendeu `/remember/file` para `.json`, `.csv`, `.html` e `.htm`, com JSON determinístico,
CSV stdlib preservado e HTML visível extraído por `html.parser`, reutilizando o loader e
o fluxo de remember existente.

### Não incluiu

PDF, DOCX, URL, OCR, browser rendering, embeddings ou cognify changes.

---

## SM-410 — Add textual PDF and DOCX file ingestion

**Status:** DONE  
**Commit:** `5281e38e9027099c27f5b433ea6f8e4bfbdf6ae0` — `feat: add pdf and docx file ingestion`  
**Prioridade:** P0  
**PRD:** FR-020

### Resultado

Adicionou ingestão de PDF textual com `pypdf` e DOCX com `python-docx`, preservando bytes
originais, MIME canônico, storage, hashes e loaders existentes.

### Não incluiu

OCR, imagens, macros, comentários avançados, XLSX/PPTX ou novas dependências.

---

## SM-411 — Add safe HTTPS URL ingestion

**Status:** DONE  
**Commit:** `76e0ad5501f35962e8d22f07e0eb5825ccd563b9` — `feat: add secure https url ingestion`  
**Prioridade:** P0  
**PRD:** FR-020; segurança de ingestão URL

### Resultado

Implementou `POST /api/v1/remember/url` para uma única URL HTTPS, com validação SSRF,
DNS por request/redirect, redirects manuais, limite de tamanho, Content-Type permitido
e reutilização dos loaders existentes.

### Não incluiu

Crawler, HTTP plain, auth web, cookies, JavaScript rendering, Playwright ou worker.

---

## SM-412 — Add direct summaries recall

**Status:** DONE  
**Commit:** `4dfddb97005f82e8b50ef790c96f1207b97b4ae4` — `feat: add summaries recall`  
**Prioridade:** P0  
**PRD:** FR-060

### Resultado

Habilitou `mode=summaries` em recall, usando embeddings de `Summary(target_type=document)`,
filtros de source/date/metadata, provenance em context item próprio e auditoria `Query`.

### Não incluiu

LLM, Neo4j, lexical summary search, dataset/entity/cluster summary retrieval ou regeneração.

---

## SM-413 — Add durable feedback recording

**Status:** DONE  
**Commit:** `b00fea8fdac7fd7a07d4376a29e360f0656b223d` — `feat: add feedback recording`  
**Prioridade:** P0  
**PRD:** FR-080

### Resultado

Implementou `POST /api/v1/feedback` para feedback durável sobre answer/reference,
validando `Query`, membership de reference em `Query.references`, `score`, comment e
`applied_at=None`.

### Não incluiu

Aplicação de pesos, ranking mutation, improve, Neo4j ou worker.

---

## SM-414 — Improve v1: apply persisted feedback weights

**Status:** DONE  
**Commit:** `2c220ab098b7bbec57636df0b1698bfb2eba3e94` — `feat: apply feedback weights`  
**Prioridade:** P0  
**PRD:** FR-070; FR-080

### Resultado

Implementou `POST /api/v1/improve` para `feedback_weights`, aplicando feedback não
aplicado a entidades/relações ativas de generation corrente, enfileirando outbox
entity/relation na mesma transação e marcando feedback como aplicado.

### Não incluiu

Relation embeddings, entity deduplication, summaries, graph reconciliation, centrality,
cleanup ou worker.

---

## SM-415 — Improve relation_embeddings stage

**Status:** DONE  
**Commit:** `695d941880e3c32f23e8a973dd683c8e20ddd5c8` — `feat: add relation embeddings improve`  
**Prioridade:** P0  
**PRD:** FR-070

### Resultado

Adicionou `relation_embeddings` ao Improve, gerando embeddings reais para relações
ativas/correntes sem embedding, usando texto triplet determinístico e persistindo no
PostgreSQL.

### Não incluiu

Graph outbox, Neo4j, LLM, relation retrieval por embedding ou replacement de embeddings existentes.

---

## SM-416 — Improve entity_deduplication candidate detection

**Status:** DONE  
**Commit:** `88d4bf09fec6345299f38865d383cdab856e1827` — `feat: detect entity duplicate candidates`  
**Prioridade:** P0  
**PRD:** FR-070; risco de entity resolution imperfeito

### Resultado

Adicionou embeddings de entidades baseados apenas em `Entity.name`, threshold
`ENTITY_DEDUP_SIMILARITY_THRESHOLD`, detecção de pares candidatos por pgvector e contagem
`entity_duplicate_candidates`, sem merge destrutivo.

### Não incluiu

Merge, desativação, rewire de mentions/relations, outbox ou Neo4j.

---

## SM-417 — Entity Merge seguro dentro de entity_deduplication

**Status:** DONE  
**Commit:** `bd426c99d4f1bc96922b00bb62e2598f545306c1` — `feat: merge duplicate entities`  
**Prioridade:** P0  
**PRD:** FR-070; risco de entity resolution imperfeito  
**ADR:** ADR-0008

### Resultado

Estendeu `entity_deduplication` para merge seguro acima de
`ENTITY_MERGE_SIMILARITY_THRESHOLD`, com survivor determinístico, aliases consolidados,
duplicata inativa, mentions reatribuídas, relações reescritas, self-loops inativos,
colisões resolvidas, evidência preservada/copiada, outbox transacional e drain após commit.

### Não incluiu

Novo stage, LLM, fuzzy matching, merge transitive perigoso, deleção física, migration,
worker ou alteração de recall/ranking.

### Evidência real pós-smoke

Durante o smoke real da SM-417 foi constatado:

- PostgreSQL: 88 active entities;
- Neo4j: 44 `Entity` nodes;
- 46 entity IDs únicos tinham histórico de `entity upsert done`;
- 2 desses IDs receberam `entity delete done` na SM-417;
- resultado Neo4j = 44;
- portanto as outras 44 entidades ativas nunca receberam `entity upsert` no outbox.

A SM-417 projetou corretamente seus merges. O achado indica uma lacuna de reconciliação
da projeção para entidades ativas preexistentes e deve alimentar SM-418.

---

# 7. SM-418 concluída e tasks B4 restantes

## SM-418 — Improve graph_reconciliation: reconciliar projeção Neo4j com PostgreSQL

**Status:** DONE
**Commit:** `29d5906` — `feat: add graph reconciliation improve stage`
**Prioridade:** P0  
**Dependências:** SM-407, SM-417, ADR-0008  
**PRD:** FR-070 item 8; FR-110; ADR-0002; ADR-0008  

### Objetivo

Implementar a primeira parte do stage `graph_reconciliation` do Improve para detectar
e reparar divergências PostgreSQL authoritative → Neo4j projection, sem introduzir
worker ou dual-write.

### Implementação esperada

- Usar PostgreSQL como autoridade para entidades, chunks, mentions, relations e NEXT;
- comparar projeção esperada com estado Neo4j por dataset, usando IDs técnicos;
- identificar ausência/excesso de nodes/edges de projeção;
- reparar por rebuild dataset-scoped ou por projection commands seguros, reaproveitando
  `GraphRebuildService`/ADR-0008 quando for mais simples;
- incluir a evidência real SM-417 como regressão: entidades ativas sem `entity upsert`
  histórico devem ser reprojetadas;
- retornar contagens operacionais no `ImproveResult`;
- persistir relatório resumido no `PipelineRun.metrics`;
- drenar outbox somente após commit PostgreSQL quando houver eventos.

### Não fazer

- não criar polling worker;
- não criar `FOR UPDATE SKIP LOCKED`;
- não executar reset global contra Neo4j local;
- não mudar identidades ADR-0008;
- não criar labels/relationships novos;
- não usar Neo4j como fonte de verdade;
- não expor Cypher arbitrário.
- não criar stage novo além dos definidos pelo PRD.

### Critérios de aceite

- `stages=["graph_reconciliation"]` deixa Neo4j convergente para o dataset solicitado;
- entidades/chunks/edges ativos e current-generation ausentes são recriados;
- projeção obsoleta pertencente ao dataset é removida sem afetar outros datasets;
- dados externos ao modelo Sofias no Neo4j são preservados;
- execução repetida é idempotente;
- falha Neo4j deixa erro seguro e estado PostgreSQL recuperável;
- o known issue SM-417 vira teste/regressão.

### Validação

Unit tests focados + teste opt-in real dataset-scoped. Não executar reset global local.

### Evidência real pós-smoke

Dataset validado:

- `dataset_id`: `7fae0f62-bd6a-40f5-8143-771a238111dc`;
- `active_generation`: `0`.

Baseline anterior:

- PostgreSQL active `Entity`: 88;
- Neo4j `Entity`: 44.

Primeira execução:

- request: `POST /api/v1/improve`, `stages=["graph_reconciliation"]`, `wait=true`;
- `run_id`: `386726f4-3ba6-4931-963d-9a7eb77d75ab`;
- `status=succeeded`;
- `graph_entities_missing=44`;
- `graph_entities_extra=0`;
- `graph_chunks_missing=6`;
- `graph_chunks_extra=0`;
- `graph_entity_mentions_missing=119`;
- `graph_entity_mentions_extra=0`;
- `graph_relations_missing=93`;
- `graph_relations_extra=0`;
- `graph_next_missing=4`;
- `graph_next_extra=0`;
- `graph_rebuilt=true`;
- `graph_events_enqueued=0`;
- `graph_events_processed=0`.

Segunda execução idempotente:

- `run_id`: `2538f69c-64a6-4d9c-96dd-2c12e27a9a18`;
- `status=succeeded`;
- todos os `graph_*_missing = 0`;
- todos os `graph_*_extra = 0`;
- `graph_rebuilt=false`;
- `graph_events_enqueued=0`;
- `graph_events_processed=0`.

O smoke confirmou que o known issue descoberto na SM-417 foi corrigido. A divergência
histórica não afetava apenas `Entity`: também incluía `Chunk`, `MENTIONED_IN`,
`RELATES_TO` e `NEXT`. A correção reutilizou `GraphRebuildService` dataset-scoped,
sem rebuild global e sem `graph_outbox` artificial. A segunda execução comprovou
convergência e idempotência.

---

## SM-419 — Improve summaries stage: rebuild supported persisted summaries

**Status:** DONE  
**Commit:** `a9c8721` — `feat: add summaries improve stage`  
**Próxima task:** NÃO
**Prioridade:** P1  
**Dependências:** SM-405, SM-412, SM-417  
**PRD:** FR-070 item 5; FR-050 summaries; FR-060 summaries recall

### Objetivo

Implementar o stage `summaries` do Improve para reconstruir summaries persistidos
suportados pelo contrato atual quando documentos ou conhecimento derivado mudarem,
sem regenerar conhecimento desnecessário.

### Implementação esperada

- usar `extraction.summary` dos chunks como dado intermediário do Cognify, sem criar
  `Summary` de chunk;
- reconstruir `Summary(target_type=document, level=0)` quando marker estiver ausente,
  stale ou incompatível;
- criar summary de dataset somente se o contrato atual de `SummaryTargetType.DATASET`
  e o PRD forem satisfeitos sem migration;
- gerar embedding real para summaries reconstruídos;
- desativar summaries antigos de forma lógica quando substituídos;
- registrar contagens e mudança no `PipelineRun.metrics`.

### Não fazer

- não criar hierarchy/global context genérica do Cognee;
- não inventar `SummaryTargetType.CHUNK`;
- não usar Neo4j;
- não criar provider registry;
- não mudar chunk summaries da SM-404 sem necessidade;
- não implementar worker.

### Critérios de aceite

- `stages=["summaries"]` roda isoladamente;
- summaries atuais continuam no-op quando completos;
- summaries stale são reconstruídos de forma idempotente;
- recall `mode=summaries` usa o novo estado;
- falha LLM/embedding não persiste summary parcial.
### Evidência real pós-smoke

Dataset validado:

- `dataset_id`: `7fae0f62-bd6a-40f5-8143-771a238111dc`;
- `active_generation`: `0`;
- 3 documentos elegíveis;
- 2 document summaries válidos;
- 1 document summary ausente/inválido;
- 0 dataset summaries ativos.

Primeira execução:

- `run_id`: `cc8d015f-e870-479d-a109-325e9fbd18b9`;
- `status=succeeded`;
- `document_summaries_rebuilt=1`;
- `dataset_summaries_rebuilt=1`;
- `summaries_deactivated=0`;
- nenhum evento/rebuild de grafo.

Segunda execução idempotente:

- `run_id`: `023032fb-2ab2-4025-91f5-7a23bf7058ae`;
- `status=succeeded`;
- `document_summaries_rebuilt=0`;
- `dataset_summaries_rebuilt=0`;
- `summaries_deactivated=0`.

Smoke stale controlado:

- document `11c96cbb-2693-4f82-b4a4-a45e90d297e7`;
- marker persistido com `prompt_version=stale-smoke-test`;
- `run_id`: `bb180d69-c2da-495f-be20-10ae3814973a`;
- `status=succeeded`;
- `document_summaries_rebuilt=1`;
- `dataset_summaries_rebuilt=1`;
- `summaries_deactivated=0`.

Execução final de convergência:

- `run_id`: `649b5f1e-c76c-4834-b861-2eba81870dd2`;
- `status=succeeded`;
- `document_summaries_rebuilt=0`;
- `dataset_summaries_rebuilt=0`;
- `summaries_deactivated=0`.

O smoke comprovou reconstrução seletiva de document summary, reconstrução derivada do
dataset summary, idempotência e convergência, sem `SummaryTargetType.CHUNK`, sem alteração
do contrato público de Recall e sem efeitos colaterais em Neo4j/outbox.

---

## SM-420 — Complete graph_reconciliation: graph hygiene, centrality and change report

**Status:** TODO
**Próxima task:** SIM 
**Prioridade:** P1  
**Dependências:** SM-414, SM-417, SM-418  
**PRD:** FR-070 itens 6, 7 e 9; completa o mesmo stage `graph_reconciliation` iniciado em SM-418

### Objetivo

Completar o mesmo stage `graph_reconciliation`, incorporando os comportamentos ainda
faltantes de FR-070: graph hygiene, centrality/importance recalculation e change report.
Após SM-420, `graph_reconciliation` deve satisfazer conjuntamente os itens aplicáveis
6, 7, 8 e 9 do FR-070.

### Implementação esperada

- identificar relations ativas/current-generation que não possuem evidence ligada a
  `Chunk` authoritative ativo e na `active_generation` do dataset, respeitando os
  estados authoritative aplicáveis, e marcar a relation como `is_active=false`;
- recalcular importância/centralidade determinística a partir do grafo PostgreSQL ativo;
- preservar feedback weights já aplicados sem sobrescrever arbitrariamente;
- enfileirar outbox transacional para relations/entities alteradas;
- persistir change report resumido em `PipelineRun.metrics`;
- manter campos de response aditivos e estáveis.

### Não fazer

- não usar GDS/APOC;
- não calcular centralidade no Neo4j como autoridade;
- não deletar fisicamente relations/evidence;
- não adicionar coluna `RelationEvidence.is_active` ou migration;
- não criar stages `graph_hygiene`, `centrality` ou similares;
- não criar scheduler;
- não alterar recall ranking neste checkpoint.

### Critérios de aceite

- relations sem evidence ligada a chunks authoritative ativos/current-generation deixam
  de aparecer em recall/projeção;
- importance recalculation é determinístico e limitado ao dataset;
- outbox e PostgreSQL são commitados juntos;
- reexecução é idempotente;
- relatório informa o que mudou sem expor documentos completos.

---

## SM-421 — Dataset management API

**Status:** TODO  
**Prioridade:** P1  
**Dependências:** B2 datasets; SM-401; SM-418  
**PRD:** FR-010; API datasets

### Objetivo

Implementar a API pública mínima de datasets que ainda falta no HEAD atual.

### Implementação esperada

- cobrir o subconjunto síncrono B4 da API pública de datasets:
  - `POST /api/v1/datasets`;
  - `GET /api/v1/datasets`;
  - `GET /api/v1/datasets/{dataset_id}`;
  - `PATCH /api/v1/datasets/{dataset_id}`;
  - `GET /api/v1/datasets/{dataset_id}/sources`;
  - `GET /api/v1/datasets/{dataset_id}/stats`;
- criar dataset;
- listar datasets;
- obter dataset por id/slug conforme contrato final;
- renomear dataset;
- listar sources do dataset;
- retornar contadores operacionais;
- seguir somente as rotas públicas de datasets previstas no PRD;
- quando reconstrução de memória for necessária, reutilizar o contrato existente
  `POST /api/v1/cognify` com `rebuild=true`, salvo se o PRD explicitamente determinar
  outra rota;
- preservar criação lazy de `main` pelo remember.
- registrar que a deleção operacional de dataset que exigir processamento assíncrono,
  incluindo `DELETE /api/v1/datasets/{dataset_id}` quando houver artefatos, fica para B5.

### Não fazer

- não criar users/ACL/tenant;
- não criar delete assíncrono com worker;
- não inventar endpoint de dataset rebuild;
- não tratar `POST /api/v1/forget` como substituto de
  `DELETE /api/v1/datasets/{dataset_id}`;
- não inventar comportamento intermediário para `DELETE /api/v1/datasets/{dataset_id}`;
- não implementar operação de dataset que dependa obrigatoriamente de processamento
  assíncrono B5;
- não alterar contrato de remember/cognify/recall.

### Critérios de aceite

- endpoints usam envelope padrão e `X-API-Key`;
- slugs são normalizados e únicos;
- dataset inexistente retorna erro estável;
- counters não dependem de Neo4j como fonte de verdade;
- `GET /api/v1/datasets/{dataset_id}/sources` lista sources authoritative do dataset;
- operações síncronas de dataset respeitam o contrato público do PRD.

---

## SM-422 — Forget source e memory-only de source

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-407, SM-418, SM-421  
**PRD:** FR-090; FR-110; ADR-0008

### Objetivo

Implementar a primeira fatia autoritativa de `POST /api/v1/forget` para apagar uma
source inteira ou apenas memória derivada da source, com execução síncrona `wait=true`
e recuperação por retry explícito.

### Implementação esperada

- validar dataset/source;
- marcar source como `deleting`;
- excluir/desativar chunks e derivados autoritativos;
- desativar entities/relations órfãs quando não houver outra evidence ligada a chunks
  authoritative ativos/current-generation;
- enfileirar deletes/upserts necessários na `graph_outbox` na mesma transação;
- opcionalmente remover arquivo original quando `memory_only=false`;
- finalizar source como `deleted`;
- retornar contagens.

### Não fazer

- não implementar worker/lock B5;
- não implementar `wait=false`, enqueue assíncrono ou lifecycle de worker;
- não deletar dados de outro dataset;
- não usar Neo4j para descobrir o que apagar;
- não apagar storage antes de commit crítico sem estratégia recuperável;
- não implementar everything.

### Critérios de aceite

- source esquecida não aparece em recall;
- Neo4j converge por outbox/drain;
- `memory_only=true` preserva storage original;
- falha parcial é recuperável por retry explícito síncrono;
- PostgreSQL permanece autoridade.

---

## SM-423 — Forget dataset e everything

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-422  
**PRD:** FR-090

### Objetivo

Completar a semântica/core autoritativa de `POST /api/v1/forget` para dataset inteiro,
memory-only de dataset e everything com confirmação explícita, usando execução síncrona
`wait=true`.

### Implementação esperada

- `dataset + memory_only`;
- `dataset + memory_only=false`;
- `everything=true` exige `confirm="DELETE EVERYTHING"`;
- apagar/desativar artefatos por escopo;
- limpar projeção Neo4j somente pelo escopo permitido;
- preservar recuperação em falha parcial;
- retornar contagens de sources, documents, chunks, entities, relations, summaries,
  graph events e storage.

### Não fazer

- não criar worker;
- não implementar `wait=false`, enqueue assíncrono ou lifecycle operacional B5;
- não executar operação destrutiva sem confirmação explícita;
- não usar `MATCH (n) DETACH DELETE n`;
- não remover labels arbitrários no Neo4j;
- não criar usuário/tenant/ACL.

### Critérios de aceite

- dataset esquecido não aparece em remember/cognify/recall;
- everything exige confirmação exata;
- cleanup Neo4j respeita ADR-0008;
- dados externos ao Sofias no Neo4j são preservados;
- operação é idempotente.

---

## SM-424 — Graph/provenance read-only API

**Status:** TODO  
**Prioridade:** P1  
**Dependências:** SM-408, SM-418, SM-422  
**PRD:** FR-110

### Objetivo

Implementar endpoints read-only de graph/provenance previstos no PRD, sem Cypher arbitrário.

### Implementação esperada

- obter schema de tipos/predicados;
- listar entidades relacionadas a uma source;
- obter evidências de uma relation;
- obter caminho limitado entre duas entidades;
- visualizar subgrafo JSON limitado;
- rastrear uma reference até source/document/chunk/storage;
- hidratar conteúdo/evidência do PostgreSQL.

### Não fazer

- não expor Cypher;
- não usar Neo4j como fonte de conteúdo;
- não criar graph write;
- não implementar graph-RAG novo;
- não criar endpoint de administração destrutiva.

### Critérios de aceite

- todas as respostas usam envelope padrão;
- filtros respeitam dataset/generation/is_active;
- referências são rastreáveis até PostgreSQL/source storage;
- ausência de evidência retorna resposta segura;
- limites impedem expansão ilimitada do grafo.

---

# 8. GATE-B4 — Core Memory funcional síncrono concluído

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-401..SM-424  
**Tipo:** FUNCTIONAL / INTEGRATION GATE síncrono

## Objetivo

Provar que o núcleo funcional de memória opera de ponta a ponta em execução síncrona
`wait=true`, sem depender do worker B5. Este gate não é ainda o gate final do MVP
operacional.

## Critérios obrigatórios

- remember text/file/url cobre todos os formatos MVP: text, TXT, Markdown, JSON, CSV,
  HTML, textual PDF, DOCX e HTTPS URL;
- cognify gera chunks, embeddings, entities, relations, evidence, document summary,
  summary embedding e graph outbox;
- outbox/drain explícito projeta Neo4j;
- graph reconciliation corrige projeção divergente;
- recall cobre `chunks`, `summaries`, `rag`, `graph`, `hybrid` e `triplets`;
- feedback é registrado e aplicado por Improve;
- Improve cobre `feedback_weights`, `entity_deduplication`, `relation_embeddings`,
  `summaries` e `graph_reconciliation`;
- API síncrona de datasets valida create, list, get, rename, sources e stats, sem
  users/ACL/tenant e usando PostgreSQL como autoridade;
- entity merge preserva provenance e não faz deleção física;
- Forget source/dataset/everything funciona nos escopos definidos;
- SM-423 Forget dataset/everything cobre a semântica de esquecimento de
  memória/conteúdo e não elimina a obrigação futura do contrato público
  `DELETE /api/v1/datasets/{dataset_id}`;
- graph/provenance read-only rastreia evidências até source/document/chunk;
- forget executa a semântica/core autoritativa em modo síncrono;
- PostgreSQL continua fonte de verdade;
- Neo4j é descartável/reconstruível;
- nenhum conteúdo essencial existe apenas no Neo4j;
- nenhum worker/polling/scheduler B5 foi introduzido.

## Pendências explícitas para B5

Antes do MVP operacional final, B5 ainda precisa implementar:

- polling worker;
- queue claiming;
- pipeline scheduling;
- `wait=false` real;
- runs/retry/cancel operacionais;
- heartbeat;
- stale recovery;
- cancellation cooperativo;
- dataset deletion assíncrona quando houver artefatos, conforme FR-010;
- lifecycle operacional correspondente;
- lifecycle de worker.

## Validação esperada

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory scripts
uv run pytest
git diff --check
```

Além da suíte normal, executar smoke real opt-in com PostgreSQL/Neo4j reais para:

- remember → cognify → Neo4j projection → recall;
- summaries recall;
- graph/hybrid/triplets recall;
- feedback → improve → projection;
- entity merge;
- graph reconciliation;
- forget source/dataset;
- rebuild/reconciliation sem histórico de `graph_outbox`;
- preservação de dados externos no Neo4j.

Não executar cenários destrutivos globais contra backend local persistente sem confirmação
explícita e ambiente descartável.

---

# 9. O que fica explicitamente para B5

B5 deverá cuidar de execução operacional assíncrona e não deve ser antecipado dentro de B4:

- polling worker;
- queue claiming;
- pipeline scheduling;
- `wait=false` real;
- retries por etapa;
- run retry/cancel operacional;
- heartbeat;
- stale recovery;
- lock por dataset;
- múltiplos pipelines concorrentes controlados;
- lifecycle de worker.

---

# 10. Definition of Done mínima por task B4

Uma task B4 só pode ser marcada `DONE` quando:

- escopo implementado sem antecipar B5;
- PRD/ADR respeitados;
- PostgreSQL permanece fonte de verdade;
- Neo4j permanece projeção reconstruível;
- testes aplicáveis criados/atualizados;
- erros públicos são estáveis e seguros;
- secrets/conteúdo sensível não são logados;
- `uv lock --check` passa quando aplicável;
- `ruff` passa;
- `ruff format --check` passa;
- `mypy` passa;
- `pytest` aplicável passa;
- `git diff --check` passa;
- nenhum commit/push/PR foi feito pelo Codex.
