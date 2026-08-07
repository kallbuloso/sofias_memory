# Sofias Memory — Backlog Técnico Executável B0/B1/B2

**Documento:** Backlog técnico inicial  
**Escopo:** B0 Repository Foundation + B1 Application Foundation + B2 PostgreSQL Foundation  
**Status:** Pronto para execução pelo Codex  
**Pré-requisitos:** `AGENTS.md` e `docs/product/Sofias_Memory_PRD_SPECS.md` no repositório  
**Regra:** executar uma task por vez, respeitando dependências e gates.

---

## 1. Objetivo deste backlog

Este backlog transforma a fundação do Sofias Memory em unidades pequenas de trabalho que
podem ser entregues individualmente ao Codex sem exigir que o agente reinterprete todo o
PRD a cada execução.

O objetivo das fases B0/B1/B2 não é implementar memória semântica ainda.

Ao final deste backlog devemos possuir:

- repositório Python organizado;
- documentação de autoridade;
- tooling reproduzível;
- aplicação FastAPI inicial;
- configuração validada no startup;
- API key estática;
- request IDs e erros padronizados;
- health/info;
- containers e Compose oficiais;
- PostgreSQL assíncrono;
- Alembic;
- extensões PostgreSQL;
- schema autoritativo inicial;
- repositories fundamentais;
- unit of work;
- infraestrutura de readiness;
- testes de migrations e schema.

Somente depois disso o projeto deve avançar para Neo4j, contratos completos, pipeline
engine e ingestão.

---

# 2. Regras gerais de execução

Para cada task:

1. ler `AGENTS.md`;
2. ler as referências do PRD indicadas na task;
3. inspecionar o estado atual do repositório;
4. implementar somente o escopo da task;
5. não antecipar tasks futuras;
6. adicionar testes aplicáveis;
7. executar validações;
8. revisar o diff;
9. reportar arquivos alterados, testes executados e pendências reais.

Não fazer commit, push ou PR sem solicitação explícita.

## 2.1 Estados

Use estes estados no acompanhamento manual:

```text
TODO
IN_PROGRESS
BLOCKED
DONE
```

## 2.2 Prioridade

Todas as tasks deste documento são `P0` para a fundação, salvo indicação contrária.

## 2.3 Definition of Done mínima por task

Uma task só pode ser marcada `DONE` quando:

- escopo implementado;
- testes aplicáveis criados/atualizados;
- `ruff` passa;
- `mypy` passa no código afetado;
- `pytest` aplicável passa;
- documentação afetada foi atualizada;
- nenhuma decisão proibida do `AGENTS.md` foi violada.

---

# 3. Ordem de execução

```text
B0
SM-001
  ↓
SM-002
  ↓
SM-003
  ↓
SM-004
  ↓
SM-005
  ↓
SM-006
  ↓
SM-007
  ↓
SM-008
  ↓
GATE-B0

B1
SM-101
  ↓
SM-102
  ↓
SM-103
  ↓
SM-104
  ↓
SM-105
  ↓
SM-106
  ↓
SM-107
  ↓
SM-108
  ↓
SM-109
  ↓
SM-110
  ↓
GATE-B1

B2
SM-201
  ↓
SM-202
  ↓
SM-203
  ↓
SM-204
  ↓
SM-205
  ↓
SM-206
  ↓
SM-207
  ↓
SM-208
  ↓
SM-209
  ↓
SM-210
  ↓
SM-211
  ↓
SM-212
  ↓
SM-213
  ↓
SM-214
  ↓
SM-215
  ↓
GATE-B2
```

Tasks independentes podem ser paralelizadas somente quando não alterarem os mesmos
arquivos ou contratos.

---

# 4. B0 — Repository Foundation

---

## SM-001 — Criar estrutura raiz do repositório

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** nenhuma  
**PRD:** arquitetura de software / estrutura do projeto  
**AGENTS:** estrutura obrigatória do repositório

### Objetivo

Criar a árvore inicial do projeto sem layout `src/`.

### Entregáveis esperados

```text
AGENTS.md
README.md
pyproject.toml
alembic.ini
Dockerfile
compose.yaml
compose.easypanel.yaml
compose.portainer.yaml
.env.example

sofias_memory/
    __init__.py
    api/
    domain/
    services/
    pipelines/
    loaders/
    infrastructure/
    prompts/
    schemas/

migrations/
tests/
    unit/
    integration/
    contract/
    e2e/
    security/
    performance/

scripts/

docs/
    product/
    adr/
    exec-plans/
        active/
        completed/
    generated/
```

Arquivos que ainda não possuem implementação podem conter apenas estrutura mínima
necessária para o Git preservá-los quando aplicável.

### Restrições

- NÃO criar `src/`;
- NÃO criar package `cognee`;
- NÃO copiar árvore do Cognee;
- NÃO criar `plugins/`;
- NÃO criar frontend/MCP.

