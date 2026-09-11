# Sofias Memory — Backlog Técnico Executável B5

**Documento:** Backlog técnico — B5 Operational Async Pipeline Runtime
**Escopo:** B5 execução operacional assíncrona, worker interno, lifecycle durável de pipelines e fechamento do MVP operacional sobre B0–B4
**Status:** GATE-B5 PASSED; SM-501..SM-516 concluídas
**Pré-requisitos:** GATE-B4 PASSED; `AGENTS.md`; `docs/product/Sofias_Memory_PRD_SPECS.md`; ADRs aceitos, especialmente ADR-0002, ADR-0007 e ADR-0008
**Regra:** executar uma task por vez, respeitando dependências, contratos congelados e gates. Não antecipar stories seguintes.
**Base de elaboração:** estado do repositório após `f202e86` — `docs: close B4 synchronous core memory milestone`.
**Baseline funcional Cognee:** `topoteretes/cognee` v1.4.1, commit `38eece5bbb0cb9f5706fed908abd16dba0f5505e`.

---

## 1. Objetivo deste backlog

B4 encerrou e comprovou o **Core Memory funcional síncrono**. O objetivo de B5 é transformar esse core já aprovado em um **runtime operacional assíncrono, durável e recuperável**, mantendo a arquitetura deliberadamente simples do Sofias Memory:

- uma única aplicação FastAPI;
- worker interno no mesmo processo da aplicação;
- PostgreSQL como fila, estado operacional e fonte de verdade;
- Neo4j como projeção reconstruível;
- filesystem local para sources;
- uma única réplica suportada no MVP;
- nenhuma fila externa;
- nenhuma segunda implementação paralela dos casos de uso síncronos.

Ao final de B5, as operações de escrita deverão poder ser submetidas com `wait=false`, executadas pelo worker, acompanhadas pela API de runs, retomadas após falhas/restart e canceladas ou repetidas de forma controlada. `wait=true` deverá permanecer disponível, mas deverá observar **o mesmo lifecycle durável** usado por `wait=false`, em vez de manter um segundo motor síncrono independente.

B5 também fecha duas obrigações funcionais que ficaram deliberadamente fora do gate síncrono B4:

- `Remember mode=full`, compondo ingestão + cognificação dentro de um único lifecycle operacional;
- `DELETE /api/v1/datasets/{dataset_id}` administrativo, distinto de `POST /api/v1/forget`.

B5 é o milestone que deve levar o projeto ao **MVP operacional final da versão 1**, sem introduzir recursos SaaS, multiusuário, distribuídos ou de alta disponibilidade.

---

## 2. Fontes de verdade e precedência

Para toda task B5, respeitar a precedência definida no `AGENTS.md`:

1. instrução explícita do usuário na task atual;
2. `AGENTS.md` mais específico;
3. `AGENTS.md` raiz;
4. ADR aceito;
5. `docs/product/Sofias_Memory_PRD_SPECS.md`;
6. contratos, migrations e testes versionados;
7. Cognee upstream congelado.

Referências B5 obrigatórias:

- PRD `FR-001` — startup, worker e recovery;
- PRD `FR-010` — datasets, incluindo delete e rebuild;
- PRD `FR-020` — ingestão e idempotência;
- PRD `FR-050` — cognificação e generations;
- PRD `FR-070` — Improve explícito;
- PRD `FR-090` — Forget e recovery de deleção;
- PRD `FR-100` — Runs e worker;
- PRD `FR-120` — readiness;
- PRD seção 11 — API, especialmente Runs;
- PRD seções 12.13 e 12.14 — `pipeline_runs` e `pipeline_steps`;
- PRD seção 14.3 — pipeline engine;
- PRD seção 15 — pipelines;
- PRD seção 19 — configuração Worker;
- PRD seção 21 — startup/shutdown;
- PRD seção 23 — observabilidade;
- PRD `NFR-004` — confiabilidade;
- PRD seção 25 — integração/E2E e recovery;
- ADR-0001 — modular monolith;
- ADR-0002 — PostgreSQL source of truth + Neo4j rebuildable projection;
- ADR-0007 — PostgreSQL enums/FK/delete policies;
- ADR-0008 — Neo4j Projection and Rebuild Contract;
- backlog B4, especialmente `Boundary B4 / B5` e `O que fica explicitamente para B5`.

---

## 3. Baseline B5 — estado real após GATE-B4

B5 não parte de infraestrutura vazia. O repositório já contém fundações importantes que devem ser **reutilizadas**, não duplicadas.

### 3.1 Já existe

- `pipeline_runs` persistido no PostgreSQL;
- `pipeline_steps` persistido no PostgreSQL;
- estados de run: `queued`, `running`, `succeeded`, `failed`, `cancelling`, `cancelled`;
- estados equivalentes de step;
- `PipelineRun.worker_id`;
- `PipelineRun.heartbeat_at`;
- `PipelineRun.attempt`;
- `PipelineRun.progress`;
- `PipelineRun.current_step`;
- `PipelineRun.config_fingerprint`;
- `PipelineStep.attempt`;
- `PipelineStep.input_hash`;
- `PipelineStep.output`, `metrics` e `error`;
- índices de run por status, dataset/status, heartbeat e created_at;
- `graph_outbox` durável;
- processor unitário de uma row da `graph_outbox`;
- batch drain explícito por dataset;
- Settings para worker:
  - `WORKER_ENABLED`;
  - `WORKER_POLL_INTERVAL_MS`;
  - `WORKER_STALE_AFTER_SECONDS`;
  - `WORKER_MAX_CONCURRENT_DATASETS`;
  - `WORKER_MAX_CONCURRENT_READS`;
- serviços B4 síncronos e aprovados para Remember, Cognify, Improve e Forget;
- `PipelineRun` já utilizado pelos writes síncronos B4;
- `graph_outbox` já respeita ADR-0008 nos fluxos funcionais B4.

### 3.2 Ainda não existe

- polling worker;
- queue claiming operacional;
- `FOR UPDATE SKIP LOCKED` para `pipeline_runs`;
- scheduler/dispatcher de pipelines;
- pipeline engine operacional em `sofias_memory/pipelines/`;
- registry de pipelines executáveis;
- execução durável de `PipelineStep`;
- heartbeat do worker/run em runtime;
- stale run recovery;
- restart recovery;
- retry automático por step;
- cancellation cooperativo;
- API pública de Runs;
- `wait=false` real;
- lifecycle de worker no `lifespan`;
- recovery autônomo de `graph_outbox` deixada pendente/processing após crash;
- `DELETE /api/v1/datasets/{dataset_id}`;
- worker como componente de readiness.

### 3.3 Gaps de contrato já visíveis

- Remember aceita atualmente apenas `mode=ingest` e `wait=true`;
- Cognify rejeita `wait=false`;
- Cognify rejeita `rebuild=true`, apesar do contrato de produto prever rebuild/new generation;
- Improve possui `wait` no schema, porém a rota executa o serviço inline;
- Forget permanece síncrono `wait=true`;
- não existe `routes/runs.py`;
- `lifespan` inicializa recursos de banco/Neo4j, mas não inicia worker;
- `graph_outbox` em `processing` não possui hoje um timestamp/lease dedicado para stale recovery;
- o PRD fala em run "stale", mas o enum persistido não possui status `stale`;
- `pipeline_type` possui apenas `remember`, `cognify`, `improve`, `forget`, portanto a identidade operacional do futuro administrative Dataset DELETE ainda não está congelada;
- runs podem ter `dataset_id=NULL`, o que exige decisão explícita para serialização de operações globais e para writes cujo Dataset ainda não foi materializado;
- o significado operacional de `config_fingerprint` durante resume/retry após restart ainda não está congelado.

Esses pontos não são falhas retroativas do B4. São justamente decisões e capacidades reservadas ao B5.


### 3.4 Auditoria funcional do Cognee v1.4.1

A baseline funcional congelada do Cognee foi revisada especificamente para o escopo B5.

Conclusões incorporadas ao desenho:

- o Cognee v1.4.1 serializa mutações do mesmo dataset com `asyncio.Lock` process-local e
  documenta explicitamente que esse mecanismo não protege múltiplos processos; B5 não
  deve copiar esse lock como autoridade operacional;
