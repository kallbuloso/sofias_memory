# AGENTS.md — Sofias Memory

> Instruções operacionais para agentes de desenvolvimento.
>
> Este arquivo fica na raiz do repositório e vale para toda a árvore, salvo quando existir um `AGENTS.md` mais específico em um subdiretório.

---

# 1. Missão

O **Sofias Memory** é uma implementação independente, focada e single-user de infraestrutura de memória persistente, conhecimento, contexto e recuperação para aplicações, assistentes e agentes.

O projeto teve como referência funcional inicial o Cognee, mas sua arquitetura, contratos, evolução e limites são definidos exclusivamente por este repositório e pelos artefatos APPROVED do produto.

Não trate este projeto como um fork a ser podado.

Não:

- copie a arquitetura inteira do Cognee;
- reintroduza abstrações removidas apenas porque existem no upstream;
- transforme uma referência funcional em autoridade arquitetural;
- expanda o Sofias Memory para Agent Runtime, orchestration framework ou plataforma genérica sem decisão explícita.

Baseline histórico de referência funcional:

```text
upstream: topoteretes/cognee
branch: main
version: 1.4.1
commit: 38eece5bbb0cb9f5706fed908abd16dba0f5505e
```

O Cognee pode servir como:

- referência funcional;
- inspiração;
- fonte de código Apache-2.0 quando realmente vantajoso.

Ele não define a arquitetura atual do Sofias Memory.

---

# 2. Fontes de verdade

Antes de qualquer tarefa não trivial:

1. leia este `AGENTS.md`;
2. identifique o Slice ou feature em execução;
3. leia os artefatos APPROVED relevantes;
4. leia ADRs relacionados;
5. inspecione código, migrations e testes existentes;
6. só então altere código.

Não implemente uma feature apenas a partir de memória, comentário de issue ou suposição sobre intenção futura.

---

## 2.1 Precedência

Em caso de conflito:

```text
1. instrução explícita atual do usuário
2. AGENTS.md mais específico no subdiretório
3. implementation handoff APPROVED do Slice explicitamente CURRENT
4. baseline normativa APPROVED da feature: Contract, ADRs, Architecture,
   Spec/PRD/Product Definition, conforme a responsabilidade de cada artefato
5. guardrails operacionais e invariantes gerais deste AGENTS.md
6. baseline geral do projeto e documentação geral
7. código, migrations e testes como evidência de implementação
8. Cognee upstream como referência funcional
```

Regras operacionais da raiz não podem anular silenciosamente uma evolução
posterior explicitamente APPROVED. O handoff CURRENT especializa a execução do
Slice; não autoriza mudar o Contract público ou uma decisão arquitetural aprovada.
O Contract rege nomes e comportamento públicos; ADRs regem decisões duráveis;
Architecture rege sua composição; Spec/PRD/Product Definition regem os requisitos
correspondentes. Se dois artefatos APPROVED realmente conflitarem e não houver
precedência ou amendment explícito que resolva o conflito: STOP e reporte
`NEEDS HUMAN DECISION`, sem escolher ou reescrever uma decisão silenciosamente.

Quando código existente divergir de uma decisão APPROVED:

> não altere o documento para fazer o código caber.

Diagnostique a divergência.

---

## 2.2 Baseline geral do produto

O baseline histórico principal continua disponível em:

```text
docs/product/Sofias_Memory_PRD_SPECS.md
```

Esse documento não é, contudo, a única fonte de verdade para features posteriores.

Features evoluídas depois do PRD original podem possuir seus próprios:

- Discovery;
- Product Definition;
- PRD;
- Specs;
- Contracts;
- Architecture;
- ADRs;
- Technical Backlog;
- Implementation handoffs.

Quando esses documentos estiverem APPROVED, eles complementam e, dentro do escopo da feature, especializam o baseline anterior.

---

## 2.3 Contextual Project Memory — baseline aprovado

Existe uma evolução APPROVED denominada:

```text
Contextual Project Memory
```

Os artefatos normativos estão em:

```text
docs/product-implementation/
├── 01-SF-Discovery-Memoria-Contextual-Por-Projeto.md
├── 02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md
├── 03-SF-PRD-Memoria-Contextual-Por-Projeto.md
├── 04-SF-Spec-Memoria-Contextual-Por-Projeto.md
├── 05-SF-Contract-Memoria-Contextual-Por-Projeto.md
├── 06-SF-Architecture-Memoria-Contextual-Por-Projeto.md
├── 07-SF-ADRs-Memoria-Contextual-Por-Projeto.md
└── 08-SF-Technical-Backlog-Memoria-Contextual-Por-Projeto.md
```

Implementation handoffs ficam em:

```text
docs/product-implementation/implementation/
```

Os documentos `09–16` podem ser lidos para contexto e dependências. Respeite o
status real de cada arquivo: um handoff `READY FOR REVIEW` ainda exige aprovação.
Artefatos APPROVED não devem ser reinterpretados durante implementação.

Um implementation handoff APPROVED desta iniciativa satisfaz o requisito de
execution plan do Slice; não criar um segundo plano duplicado em
`docs/exec-plans/active/`. Para outras iniciativas longas/multietapas, mantenha um
plano curto nesse diretório e mova para `completed/` ao finalizar, salvo quando já
existir handoff APPROVED equivalente.