### Critérios de aceite

- `sofias_memory` é importável como package;
- não existe diretório `src`;
- estrutura bate com `AGENTS.md`;
- nenhum módulo proibido é criado.

### Validação

```bash
python -c "import sofias_memory"
find . -maxdepth 2 -type d | sort
```

---

## SM-002 — Instalar PRD, AGENTS e estrutura documental

**Status:** TODO  
**Dependências:** SM-001  
**PRD:** documento completo  
**AGENTS:** fontes de verdade

### Objetivo

Garantir que o Codex encontre as especificações dentro do próprio repositório.

### Entregáveis

```text
AGENTS.md
docs/product/Sofias_Memory_PRD_SPECS.md
docs/adr/README.md
docs/exec-plans/README.md
```

### Conteúdo mínimo

`docs/adr/README.md` deve explicar:

- o que é um ADR;
- quando criar;
- estados `proposed`, `accepted`, `superseded`;
- padrão de nome: `NNNN-short-title.md`.

`docs/exec-plans/README.md` deve explicar:

- quando usar plano de execução;
- `active/`;
- `completed/`;
- plano não substitui PRD/ADR.

### Critérios de aceite

- PRD pode ser encontrado sem acesso externo;
- `AGENTS.md` aponta para o caminho real do PRD;
- não existem duas cópias concorrentes do PRD no repositório.

---

## SM-003 — Criar ADRs arquiteturais mandatórios

**Status:** TODO  
**Dependências:** SM-002  
**PRD:** decisões mandatórias + Epic 0

### Objetivo

Congelar decisões que não podem ser reinterpretadas por futuros agentes.

### Entregáveis

```text
docs/adr/0001-modular-monolith.md
docs/adr/0002-postgresql-source-of-truth-neo4j-projection.md
docs/adr/0003-single-static-api-key.md
docs/adr/0004-openai-compatible-only.md
docs/adr/0005-no-optional-dependencies.md
```

### Cada ADR deve conter

- contexto;
- decisão;
- consequências;
- alternativas rejeitadas;
- status `accepted`;
- referências ao PRD.

### Critérios de aceite

Os ADRs devem proibir explicitamente a reintrodução casual de:

- auth;
- permissions;
- tenants;
- DB providers;
- plugin system;
- cloud/sync.

---

## SM-004 — Configurar `pyproject.toml` e `uv`

**Status:** TODO  
**Dependências:** SM-003  
**PRD:** dependências Python propostas

### Objetivo

Criar ambiente Python reproduzível usando `uv`.

### Requisitos

```toml
requires-python = ">=3.12,<3.13"
```

Adicionar dependências runtime do PRD:

- fastapi;
- uvicorn[standard];
- pydantic;
- pydantic-settings;
- sqlalchemy[asyncio];
- asyncpg;
- alembic;
- pgvector;
- neo4j;
- openai;
- httpx;
- tenacity;
- structlog;
- orjson;
- python-multipart;
- aiofiles;
- filetype;
- tiktoken;
- pypdf;
- python-docx;
- beautifulsoup4;
- lxml;
- charset-normalizer;
- prometheus-client.

Dev:

- pytest;
- pytest-asyncio;
- pytest-cov;
- testcontainers[postgres,neo4j];
- ruff;
- mypy;
- pip-audit;
- bandit.

### Restrições

Não criar:

```toml
[project.optional-dependencies]
```

### Entregáveis

```text
pyproject.toml
uv.lock
```

### Critérios de aceite

```bash
uv sync --dev
uv run python -c "import sofias_memory"
```

passam em Python 3.12.

---

## SM-005 — Configurar qualidade estática e testes

**Status:** TODO  
**Dependências:** SM-004

### Objetivo

Fazer o projeto falhar cedo quando qualidade básica for quebrada.

### Configurar

- Ruff lint;
- Ruff format;
- mypy;
- pytest;
- pytest-asyncio;
- coverage.

### Convenções mínimas

- line length consistente;
- Python 3.12;
- `asyncio_mode` explícito;
- diretórios de migrations tratados adequadamente;
- typing estrito progressivo, sem desligar mypy globalmente.

### Entregáveis

Configurações dentro de:

```text
pyproject.toml
```

e testes smoke mínimos em:

```text
tests/unit/
```

### Validação

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest tests/unit
```

---

## SM-006 — Criar higiene Git e ambiente de desenvolvimento

**Status:** TODO  
**Dependências:** SM-005  
**PRD:** configuração `.env`

### Objetivo

Evitar vazamento de secrets e documentar a stack local existente.

### Entregáveis

```text
.gitignore
.env.example
README.md
```

### `.gitignore`

Deve ignorar no mínimo:

- `.env`;
- `.venv`;
- caches Python;
- coverage;
- build/dist;
- logs;
- dados locais;
- arquivos temporários;
- IDEs comuns sem excluir código útil.

### `.env.example`

Usar nomes do PRD e valores seguros/fictícios.

Não inserir senha real.

### README — desenvolvimento local

Documentar a stack existente:

```text
stack: sofias_memory_db
PostgreSQL: 127.0.0.1:5440
database: cognee_db
user: cognee