- o `DatasetQueue` do upstream é um semaphore process-local para limitar concorrência e
  recursos, não uma fila durável de trabalho;
- `run_in_background=True` usa `asyncio.create_task` e uma queue em memória para updates;
  isso é útil como referência de UX, mas não satisfaz durability/restart recovery do
  Sofias Memory;
- o recovery de Cognify do upstream detecta runs antigos por idade e faz rollback/reset;
  o próprio código reconhece heartbeat/lease como solução mais precisa; B5 mantém a
  decisão do PRD de heartbeat + stale recovery durável;
- o upstream possui um `PipelineContext` tipado e composição explícita de tasks, conceito
  útil para SM-504, mas o Sofias não deve herdar custom pipelines, users/ACL ou configuração
  dinâmica do upstream;
- o mecanismo de reentrância por dataset do upstream existe para evitar self-deadlock em
  nested pipelines; no Sofias, a regra preferida é um único top-level run e steps internos
  que não tentam reclamar novamente o mesmo slot operacional.

A auditoria não encontrou razão para reabrir nenhuma das 14 decisões B5 aprovadas.

---

## 4. Invariantes B5

Nunca introduzir, nem temporariamente:

- Redis;
- Celery;
- RabbitMQ;
- Kafka;
- SQS ou fila externa;
- processo de worker separado da aplicação;
- `asyncio.create_task`/queue em memória como mecanismo autoritativo de submissão assíncrona;
- microserviço de worker;
- scheduler externo obrigatório;
- cron interno de Improve;
- pipeline arbitrário enviado pelo cliente;
- imports dinâmicos controlados por request;
- múltiplas réplicas como requisito do MVP;
- cluster de workers;
- conhecimento exclusivo no Neo4j;
- dual-write PostgreSQL + Neo4j;
- transação distribuída;
- lock PostgreSQL de longa duração mantido durante chamadas de LLM, embeddings, HTTP ou Neo4j;
- violar o identity boundary já congelado em `AGENTS.md`/PRD (sem users, ownership, tenant, ACL, roles ou permissions);
- provider registry;
- abstração de queue provider;
- sync/cloud client;
- plugin system;
- frontend;
- MCP no repositório principal.

Invariantes positivas:

- PostgreSQL é a única autoridade de lifecycle e fila;
- `pipeline_runs` é o registro durável do trabalho de escrita;
- `pipeline_steps` registra progresso operacional por etapa;
- `graph_outbox` continua sendo a única fronteira durável PostgreSQL → Neo4j;
- Neo4j continua descartável/reconstruível;
- cada request público de write cria ou reutiliza **um único top-level `PipelineRun`**;
- não criar árvore de runs aninhados apenas para compor Remember/full com Cognify;
- steps internos de um run já serializado não devem voltar à fila para reclamar o mesmo dataset;
- `wait=true` e `wait=false` devem convergir para o mesmo engine/lifecycle;
- reads não entram na fila de writes;
- reads usam somente estado authoritative ativo;
- no máximo um write pipeline por dataset lógico executa simultaneamente;
- operações globais devem possuir semântica de exclusão explícita contra writes de dataset;
- nenhum estado essencial de recovery pode existir apenas em memória do processo;
- steps devem ser idempotentes ou possuir regra explícita de retomada;
- cancellation ocorre apenas em safe points;
- transações críticas nunca são interrompidas no meio por cancelamento;
- shutdown não transforma estado ambíguo em sucesso silencioso.

---

## 5. Architecture Gates e decisões já congeladas antes do código B5

B5 começa pelo contrato operacional do worker:

```text
ADR-0009 — Worker, Queue Claiming and Pipeline Lifecycle Contract
```

A deleção administrativa de Dataset terá contrato próprio, mais próximo de sua
implementação:

```text
ADR-0010 — Administrative Dataset Deletion Contract
```

`ADR-0010` será produzido antes de SM-515 e não bloqueia SM-502..SM-514.

Nenhum worker, queue claim, retry/cancel ou async write público deve ser implementado
antes de ADR-0009 ser aceito.

As decisões abaixo já estão aprovadas no planejamento B5 e não devem ser reabertas
ad hoc durante implementação. O ADR-0009 deve registrar e detalhar os mecanismos
transacionais necessários para cumpri-las.

### 5.1 Run lifecycle e stale

Estados persistidos de `PipelineRun` permanecem:

```text
queued
running
succeeded
failed
cancelling
cancelled
```

Não introduzir `PipelineRunStatus.STALE` apenas para representar heartbeat vencido.

`stale` é uma classificação operacional derivada de estado persistido + heartbeat,
por exemplo:

```text
status = running
AND heartbeat_at < stale_threshold
```

ADR-0009 deve congelar:

- transições válidas/proibidas;
- quem pode realizar cada transição;
- quando `started_at` e `finished_at` são definidos;
- semântica de `progress` e `current_step`;
- semântica exata de `attempt` do run;
- recovery de `RUNNING` stale;
- recovery de `CANCELLING` stale sem perder a intenção de cancelamento.

### 5.2 Step lifecycle

ADR-0009 deve congelar:

- estados e transições válidas de `PipelineStep`;
- semântica de `attempt`;
- `input_hash`;
- política de `output`/`metrics` sem conteúdo sensível;
- quando um step sucedido pode ser pulado em resume;
- quando um step precisa ser reexecutado integralmente;
- cancellation checkpoints entre steps.

### 5.3 Queue claiming

O worker usa PostgreSQL e `FOR UPDATE SKIP LOCKED`.

ADR-0009 deve congelar:

- query de claim;
- ordenação determinística mínima;
- commit do claim antes de executar I/O externo;
- ausência de locks de longa duração durante LLM/embedding/Neo4j;
- proteção contra double claim;
- fairness mínima compatível com uma única réplica oficial.

### 5.4 Dataset serialization e global barrier

Direção arquitetural aprovada:

- resolver/criar o Dataset antes de colocar um write dataset-scoped na fila;
- usar `pipeline_runs.dataset_id` UUID autoritativo como identidade principal de
  serialização;
- não adicionar `concurrency_key` persistida sem necessidade comprovada;
- `dataset_id = NULL` fica reservado a operações realmente globais, como
  `Forget Everything`;
- operação global funciona como barrier contra writes dataset-scoped conflitantes;
- a exclusão não pode depender apenas de mutex em memória.

ADR-0009 deve congelar o mecanismo PostgreSQL exato para:

- primeiro Remember de Dataset ainda inexistente;
- writes do mesmo Dataset;
- concorrência entre Datasets distintos;
- `everything=true`;
- Forget source × Forget dataset × Everything × outros writes.

### 5.5 Worker identity e heartbeat

ADR-0009 deve congelar:

- formato seguro de `worker_id`;
- geração por boot/processo;
- intervalo do heartbeat;
- relação entre heartbeat interval e `WORKER_STALE_AFTER_SECONDS`;
- heartbeat sem manter transaction aberta;
- worker ownership/recovery após restart.

### 5.6 Config fingerprint em recovery

Decisão aprovada:

- um run abandonado não deve ser retomado automaticamente quando seu
  `config_fingerprint` difere da configuração atual;
- o run deve terminar de forma segura como `failed`, com erro público/operacional
  estável como `CONFIG_FINGERPRINT_MISMATCH` ou equivalente;
- novo processamento sob configuração nova ocorre por novo run/manual retry, não
  misturando configurações no mesmo run.

ADR-0009 deve congelar o ponto exato do check e as exceções, se existirem.

### 5.7 Retry automático durável

Decisão aprovada: o schedule de retry deve sobreviver a restart.

B5 deve persistir a elegibilidade temporal da próxima tentativa, preferencialmente por
campo como:

```text
next_attempt_at TIMESTAMPTZ NULL
```

ou equivalente decidido no ADR.

O worker só deve reclamar run queued quando a tentativa estiver elegível.

ADR-0009 deve congelar:

- erros retryable versus permanentes;
- limite de tentativas;
- backoff exponencial + jitter;
- semântica de `PipelineRun.attempt`;
- semântica de `PipelineStep.attempt`;
- cancellation enquanto aguarda retry;
- migration exata necessária.

### 5.8 Manual retry

Decisão aprovada:

```text
POST /api/v1/runs/{run_id}/retry
```

