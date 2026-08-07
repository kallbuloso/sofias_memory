# AGENTS.md — Sofias Memory

> Instruções operacionais para agentes de desenvolvimento.
> Este arquivo fica na raiz e vale para toda a árvore, salvo quando um `AGENTS.md`
> mais específico existir em um subdiretório.

## 1. Missão

O **Sofias Memory** é uma reimplementação independente, focada e single-user do núcleo
de memória semântica e knowledge graph do Cognee.

Não trate este projeto como um fork a ser podado. Não copie a arquitetura inteira do
Cognee nem reintroduza abstrações removidas só porque existem no upstream.

Baseline funcional do PRD:

- upstream: `topoteretes/cognee`
- branch: `main`
- versão: `1.4.1`
- commit: `38eece5bbb0cb9f5706fed908abd16dba0f5505e`

O Cognee é referência funcional e, quando realmente vantajoso, fonte de código
Apache-2.0. A arquitetura do **Sofias Memory** é definida por este repositório.

## 2. Fontes de verdade

Antes de uma tarefa não trivial:

1. leia este `AGENTS.md`;
2. leia o trecho relevante do PRD;
3. leia ADRs relacionados;
4. inspecione código, migrations e testes existentes;
5. só então altere código.

Precedência em conflitos:

1. instrução explícita do usuário na tarefa atual;
2. `AGENTS.md` mais específico;
3. este `AGENTS.md`;
4. ADR aceito;
5. PRD/SPECS;
6. contratos, migrations e testes versionados;
7. Cognee upstream.

Documento de produto esperado:

```text
docs/product/Sofias_Memory_PRD_SPECS.md
```

Se estiver em outro caminho, descubra o arquivo existente. Não crie cópia concorrente.

### Overrides já decididos após o PRD inicial

Estas decisões vencem qualquer trecho antigo conflitante:

- package Python em `sofias_memory/` na raiz;
- NÃO usar `src/sofias_memory`;
- `compose.yaml` é canônico e portável;
- `compose.easypanel.yaml` é deployment EasyPanel;
- `compose.portainer.yaml` é deployment Portainer;
- EasyPanel/Portainer não fazem parte da arquitetura da aplicação.

Não reabra essas decisões sem solicitação explícita.

## 3. Invariantes do MVP

Nunca introduza, nem "temporariamente":

- users;
- login/register;
- JWT/cookie auth;
- `owner_id`;
- `tenant_id`;
- roles;
- permissions;
- ACL;
- organizations;
- multitenancy;
- API key management;
- settings persistidos ou por rota;
- sync entre instâncias;
- cloud/remote client;
- `serve`/`push`;
- telemetria externa;
- providers de banco selecionáveis;
- optional dependencies;
- plugin system;
- frontend;
- MCP no repositório principal;
- múltiplas réplicas da aplicação no MVP.

Autenticação HTTP:

```http
X-API-Key: sf-...
```

Configuração:

```env
API_KEY=sf-...
```

A chave não vai para banco, logs, traces, `/info`, exceptions ou métricas.

## 4. Stack fixa

Versão 1:

- Python 3.12;
- FastAPI;
- Pydantic 2 + pydantic-settings;
- SQLAlchemy 2 async;
- Alembic;
- PostgreSQL 17 + pgvector;
- Neo4j 5.x;
- OpenAI-compatible API para LLM;
- OpenAI-compatible API para embeddings;
- filesystem local;
- worker interno;
- PostgreSQL como fila/estado dos pipelines.

Não substituir por SQLite, LanceDB, Kuzu, Qdrant, Turso, Redis, Celery, LiteLLM,
Instructor ou BAML sem decisão arquitetural explícita.

Runtime dependencies ficam em `[project.dependencies]`.
Dev dependencies ficam em `[dependency-groups].dev`.
Não criar `[project.optional-dependencies]` no MVP.

## 5. Autoridade dos dados