Neo4j Bolt: 127.0.0.1:7688
database: neo4j
user: neo4j
```

Exemplos:

```env
DATABASE_URL=postgresql+asyncpg://cognee:<DB_PASSWORD>@127.0.0.1:5440/cognee_db
NEO4J_URI=bolt://127.0.0.1:7688
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<DB_NEO4J_PASSWORD>
NEO4J_DATABASE=neo4j
```

### Registrar problema conhecido

A stack recebida usa:

```text
NEO4J_AUTH=neo4j/${DB_NEO4J_PASSWORD}
```

mas o healthcheck original consulta com `${DB_PASSWORD}`.

Documentar que o healthcheck correto usa:

```text
${DB_NEO4J_PASSWORD}
```

### Critérios de aceite

- `.env` não é rastreável;
- nenhuma credencial real foi adicionada;
- portas 5440/7688 aparecem apenas em documentação/config local, nunca hard-coded no código.

---

## SM-007 — Licença, NOTICE e baseline upstream

**Status:** TODO  
**Dependências:** SM-002  
**PRD:** licença e propriedade intelectual

### Objetivo

Preparar o repositório para eventual reutilização legal de código Apache-2.0.

### Entregáveis

```text
LICENSE
NOTICE.md
docs/product/upstream-baseline.md
```

### `upstream-baseline.md`

Registrar:

```text
repository: topoteretes/cognee
branch: main
version: 1.4.1
commit: 38eece5bbb0cb9f5706fed908abd16dba0f5505e
```

Registrar política:

- não acompanhar upstream silenciosamente;
- toda cópia/adaptação deve ter origem identificável;
- módulos desnecessários não são copiados.

### Critérios de aceite

- Apache-2.0 presente;
- NOTICE inicial presente;
- baseline congelado.

---

## SM-008 — Criar scripts operacionais mínimos

**Status:** TODO  
**Dependências:** SM-004, SM-006

### Objetivo

Criar ferramentas pequenas que serão usadas durante a fundação.

### Entregáveis

```text
scripts/generate_api_key.py
scripts/verify_installation.py
```

### `generate_api_key.py`

Deve:

- gerar prefixo `sf-`;
- usar CSPRNG;
- gerar pelo menos 32 caracteres aleatórios após prefixo;
- imprimir somente a chave gerada;
- não persistir chave.

### `verify_installation.py`

Nesta fase deve validar apenas:

- Python correto;
- package importável;
- settings carregáveis quando ambiente estiver preenchido.

Conectividade com bancos será adicionada em B2/B3.

### Testes

Criar teste para formato/entropia estrutural da chave.

---

# GATE-B0 — Repository Foundation concluída

Antes de B1:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest tests/unit
```

Além disso:

- `src/` não existe;
- PRD e AGENTS estão no repo;
- ADR-0001..0005 aceitos;
- `.env` ignorado;
- `uv.lock` versionado;
- licença presente.

---

# 5. B1 — Application Foundation

---

## SM-101 — Implementar Settings tipado

**Status:** TODO  
**Dependências:** GATE-B0  
**PRD:** FR-001, configuração `.env`

### Objetivo

Centralizar toda configuração da aplicação.

### Arquivos esperados

```text
sofias_memory/config.py
tests/unit/test_config.py
```

### Requisitos

Usar `pydantic-settings`.

Cobrir grupos:

- application;
- PostgreSQL;
- Neo4j;
- storage;
- LLM;
- embeddings;
- chunking;
- retrieval;
- worker;
- privacy.

### Validações obrigatórias

- `API_KEY` começa com `sf-`;
- mínimo de 32 caracteres aleatórios após prefixo;
- URLs válidas;
- embedding dimensions > 0;
- `CHUNK_OVERLAP_TOKENS < CHUNK_MAX_TOKENS`;
- `CHUNK_MIN_TOKENS <= CHUNK_MAX_TOKENS`;
- concurrencies >= 1;
- timeouts positivos;
- `CORS_ALLOWED_ORIGINS=""` significa desabilitado;
- `EMBEDDING_API_KEY` vazio herda `LLM_API_KEY`;
- secrets ausentes impedem startup conforme PRD.

### Segurança

Use tipos de secret do Pydantic.

`repr(settings)` não pode revelar secrets.

### Regra

Nenhum outro módulo deve interpretar `os.getenv()` diretamente.

---

## SM-102 — Implementar fingerprint seguro de configuração

**Status:** TODO  
**Dependências:** SM-101  
**PRD:** `/info`, `config_fingerprint`

### Objetivo

Gerar fingerprint estável das configurações que alteram comportamento da memória.

### Deve incluir