cria um **novo `PipelineRun`**, preservando o run anterior como histórico imutável.

O novo run deve referenciar o anterior de forma persistida e auditável, por campo como:

```text
retry_of_run_id UUID NULL
```

ou equivalente congelado no ADR/migration.

ADR-0009 deve definir:

- estados que permitem retry;
- identidade/idempotência do novo run;
- tratamento de steps já committed;
- attempts;
- erro público em conflito.

### 5.9 Cancellation

ADR-0009 deve congelar:

- cancel de `queued`;
- `running → cancelling → cancelled`;
- cancellation checkpoints;
- cancel durante backoff;
- comportamento durante provider call;
- nenhuma interrupção no meio de transação crítica;
- recovery quando o processo cai em `cancelling`.

### 5.10 `wait=false`, `wait=true` e timeout

Decisão aprovada:

`wait=false`:

- run duravelmente persistido antes da resposta;
- HTTP `202 Accepted`;
- retorna `run_id` e status observável.

`wait=true`:

- submete exatamente o mesmo tipo de trabalho à mesma fila/engine;
- aguarda o mesmo run;
- não chama implementação síncrona paralela;
- respeita `REQUEST_WAIT_TIMEOUT_SECONDS`;
- se o run ainda não terminou no timeout, retorna `202 Accepted` com o mesmo
  `run_id` e status atual;
- o run continua executando;
- timeout/desconexão HTTP não cancela automaticamente o trabalho.

### 5.11 Async idempotency e identidade do trabalho

Decisão aprovada: `wait` **não participa** do payload hash/idempotency identity do
trabalho.

A identidade canônica deve representar o trabalho solicitado, não a preferência do
cliente por aguardar a resposta.

Consequência esperada:

```text
request A: mesma operação + wait=false + Idempotency-Key K
→ cria/resolve Run X

request B: mesma operação + wait=true + Idempotency-Key K
→ resolve o mesmo Run X e apenas escolhe aguardar X
```

ADR-0009 deve ainda congelar comportamento para run existente em:

- queued;
- running;
- succeeded;
- failed;
- cancelling;
- cancelled.

### 5.12 Remember/full sem nested top-level runs

Decisão aprovada:

```text
um request público = um top-level PipelineRun
```

`Remember mode=full` usa um único `PipelineType.REMEMBER` top-level e compõe ingest +
cognify como steps internos reutilizáveis.

Não criar `REMEMBER` run aguardando um segundo top-level `COGNIFY` run para a mesma
request.

`POST /cognify` standalone continua criando seu próprio top-level `COGNIFY` run.

### 5.13 Graph outbox recovery

Decisão aprovada: `graph_outbox.status = processing` abandonado precisa de recovery
durável; não pode ficar permanentemente inprocessável após crash.

SM-506 deve introduzir estado/lease temporal persistido suficiente para identificar
`processing` abandonado, com migration explícita. A forma mínima esperada pode incluir:

```text
processing_started_at TIMESTAMPTZ NULL
worker_id TEXT NULL
```

ou equivalente justificado.

ADR-0008 continua autoridade para identidade, payload e idempotência da projeção.

ADR-0009 deve congelar:

- claim do outbox;
- lease/recovery;
- crash após Neo4j apply e antes de `mark_done`;
- dependency ordering;
- relação entre drain explícito e recovery autônomo.

### 5.14 Worker lifecycle e `WORKER_ENABLED`

Decisão aprovada:

```text
WORKER_ENABLED=false
```

não ativa um engine síncrono alternativo.

Nesse estado:

- reads continuam disponíveis quando suas dependências estiverem saudáveis;
- writes que dependem do runtime B5 retornam `503` com erro estável de worker
  indisponível;
- `/health/live` continua representando apenas processo vivo;
- `/health/ready` fica false porque o runtime operacional obrigatório não está pronto.

ADR-0009 deve ainda congelar:

- startup;
- poll interval;
- limites de concorrência;
- graceful shutdown;
- parar novos claims antes de finalizar safe checkpoint;
- nenhuma task asyncio órfã.

### 5.15 Administrative Dataset DELETE — direção aprovada, ADR separado

Administrative Dataset DELETE não pertence semanticamente a Forget e terá
`ADR-0010 — Administrative Dataset Deletion Contract` antes de SM-515.

Direções já aprovadas:

- endpoint:
  `DELETE /api/v1/datasets/{dataset_id}`;
- lifecycle lógico:
  `ACTIVE → DELETING → DELETED`;
- a row `Dataset` é preservada como tombstone `DELETED`;
- o conteúdo/storage/projeção do Dataset é removido conforme contrato;
- `main` não pode sofrer DELETE administrativo no MVP;
- limpeza de memória/conteúdo de `main` continua sendo feita por Forget;
- usar novo `PipelineType.DATASET_DELETE`, com migration do enum PostgreSQL;
- não implementar o endpoint como alias de `POST /api/v1/forget`.

ADR-0010 deve congelar:

- semântica detalhada de tombstone;
- reuse futuro de name/slug;
- comportamento de Dataset vazio versus com artifacts;
- idempotência;
- response HTTP;
- cancel/retry;
- retenção de histórico e FKs;
- interação com runs existentes.

### 5.16 Cognify `rebuild=true`

Decisão aprovada:

- `rebuild=true` significa rebuild da memória **do Dataset inteiro** em nova generation;
- quando `rebuild=true`, `source_ids` deve ser omitido/rejeitado;
- construir generation `N+1` sem expor estado parcial;
- `datasets.active_generation` muda atomicamente somente ao final bem-sucedido;
- não confundir Cognify rebuild com `GraphRebuildService`, que reconstrói somente a
  projeção Neo4j descartável.

### 5.17 Consequências para migrations B5

Com as decisões acima, migrations B5 deixam de ser "preventivas" e passam a ter
necessidades concretas que o ADR deve detalhar:

- durable retry schedule (`next_attempt_at` ou equivalente);
- vínculo de manual retry (`retry_of_run_id` ou equivalente);
- lease/recovery de `graph_outbox.processing`;
- novo `PipelineType.DATASET_DELETE`;
- índices/constraints necessários aos claims e recovery.

Não criar outras colunas/estados sem necessidade comprovada.


## 6. Ordem de execução B5

```text
B5
SM-501  Architecture Gate — Worker, Queue and Pipeline Lifecycle Contract / ADR-0009
  ↓
SM-502  PipelineRun/PipelineStep operational persistence and state transitions
  ↓
SM-503  PostgreSQL queue claiming and dataset/global serialization
  ↓
SM-504  Resumable pipeline engine, registry and persisted step execution
  ↓
SM-505  Internal worker lifecycle, polling, heartbeat and graceful shutdown
  ↓
SM-506  Autonomous graph_outbox processing and crash recovery
  ↓
SM-507  Stale PipelineRun/PipelineStep startup recovery
  ↓
SM-508  Runs read API
  ↓
SM-509  Shared async submission and wait contract
  ↓
SM-510  Cognify operational async + rebuild/new generation
  ↓
SM-511  Improve operational async
  ↓
SM-512  Forget operational async
  ↓
SM-513  Remember mode=full + async text/file/url
  ↓
SM-514  Run cancellation and manual retry API
  ↓
SM-515  Administrative asynchronous Dataset DELETE
  ↓
SM-516  Worker observability, readiness and security hardening
  ↓
GATE-B5  MVP operacional assíncrono
```

SM-501..SM-516 estão DONE. GATE-B5 — PASSED.

Dependências cruzadas podem ser refinadas após ADR-0009, mas não devem ser reordenadas silenciosamente se isso alterar o contrato público ou o gate.

---

# 7. Stories B5

## SM-501 — Architecture Gate — Worker, Queue and Pipeline Lifecycle Contract

**Status:** DONE
**Prioridade:** P0
**Dependências:** GATE-B4 PASSED
**PRD:** FR-001, FR-010, FR-020, FR-050, FR-090, FR-100, FR-120; seções 14.3, 15, 19, 21 e 25
**ADR:** criar ADR-0009

### Objetivo

Congelar todas as decisões estruturais necessárias para que worker, queue claiming, recovery, retry, cancel e async API sejam implementados sem semântica implícita ou concorrência ad hoc.

### Entregáveis esperados