### PostgreSQL

PostgreSQL é a **fonte de verdade**.

Tudo necessário para reconstrução, auditoria, proveniência e deleção precisa ser
recuperável a partir do PostgreSQL + armazenamento de sources.

Schema previsto:

- datasets
- sources
- documents
- chunks
- entities
- entity_mentions
- relations
- relation_evidence
- summaries
- memory_entries
- queries
- feedback
- pipeline_runs
- pipeline_steps
- graph_outbox

### Neo4j

Neo4j é uma **projeção reconstruível**.

Nunca grave conhecimento exclusivamente no Neo4j.

Escritas devem ser:

- idempotentes;
- derivadas de estado confirmado no PostgreSQL;
- acionadas pela outbox;
- recuperáveis;
- reconstruíveis.

APOC e GDS podem existir no ambiente local, mas o core do MVP não deve depender deles
sem ADR explícito. Prefira Cypher padrão e o driver oficial.

## 6. Estrutura do repositório

```text
sofias-memory/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── Dockerfile
├── compose.yaml
├── compose.easypanel.yaml
├── compose.portainer.yaml
├── .env.example
├── LICENSE
├── NOTICE.md
├── sofias_memory/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── lifespan.py
│   ├── api/
│   │   ├── dependencies.py
│   │   ├── errors.py
│   │   ├── middleware/
│   │   │   ├── api_key.py
│   │   │   ├── request_id.py
│   │   │   └── limits.py
│   │   └── routes/
│   │       ├── health.py
│   │       ├── info.py
│   │       ├── datasets.py
│   │       ├── remember.py
│   │       ├── cognify.py
│   │       ├── recall.py
│   │       ├── feedback.py
│   │       ├── improve.py
│   │       ├── forget.py
│   │       ├── runs.py
│   │       └── graph.py
│   ├── domain/
│   │   ├── datasets/
│   │   ├── sources/
│   │   ├── documents/
│   │   ├── graph/
│   │   ├── memory/
│   │   ├── queries/
│   │   └── runs/
│   ├── services/
│   │   ├── remember_service.py
│   │   ├── cognify_service.py
│   │   ├── recall_service.py
│   │   ├── improve_service.py
│   │   └── forget_service.py
│   ├── pipelines/
│   │   ├── engine.py
│   │   ├── registry.py
│   │   ├── context.py
│   │   ├── remember_pipeline.py
│   │   ├── cognify_pipeline.py
│   │   ├── improve_pipeline.py
│   │   ├── forget_pipeline.py
│   │   └── steps/
│   ├── loaders/
│   │   ├── base.py
│   │   ├── text.py
│   │   ├── markdown.py
│   │   ├── json_loader.py
│   │   ├── csv_loader.py
│   │   ├── html.py
│   │   ├── pdf.py
│   │   ├── docx.py
│   │   └── url.py
│   ├── infrastructure/
│   │   ├── postgres/
│   │   ├── neo4j/
│   │   ├── llm/
│   │   ├── embeddings/
│   │   ├── storage/
│   │   ├── queue/
│   │   └── telemetry/
│   ├── prompts/
│   │   ├── graph_extraction.v1.md
│   │   ├── chunk_summary.v1.md
│   │   ├── answer_rag.v1.md
│   │   └── answer_graph.v1.md
│   └── schemas/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── e2e/
│   ├── security/
│   └── performance/
├── scripts/
    ├── generate_api_key.py
    ├── rebuild_graph.py
    └── verify_installation.py
└── docs/
    ├── product/
    ├── adr/
    ├── exec-plans/
    └── generated/
```

Regras:

- NÃO criar `src/`;
- NÃO criar outro package raiz para o core;
- evite `utils/` genérico como depósito;
- módulos pequenos, responsabilidade clara;
- features futuras são código versionado, não plugins dinâmicos.

Para mudanças longas/multietapas, mantenha um plano curto em
`docs/exec-plans/active/` e mova para `completed/` ao finalizar.