---

## 2.4 ADRs de Contextual Project Memory

As decisões arquiteturais aprovadas são:

```text
ADR-0017 — First-Class Contextual Memory Domain and PostgreSQL Authority

ADR-0018 — ContextCheckpoint Revision and Concurrency Model

ADR-0019 — Provenance-Aware Evidence and Synchronous Forget Propagation
```

Enquanto esses ADRs não estiverem materializados como arquivos independentes em `docs/adr/`, a fonte normativa é:

```text
docs/product-implementation/
07-SF-ADRs-Memoria-Contextual-Por-Projeto.md
```

Não renumerar, reinterpretar ou substituir esses ADRs silenciosamente.

---

# 3. Overrides já decididos

Estas decisões vencem qualquer trecho antigo conflitante:

- package Python fica em `sofias_memory/` na raiz;
- NÃO usar `src/sofias_memory`;
- `compose.yaml` é o deployment canônico e portável;
- EasyPanel/Portainer não fazem parte da arquitetura da aplicação;
- arquivos específicos de EasyPanel/Portainer só devem existir quando houver diferença real e documentada;
- PostgreSQL permanece fonte de verdade;
- Neo4j permanece projeção reconstruível;
- Sofias Memory permanece single-user por instalação no baseline atual;
- runtime de Agent permanece fora do Sofias Memory;
- memória contextual não cria autoridade operacional.

Não reabra essas decisões sem solicitação explícita.

---

# 4. Invariantes de produto

Nunca introduza, nem temporariamente, sem nova decisão explícita:

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
- settings persistidos por usuário;
- configuração mutável por rota;
- sync entre instâncias;
- cloud/remote client genérico;
- `serve`/`push`;
- telemetria externa;
- providers de banco selecionáveis;
- plugin system genérico;
- frontend no repositório core;
- MCP no repositório principal;
- Agent Runtime;
- Task Runtime;
- tool execution;
- provider session orchestration.

Autenticação HTTP:

```http
X-API-Key: sf-...
```

Configuração:

```env
API_KEY=sf-...
```

A chave nunca vai para:

- banco;
- logs;
- traces;
- `/info`;
- exceptions;
- métricas.

---

# 5. Stack fixa

Baseline técnico:

- Python 3.12;
- FastAPI;
- Pydantic 2;
- pydantic-settings;
- SQLAlchemy 2 async;
- Alembic;
- PostgreSQL 17;
- pgvector;
- Neo4j 5.x;
- OpenAI-compatible API para LLM;
- OpenAI-compatible API para embeddings;
- filesystem/S3 para Source originals;
- worker interno;
- PostgreSQL como fila/estado dos pipelines.

Armazenamento de Source originals possui exatamente dois backends first-party:

```text
filesystem
s3
```

S3 pode apontar para:

- AWS S3;
- endpoint compatível explicitamente configurado.

Implementação via:

```text
boto3
```

Não criar provider registry.

Não substituir por:

- SQLite;
- LanceDB;
- Kuzu;
- Qdrant;
- Turso;
- Redis;
- Celery;
- LiteLLM;
- Instructor;
- BAML;

sem decisão arquitetural explícita.

Runtime dependencies ficam em:

```text
[project.dependencies]
```

Dev dependencies ficam em:

```text
[dependency-groups].dev
```

Não criar:

```text
[project.optional-dependencies]
```

apenas para acomodar features futuras.

---

# 6. Autoridade dos dados

## 6.1 PostgreSQL

PostgreSQL é a fonte de verdade.

Tudo necessário para:

- reconstrução;
- auditoria;
- provenance;
- lifecycle;
- Forget;
- recovery;

deve ser recuperável a partir do PostgreSQL e, quando aplicável, do armazenamento de Source originals.

Domínios existentes incluem:

- datasets;
- sources;
- documents;
- chunks;
- entities;
- entity_mentions;
- relations;
- relation_evidence;
- summaries;
- memory_entries;
- queries;
- feedback;
- pipeline_runs;
- pipeline_steps;
- graph_outbox;
- sessions;
- session_entries;
- skills;
- skill_revisions;
- agents;
- agent_skills;
- agent_sessions;
- memory_items;
- memory_provenance;
- cognitive_memory_idempotency.

Contextual Project Memory adicionará incrementalmente:

- episodes;
- episode provenance;
- contextual idempotency;
- context checkpoints;
- context statements;
- contextual Evidence.

Esses recursos, quando implementados no respectivo Slice, terão estado,
provenance, revisions, Evidence e idempotência autoritativos no PostgreSQL.

Não antecipe tabelas de Slices posteriores.

---

## 6.2 Sessions

`queries.session_id` e `pipeline_runs.session_id` são associações first-class com:

```text
sessions.id
```

e usam:

```text
ON DELETE SET NULL
```

Nunca inferir Session a partir de:

- texto;
- metadata antiga;
- `MemoryEntry`;
- `Document.metadata`;
- `PipelineRun.input`.

Sessions não recebem hard delete público.

---

## 6.3 Neo4j