- `docs/adr/0009-worker-queue-and-pipeline-lifecycle-contract.md`;
- lifecycle de run e step compatível com as decisões congeladas na seção 5;
- claim PostgreSQL;
- serialização por `dataset_id` + global barrier;
- heartbeat/stale derivado;
- durable retry schedule;
- manual retry como novo run;
- retry/cancel;
- `wait=true`/`wait=false` com timeout → `202`;
- async idempotency sem `wait` na work identity;
- graph_outbox lease/recovery;
- comportamento de `WORKER_ENABLED=false`;
- proibição explícita de usar fire-and-forget `asyncio.create_task` ou queue em memória como durability boundary;
- regra para evitar self-deadlock/reclaim do mesmo dataset por steps internos;
- migrations operacionais necessárias detalhadas.

Administrative Dataset DELETE fica reservado a ADR-0010/SM-515 e não deve inflar ADR-0009.

### Não fazer

- não implementar worker;
- não criar migration sem decisão ADR;
- não alterar endpoints;
- não converter os services B4 ainda;
- não introduzir queue externa.

### Critérios de aceite

- todas as decisões da seção 5 foram registradas no ADR sem reabrir as direções já aprovadas;
- nenhuma contradição relevante PRD/schema atual fica escondida;
- ADR respeita B4 e ADR-0008;
- caminho de implementação SM-502..SM-516 fica determinístico.

---

## SM-502 — PipelineRun/PipelineStep operational persistence and state transitions

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-501
**PRD:** FR-100; seções 12.13 e 12.14

### Objetivo

Transformar os models já existentes em uma API de persistência operacional completa para o worker, sem polling ainda.

### Implementação esperada

- repository/service de lifecycle de `PipelineRun`;
- repository de `PipelineStep`;
- criação de run `queued`;
- transition guards conforme ADR-0009;
- timestamps coerentes;
- `progress`/`current_step`;
- `worker_id`/`heartbeat_at`;
- attempts;
- errors/metrics;
- criação/listagem/update de steps;
- queries necessárias para recovery e API de Runs;
- migrations necessárias ao durable retry, manual retry e índices de lifecycle conforme ADR-0009; não criar `STALE` apenas para heartbeat vencido.

### Não fazer

- não criar polling loop;
- não executar pipeline;
- não iniciar worker no lifespan;
- não implementar endpoints ainda.

### Critérios de aceite

- transições inválidas falham deterministicamente;
- estado não depende de memória do processo;
- concorrência/update races relevantes possuem testes PostgreSQL reais;
- migrations, se existirem, são reversíveis e justificadas pelo ADR.

### Decisão de sequenciamento — partial unique operational constraint deferida

ADR-0009 §D exige, como invariante final do runtime B5, um backstop físico em
PostgreSQL equivalente a `UNIQUE(dataset_id) WHERE dataset_id IS NOT NULL AND
status IN ('running', 'cancelling')`. A migration `0008` desta story adiciona
`pipeline_runs.next_attempt_at`, `pipeline_runs.retry_of_run_id` e o unique
`(run_id, ordinal)` de `pipeline_steps`, mas **não** ativa fisicamente esse
backstop.

Motivo comprovado empiricamente: os write pipelines B4 ainda síncronos
(`forget.py`, `remember.py`, `cognify.py`, `improve.py`) criam `PipelineRun`
diretamente em `RUNNING` e resolvem conflito de concorrência por checagem em
nível de aplicação **após** esse insert (ex.: `forget_dataset` cria a run
RUNNING e só depois verifica `find_running_forget_for_dataset_except`).
Ativar a constraint agora rejeita esse insert antes da checagem da aplicação
rodar, quebrando cenários reentrantes/concorrentes já cobertos por testes
PostgreSQL reais existentes (`test_forget_postgres_integration.py`).

Esta é uma decisão de sequenciamento de rollout, não uma revisão do
invariante do ADR-0009 — que permanece Accepted e inalterado. A ativação
física fica registrada como obrigação de cutover em SM-513 (ver abaixo) e
verificação obrigatória em GATE-B5.

---

## SM-503 — PostgreSQL queue claiming and dataset/global serialization

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-502
**PRD:** FR-100.5–8; NFR-002

### Objetivo

Implementar a seleção/claim transacional de runs queued e garantir exclusão operacional de writes conflitantes.

### Implementação esperada

- `FOR UPDATE SKIP LOCKED`;
- claim somente de runs elegíveis;
- ordenação determinística;
- commit do claim antes da execução;
- proteção contra double claim;
- serialization por `pipeline_runs.dataset_id` autoritativo;
- resolução/criação do Dataset antes do enqueue de write dataset-scoped;
- no máximo um write por dataset lógico;
- concorrência entre datasets limitada por configuração;
- global/everything barrier conforme ADR;
- release/recovery sem lock de longa duração.

### Não fazer

- não manter transaction/row lock durante LLM/embedding/Neo4j;
- não usar mutex somente em memória como fonte de exclusão;
- não criar worker loop ainda.

### Critérios de aceite

- dois claimers concorrentes não recebem o mesmo run;
- dois runs do mesmo dataset não ficam `running` simultaneamente;
- runs de datasets distintos podem ser claimados conforme limite;
- operação global respeita exclusão definida no ADR;
- teste de concorrência usa PostgreSQL real.

---

## SM-504 — Resumable pipeline engine, registry and persisted step execution

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-502, SM-503
**PRD:** seção 14.3; FR-100

### Objetivo

Criar o engine interno que executa pipelines versionados em código e persiste o lifecycle de cada step.

### Implementação esperada

- `pipelines/engine.py` ou estrutura equivalente;
- registry fechado em código;
- `PipelineContext`;
- handlers por `PipelineType`/versão;
- materialização de `PipelineStep`;
- execução sequencial/segura;
- `current_step`/progress;
- skip/resume de succeeded steps conforme ADR;
- `input_hash` quando aplicável;
- erro retryable/permanent;
- attempts;
- backoff/jitter conforme ADR;
- cancellation checkpoints;
- resultado final do run.

### Não fazer

- não aceitar pipeline enviado pelo cliente;
- não usar import dinâmico controlado por input;
- não iniciar polling worker;
- não duplicar regras de negócio B4 dentro do engine.

### Critérios de aceite

- engine pode executar pipeline fake/determinístico com steps persisted;
- crash/restart simulado consegue retomar a partir do estado persistido conforme ADR;
- step succeeded não é duplicado indevidamente;
- erro permanente finaliza run corretamente;
- retryable respeita attempts e política de retry.

---

## SM-505 — Internal worker lifecycle, polling, heartbeat and graceful shutdown

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-503, SM-504
**PRD:** FR-001, FR-100, FR-120; seção 21

### Objetivo

Adicionar o worker interno ao mesmo processo FastAPI e conectá-lo ao lifespan.

### Implementação esperada

- worker coordinator interno;
- start no lifespan após recursos obrigatórios;
- worker identity por boot;
- polling conforme configuração;
- claim + dispatch;
- concorrência limitada;
- heartbeat periódico;
- stop de novos claims durante shutdown;
- safe completion/checkpoint das tasks em andamento;
- cleanup de tasks;
- comportamento `WORKER_ENABLED` conforme ADR.

### Não fazer

- não criar container/processo worker separado;
- não criar scheduler externo;
- não transformar reads em jobs.

### Critérios de aceite

- worker inicia e para deterministicamente;
- run queued é executado pelo engine;
- heartbeat é atualizado;
- shutdown não deixa task asyncio órfã;
- nenhum lock DB longo permanece durante I/O.

---

## SM-506 — Autonomous graph_outbox processing and crash recovery

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-501, SM-505
**PRD:** FR-050, FR-100; NFR-004
**ADR:** ADR-0008 + ADR-0009

### Objetivo

Fechar autonomamente o gap `PostgreSQL commit OK → processo morre antes do drain Neo4j`, sem depender de uma nova request manual.

### Implementação esperada

- processamento autônomo de outbox `pending`/`failed`;
- claim seguro conforme contrato ADR;
- recovery de row stale em `processing` por lease/timestamp persistido introduzido por migration;
- dependency order;
- retries controlados;
- reaproveitar `GraphOutboxProcessor`/ProjectionCommand;
- crash após Neo4j apply e antes de `mark_done` converge idempotentemente;
- cooperação clara com drains explícitos dos pipelines durante migração B5.