- modelos LLM/embedding;
- embedding dimensions;
- chunking;
- parâmetros relevantes de retrieval;
- versões de prompts quando existirem;
- versão de configuração/schema.

### Não deve incluir em claro

- API key;
- LLM key;
- embedding key;
- passwords;
- URLs com credenciais.

### Entregáveis

Função tipada e testes de estabilidade.

### Critério importante

Alterar um secret sem alterar configuração funcional não deve vazar o secret no
fingerprint ou output.

---

## SM-103 — Criar logging estruturado e redaction

**Status:** TODO  
**Dependências:** SM-101

### Arquivos esperados

```text
sofias_memory/observability/
    logging.py
```

ou localização equivalente coerente com a arquitetura.

### Requisitos

JSON estruturado com suporte a:

- timestamp;
- level;
- event;
- request_id;
- run_id;
- dataset_id;
- source_id;
- step;
- duration_ms;
- error_code.

### Redaction

Nunca logar:

- `API_KEY`;
- `LLM_API_KEY`;
- `EMBEDDING_API_KEY`;
- DB passwords;
- Neo4j password;
- documento integral;
- embeddings;
- payload LLM integral.

### Testes

Fixtures com secrets conhecidos devem provar que esses valores não aparecem no output.

---

## SM-104 — Implementar request ID middleware

**Status:** TODO  
**Dependências:** SM-103

### Objetivo

Ter correlação por request desde o primeiro endpoint.

### Regras

- aceitar `X-Request-Id` válido;
- quando ausente, gerar UUID;
- rejeitar ou substituir valores absurdos conforme política explícita;
- retornar `X-Request-Id`;
- bind no contexto de logging;
- limpar contexto ao final da request.

### Testes

- request sem header;
- request com header;
- requests concorrentes não compartilham contexto.

---

## SM-105 — Criar envelopes e exceptions da API

**Status:** TODO  
**Dependências:** SM-104  
**PRD:** API conventions

### Arquivos esperados

```text
sofias_memory/api/errors.py
sofias_memory/schemas/common.py
```

### Criar

- success envelope;
- meta;
- error envelope;
- base application exception;
- stable error codes;
- FastAPI handlers.

### Nesta fase, suportar ao menos

```text
INVALID_REQUEST
MISSING_API_KEY
INVALID_API_KEY
CONFIGURATION_ERROR
DEPENDENCY_UNAVAILABLE
INTERNAL_ERROR
```

### Regras

- traceback nunca vai ao cliente;
- `request_id` sempre presente em erro HTTP;
- detalhes internos ficam no log;
- mensagem pública é segura.

---

## SM-106 — Implementar API key middleware/dependency

**Status:** TODO  
**Dependências:** SM-105  
**PRD:** FR-002

### Objetivo

Proteger toda API privada com uma única chave.

### Requisitos

- header: `X-API-Key`;
- health endpoints públicos;
- chave ausente => `401`;
- chave inválida => `403`;
- usar `hmac.compare_digest`, `secrets.compare_digest` ou equivalente;
- nunca aceitar query string;
- nunca logar chave;
- OpenAPI terá apenas `ApiKeyAuth`.

### Testes obrigatórios

- chave correta;
- chave ausente;
- chave errada;
- chave passada por query string;
- health sem chave.

---

## SM-107 — Criar FastAPI app e lifespan

**Status:** TODO  
**Dependências:** SM-101, SM-103, SM-106

### Arquivos esperados

```text
sofias_memory/app.py
sofias_memory/lifespan.py
```

### Nesta fase

Lifespan deve:

1. carregar settings;
2. configurar logging;
3. preparar registries internos;
4. inicializar aplicação;
5. encerrar recursos registrados no shutdown.

Banco e Neo4j serão adicionados incrementalmente.

### Não fazer

- `create_all()`;
- criar usuário default;
- abrir conexões não necessárias;
- executar Cognify;
- iniciar worker ainda.

---

## SM-108 — Implementar health live e readiness framework

**Status:** TODO  
**Dependências:** SM-107  
**PRD:** FR-120

### `/health/live`

Deve:

- responder sem API key;
- não consultar DB/Neo4j/LLM;
- retornar rapidamente;
- indicar processo vivo.

### `/health/ready`

Criar framework de readiness por componentes.

Nesta fase pode informar componentes ainda não inicializados.

Estrutura sugerida:

```json
{
  "status": "not_ready",
  "components": {
    "configuration": "ready",
    "postgres": "not_initialized",
    "neo4j": "not_initialized",
    "worker": "not_initialized"
  }
}
```

O contrato final será fechado em B4.

### Regra

Não retornar secret/config completa.

---

## SM-109 — Implementar `/api/v1/info`

**Status:** TODO  
**Dependências:** SM-102, SM-106, SM-108  
**PRD:** FR-120

### Retornar

- app name;
- app version;
- build/commit quando disponível;
- component status;
- LLM model;
- embedding model;
- embedding dimensions;
- safe config fingerprint.