Neo4j é projeção reconstruível.

Nunca grave conhecimento exclusivamente nele.

Escritas projetadas devem ser:

- idempotentes;
- derivadas do PostgreSQL;
- acionadas pela outbox;
- recuperáveis;
- reconstruíveis.

Não implemente distributed transaction PostgreSQL ↔ Neo4j.

---

## 6.4 Domínios PostgreSQL-only

São PostgreSQL-only no baseline atual:

```text
Sessions
SessionEntries
Skills
Agent Profiles
Native Cognitive Memory
Contextual Project Memory v1
```

Contextual Project Memory MUST NOT:

- escrever `graph_outbox`;
- depender de Neo4j;
- ler Neo4j como autoridade;
- exigir Neo4j disponível para suas mutations.

Qualquer futura graph projection contextual exige ADR explícito.

---

# 7. Contextual Project Memory — invariantes

Contextual Project Memory adiciona novos planos de memória sem fundir os existentes.

Modelo conceitual:

```text
MemoryItem
    = current profile/semantic cognitive truth

Episode
    = historical experience

ContextCheckpoint
    = current continuity state

Session / SessionEntry
    = temporal interaction history

Knowledge Memory
    = source-backed knowledge
```

---

## 7.1 MemoryItem permanece Cognitive Memory

`MemoryItem` continua suportando exclusivamente:

```text
profile
semantic
```

Nunca adicionar:

```text
episodic
procedural
```

ao `memory_type` como atalho para novas features.

Procedural Memory continua representada por Skills.

Historical experience será representada por Episode.

---

## 7.2 Episode

Episode é first-class.

Representa:

```text
what happened
```

Não representa automaticamente:

```text
what is currently true
```

Episode possui lifecycle próprio.

Não transformar Episode em:

- MemoryItem;
- SessionEntry;
- Document;
- Source;
- Dataset metadata.

---

## 7.3 ContextCheckpoint

ContextCheckpoint é first-class.

Representa:

```text
where this work currently stands
```

Não é:

- Task;
- workflow execution state;
- Session summary obrigatório;
- authority sobre sistemas externos.

---

## 7.4 Context Stream

A identidade de continuidade é:

```text
(scope, context_key)
```

Exemplo:

```text
project:sofias-memory
feature:contextual-memory
```

`context_key` não é permission scope.

---

## 7.5 Project Scope

```text
project:<key>
```

é namespace contextual.

Nunca trate como:

- tenant;
- owner;
- ACL;
- authorization boundary;
- API credential.

---

## 7.6 Memory is context, not authority

Nenhuma memória recuperada cria:

- Policy;
- Permission;
- system instruction;
- execution authority.

O Consumer é responsável por:

- interpretar;
- autorizar;
- planejar;
- executar.

---

## 7.7 Supervised-first

No Contract v1:

- Episode creation é explícita;
- Checkpoint creation é explícita;
- SessionEntry não vira memória automaticamente;
- Recall não cria memória silenciosamente;
- não existe automatic distillation;
- não existe automatic consolidation;
- não existe AI contradiction resolution.

---

## 7.8 Forget

Forget contextual deve respeitar provenance.

Quando Evidence existir, conteúdo derivado não poderá continuar recuperável se perdeu seu suporte necessário.

No Contract v1:

- decisão de Evidence sufficiency é determinística;
- LLM não decide se conteúdo esquecido “pode continuar”;
- propagação contextual será provenance-aware.

Não implementar Evidence ou propagation antes do Slice correspondente.

---

# 8. Estrutura do repositório

Estrutura indicativa:

```text
sofias-memory/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── Dockerfile
├── compose.yaml
├── .env.example
├── LICENSE
├── NOTICE.md
│
├── sofias_memory/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── lifespan.py
│   │
│   ├── api/
│   │   ├── dependencies.py
│   │   ├── errors.py
│   │   ├── middleware/
│   │   └── routes/
│   │
│   ├── domain/
│   ├── services/
│   ├── pipelines/
│   ├── loaders/
│   ├── infrastructure/
│   │   ├── postgres/
│   │   ├── neo4j/
│   │   ├── llm/
│   │   ├── embeddings/
│   │   ├── storage/
│   │   ├── queue/
│   │   └── telemetry/
│   ├── prompts/
│   └── schemas/
│
├── migrations/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── e2e/
│   ├── security/
│   └── performance/
│
├── scripts/
│
└── docs/
    ├── product/
    ├── product-implementation/
    │   └── implementation/
    ├── adr/
    ├── exec-plans/
    └── generated/
```

Essa árvore é orientativa, não justificativa para criar diretórios vazios ou abstrações futuras.

Regras:

- NÃO criar `src/`;
- NÃO criar outro package raiz;
- evitar `utils/` genérico;
- módulos pequenos;
- responsabilidade clara;
- features futuras são código versionado, não plugins dinâmicos.

---

# 9. Dependências entre camadas

Fluxo arquitetural:

```text
api
 ↓
services
 ↓
domain / pipelines
 ↓
ports / repositories
 ↓
infrastructure
```

Regras:

- routes conhecem schemas e services;
- routes não executam SQL;
- routes não executam Cypher;
- services orquestram casos de uso;
- domain não depende de FastAPI;
- domain não depende de SQLAlchemy;
- domain não depende de Neo4j;
- pipelines não resolvem autenticação;
- loaders não conhecem API;
- infrastructure implementa persistência e integrações;
- configuração é injetada;
- não use service locator;
- não use imports dinâmicos controlados por request;
- não crie provider abstractions para providers inexistentes.

Prefira a menor abstração que preserve essas fronteiras.

---

# 10. Ambiente local

Existe uma stack de bancos para desenvolvimento:

```text
sofias_memory_db
```

A aplicação pode rodar diretamente no host apontando para ela.

---

## 10.1 PostgreSQL

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

---

## 10.2 Neo4j

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

---

## 10.3 Known local-stack issue

Neo4j usa:

```text
NEO4J_AUTH=neo4j/${DB_NEO4J_PASSWORD}
```

O healthcheck também deve usar:

```text
DB_NEO4J_PASSWORD
```

e não `DB_PASSWORD`.

Verifique isso antes de diagnosticar falha do Neo4j.

---

## 10.4 Nunca hard-code host mappings

`5440` e `7688` são portas do host de desenvolvimento.

Dentro do Compose:

```env
DATABASE_URL=postgresql+asyncpg://...@postgres:5432/...
NEO4J_URI=bolt://neo4j:7687
```

---

# 11. Tooling

Use:

```text
uv
```

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
uv run uvicorn sofias_memory.app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload
```

Migrations:

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic heads
```

Não introduza:

- Poetry;
- pipenv;
- requirements.txt paralelo;

sem decisão explícita.

---

# 12. Configuração

Centralize configuração em:

```text
sofias_memory/config.py
```

via `pydantic-settings`.

Config deve:

- carregar no startup;
- validar integralmente;
- permanecer imutável durante o processo;
- abortar startup quando inválida;
- tipar secrets;
- redigir secrets;
- ser a única camada que interpreta ambiente.

Não espalhar:

```python
os.getenv(...)
```

pelo código.

`.env` nunca entra no Git.

`.env.example` não contém secrets reais.

Não criar:

- settings no banco;
- endpoint de configuração;
- provider override por request;
- database override por request.

---

# 13. Convenções de código

Código, tabelas, colunas, classes, funções, endpoints, schemas e variáveis:

```text
English
```

Documentação pode permanecer em pt-BR.

Python:

- `>=3.12,<3.13`;
- typing em código novo;
- async ponta a ponta para I/O;
- `pathlib.Path`;
- UTC timezone-aware;
- UUID para IDs públicos/persistidos;
- enums para estados finitos;
- evitar `Any`;
- não capturar `Exception` silenciosamente;
- não usar mutable defaults;
- evitar `None` quando representa estado ambíguo.

Ruff é autoridade de lint/format.

---

## 13.1 Logging

Logging deve ser estruturado.

Pode incluir, quando aplicável:

- `request_id`;
- `run_id`;
- `step`;
- `dataset_id`;
- `source_id`;
- `document_id`;
- `memory_id`;
- `episode_id`;
- `checkpoint_id`;
- `context_key`.

Nunca logar:

- API keys;
- HMAC keys;
- Idempotency-Key;
- documentos completos;
- prompts integrais;
- memory content integral;
- Episode `summary/situation/reason/outcome` por padrão.

---

# 14. API e OpenAPI

Base:

```text
/api/v1
```

Health público:

```text
GET /health/live
GET /health/ready
```

Todas as outras rotas exigem:

```http
X-API-Key
```

Use comparação constant-time.

Headers reconhecidos:

```text
X-API-Key
X-Request-Id
Idempotency-Key
```

---