### Não fazer

- não mudar identidade/payload ADR-0008 sem novo ADR;
- não usar rebuild como substituto silencioso de outbox;
- não usar wipe global Neo4j;
- não criar placeholder nodes.

### Critérios de aceite

- pending abandonado é processado sem nova request;
- processing stale volta a estado processável conforme lease/recovery ADR-0009;
- duplicate delivery não duplica projection;
- external Neo4j data permanece intacto;
- PostgreSQL continua autoridade.

---

## SM-507 — Stale PipelineRun/PipelineStep startup recovery

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-504, SM-505
**PRD:** FR-001.8, FR-100.9–12; NFR-004

### Objetivo

Implementar recovery de runs abandonados após término anormal de processo, incluindo a dívida explícita herdada do B4.

### Implementação esperada

- scan/recovery no startup ou componente equivalente definido no ADR;
- detecção por `heartbeat_at` + stale threshold;
- `RUNNING` stale como classificação derivada, sem novo enum `STALE`;
- `CANCELLING` stale preservando intenção de cancelamento;
- step `RUNNING` stale;
- resume/retry/fail conforme tipo de step;
- `config_fingerprint` incompatível → fail seguro, sem resume automático;
- atualização segura de worker ownership;
- scenario Forget stale que não bloqueia retry indefinidamente.

### Não fazer

- não considerar stale apenas por `created_at`;
- não roubar run com heartbeat válido;
- não duplicar side effects já committed;
- não implementar timeout arbitrário específico de Forget fora do runtime comum.

### Critérios de aceite

- kill/restart real recupera run conforme ADR;
- run saudável não é tocado;
- stale Forget deixa de produzir bloqueio permanente;
- recovery é idempotente em restart repetido.

---

## SM-508 — Runs read API

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-502
**PRD:** UC-10; FR-100; seção 11.2 Runs

### Objetivo

Expor observabilidade pública e segura do lifecycle durável.

### Endpoints

```http
GET /api/v1/runs
GET /api/v1/runs/{run_id}
```

### Implementação esperada

- paginação;
- filtros seguros por status/type/dataset quando apropriado;
- `run_id`;
- pipeline type;
- status;
- progress;
- current step;
- attempt;
- timestamps;
- error code/message seguros;
- metrics seguras;
- steps no detail ou representação equivalente;
- envelope padrão + API key.

### Não expor

- secrets;
- raw provider payload;
- documento completo;
- embedding;
- DB URL;
- storage path interno;
- stack trace.

### Critérios de aceite

- queued/running/terminal são observáveis;
- paginação/filtros não vazam dados internos;
- OpenAPI contém somente o contrato previsto;
- status/steps refletem PostgreSQL, nunca memória transitória do worker.

---

## SM-509 — Shared async submission and wait contract

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-505, SM-508
**PRD:** convenções HTTP; FR-100

### Objetivo

Criar a camada comum de submissão de writes para que endpoints B5 usem um único caminho operacional.

### Implementação esperada

`wait=false`:

- persist/resolve idempotency;
- run `queued` antes da resposta;
- `202 Accepted`;
- `run_id` + status.

`wait=true`:

- submete o mesmo run;
- aguarda terminal state pelo contrato ADR;
- respeita `REQUEST_WAIT_TIMEOUT_SECONDS`;
- timeout não mata automaticamente o run;
- timeout antes de terminal state retorna `202 Accepted` com o mesmo `run_id` e status atual;
- resposta terminal preserva resultado público aplicável.

Também cobrir:

- idempotency em queued/running/succeeded/failed/cancelling/cancelled;
- `wait` excluído do payload hash/work identity;
- worker disabled/unready: write retorna `503`; não executar fallback sync;
- helper/waiter não depende exclusivamente de Event em memória.

### Não fazer

- não manter dois engines sync/async;
- não executar service B4 inline quando o endpoint já migrou para o runtime B5.

### Critérios de aceite

- mesma operação com wait true/false produz o mesmo lifecycle durável;
- `202` é devolvido antes da conclusão real;
- retry HTTP/idempotency não cria run duplicado.

---

## SM-510 — Cognify operational async + rebuild/new generation

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-509
**PRD:** FR-050; endpoint Cognify

### Objetivo

Migrar Cognify para o runtime B5 sem alterar os algoritmos funcionais aprovados no B4 e fechar `rebuild=true`.

### Implementação esperada

- `wait=false` real;
- `wait=true` pelo mesmo run/worker;
- `source_ids`;
- seleção de fontes pending;
- steps persisted;
- progress;
- provider calls fora de transaction longa;
- graph_outbox;
- failure/retry/recovery;
- `rebuild=true` cria nova generation para o Dataset inteiro;
- `rebuild=true` rejeita `source_ids` explícito;
- generation `N+1` permanece invisível até ativação atômica final;
- Cognify rebuild não é GraphRebuildService/Neo4j rebuild.

### Não fazer

- não mudar chunking/extraction/summaries B4 sem causa comprovada;
- não criar um segundo `COGNIFY` run interno quando já existe top-level run;
- não usar Neo4j como autoridade de generation.

### Critérios de aceite

- async Cognify E2E;
- wait true e false compartilham engine;
- rebuild não expõe generation parcial;
- crash/retry não duplica artifacts ativos.

---

## SM-511 — Improve operational async

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-509
**PRD:** FR-070

### Objetivo

Migrar Improve explícito para o worker B5.

### Implementação esperada

- wait false/true pelo mesmo lifecycle;
- stages persistidos como steps ou agrupamento coerente definido pelo ADR;
- `feedback_weights`;
- `entity_deduplication`;
- `relation_embeddings`;
- `summaries`;
- `graph_reconciliation`;
- retry/cancel entre stages;
- progress/report final;
- graph_outbox recovery.

### Invariante crítica

A correção do GATE-B4 deve permanecer:

```text
reconcile → maintain → drain
```

Não reintroduzir `maintain → drain → reconcile`.

### Não fazer

- não criar cron/auto-improve;
- não alterar limiares/algoritmos sem story própria.

### Critérios de aceite

- cada stage pode ser observado no run/steps;
- restart em safe point converge;
- segunda execução continua idempotente quando o stage for idempotente por contrato.

---

## SM-512 — Forget operational async

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-509, SM-507
**PRD:** FR-090

### Objetivo

Migrar toda a semântica B4 de Forget para execução operacional assíncrona e recuperável.

### Escopos

- source full;
- source memory-only;
- dataset full;
- dataset memory-only;
- everything.

### Implementação esperada

- `wait=false` real;
- `wait=true` mesmo runtime;
- locks/serialization B5;
- storage deletion segura;
- graph_outbox;
- confirmação exata Everything;
- recovery de falha parcial;
- cancellation apenas em safe points;
- integração com stale recovery;
- intent/retry já existente preservado ou simplificado somente se o novo runtime provar equivalência melhor.

### Não fazer

- não confundir Forget Dataset com administrative Dataset DELETE;
- não global-wipe Neo4j;
- não apagar storage antes do ponto durável correto.

### Critérios de aceite

- crash em pontos críticos é recuperável;
- stale run não bloqueia target indefinidamente;
- Everything permanece dataset-authoritative e preserva dados externos Neo4j;
- `main` não é criado por Forget.

---

## SM-513 — Remember mode=full + async text/file/url

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-510, SM-509
**PRD:** UC-01, UC-02, UC-03; FR-020; Remember API

### Objetivo

Completar o contrato público de Remember com `mode=full` e execução assíncrona, reutilizando o runtime Cognify sem nested top-level runs.

### Entradas

- text;
- TXT/Markdown;
- JSON/CSV/HTML;
- textual PDF;
- DOCX;
- HTTPS URL.

### Modos

`mode=ingest`:

- persist source/document;
- terminal após ingestão.

`mode=full`:

- ingest;
- cognify steps;
- projection;
- resultado final completo.

### Implementação esperada

- wait false/true;
- `Idempotency-Key` em queued/running/succeeded;
- `force`;
- storage;
- source/document identities;
- resultado terminal compatível com o contrato público;
- nenhuma duplicação de pipeline Cognify interno.

### Não fazer