## 7. Dependências entre camadas

Fluxo:

```text
api -> services -> domain/pipelines -> ports -> infrastructure
```

Regras:

- routes conhecem schemas e services;
- routes não executam SQL ou Cypher;
- services orquestram casos de uso;
- domain não depende de FastAPI, SQLAlchemy ou Neo4j;
- pipelines não resolvem autenticação;
- loaders não conhecem API;
- infrastructure implementa persistência/integrações;
- config é injetada;
- não use service locator;
- não use imports dinâmicos controlados por request;
- não crie provider abstractions para providers inexistentes.

Prefira a solução mais simples que preserve os limites acima.

## 8. Ambiente local atual

Existe uma stack de bancos para desenvolvimento:

```text
sofias_memory_db
```

A aplicação pode rodar diretamente no host apontando para ela.

### PostgreSQL

```text
host:       127.0.0.1
host port:  5440
container:  5432
database:   cognee_db
user:       cognee
password:   DB_PASSWORD
```

Exemplo:

```env
DATABASE_URL=postgresql+asyncpg://cognee:${DB_PASSWORD}@127.0.0.1:5440/cognee_db
```

### Neo4j

```text
host:       127.0.0.1
host Bolt:  7688
container:  7687
database:   neo4j
user:       neo4j
password:   DB_NEO4J_PASSWORD
```

Exemplo:

```env
NEO4J_URI=bolt://127.0.0.1:7688
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=${DB_NEO4J_PASSWORD}
NEO4J_DATABASE=neo4j
```

### Inconsistência conhecida da stack recebida

Neo4j usa:

```text
NEO4J_AUTH=neo4j/${DB_NEO4J_PASSWORD}
```

mas o healthcheck original usa:

```text
-p ${DB_PASSWORD}
```

O healthcheck deve usar:

```text
-p ${DB_NEO4J_PASSWORD}
```

Verifique isso antes de diagnosticar falha do Neo4j.

### Nunca hard-code as portas locais

`5440` e `7688` são mapeamentos do host de desenvolvimento.

Quando a aplicação estiver no mesmo Compose dos bancos:

```env
DATABASE_URL=postgresql+asyncpg://...@postgres:5432/...
NEO4J_URI=bolt://neo4j:7687
```

## 9. Tooling

Use `uv`.

Comandos padrão:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

Execução local:

```bash
uv run uvicorn sofias_memory.app:app --host 127.0.0.1 --port 8000 --reload
```

Migrations:

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic heads
```

Não misture Poetry, pipenv ou requirements.txt sem decisão explícita.

## 10. Configuração

Centralize configuração em `sofias_memory/config.py` com `pydantic-settings`.

A config deve:

- carregar no startup;
- validar integralmente;
- permanecer imutável durante o processo;
- abortar startup se inválida;
- tipar/redigir secrets;
- ser a única camada que interpreta o ambiente.

Não espalhe `os.getenv()`.

`.env` nunca entra no Git.
`.env.example` não contém secrets reais.

Não criar:

- settings no banco;
- endpoint de configuração;
- override de banco/model/provider por request.

## 11. Convenções de código

Código, tabelas, colunas, classes, funções, endpoints, schemas e variáveis em **inglês**.

Python:

- `>=3.12,<3.13`;
- typing em código novo;
- async ponta a ponta para I/O;
- `pathlib.Path`;
- timestamps UTC timezone-aware;
- UUID para IDs públicos/persistidos;
- enums para estados finitos;
- evite `Any`;
- não capture `Exception` silenciosamente;
- não use mutable defaults;
- não devolva `None` como estado ambíguo quando um resultado explícito for melhor.

Ruff é autoridade de lint/format.

Logging deve ser estruturado e incluir quando aplicável:

- `request_id`
- `run_id`
- `step`
- `dataset_id`
- `source_id`
- `document_id`

Nunca logue secrets ou documentos/payloads integrais por padrão.

## 12. API e OpenAPI

Base:

```text
/api/v1
```

Health público:

```text
GET /health/live
GET /health/ready
```

Todas as demais rotas exigem `X-API-Key`.

Use comparação constant-time (`secrets.compare_digest` ou equivalente).

Headers suportados:

```text
X-API-Key
X-Request-Id
Idempotency-Key
```

Sucesso:

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid",
    "timestamp": "UTC ISO-8601"
  }
}
```