## 14.1 Success envelope

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid",
    "timestamp": "UTC ISO-8601"
  }
}
```

---

## 14.2 Error envelope

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

---

# 15. Superfícies públicas

Somente famílias definidas no **baseline normativo vigente** podem ser expostas.

O baseline é composto pelo produto original mais features posteriores explicitamente APPROVED.

Não invente endpoints de conveniência.

---

## 15.1 Knowledge Memory e pipelines

Famílias existentes incluem:

- health;
- info;
- datasets;
- remember;
- cognify;
- recall;
- feedback;
- improve;
- forget;
- runs;
- graph;
- provenance.

---

## 15.2 Sessions

Sessions são durable temporal context.

Incluem:

- Session management;
- SessionEntries;
- associações first-class com Query/PipelineRun.

SessionEntry permanece temporal.

Nunca promover automaticamente para memória persistente.

---

## 15.3 Skills

Skills representam durable procedural memory.

v0.4.0 implementado.

Management inclui:

- create;
- get;
- list;
- PATCH;
- archive/restore;
- revisions;
- SKILL.md import/export;
- semantic resolve.

Semantic resolve:

- descobre;
- ranqueia;
- não escolhe em nome do caller;
- não executa tool;
- não executa procedure.

Fora do domínio:

- `SkillRun`;
- runtime de Skill;
- Agent Runtime.

---

## 15.4 Agents

Agent Profile management é durable identity/profile management.

Não é Agent Runtime.

Inclui:

- management;
- Agent↔Skill;
- Agent↔Session.

Essas relações:

- não são authorization;
- não significam execução;
- não são provenance de participação;
- não atribuem Query/PipelineRun a Agent.

Fora de escopo:

- AgentRun;
- `/execute`;
- `/chat`;
- `/invoke`;
- tool authorization;
- provider session management.

---

## 15.5 Native Cognitive Memory

Native Cognitive Memory v0.7.0 é first-class.

`MemoryItem` suporta:

```text
profile
semantic
```

Scopes:

```text
global
project:<key>
```

Lifecycle:

```text
active
superseded
forgotten
```

Superfície:

```http
POST /memories
GET  /memories/{memory_id}
POST /memories/recall
POST /memories/{memory_id}/supersede
POST /memories/{memory_id}/forget
```

Características:

- pgvector cosine exato;
- provenance first-class;
- `confidence`;
- temporal truth;
- idempotência HMAC;
- PostgreSQL-only;
- sem graph_outbox;
- sem PipelineRun.

Não adicionar:

- PATCH;
- semantic dedupe;
- auto conflict resolution;
- consolidation;
- decay;
- generic tenancy/ACL.

---

# 16. Contextual Project Memory — public contract

O Contract v1 APPROVED prevê:

```text
/api/v1/episodes
/api/v1/context/checkpoints
/api/v1/context/recall
```

Isso não significa que todas essas rotas estejam autorizadas a aparecer imediatamente.

A implementação ocorre por Slices.

---

## 16.1 Contract activation gate

Enquanto o activation gate de `SM-CTX-008` não tiver passado, a aplicação MUST NOT anunciar o Contract nem qualquer uma das cinco capabilities contextuais em `GET /api/v1/info`:

```json
{
  "contracts": {
    "contextual_project_memory": "1"
  }
}
```

como Contract completamente implementado.

Os nomes públicos pertencem ao Contract `05`, seção 6; não inventar capabilities
parciais. `api_contract_version` permanece `"1"` após a ativação.

Não deixe Consumers confundirem:

```text
partial implementation
```

com:

```text
full Contract v1 support
```

---

## 16.2 Slice gating

Uma rota/contextual capability só pode ser implementada quando estiver incluída no Slice APPROVED atual.

Não implementar antecipadamente APIs de Slices futuros só porque o Contract já as descreve.

---

# 17. Current implementation slice

CURRENT PHASE:

```text
Implementation
```

CURRENT ROLE:

```text
Senior Software Engineer
```

CURRENT SLICE:

```text
SM-CTX-001 — First-Class Episodes
```

Implementation handoff APPROVED:

```text
docs/product-implementation/implementation/
09-SF-Implementation-SM-CTX-001-First-Class-Episodes.md
```

Este documento deve ser lido integralmente antes de modificar código relacionado ao Slice.

---

## 17.1 Único escopo atualmente autorizado

Durante `SM-CTX-001`, implementar somente Episode first-class:

```text
Episode
├── create
├── get
├── list
├── internal semantic retrieval
├── invalidate
├── direct forget
├── provenance
├── idempotency
└── durable PostgreSQL persistence
```

---

## 17.2 Explicitamente fora do Slice

MUST NOT implementar em SM-CTX-001:

```text
Evidence
ContextCheckpoint
ContextStatement
Contextual Recall
Forget propagation to derived resources
automatic Episode extraction
automatic Session distillation
automatic consolidation
Neo4j projection
```

Também não implementar antecipadamente:

```text
SM-CTX-002+
```

---

## 17.3 Próximo Slice não é autorização implícita

A existência de:

```text
SM-CTX-002
SM-CTX-003
...
SM-CTX-008
```

no Technical Backlog não autoriza sua implementação.

Cada Slice requer seu próprio implementation handoff APPROVED **e** autorização
explícita como CURRENT. `future handoff existence != authorization`.
Ler `10–16` para contexto não autoriza implementá-los durante SM-CTX-001.

---

## 17.4 Scope discipline

Boa ideia fora do Slice:

```text
DEFERRED
```

Não implemente silenciosamente.

Se uma necessidade exigir mudança em:

- Contract;
- Architecture;
- ADR;
- lifecycle;
- security;
- migration semantics;

pare e reporte.

---

# 18. Prefixos e superfícies proibidas

OpenAPI deve falhar se aparecer sem decisão explícita:

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
/proposals
```

`/skills`, `/agents`, `/memories` e as superfícies Contextual Project Memory autorizadas por Slice não são proibidas.

Continuam proibidos runtime-shaped endpoints como:

```text
/run-agent
/execute-agent
/chat-agent
/invoke-agent
/tools/authorize
```

ou equivalentes que introduzam runtime no Memory.

---

# 19. Pydantic schemas

Separe schemas públicos de persistence models.

Regras:

- nunca exponha SQLAlchemy model diretamente;
- request/response explícitos;
- `extra="forbid"` por padrão;
- validators centralizados quando houver regra compartilhada;
- enums estáveis;
- exemplos válidos;
- compatibilidade dentro do Contract version;
- breaking change exige decisão explícita.

Schemas seguem Contract/Spec, não DTO legado do Cognee.

---