- não reimplementar loaders B4;
- não criar nested top-level `COGNIFY` run; `mode=full` permanece um único top-level `REMEMBER` run;
- não processar `mode=full` inline fora do worker.

### Critérios de aceite

- Remember/full text/file/url E2E;
- 202 imediato em wait=false;
- wait=true usa exatamente o mesmo run;
- duplicate submission não duplica Source/memória/run indevidamente.

### Obrigação de cutover — ativar a partial unique operational constraint

SM-502 deferiu deliberadamente a ativação física do backstop de ADR-0009 §D
(`UNIQUE(dataset_id) WHERE dataset_id IS NOT NULL AND status IN ('running',
'cancelling')`) porque Forget/Remember/Cognify/Improve B4 ainda criavam
`PipelineRun` diretamente em `RUNNING` fora do claimant.

Após esta story (Remember migrado) e com Cognify/Improve/Forget já operando
pelo engine B5 (SM-510/SM-511/SM-512), esta é a obrigação de fechamento antes
do GATE-B5:

- confirmar por inspeção que nenhum public write pipeline cria mais
  `PipelineRun` diretamente em `RUNNING` fora do claimant do SM-503;
- criar uma nova migration adicionando o unique parcial
  `UNIQUE(dataset_id) WHERE dataset_id IS NOT NULL AND status IN ('running',
  'cancelling')` (ou equivalente aprovado no ADR) sobre `pipeline_runs`;
- reintroduzir/expandir a cobertura de teste PostgreSQL real removida em
  SM-502 para essa constraint (bloqueio de segunda run RUNNING/CANCELLING no
  mesmo dataset, convivência com múltiplos QUEUED, liberação após terminal).

Número de migration não reservado agora.

---

## SM-514 — Run cancellation and manual retry API

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-507, SM-508, SM-509
**PRD:** FR-100; seção 11.2 Runs

### Endpoints

```http
POST /api/v1/runs/{run_id}/retry
POST /api/v1/runs/{run_id}/cancel
```

### Objetivo

Expor os controles operacionais congelados pelo ADR-0009.

### Cancel

- queued → terminal conforme ADR;
- running → cancelling;
- worker observa cancellation em safe checkpoint;
- critical transaction não é interrompida;
- estado final `cancelled` quando possível;
- conflito/terminal retorna erro estável ou no-op conforme ADR.

### Retry

- somente estados permitidos;
- run/step attempt correto;
- resume/restart conforme ADR;
- não duplicar side effects committed;
- idempotency preservada;
- manual retry cria novo `PipelineRun`, com vínculo persistido ao run anterior conforme ADR-0009.

### Critérios de aceite

- cancel queued;
- cancel running em pipeline controlável;
- retry de falha transitória/permanente conforme contrato;
- API nunca promete cancel imediato no meio de transação/provider call não-interrompível.

---

## SM-515 — Administrative asynchronous Dataset DELETE

**Status:** DONE
**Prioridade:** P0
**Dependências:** SM-512, SM-514
**PRD:** FR-010; seção 11.2 Datasets
**ADR:** criar/aceitar ADR-0010 antes da implementação

### Endpoint

```http
DELETE /api/v1/datasets/{dataset_id}
```

### Objetivo

Implementar a deleção administrativa de Dataset que ficou explicitamente pendente após SM-423/B4.

### Implementação esperada

- lifecycle `ACTIVE → DELETING → DELETED` conforme ADR-0010;
- row `Dataset` preservada como tombstone `DELETED`;
- Dataset vazio;
- Dataset com artifacts;
- operação assíncrona quando houver artifacts;
- storage;
- authoritative artifacts;
- graph projection;
- runs/audit;
- bloqueio de Remember/Cognify/Improve/Forget incompatíveis durante lifecycle;
- idempotência;
- `main` protegido contra DELETE administrativo no MVP;
- response HTTP estável;
- novo `PipelineType.DATASET_DELETE`, incluindo migration do enum PostgreSQL.

### Não fazer

- não implementar como alias de `POST /forget`;
- não remover fisicamente a row Dataset; preservar tombstone/auditoria;
- não usar Neo4j para descobrir artifacts authoritative.

### Critérios de aceite

- delete com artifacts retorna lifecycle assíncrono correto;
- Dataset não aceita novos writes após entrar no estado administrativo de deleção;
- storage/projection convergem;
- retry/cancel seguem regras explícitas;
- external Neo4j data é preservado;
- `main` rejeita DELETE administrativo de forma estável.

---

## SM-516 — Worker observability, readiness and security hardening

**Status:** DONE
**Commit:** `b2528af` — `feat(runtime): harden worker observability and readiness`
**Prioridade:** P0
**Dependências:** SM-505..SM-515
**PRD:** FR-120; seção 23; NFR-004/NFR-005/NFR-006

### Objetivo

Fechar o runtime operacional com readiness, métricas, logs e testes de segurança/restart apropriados ao worker.

### Implementação esperada

- `/health/ready` exige worker operacional quando `WORKER_ENABLED=true` e permanece false quando o runtime B5 está disabled;
- worker start failure é observável;
- queue pending metrics;
- runs por status;
- step duration;
- heartbeat stale metric;
- graph_outbox pending/processing/failed;
- logs estruturados com:
  - run_id;
  - step;
  - dataset_id;
  - source_id;
  - worker_id seguro quando aplicável;
  - duration_ms;
  - error_code;
- shutdown/restart tests;
- worker disabled behavior;
- OpenAPI final;
- security tests de redaction.

### Não logar/expor

- API key;
- provider keys;
- DB/Neo4j password;
- document content completo;
- embedding;
- raw prompt/provider payload;
- storage path interno;
- traceback interno em resposta pública.

### Não fazer

- não adicionar tracing distribuído obrigatório;
- não adicionar stack externa de métricas como requisito funcional se o PRD não exigir;
- não criar novo serviço de deployment para worker.

### Critérios de aceite

- readiness reflete runtime real;
- observabilidade permite diagnosticar run stuck/retry sem conteúdo sensível;
- shutdown e restart são cobertos em integração;
- nenhuma rota proibida ou secret leak aparece no OpenAPI/logs.

---

# 8. GATE-B5 — MVP operacional assíncrono

**Status:** DONE / PASSED
**Prioridade:** P0
**Dependências:** SM-501..SM-516
**Tipo:** FUNCTIONAL / INTEGRATION / RECOVERY GATE

## Objetivo

Provar que o Sofias Memory opera como MVP assíncrono durável em uma única aplicação API+worker, com PostgreSQL como fila/estado/autoridade e Neo4j reconstruível.

## Critérios obrigatórios

### A. Baseline e arquitetura

- HEAD/worktree limpos;
- ADR-0009 aceito;
- nenhuma queue externa;
- nenhuma segunda aplicação worker;
- exactly-one-replica continua a configuração suportada do MVP;
- `uv lock --check`;
- `ruff check`;
- `ruff format --check`;
- `mypy`;
- `pytest`;
- `git diff --check`.

### B. Submission e lifecycle

- `wait=false` retorna `202` antes da conclusão;
- run já existe duravelmente como `queued` antes da resposta;
- worker faz claim;
- lifecycle `queued → running → succeeded` comprovado;
- PipelineSteps persistidos;
- progress/current_step coerentes;
- Runs API observa o lifecycle real;
- `wait=true` usa o mesmo runtime/engine;
- timeout de wait segue ADR sem cancelar o run.

### C. Remember/full

- text `mode=full` async;
- file `mode=full` async;
- URL `mode=full` async;
- projection Neo4j;
- Recall posterior encontra memória;
- idempotency em queued/running/succeeded;
- nenhum nested top-level Cognify run para a mesma request Remember/full.

### D. Cognify

- wait=false;
- source_ids;
- pending sources;
- rebuild=true/new generation;
- generation parcial não fica authoritative;
- retry/restart seguro.

### E. Improve

- wait=false;
- stages observáveis;
- graph_reconciliation mantém `reconcile → maintain → drain`;
- retry/cancel entre safe points;
- projection final convergente.

### F. Forget

- source async;
- source memory-only async;
- dataset async;
- dataset memory-only async;
- Everything async;
- confirmação exata;
- storage e projection corretos;
- stale Forget não bloqueia permanentemente;
- dados externos Neo4j preservados.

### G. Queue concurrency