### Não retornar

- API key;
- provider keys;
- passwords;
- DATABASE_URL completa;
- Neo4j URI com credentials;
- caminhos sensíveis desnecessários.

### Autenticação

`/api/v1/info` exige API key.

---

## SM-110 — CORS, limites básicos e containerização inicial

**Status:** TODO  
**Dependências:** SM-107, SM-108, SM-109  
**PRD:** segurança + deployment

### CORS

- desabilitado por default;
- habilitado somente quando origens explícitas existirem;
- não usar `*` com credentials.

### Request body

Preparar limite global coerente com `MAX_REQUEST_BODY_MB`.
Streaming de uploads será tratado na ingestão, mas não deixe o servidor aceitar payload
infinito sem política.

### Dockerfile

Requisitos:

- Python 3.12;
- build reproduzível;
- `uv`;
- usuário non-root;
- somente arquivos necessários;
- healthcheck coerente;
- sem secrets na imagem.

### Compose

Criar/validar:

```text
compose.yaml
compose.easypanel.yaml
compose.portainer.yaml
```

O canônico deve conter:

```text
sofias-memory
postgres
neo4j
```

EasyPanel/Portainer não podem alterar funcionalidade.

### Atenção

A stack `sofias_memory_db` em 5440/7688 é ambiente local separado e NÃO deve ditar as
portas internas do Compose oficial.

No Compose oficial:

```text
postgres:5432
neo4j:7687
```

### Validação

```bash
docker compose -f compose.yaml config
docker compose -f compose.easypanel.yaml config
docker compose -f compose.portainer.yaml config
```

---

# GATE-B1 — Application Foundation concluída

Critérios:

- app inicia com config válida;
- app falha cedo com config inválida;
- `/health/live` público;
- `/health/ready` existe;
- `/api/v1/info` privado;
- API key ausente = 401;
- API key inválida = 403;
- secrets redigidos;
- request IDs funcionam;
- três Compose validam;
- Docker image roda non-root.

Validação:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest tests/unit tests/contract
docker compose -f compose.yaml config
docker compose -f compose.easypanel.yaml config
docker compose -f compose.portainer.yaml config
```

---

# 6. B2 — PostgreSQL Foundation

---

## SM-201 — Gate de persistência: congelar estratégia pgvector e extensões

**Status:** TODO  
**Dependências:** GATE-B1  
**Tipo:** ARCHITECTURE GATE  
**PRD:** FR-001, chunks/entities/relations/summaries, embeddings

### Problema 1 — `CITEXT`

O schema de `datasets.name` usa `CITEXT`.

Portanto a migration precisa habilitar:

```sql
CREATE EXTENSION IF NOT EXISTS citext;
```

Além das extensões explicitamente exigidas no PRD:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

### Problema 2 — `VECTOR(3072)` + ANN

O PRD usa `VECTOR(3072)` e recomenda HNSW/IVFFlat.

Antes de criar a migration final, congelar uma estratégia compatível com pgvector.

### Estratégia recomendada

Preservar embeddings completos:

```text
embedding VECTOR(3072)
```

e usar índice ANN half-precision por expressão:

```text
embedding::halfvec(3072)
```

com HNSW e cosine/inner-product conforme a política escolhida.

Fluxo de retrieval futuro:

1. ANN em halfvec para candidatos;
2. hidratar candidatos;
3. opcionalmente rerank usando o `VECTOR(3072)` original.

### Alternativas que NÃO podem ser escolhidas silenciosamente

- reduzir embedding para 1536;
- trocar modelo;
- remover HNSW;
- trocar Postgres por vector DB;
- armazenar dimensão diferente do PRD.

Qualquer alternativa exige ADR.

### Entregável

```text
docs/adr/0006-pgvector-3072-indexing.md
```

### Protótipo obrigatório

Criar teste/SQL mínimo que prove:

- coluna 3072 aceita insert;
- índice escolhido pode ser criado;
- query utiliza sintaxe compatível;
- distância retorna resultado esperado.

Só depois marcar SM-201 como `DONE`.

---

## SM-202 — Congelar enums e políticas de FK

**Status:** TODO  
**Dependências:** SM-201  
**Tipo:** SCHEMA GATE

### Objetivo

Evitar que cada migration invente seus próprios estados.

### Enums já explícitos no PRD

```text
DatasetStatus:
  active
  deleting
  deleted

SourceKind:
  text
  file
  url

SummaryTargetType:
  document
  entity
  dataset
  cluster

MemoryEntryType:
  text
  qa
  feedback
  note

RunStatus:
  queued
  running
  succeeded
  failed
  cancelling
  cancelled

GraphOutboxOperation:
  upsert
  delete

GraphOutboxStatus:
  pending
  processing
  done
  failed