Erro:

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "Safe message.",
    "details": {},
    "request_id": "uuid"
  }
}
```

Nunca devolver traceback ao cliente.

### Superfícies previstas

Somente as famílias definidas no PRD:

- health
- info
- datasets
- remember text/file/url
- cognify
- recall
- feedback
- improve
- forget
- runs/retry/cancel
- graph
- provenance

Não invente aliases/endpoints de conveniência.

### Prefixos proibidos

O teste de OpenAPI deve falhar se aparecer:

```text
/auth
/users
/permissions
/api-keys
/settings
/configuration
/sync
/cloud
/serve
/push
/slack
/integrations
/agents
/skills
/proposals
```

## 13. Pydantic schemas

Separe schemas públicos de persistence models.

Regras:

- nunca exponha SQLAlchemy model diretamente;
- request/response models explícitos;
- `extra="forbid"` em request público salvo justificativa;
- constraints declaradas com `Field`;
- enums estáveis;
- exemplos válidos;
- compatibilidade dentro da major version;
- breaking changes exigem decisão explícita.

Schemas seguem o PRD, não DTOs legados do Cognee.

## 14. SQLAlchemy e Alembic

Alembic é a única autoridade de evolução do schema.

Nunca use `Base.metadata.create_all()` como mecanismo de startup/produção.

Cada mudança de schema deve:

1. ter migration;
2. ter upgrade determinístico;
3. ter downgrade quando seguro;
4. ser testada em PostgreSQL real;
5. criar/remover índices e constraints explicitamente;
6. preservar dados ou documentar perda.

Fundação do schema, em ordem coerente:

1. extensão `vector`;
2. datasets;
3. sources;
4. documents;
5. chunks;
6. entities;
7. entity_mentions;
8. relations;
9. relation_evidence;
10. summaries;
11. memory_entries;
12. queries;
13. feedback;
14. pipeline_runs;
15. pipeline_steps;
16. graph_outbox.

Use campos/constraints/índices do PRD.

É proibido criar:

```text
users roles permissions acl api_keys settings tenants owner_id tenant_id
```

A dimensão pgvector deve ser compatível com `EMBEDDING_DIMENSIONS`.
Mudança de dimensão exige migration/reindex planejado.

## 15. Repositories, transações e outbox

Não espalhe ORM por routes/pipelines.

Use repositories e boundaries transacionais claros.

Operação PostgreSQL + Neo4j:

1. persista estado autoritativo no PostgreSQL;
2. grave `graph_outbox` na mesma transação;
3. commit;
4. worker projeta no Neo4j;
5. marque evento como processado.

Não implemente distributed transaction entre PostgreSQL e Neo4j.

## 16. Pipeline engine

Estados:

```text
queued
running
succeeded
failed
cancelling
cancelled
```

Requisitos:

- PostgreSQL queue;
- `FOR UPDATE SKIP LOCKED`;
- heartbeat;
- retry por step;
- idempotência;
- lock por dataset;
- stale recovery;
- cancelamento cooperativo;
- uma escrita simultânea por dataset;
- progresso persistido;
- generation nova só ativa após conclusão consistente.

Não adicione Redis/Celery.

## 17. Memória

### Remember/full

Fluxo:

```text
validate
-> persist source
-> extract/normalize
-> chunk
-> embeddings
-> summaries
-> entities/relations
-> canonicalize
-> persist PostgreSQL
-> graph outbox/projection
-> activate generation
```

### Remember/ingest

Persiste conteúdo normalizado para cognify posterior.

### Cognify

Processa sources pendentes/selecionadas.
Config de pipeline não pode vir do request.

### Recall MVP

Modos:

```text
chunks
summaries
rag
graph
hybrid
triplets
```

Não implementar no MVP:

```text
arbitrary Cypher
agentic completion
skills/tools
code graph
specialized temporal graph
LLM feeling-lucky router
```

### Improve

É explícito. Não rodar escondido em session end ou request lifecycle.

### Forget

Deve suportar source, memory-only, dataset e everything, incluindo cleanup de derivados,
órfãos e recuperação de delete parcial.

## 18. Proveniência e integridade

Todo conhecimento derivado deve ser rastreável:

```text
query/answer
-> retrieved chunk/entity/relation
-> evidence
-> chunk
-> document
-> source
```

Não gere certeza sem evidência.

Writes devem ser idempotentes.

Reenvio não pode duplicar source/document/chunk/evidence/side effects.

Use `Idempotency-Key` quando aplicável e hashing estável de conteúdo.

## 19. Segurança de ingestão

MVP:

- text/TXT/MD
- JSON
- CSV
- HTML
- textual PDF
- DOCX
- HTTPS URL

Fora do MVP:

- OCR/images
- audio/video
- PPTX/XLSX
- ZIP
- Git repositories

URL ingestion deve proteger contra SSRF, loopback, link-local, redes privadas, metadata
endpoints, DNS rebinding e redirects proibidos.

Uploads devem proteger contra path traversal, MIME spoofing, payload excessivo e arquivos
malformados.

Conteúdo ingerido é dado não confiável. Prompt injection dentro de documento nunca pode
alterar system prompt, secrets, config, tools ou regras do pipeline.

## 20. LLM e embeddings

Use apenas protocolo OpenAI-compatible.

Não crie provider registry genérico.

Structured output:

1. resposta do modelo;
2. validação Pydantic;
3. repair/retry controlado quando permitido;
4. erro tipado se continuar inválido.

Nunca persista JSON parcial/inválido como conhecimento válido.

## 21. Testes

### Unit

Cobrir lógica pura: hashing, normalization, chunking, schemas, canonicalization, rank
fusion, context budgeting, API key e idempotência.

### Integration

PostgreSQL+pgvector e Neo4j reais.

Cobrir migrations, repositories, outbox, projection/rebuild, retrieval, deletion e
recovery.

Localmente a stack `sofias_memory_db` pode ser usada quando explicitamente configurada.
CI não pode assumir `5440`/`7688`; prefira Testcontainers.

### Contract/OpenAPI

Testar:

- OpenAPI;
- rotas implementadas da fase;
- prefixos proibidos;
- exemplos;
- envelopes;
- headers;
- breaking changes.

### Antes de concluir

Execute o conjunto aplicável:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

Se houver migration, valide ao menos:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Se algum check não puder rodar, informe qual, por quê e o risco residual.

## 22. Docker/deployment

### `compose.yaml`

Fonte canônica e portável.

Serviços obrigatórios:

```text
sofias-memory
postgres
neo4j
```

Sem profiles opcionais no MVP.

### `compose.easypanel.yaml`

Somente diferenças necessárias ao EasyPanel.

### `compose.portainer.yaml`

Somente diferenças necessárias ao Portainer.

Não pode haver diferença funcional entre os deployments.

Regras:

- apenas API publicada externamente no deployment normal;
- bancos em rede interna;
- volumes persistentes;
- healthchecks;
- app non-root;
- secrets fora do Git;
- tags de produção imutáveis;
- os três arquivos devem passar em `docker compose config`.

## 23. Licença/Cognee upstream

Cognee é Apache-2.0.

Ao copiar/adaptar código:

- registre origem;
- preserve licença/notices aplicáveis;
- documente modificações relevantes;
- não copie módulos desnecessários;
- adapte naming;
- elimine auth/multitenancy de verdade; não esconda com defaults;
- mantenha `LICENSE` e `NOTICE.md` corretos.

Use preferencialmente o commit baseline do PRD.
Não acompanhe upstream novo silenciosamente durante uma feature.

## 24. Git e escopo

Não faça sem solicitação:

- commit;
- push;
- PR.

Não:

- reformate arquivos não relacionados;
- atualize deps sem relação com a tarefa;
- reescreva migration já aplicada em ambiente compartilhado;
- altere PRD para fazer o código caber;
- mude teste para esconder incompatibilidade.

Faça diffs pequenos, testes de regressão e ADR para decisão estrutural nova.

## 25. Fluxo de trabalho do agente

Em tarefa não trivial:

1. identifique requisito/FR;
2. leia PRD/ADR relevante;
3. inspecione módulos e migrations;
4. liste invariantes;
5. implemente o menor corte vertical completo;
6. ajuste testes/contratos;
7. rode checks;
8. revise diff;
9. reporte mudanças, migrations, contratos, testes e riscos reais.

Não entregue fake success, scaffold enganoso ou TODO crítico escondido.

## 26. Fase atual e backlog inicial

Estamos na fundação. Salvo tarefa explícita em contrário, trabalhe nesta ordem:

### B0 — Repository foundation

- estrutura raiz sem `src/`;
- docs/product, docs/adr, docs/exec-plans;
- `pyproject.toml`;
- `uv.lock`;
- Ruff/mypy/pytest;
- `.gitignore`;
- `.env.example`;
- LICENSE/NOTICE.

### B1 — Application foundation

- typed Settings;
- secret validation;
- logging estruturado;
- request ID;
- API key middleware;
- exception handlers;
- FastAPI app/lifespan;
- live/ready/info.

### B2 — PostgreSQL foundation

- async engine/session;
- Alembic;
- pgvector extension;
- core schema do PRD;
- indexes/constraints;
- repositories;
- migration tests.

### B3 — Neo4j foundation

- driver lifecycle;
- readiness;
- constraints;
- projection interface;
- graph outbox consumer skeleton;
- rebuild contract.

### B4 — Pydantic/OpenAPI foundation

- schemas públicos;
- envelopes;
- stable error codes;
- contratos das rotas do PRD;
- OpenAPI metadata/examples;
- contract snapshot/tests;
- forbidden-route test.

### B5 — Pipeline engine

- run/step models;
- queue claim;
- heartbeat;
- retries;
- cancellation;
- stale recovery;
- dataset lock.

Depois:

```text
ingestion -> cognify -> recall -> improve -> forget -> hardening/release
```

Não pule para graph-RAG antes de schema, migrations, provenance e contratos estarem sólidos.

## 27. Definition of Done

Uma feature está pronta quando, conforme aplicável:

- comportamento implementado;
- typing/lint/format verdes;
- unit/integration tests verdes;
- migration criada e validada;
- OpenAPI atualizado;
- erros estáveis;
- logs/métricas relevantes;
- segurança revisada;
- idempotência considerada;
- rollback/recovery considerado;
- docs/exemplo atualizados;
- nenhum conceito proibido reintroduzido;
- diff revisado.

## 28. Regra final

Entre duas soluções, prefira a que:

1. mantém single-user;
2. reduz caminhos de execução;
3. preserva PostgreSQL como fonte de verdade;
4. mantém Neo4j reconstruível;
5. aumenta proveniência;
6. torna falhas recuperáveis;
7. é testável com infraestrutura real;
8. evita abstrações especulativas;
9. respeita PRD/ADRs;
10. reduz a chance de recriar o Cognee inteiro por acidente.

O objetivo não é o sistema mais genérico.

O objetivo é o **Sofias Memory correto**.