- dois claimers não executam o mesmo run;
- same-dataset writes nunca executam simultaneamente;
- datasets diferentes podem executar conforme `WORKER_MAX_CONCURRENT_DATASETS`;
- operação global respeita barrier;
- nenhum DB lock fica aberto durante chamada de provider/Neo4j;
- a partial unique operational constraint de ADR-0009 §D
  (`UNIQUE(dataset_id) WHERE dataset_id IS NOT NULL AND status IN ('running',
  'cancelling')`), deliberadamente deferida em SM-502 e ativada conforme a
  obrigação de cutover de SM-513, está fisicamente presente em
  `pipeline_runs` e comprovada por teste PostgreSQL real.

### H. Retry

- step com erro transitório é reexecutado conforme política;
- attempt incrementa corretamente;
- backoff/jitter não quebra restart recovery;
- erro permanente finaliza failed;
- manual retry funciona conforme ADR;
- side effects já committed não duplicam.

### I. Cancellation

- cancel queued;
- cancel running;
- `cancelling` observável quando aplicável;
- cancel ocorre em safe checkpoint;
- transação crítica não é interrompida no meio;
- restart durante cancelling converge.

### J. Stale/restart recovery

- matar o processo durante run;
- reiniciar a mesma aplicação;
- stale detectado por heartbeat;
- run é retomado/reavaliado conforme ADR;
- run saudável não é roubado;
- fingerprint incompatível segue decisão ADR;
- stale Forget recovery comprovado.

### K. Graph outbox autonomous recovery

- crash após PostgreSQL commit e antes de Neo4j apply;
- worker recupera outbox sem nova request;
- crash após Neo4j apply e antes de mark done;
- replay idempotente;
- processing stale recuperado;
- rebuild não é usado como atalho para esconder outbox quebrada;
- external Neo4j sentinel preservado.

### L. Administrative Dataset DELETE

- Dataset vazio;
- Dataset com artifacts;
- lifecycle assíncrono quando aplicável;
- novos writes bloqueados enquanto deleting/deleted;
- storage removido;
- projection removida por escopo;
- PostgreSQL authority;
- retry/cancel conforme contrato;
- `POST /forget` permanece operação distinta.

### M. Worker lifecycle/readiness

- worker inicia somente quando permitido pelas dependências;
- readiness fica true somente no estado definido pelo ADR;
- worker disabled segue contrato explícito;
- graceful shutdown para novos claims;
- restart não perde estado;
- nenhuma task asyncio órfã.

### N. Segurança

- run input/output/metrics não expõem documento completo sem necessidade;
- secrets ausentes de logs/API;
- storage path interno ausente das respostas;
- erro worker/provider não vaza traceback;
- API key permanece obrigatória em Runs/Dataset DELETE;
- OpenAPI sem rotas proibidas.

### O. PostgreSQL authority

Provar explicitamente:

1. lifecycle do trabalho sobrevive restart porque está no PostgreSQL;
2. Neo4j ausente/divergente pode convergir por outbox/rebuild sem virar authority;
3. worker não depende de estado in-memory para decidir o que já foi committed;
4. historical outbox não é source of truth do grafo;
5. Dataset/source/artifacts são determinados pelo PostgreSQL.

## Ambiente do gate

- PostgreSQL descartável dedicado;
- Neo4j real com UUID-scoped cleanup;
- sentinel externo Neo4j;
- storage temporário exclusivo;
- provider real apenas nos cenários que realmente exigem LLM/embeddings;
- nenhum wipe global;
- nenhum reset de DB de desenvolvimento;
- kill/restart controlado do processo da aplicação para cenários de recovery.

## Resultado esperado

Se todas as validações passarem:

```text
GATE-B5 PASSED — MVP OPERACIONAL ASSÍNCRONO CONCLUÍDO
```

## Nota de fechamento

`GATE-B5` foi executado e aprovado (`GATE-B5 PASSED — MVP OPERACIONAL ASSÍNCRONO
CONCLUÍDO`). Baseline: `b2528af` (`feat(runtime): harden worker observability and
readiness`, SM-516). Evidência combinada:

- **A→O**: PASS. Auditoria estática confirmou exatamente uma aplicação FastAPI, worker
  interno no mesmo processo, exatamente cinco `PipelineType` (`remember, cognify,
  improve, forget, dataset_delete`), nenhuma fila/broker externo, nenhuma rota
  proibida no OpenAPI, e nenhum writer público criando `PipelineRun` diretamente
  `RUNNING` (apenas o claimant executa `QUEUED → RUNNING`).
- **PostgreSQL real**: schema fresco migrado `0001 → 0011` e auditado fisicamente via
  catálogo (`pg_indexes`/`pg_type`/`pg_enum`) — enum `pipeline_type` com os cinco
  valores, índice parcial único `uq_pipeline_runs_dataset_id_operational` com o
  predicado exato `WHERE (dataset_id IS NOT NULL) AND (status IN ('running',
  'cancelling'))`; downgrade `NotImplementedError` de `0011` confirmado documentado,
  não contornado.
- **Neo4j real**: sentinel externo `SmokeExternalControl` verificado presente antes e
  depois de toda a execução — nunca wipeado.
- **Provider real (E2E ao vivo)**: `POST /remember` (mode=full) executado contra
  OpenAI real (`gpt-5-mini` + `text-embedding-3-large`) — extração real de 5 entidades
  e 6 relações, 1 chunk, projeção Neo4j real; `POST /recall` (chunks e graph) recuperou
  a memória com resposta gerada real e cadeia de proveniência completa; `DELETE
  /datasets/{id}` (Administrative Dataset Delete) executado ao vivo, 5 steps
  concluídos, projeção convergida de volta ao sentinel externo.
- **Kill/restart real de processo**: novo teste
  (`tests/integration/test_process_kill_recovery_postgres_integration.py` +
  `_process_kill_child.py`) derruba um processo filho real via `SIGKILL`/
  `TerminateProcess` (nunca `worker.stop()`) enquanto um step está em execução, e prova
  — por timestamps reais capturados de um segundo processo com novo `worker_id` — que
  `recovery_finished < first_claim_by_B`, com o run abandonado sendo reconciliado
  (`WORKER_LOST` → retry → reclaim) sem efeito colateral duplicado. 3/3 execuções
  determinísticas.
- **Queue/concorrência, retry automático/manual, cancelamento, outbox
  crash/replay, Dataset Delete**: suítes SM-505..515 completas passando contra os 16
  bancos PostgreSQL dedicados por família já provisionados neste ambiente (mais
  Neo4j real onde aplicável).
- **Segurança**: nova suíte SSRF dedicada
  (`tests/security/test_url_ssrf.py`, 32/32) cobrindo os 12 cenários exigidos
  (loopback literal, `localhost`, hostname privado, RFC1918, link-local/metadata
  IPv4+IPv6, IPv6 público aceito, redirect público→privado rejeitado com
  re-resolução independente por hop, DNS multi-resposta com IP privado, userinfo na
  URL, esquema não suportado, limite de tamanho declarado e por stream adversarial) —
  incluindo prova de que o único caller (`PrepareAndIngestStep`) nunca vaza a URL/razão
  interna em caso de rejeição. `bandit`: 0 High, 1 Medium triado como não aplicável
  (bind `0.0.0.0` é o comportamento pretendido para o container single-process,
  AGENTS.md §22). `pip-audit`: 1 achado (`pytest`/`PYSEC-2026-1845`) triado como não
  aplicável — dependência apenas de dev, nunca embarcada na imagem de produção.
- **OpenAPI**: 24 operações privadas com `security: [{"ApiKeyAuth": []}]`; rotas de
  health isentas; nenhuma rota proibida presente.
- **Cobertura NFR-006**: `sofias_memory.domain` + `sofias_memory.pipelines` = 85%
  (2049 stmts, 228 miss) — acima do mínimo de 80%. Cobertura total do pacote: 88%.
- **Hygiene de dependência**: `prometheus-client` confirmado sem nenhum import em
  todo o repositório e removido de `[project.dependencies]`; `uv.lock` regenerado e
  `uv lock --check` verde.
- **Suíte completa**: 1718 passed, 0 failures reais. 8 testes
  (`test_api_errors.py`, `test_request_metrics_middleware.py`) mostraram-se
  sensíveis a timing de captura de log apenas sob a suíte massiva completa — 3/3
  execuções determinísticas em isolamento confirmam que não é regressão, seguindo a
  mesma ressalva já documentada por SM-516 (§42 do gate).