```

### Enums não totalmente definidos no PRD

Antes das migrations, definir explicitamente:

- `SourceStatus`;
- `PipelineType`;
- `PipelineStepStatus`.

Não deixe isso ser inferido em migrations diferentes.

### Política de FK

Definir por tabela:

- `RESTRICT`;
- `CASCADE`;
- `SET NULL`.

### Recomendação

Como deleção é um workflow de produto, evite usar CASCADE indiscriminadamente para
"resolver" Forget.

FK cascade pode ser usada em registros estritamente subordinados, mas deleção de
artefatos de memória deve permanecer observável e recuperável.

### Entregável

```text
docs/adr/0007-postgresql-state-machines-and-delete-policy.md
```

---

## SM-203 — Criar infraestrutura SQLAlchemy async

**Status:** TODO  
**Dependências:** SM-202

### Arquivos esperados

```text
sofias_memory/infrastructure/postgres/
    base.py
    engine.py
    session.py
    types.py
```

### Requisitos

- SQLAlchemy 2 typed declarative;
- `AsyncEngine`;
- `async_sessionmaker`;
- pool configurado por Settings;
- lifecycle explícito;
- nenhuma conexão global aberta durante import;
- naming convention estável para constraints/indexes.

### Regra importante — `metadata`

Várias tabelas do PRD possuem uma coluna SQL chamada:

```text
metadata
```

`metadata` é um nome especial/reservado no Declarative Base do SQLAlchemy.

Mapear, por exemplo:

```python
metadata_: Mapped[dict] = mapped_column("metadata", JSONB, ...)
```

Não renomear a coluna SQL do PRD silenciosamente e não tentar declarar um atributo ORM
`metadata` que conflite com `Base.metadata`.

### Testes

- engine constrói URL sem logar password;
- session abre/fecha;
- pool settings aplicados.

---

## SM-204 — Configurar Alembic assíncrono

**Status:** TODO  
**Dependências:** SM-203

### Entregáveis

```text
alembic.ini
migrations/env.py
migrations/script.py.mako
migrations/versions/
```

### Requisitos

- usar metadata dos models;
- URL vem de Settings;
- suportar asyncpg;
- migrations não dependem de `.env` hard-coded;
- logs não mostram password;
- naming convention compatível com autogenerate.

### Não fazer

- `create_all()`;
- fallback silencioso que cria schema sem Alembic.

### Validação inicial

```bash
uv run alembic heads
uv run alembic current
```

---

## SM-205 — Migration 0001: extensões e capabilities

**Status:** TODO  
**Dependências:** SM-204

### Criar

```sql
vector
pg_trgm
citext
```

### Validar

- extensão existe;
- versão do pgvector pode ser consultada;
- capabilities requeridas estão presentes.

### Downgrade

Não remover extensões compartilhadas automaticamente sem política explícita.

A migration pode deixar extensões instaladas no downgrade e documentar essa decisão.

### Teste

Executar contra PostgreSQL real, inclusive stack local em `127.0.0.1:5440` quando
configurada.

---

## SM-206 — Models/migration: datasets

**Status:** TODO  
**Dependências:** SM-205  
**PRD:** 12.1 datasets

### Colunas

```text
id UUID PK
name CITEXT UNIQUE NOT NULL
slug TEXT UNIQUE NOT NULL
description TEXT NULL
status ENUM(active,deleting,deleted)
active_generation INTEGER DEFAULT 0
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

### Regras

- sem owner;
- sem tenant;
- sem ACL;
- sem configuration;
- nome global;
- slug global.

### Testes

- uniqueness case-insensitive do name;
- uniqueness de slug;
- status default coerente;
- `owner_id`/`tenant_id` ausentes.

---

## SM-207 — Models/migration: sources e documents

**Status:** TODO  
**Dependências:** SM-206  
**PRD:** 12.2, 12.3

### `sources`

Implementar todas as colunas do PRD e:

- unique `(dataset_id, content_sha256, version)`;
- GIN `metadata`;
- index `status`.

### `documents`

Implementar todas as colunas do PRD.

Adicionar índices necessários para:

- dataset/generation;
- source/generation;
- active generation.

### Regras

- `metadata_` no Python mapeado para `"metadata"`;
- hashes com validação estrutural;
- timestamps UTC.

### Não implementar ainda

- loader;
- storage;
- normalization;
- dedup service.

Somente persistência/schema.

---

## SM-208 — Models/migration: chunks e busca lexical

**Status:** TODO  
**Dependências:** SM-207, SM-201  
**PRD:** 12.4

### Colunas

Todas as colunas do PRD, inclusive:

```text
embedding
lexical TSVECTOR
section_path TEXT[]
metadata JSONB
is_active
```

### Constraints

```text
UNIQUE(document_id, generation, ordinal)
```

### Índices

- GIN em `lexical`;
- `(dataset_id, is_active)`;
- `(source_id, is_active)`;
- índice ANN definido pelo ADR-0006.

### Regra lexical