# 20. SQLAlchemy e Alembic

Alembic é a única autoridade de evolução do schema.

Nunca utilizar:

```python
Base.metadata.create_all()
```

como mecanismo de produção/startup.

Cada mudança de schema deve:

1. possuir migration;
2. upgrade determinístico;
3. downgrade quando seguro;
4. ser testada em PostgreSQL real;
5. criar/remover constraints explicitamente;
6. criar/remover indexes explicitamente;
7. preservar dados ou documentar perda.

---

## 20.1 Migration head

Nunca assuma o número da próxima migration sem verificar o repositório.

Antes de criar migration:

```bash
uv run alembic heads
uv run alembic current
```

Se um implementation handoff indicar um head esperado e o repositório tiver avançado:

> use o head real e registre a divergência no Implementation Report.

Não reescreva migration já aplicada em ambiente compartilhado.

---

## 20.2 pgvector

Dimensão deve permanecer compatível com:

```text
EMBEDDING_DIMENSIONS
```

Mudança de dimensão exige migration/reindex planejado.

---

## 20.3 Automatic serialized migration bootstrap

`DATABASE_MIGRATION_MODE`:

```text
auto
verify_only
```

`auto`:

- migra pristine/known ancestor;
- usa advisory lock;
- verifica antes/depois;
- fail-closed em estado ambíguo.

`verify_only`:

- nunca invoca Alembic automaticamente;
- exige exact head.

Nunca:

- stamp automático de schema desconhecido;
- inferência de revision;
- reparo silencioso de multiple heads.

CLI Alembic manual permanece suportada.

---

# 21. Repositories e transações

Não espalhe ORM por:

- routes;
- pipelines;
- schemas.

Use repositories e boundaries transacionais claros.

Repository:

- persiste;
- consulta;
- não decide regra de produto.

Service:

- coordena caso de uso;
- aplica lifecycle;
- aplica idempotência;
- controla boundary transacional.

---

## 21.1 PostgreSQL + Neo4j

Quando uma feature realmente usar projeção:

```text
1. persistir PostgreSQL
2. gravar graph_outbox na mesma transaction
3. commit
4. worker projeta Neo4j
5. marcar evento
```

Não utilizar distributed transaction.

Contextual Project Memory v1 não usa esse fluxo porque não possui graph projection.

---

# 22. Pipeline engine

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
- progresso persistido.

Não adicionar:

- Redis;
- Celery.

Contextual Project Memory mutations v1 não são PipelineRuns.

---

# 23. Knowledge Memory

## 23.1 Remember/full

Fluxo:

```text
validate
→ persist source
→ extract/normalize
→ chunk
→ embeddings
→ summaries
→ entities/relations
→ canonicalize
→ persist PostgreSQL
→ graph outbox
→ projection
→ activate generation
```

---

## 23.2 Remember/ingest

Persiste conteúdo normalizado para cognify posterior.

---

## 23.3 Cognify

Processa Sources pendentes/selecionadas.

Config de pipeline não deve vir do request.

---

## 23.4 Recall

Modos existentes:

```text
chunks
summaries
rag
graph
hybrid
triplets
```

Não introduzir arbitrariamente:

- Cypher público;
- agentic completion;
- automatic tool execution;
- generic LLM router.

---

## 23.5 Improve

É explícito.

Não executar escondido em:

- session end;
- request lifecycle.

---

## 23.6 Forget

Deve respeitar lifecycle e cleanup aprovado.

A integração futura com Contextual Evidence está definida por ADR-0019 e pelo Slice correspondente.

Não antecipar em SM-CTX-001.

---

# 24. Provenance e integridade

Conhecimento derivado deve ser rastreável.

Para Knowledge Memory:

```text
query/answer
→ retrieved evidence
→ chunk
→ document
→ source
```

Para Cognitive Memory:

```text
MemoryItem
→ first-class cognitive provenance
```

Para Contextual Project Memory:

```text
Episode / ContextStatement
→ contextual provenance
→ Evidence
```

quando Evidence for implementada.

Não gere certeza sem evidência.

Não confunda provenance com authority.

---

# 25. Segurança de ingestão

Formatos suportados pelo baseline:

- text;
- TXT;
- MD;
- JSON;
- CSV;
- HTML;
- textual PDF;
- DOCX;
- HTTPS URL.

Fora do baseline:

- OCR/images;
- audio/video;
- PPTX/XLSX;
- ZIP;
- Git repositories.

URL ingestion deve proteger contra:

- SSRF;
- loopback;
- link-local;
- private networks;
- cloud metadata endpoints;
- DNS rebinding;
- redirects proibidos.

Uploads devem proteger contra:

- path traversal;
- MIME spoofing;
- payload excessivo;
- arquivo malformado.

Conteúdo ingerido é untrusted data.

Prompt injection em documento nunca pode alterar:

- system prompt;
- secrets;
- config;
- tools;
- pipeline rules;
- permissions.

A mesma regra vale para conteúdo de memória recuperado.

---

# 26. LLM e embeddings

Use apenas protocolo OpenAI-compatible.

Não criar provider registry genérico.

Structured output:

```text
model response
→ Pydantic validation
→ controlled repair/retry when allowed
→ typed failure
```