- **`test_b3_neo4j_gate.py`** (backend `testcontainers`) não executável neste
  ambiente por ausência de daemon Docker — limitação de ambiente, não do código;
  excluído da pontuação PASS/FAIL.
- **Achado do gate corrigido**: `test_graph_maintenance_postgres_and_outbox_share_transaction`
  tinha uma expectativa obsoleta (`relations_deactivated == 1`) que não refletia a
  semântica correta e já vigente de `GraphMaintenanceService` (relações sem evidência
  autoritativa são desativadas — confirmado por seu teste-irmão
  `test_authoritative_evidence_query_uses_active_current_dataset_scope`, que já
  passava). O fixture `insert_transaction_fixture` não insere nenhuma evidência, logo
  as cinco relações do dataset-alvo são corretamente desativadas por hygiene; apenas o
  teste foi corrigido (`relations_deactivated == 5`, com asserções explícitas de que
  as cinco relações do dataset-alvo ficam inativas e a relação do outro dataset
  permanece ativa) — `GraphMaintenanceService` em produção não foi alterado.
- **Achados de teste corrigidos (pré-existentes, não introduzidos por B5)**:
  `test_postgres_migration_gate.py` referenciava o nome de índice pré-0008
  (`ix_pipeline_steps_run_id_ordinal`, renomeado por `0008` para
  `uq_pipeline_steps_run_id_ordinal`) e não tratava o downgrade intencionalmente
  `NotImplementedError` de `0011` (ADR-0010 D34) — ambos corrigidos no próprio arquivo
  de teste, sem qualquer mudança de contrato ou de código de produção.

Nenhum `BLOCKER`/`MAJOR` permanece aberto.

---

## 9. Fora do escopo B5

Mesmo após GATE-B5, continuam fora da versão 1/MVP:

- múltiplas réplicas suportadas;
- cluster de workers;
- high availability;
- Redis/Celery/RabbitMQ/Kafka/SQS;
- scheduler/cron de produto;
- auto-Improve contínuo;
- frontend;
- MCP;
- users/login;
- tenant/ACL/roles/permissions;
- API key management;
- billing/quota;
- sync entre instâncias;
- cloud client;
- plugin system;
- integrations externas de produto;
- arbitrary pipelines;
- arbitrary SQL/Cypher;
- alternative DB providers;
- S3/object storage remoto;
- export/import, salvo novo milestone explícito;
- distributed tracing obrigatório;
- multi-worker fairness/leader election de cluster;
- suporte oficial a multi-réplica.

---

## 10. Definition of Done mínima por task B5

Uma task B5 só pode ser marcada `DONE` quando:

- escopo implementado sem antecipar story futura;
- ADR-0009 e demais ADRs aplicáveis respeitados;
- PostgreSQL permanece fonte de verdade;
- Neo4j permanece projeção reconstruível;
- lifecycle/transições afetadas têm testes;
- concorrência afetada tem testes reais quando relevante;
- retry/recovery afetados têm teste de falha/restart quando relevante;
- testes unitários aplicáveis passam;
- testes PostgreSQL/Neo4j reais opt-in passam quando a story exigir;
- contratos públicos têm cobertura;
- erros públicos são estáveis e seguros;
- secrets/conteúdo sensível não são logados;
- `uv lock --check` passa quando aplicável;
- `uv run ruff check .` passa;
- `uv run ruff format --check .` passa;
- `uv run mypy sofias_memory scripts` passa;
- `uv run pytest` passa;
- `git diff --check` passa, salvo hard-break Markdown deliberado já documentado;
- diff inteiro é revisado;
- nenhum commit/push/PR é feito por agente sem solicitação explícita.

---

## 11. Estratégia de execução recomendada

B5 deve ser executado em três blocos lógicos, sem transformar isso em branches ou milestones extras obrigatórios:

### Bloco 1 — Runtime Foundation

```text
SM-501 → SM-502 → SM-503 → SM-504 → SM-505 → SM-506 → SM-507
```

Resultado esperado: runtime durável existe e se recupera, ainda sem migrar toda a API pública.

### Bloco 2 — Public Operations

```text
SM-508 → SM-509 → SM-510 → SM-511 → SM-512 → SM-513 → SM-514 → SM-515
```

Resultado esperado: writes públicos usam o runtime B5 e são observáveis/controláveis.

### Bloco 3 — Operational Gate

```text
SM-516 → GATE-B5
```

Resultado esperado: readiness, logs, recovery e E2E comprovam o MVP operacional final.

Não iniciar SM-510..SM-515 antes de o runtime foundation possuir testes reais de queue claim/recovery. Caso contrário, cada endpoint tenderá a inventar sua própria semântica assíncrona.

---

## 12. Decisões arquiteturais aprovadas e auditoria upstream concluída

As 14 decisões originalmente abertas na primeira versão deste backlog foram aprovadas e
incorporadas à seção 5:

1. Dataset DELETE terá ADR-0010 separado;
2. `stale` é condição derivada, não novo `PipelineRunStatus`;
3. serialização prefere `dataset_id` autoritativo; operações globais usam barrier explícito;
4. retry schedule será durável/persistido;
5. `graph_outbox.processing` terá lease/recovery persistido;
6. `config_fingerprint` incompatível não retoma automaticamente;
7. manual retry cria novo `PipelineRun`;
8. timeout de `wait=true` retorna `202` e mantém o mesmo run;
9. `WORKER_ENABLED=false` não habilita fallback síncrono; writes retornam `503` e readiness fica false;
10. Dataset DELETE preserva tombstone `DELETED` e protege `main`;
11. Dataset DELETE usa novo `PipelineType.DATASET_DELETE`;
12. `Cognify rebuild=true` é dataset-wide em nova generation;
13. Remember/full usa um único top-level `REMEMBER` run;
14. `wait` não participa do payload hash/idempotency identity.

A revisão arquitetural final foi realizada contra a baseline funcional congelada:

```text
topoteretes/cognee
branch main
version 1.4.1
commit 38eece5bbb0cb9f5706fed908abd16dba0f5505e
```

### Resultado da auditoria

A baseline confirmou conceitos úteis, mas também mostrou limites que B5 foi desenhado
para resolver:

- **dataset serialization:** o upstream usa lock `asyncio` process-local e declara que
  ele não é proteção cross-process; no Sofias a exclusão operacional deve estar ancorada
  no PostgreSQL;
- **concurrency limiting:** o `DatasetQueue` upstream é semaphore em memória, não queue
  durável; pode inspirar limite de concorrência, não o lifecycle persistido;
- **background execution:** o upstream usa `asyncio.create_task` + queue de status em
  memória; no Sofias isso não pode ser durability boundary porque restart precisa
  preservar trabalho;
- **recovery:** o upstream Cognify usa idade do run + rollback/reset e observa que
  heartbeat/lease seria mais preciso; o Sofias já possui `heartbeat_at` e seguirá o PRD;
- **pipeline composition:** `PipelineContext` e tasks explícitas são referências úteis
  para engine/context do SM-504;
- **nested execution:** o upstream precisa de reentrância de lock para pipelines
  aninhados; o Sofias evita esse problema mantendo um único top-level run e steps internos
  sem novo claim do mesmo dataset;
- **retry/cancel:** a baseline não oferece o lifecycle durável de step retry, manual
  retry e cooperative cancellation exigido pelo PRD do Sofias; não há comportamento do
  upstream a preservar que contradiga ADR-0009.

### Classificação

Nenhuma das 14 decisões aprovadas precisa ser revertida.

O backlog está **PRONTO PARA EXECUÇÃO**, com a seguinte ordem obrigatória:

```text
SM-501
→ criar/revisar/aceitar ADR-0009
→ somente então iniciar código B5 em SM-502
```

ADR-0010 permanece adiado até antes de SM-515, conforme planejado.

O Cognee continua referência funcional de menor precedência. Não copiar do upstream
users/ACL, providers dinâmicos, custom pipelines, locks process-local como autoridade,
fire-and-forget como fila durável ou qualquer mecanismo incompatível com os ADRs e o PRD
do Sofias Memory.