Definir mecanismo determinístico de atualização de `lexical`.

Preferência:

- coluna gerada quando tecnicamente adequada; ou
- trigger versionado; ou
- escrita explícita pela aplicação.

A decisão deve ser consistente e testada.

### Testes

- insert de vetor com 3072 dimensões;
- rejeição de dimensão incorreta;
- query lexical básica;
- criação e uso sintático do índice ANN escolhido.

---

## SM-209 — Models/migration: entities, mentions, relations e evidence

**Status:** TODO  
**Dependências:** SM-208  
**PRD:** 12.5–12.8

### `entities`

Implementar todas as colunas.

Constraint crítica:

```text
UNIQUE PARTIAL (dataset_id, canonical_key)
WHERE is_active = true
```

### `entity_mentions`

Escolher PK própria UUID ou PK composta conforme ADR/schema gate.

A escolha deve ser explícita e estável.

### `relations`

Implementar todas as colunas.

Adicionar índices para:

- dataset/active;
- source entity;
- target entity;
- predicate quando útil ao plano de queries.

### `relation_evidence`

Implementar todas as colunas e constraints.

### Regra

A database layer não deve permitir relation apontando para entity inexistente.

---

## SM-210 — Models/migration: summaries, memory_entries, queries e feedback

**Status:** TODO  
**Dependências:** SM-209  
**PRD:** 12.9–12.12

### Implementar

```text
summaries
memory_entries
queries
feedback
```

### `summaries`

- embedding conforme ADR-0006;
- índice de retrieval quando aplicável;
- filtros dataset/generation/active.

### `memory_entries`

Não criar cache Redis/session store.
`session_id` é apenas metadado.

### `queries`

Respeitar futuro:

```env
STORE_QUERY_CONTENT=false
```

Schema continua suportando conteúdo nullable conforme necessário.

### `feedback`

Constraint:

```text
score IN (-1, 0, 1)
```

FK para query.

---

## SM-211 — Models/migration: pipeline_runs, pipeline_steps e graph_outbox

**Status:** TODO  
**Dependências:** SM-210  
**PRD:** 12.13–12.15, FR-100

### Implementar integralmente

```text
pipeline_runs
pipeline_steps
graph_outbox
```

### Índices obrigatórios a considerar

`pipeline_runs`:

- status;
- dataset/status;
- heartbeat;
- idempotency key;
- created_at.

`pipeline_steps`:

- run_id;
- `(run_id, ordinal)`;
- status.

`graph_outbox`:

- status;
- `(status, created_at)`;
- dataset;
- aggregate.

### Idempotency key

Criar constraint que permita a semântica futura de:

- mesma key + mesmo payload => replay seguro;
- mesma key + payload diferente => conflict.

Não implemente ainda o service completo, apenas schema capaz de suportá-lo.

---

## SM-212 — Teste automatizado de schema proibido

**Status:** TODO  
**Dependências:** SM-211

### Objetivo

Impedir reintrodução de multi-user por migration futura.

### O teste deve falhar se existir tabela

```text
users
roles
permissions
acl
api_keys
settings
tenants
```

### Deve falhar se qualquer tabela de domínio possuir coluna

```text
owner_id
tenant_id
```

### Também verificar

- extensão vector;
- extensão pg_trgm;
- extensão citext;
- tabelas obrigatórias presentes.

Este teste deve consultar `information_schema` / catalogs em PostgreSQL real.

---

## SM-213 — Criar Unit of Work e repositories fundamentais

**Status:** TODO  
**Dependências:** SM-211

### Objetivo

Criar boundaries transacionais sem cair no anti-pattern de repository genérico.

### Não criar

```text
GenericRepository[T]
BaseCrudRepository
UniversalRepository
```

### Criar apenas o necessário para a próxima fase

Sugestão inicial:

```text
DatasetRepository
SourceRepository
DocumentRepository
PipelineRunRepository
PipelineStepRepository
GraphOutboxRepository
UnitOfWork
```

### Regras

- nenhuma route usa SQLAlchemy diretamente;
- commit/rollback explícitos;
- outbox pode ser gravada na mesma transação de domínio;
- métodos expressam intenção de domínio, não apenas CRUD genérico.

### Testes

- commit;
- rollback;
- transaction atomicity;
- outbox + mudança de domínio na mesma transaction.

---

## SM-214 — Implementar readiness PostgreSQL

**Status:** TODO  
**Dependências:** SM-205, SM-213, SM-108

### Objetivo

Integrar PostgreSQL ao `/health/ready`.

### Verificar

- conectividade;
- Alembic revision atual;
- `vector`;
- `pg_trgm`;
- `citext`;
- embedding type/dimension esperada quando schema existir.

### Comportamento

Falha deve deixar readiness false, não derrubar `/health/live`.

### Não fazer

- query pesada;
- table scan;
- criação de schema durante health check.

---

## SM-215 — Criar integração real de migrations e schema