Nunca persistir JSON parcial/inválido como conhecimento válido.

---

## 26.1 External calls and transactions

Não mantenha transaction PostgreSQL aberta durante chamadas externas sem justificativa arquitetural explícita.

Padrão:

```text
external embedding/LLM call
        ↓
short PostgreSQL transaction
```

quando possível.

Native Cognitive Memory já usa esse padrão.

Contextual Episodes deve seguir o mesmo princípio.

---

# 27. Idempotência

Writes relevantes devem ser idempotentes quando Contract exigir.

Não confundir:

```text
idempotency
```

com:

```text
semantic dedupe
```

Conteúdo idêntico não implica necessariamente identidade de recurso.

---

## 27.1 Idempotency-Key

Quando obrigatório pelo Contract:

```http
Idempotency-Key
```

deve proteger retry.

Same key + same semantic request:

```text
same logical result
```

Same key + different semantic request:

```text
IDEMPOTENCY_CONFLICT
```

Correctness deve ser database-authoritative.

Um pre-check não substitui:

```text
UNIQUE constraint / authoritative claim
```

---

## 27.2 HMAC

Request digest sensível deve usar:

```text
HMAC-SHA-256
```

ou propriedade criptográfica equivalente aprovada.

Nunca persistir:

- raw request;
- raw semantic content;
- raw Idempotency-Key-derived content hash.

Não utilizar SHA-256 não keyed para request digest sensível.

---

# 28. Testes

Testes existem para proteger comportamento real, não para aumentar contagem.

---

## 28.1 Unit

Cobrir lógica pura quando relevante:

- normalization;
- schemas;
- lifecycle;
- canonicalization;
- HMAC/idempotency;
- ranking;
- context budgeting;
- state transition;
- dependency evaluation.

---

## 28.2 Integration

Usar PostgreSQL+pgvector real onde comportamento depende do banco.

Neo4j real somente quando a feature realmente usa Neo4j.

Contextual Project Memory PostgreSQL-only não deve adquirir dependência artificial de Neo4j em seus testes.

Cobrir:

- migrations;
- repositories;
- transactions;
- constraints;
- concurrency;
- restart;
- Forget;
- recovery.

---

## 28.3 Contract/OpenAPI

Testar:

- rotas da fase/Slice;
- métodos exatos;
- forbidden routes;
- envelopes;
- headers;
- stable errors;
- breaking changes.

Uma rota não autorizada pelo Slice deve ser tratada como possível scope leak.

---

## 28.4 Testes PostgreSQL opt-in

Use banco dedicado.

Não execute testes destrutivos contra banco de desenvolvimento compartilhado.

Quando criar nova suite opt-in:

- use env flag claro;
- use DATABASE_URL dedicado;
- documente execução.

---

## 28.5 Antes de concluir

Execute o conjunto aplicável:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

Quando houver migration:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Se algum check não puder rodar:

- diga qual;
- diga por quê;
- diga o risco residual.

Nunca marque teste não executado como PASS.

---

# 29. Human Validation

Tests green não significam, sozinhos, feature pronta.

Quando o implementation handoff exigir Human Validation, prove comportamento através da API/ambiente real relevante.

Human Validation deve verificar o valor prometido pelo Slice.

Não substituir por:

- mocks;
- unit tests;
- inspection do código.

---

# 30. Docker e deployment

## 30.1 `compose.yaml`

Fonte canônica e portável.

Serviços baseline:

```text
sofias-memory
postgres
neo4j
```

Ausência de dependência funcional contextual de Neo4j não significa remover Neo4j do produto-base.

---

## 30.2 EasyPanel

Use `compose.yaml` diretamente quando possível.

Arquivo específico somente quando houver diferença necessária e documentada.

---

## 30.3 Portainer

Mesma regra.

Não criar divergência funcional entre deployments.

---

## 30.4 Regras

- apenas API publicada externamente em deployment normal;
- bancos em rede interna;
- volumes persistentes;
- healthchecks;
- app non-root;
- secrets fora do Git;
- production tags imutáveis;
- `docker compose config` deve passar.

---

# 31. Licença e Cognee upstream

Cognee é Apache-2.0.

Ao copiar/adaptar código:

- registrar origem quando aplicável;
- preservar notices/licenças necessários;
- documentar modificação relevante;
- não copiar módulo desnecessário;
- adaptar naming;
- remover autenticação/multitenancy de fato quando incompatível com Sofias Memory;
- manter `LICENSE`;
- manter `NOTICE.md`.

Não acompanhar upstream novo silenciosamente durante uma feature.

---

# 32. Git e escopo

Não fazer sem solicitação explícita:

- commit;
- push;
- PR;
- merge.

Também não:

- reformate arquivos não relacionados;
- atualize dependencies sem relação com a tarefa;
- reescreva migration aplicada;
- altere PRD/Contract para fazer código caber;
- altere teste para esconder incompatibilidade;
- inclua feature futura no Slice atual.

Faça diffs pequenos e intencionais.

---

# 33. Fluxo de trabalho do agente

Em tarefa não trivial:

```text
1. identify current phase/slice
2. read AGENTS.md
3. read approved implementation handoff
4. read relevant approved baseline
5. inspect current code/migrations/tests
6. establish repository state
7. list invariants
8. implement smallest complete vertical path
9. integrate real boundaries
10. add required protection/tests
11. run applicable checks
12. inspect diff
13. report facts, findings and risks
```

Não entregue:

- fake success;
- scaffold que pareça funcional;
- TODO crítico escondido;
- teste não executado como PASS;
- feature futura misturada.

---

# 34. Implementation Slices

Contextual Project Memory é implementada nesta sequência APPROVED:

```text
SM-CTX-001 — First-Class Episodes

SM-CTX-002 — Context Streams and Initial Checkpoints

SM-CTX-003 — Checkpoint Revision, Supersession and Concurrency

SM-CTX-004 — First-Class Evidence and Deterministic Support

SM-CTX-005 — Provenance-Aware Forget Propagation

SM-CTX-006 — Contextual Recall Core

SM-CTX-007 — Explain, History and Knowledge Integration

SM-CTX-008 — Contract Hardening and Longitudinal Validation
```

Essa lista representa planejamento.

Não representa autorização automática.

Somente o Slice explicitamente CURRENT cujo implementation handoff estiver
APPROVED pode ser executado. Atualmente é apenas `SM-CTX-001`, handoff `09`.
Esta autorização de implementação não substitui o escopo da tarefa atual do
usuário: uma auditoria documentation-only não autoriza executar Slice algum.

---

# 35. SM-CTX-001 guardrails

Durante o Slice atual:

```text
SM-CTX-001
```

MUST:

- criar Episode first-class;
- usar PostgreSQL + pgvector;
- manter embedding boundary fora da transaction;
- implementar lifecycle;
- implementar provenance;
- implementar idempotência;
- implementar direct Forget;
- garantir tombstone;
- implementar exact cosine retrieval interno;
- manter API bounded;
- sobreviver a restart.

MUST NOT:

- implementar Evidence;
- implementar Checkpoints;
- implementar Contextual Recall;
- criar graph projection;
- criar PipelineRun;
- criar new datastore;
- adicionar `memory_type=episodic`;
- alterar Session semantics;
- criar automatic promotion.

O implementation handoff detalhado é autoridade:

```text
docs/product-implementation/implementation/
09-SF-Implementation-SM-CTX-001-First-Class-Episodes.md
```

---

# 36. Findings

Durante implementação, classifique descobertas.

## BLOCKER

Impede fechamento do Slice.

Exemplos:

- Contract impossível;
- migration insegura;
- lifecycle incorreto;
- idempotency race;
- Forget leakage;
- regression crítica.

---

## IMPORTANT

Afeta diretamente o objetivo atual e merece correção no Slice quando localizada e coerente com o escopo.

---

## MINOR

Não impede o valor prometido.

---

## DEFERRED

Fora do Slice.

Não expanda escopo para “resolver” DEFERRED.

---

# 37. Quando voltar ao usuário

Não interrompa por detalhes locais e reversíveis.

Decida autonomamente:

- naming interno coerente;
- organização de arquivos;
- refactor pequeno;
- helper local;
- teste necessário;
- implementação equivalente.

Volte ao usuário quando surgir necessidade de:

- mudar scope;
- mudar requisito APPROVED;
- mudar public Contract;
- nova arquitetura durável;
- novo datastore;
- nova dependência operacional relevante;
- novo required secret;
- impacto de security/authority;
- migration destrutiva;
- alterar lifecycle aprovado;
- alterar Forget semantics;
- introduzir Neo4j em Contextual Memory;
- introduzir automatic behavior;
- implementar Slice futuro.

---

# 38. Definition of Done de um Slice

Um Slice só está DONE quando, conforme aplicável:

- objective implementado;
- acceptance criteria satisfeitos;
- required tests executados;
- tests relevantes PASS;
- migration validada;
- OpenAPI coerente;
- error semantics coerentes;
- idempotência validada;
- concurrency validada;
- restart/recovery validado;
- Human Validation concluída quando exigida;
- nenhum BLOCKER;
- remaining findings classificados;
- valor prometido realmente utilizável.

Quantidade de código ou testes não é medida de conclusão.

---

# 39. Implementation Report

Ao finalizar um Slice, reporte fatos.

Formato esperado deve seguir o implementation handoff.

No mínimo:

```text
Implemented
Changed files
Database
Verified
Acceptance Criteria
Findings
Known issues
Human validation
Git
```

Não avançar para o próximo Slice automaticamente.

---

# 40. Regra final

Entre duas soluções, prefira a que:

1. respeita o Slice APPROVED;
2. mantém PostgreSQL como fonte de verdade;
3. mantém Neo4j reconstruível;
4. reduz caminhos de execução;
5. aumenta provenance;
6. mantém lifecycle explícito;
7. torna falhas recuperáveis;
8. preserva idempotência;
9. é testável com infraestrutura real;
10. evita abstrações especulativas;
11. não transforma Memory em Agent Runtime;
12. não antecipa features futuras.

O objetivo não é construir o sistema mais genérico.

O objetivo é construir o **Sofias Memory correto**, um Slice verificável de cada vez.