**Status:** TODO  
**Dependências:** SM-212, SM-213, SM-214

### Objetivo

Criar o gate automatizado de PostgreSQL.

### Testes com PostgreSQL real

Preferência CI:

```text
Testcontainers
```

Desenvolvimento:

pode usar explicitamente:

```text
127.0.0.1:5440
cognee_db
cognee
```

com credenciais fornecidas pelo `.env` local.

### Cenários

1. banco vazio;
2. `alembic upgrade head`;
3. verificar extensões;
4. verificar tabelas;
5. verificar constraints;
6. verificar schema proibido;
7. inserts mínimos;
8. vector dimension;
9. lexical search;
10. rollback da última migration quando seguro;
11. upgrade novamente.

### Regra

Testes CI não podem depender das portas locais 5440/7688.

---

# GATE-B2 — PostgreSQL Foundation concluída

Antes de iniciar B3:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
uv run alembic heads
uv run alembic current
```

Em banco descartável:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Critérios adicionais:

- `vector`, `pg_trgm`, `citext` presentes;
- schema completo B2 criado;
- `owner_id` e `tenant_id` inexistentes;
- tabelas proibidas inexistentes;
- ANN 3072 strategy comprovada;
- repositories sem abstração genérica especulativa;
- outbox transacional disponível;
- PostgreSQL participa do readiness;
- nenhuma dependência de Neo4j foi colocada no domínio.

---

# 7. Pontos técnicos que NÃO devem ser deixados para o Codex decidir sozinho

## 7.1 Embedding 3072 e HNSW

Não permita que uma task futura reduza `EMBEDDING_DIMENSIONS` apenas para fazer um índice
compilar.

A decisão deve estar congelada no ADR-0006.

## 7.2 `CITEXT`

Como `datasets.name` usa `CITEXT`, a extensão `citext` faz parte da fundação mesmo que
uma lista anterior de extensões tenha mencionado apenas `vector` e `pg_trgm`.

## 7.3 `metadata` no ORM

A coluna SQL continua chamada `metadata`, mas o atributo Python deve evitar conflito com
`DeclarativeBase.metadata`.

## 7.4 Deleção

Não desenhar FKs pensando "CASCADE resolve Forget".

Forget é workflow persistido, auditável e recuperável.

## 7.5 Local vs Docker

Host development:

```text
PostgreSQL 127.0.0.1:5440
Neo4j      127.0.0.1:7688
```

Compose oficial:

```text
postgres:5432
neo4j:7687
```

Nunca misturar esses dois contextos.

## 7.6 Dataset `main`

O dataset default `main` é criado no primeiro uso, não no startup.

B2 deve fornecer repository capability para isso futuramente, mas não deve criar `main`
durante migration ou lifespan.

---

# 8. Template para entregar uma task ao Codex

Use este formato:

```text
Implemente somente a task SM-XXX do backlog técnico do Sofias Memory.

Antes de alterar código:
1. leia AGENTS.md;
2. leia a task SM-XXX completa;
3. leia as seções do PRD referenciadas;
4. inspecione o estado atual do repositório e tasks anteriores.

Regras:
- não implemente tasks posteriores;
- não reintroduza conceitos proibidos;
- não faça commit/push/PR;
- mantenha o diff restrito ao escopo;
- adicione testes aplicáveis;
- execute os checks exigidos pelo AGENTS.md.

Ao terminar, informe:
- arquivos criados/alterados;
- decisões tomadas;
- testes executados e resultados;
- qualquer divergência encontrada entre backlog, PRD e código atual.
```

Para tasks de migration, acrescente:

```text
Use PostgreSQL real para validar.
Não use Base.metadata.create_all().
Não modifique migrations anteriores já aplicadas; crie nova revision quando necessário.
```

---

# 9. Corte recomendado das primeiras sessões com Codex

Não entregue B0 inteiro em um único prompt.

Sequência recomendada:

### Sessão 1

```text
SM-001
SM-002
```

### Sessão 2

```text
SM-003
```

### Sessão 3

```text
SM-004
SM-005
```

### Sessão 4

```text
SM-006
SM-007
SM-008
```

Executar GATE-B0.

### Sessões seguintes

Executar B1 aproximadamente uma task por vez.

Para B2, obrigatoriamente executar `SM-201` isoladamente antes das migrations.

---

# 10. O que vem depois

Este documento para em B2 deliberadamente.

Próximos backlogs:

```text
B3 — Neo4j Foundation
B4 — Pydantic/OpenAPI Contracts
B5 — Pipeline Engine
B6 — Ingestion
B7 — Cognify
B8 — Recall
B9 — Improve/Feedback
B10 — Forget
B11 — Hardening/Release
```

Não detalhar B6+ antes de B2/B3/B4 revelarem os contratos reais necessários.

A arquitetura do Sofias Memory deve crescer a partir de invariantes comprovados, não de
código especulativo.
