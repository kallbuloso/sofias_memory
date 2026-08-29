# Sofias Memory — Release v0.1.0 Backlog

**Documento:** Discovery + arquitetura de release + backlog executável para v0.1.0
**Escopo:** Determinar o que falta entre o MVP funcional aprovado (GATE-B5) e uma
release v0.1.0 reproduzível, documentada e suportável. Este documento NÃO implementa
features de produto e NÃO é SM-517/B6.
**Status:** EM EXECUÇÃO — ver tabela de status abaixo.
**Baseline original do discovery:** `01d6b1b2e0d40439e82657dee51dff4d62062c17` —
`docs: close B5 operational async runtime milestone`.

| Task | Status | Commit/baseline |
|---|---|---|
| REL-001 — Documentação canônica | DONE | `b621dcb` — `docs: add canonical v0.1.0 user and API guides` |
| REL-002 — Empacotamento reprodutível, versão, ativos de release | DONE | `762a153` — `build: harden v0.1.0 release packaging`; `34658cc` — `build: complete v0.1.0 version packaging` |
| REL-003 — Contrato operacional de migração/upgrade/backup | DONE | `3c071c5` — `docs: define v0.1.0 operational recovery contract` |
| REL-004 — CI mínima de qualidade e integração | DONE | `f24c673` — `ci: add v0.1.0 quality and integration gates`; CI `33220003273` PASS; Integration `33222166381` PASS |
| REL-005 — Automação de release / publicação GHCR | IMPLEMENTED / RC PASS / final v0.1.0 publication pending GATE-R1 final phase | `607fb8d` — release workflow + CHANGELOG; CI `33227939187` PASS; Release RC run `33228177783` PASS; `v0.1.0-rc.1` public GHCR digest `sha256:a6f7603c6c3dc04df39dabc6f91aa339d2ac78f058cfbfb31cf032341b695b43` |
| REL-006 — Guias de deployment + smoke de produção | DONE | `0848731` — `docs: add production deployment and smoke validation`; CI `33230847900` PASS; RC production smoke PASS against published RC digest `sha256:a6f7603c6c3dc04df39dabc6f91aa339d2ac78f058cfbfb31cf032341b695b43` |
| GATE-R1 — Sofias Memory v0.1.0 Release | PRE-PUBLICATION PASS / final artifact verification pending | (uncommitted, local audit) — FASE A: critérios 1–10, 12, 13 = PASS; critério 11 = PENDING FINAL PUBLICATION (esperado); baseline `0848731`, CI `33230847900` PASS |

## Nota de sequenciamento de execução (REL-005)

A tag final `v0.1.0` **não** é criada ao final de REL-005, apesar do backlog
original posicionar "criar v0.1.0" nesta task. Sequência congelada:

```text
REL-005 (workflow + CHANGELOG implementados)
  → push + v0.1.0-rc.1 real (prova RC: build, OCI, GHCR auth, push, prerelease)
  → REL-006 (guias de deployment finais + production smoke)
  → GATE-R1 (validação final de release readiness)
  → tag final v0.1.0
  → GHCR :0.1.0 estável + GitHub Release v0.1.0 estável
```

Criar `v0.1.0` antes de REL-006/GATE-R1 excluiria do source tag a documentação
final de deployment, o tooling de production smoke, e quaisquer correções que
o smoke ainda venha a revelar. `release.yml` já está **capaz** de publicar a
tag estável — ele só não deve ser exercitado para `v0.1.0` até esses dois
gates fecharem.

---

## 1. O que "v0.1.0 released" significa aqui

O PRD (`docs/product/Sofias_Memory_PRD_SPECS.md`, Epic 9 — "Hardening e release")
já trata `1.0.0` como a release **hardened** (SBOM, load tests, restore drill,
exemplos de integração Laravel/n8n/Python). Isso é evidência direta de que `0.1.0`
**não** precisa carregar esse nível de ceremônia — é a primeira release **honesta e
operável** do MVP já provado em GATE-B5, não a release "enterprise-ready".

Definição congelada de "v0.1.0 released" para este documento:

> Um operador que nunca leu o histórico de stories consegue: (1) ler um README
> correto sobre o estado real do produto; (2) subir a stack via `compose.yaml` com
> `.env` próprio; (3) migrar o schema; (4) obter um Remember→Cognify→Recall
> funcional contra provider real; (5) saber como fazer backup/restore básico; (6)
> saber que degrau de upgrade esperar na próxima versão; (7) encontrar a
> documentação da API sem precisar ler código-fonte.

Classificação MUST/SHOULD/POST:

| Item | Classificação | Por quê |
|---|---|---|
| Source tag (`v0.1.0` no Git) | MUST | é a própria definição de "release" |
| README correto (não "foundation phase") | MUST | README stale é o achado mais visível deste discovery (§3) |
| Docker Compose documentado e íntegro | MUST | é o único caminho de deploy hoje; já existe, mas com env vars incompletas (§7/§12) |
| Documentação de migração/primeiro start | MUST | hoje não existe nenhuma instrução — sem isso a stack nunca sobe com schema válido |
| `docs/api.md` (ou equivalente) | MUST | OpenAPI sozinho não explica envelope/`X-API-Key`/`wait`/`Idempotency-Key`/`DELETE EVERYTHING` (§4) |
| Versão canônica única/consistente | SHOULD | hoje há 4 superfícies com o literal `"0.1.0"` independentes (§2.3); risco real mas não bloqueia uma primeira tag se documentado |
| Imagem de release reproduzível e não-flutuante | MUST | PRD exige explicitamente "imagens Docker reproduzíveis e sem tags flutuantes" (linha 238) — `python:3.12-slim` hoje viola isso literalmente (§2.4) |
| Imagem de release capaz de executar sua própria migração Alembic | MUST | achado corrigido nesta revisão — a imagem publicada hoje não contém `alembic.ini`/`migrations/` (§2.6) |
| GHCR image publicada | SHOULD | valioso para consumo por Portainer/EasyPanel, mas build-local já funciona hoje |
| CI mínima (lint/type/unit/build) | **MUST** (requisito de processo de release, não requisito funcional do MVP) | a sequência de release escolhida (REL-005 publica GHCR, GATE-R1 fecha) depende de validação automatizada antes de publicar uma imagem pública — GATE-B5 já provou o produto, mas nada hoje impede uma regressão silenciosa entre o baseline e a tag `v0.1.0` (§2.11) |
| Backup/restore documentado (procedimento, não feature) | MUST | autoridade é PostgreSQL; sem isso não há story de recuperação de desastre nem para o próprio operador único |
| Upgrade/rollback policy documentada | MUST | migration `0011` já é irreversível (enum nativo) — silêncio sobre isso é uma armadilha operacional |
| CHANGELOG/release notes | SHOULD | 16 stories internas não devem vazar para o usuário final, mas alguma nota é esperada de uma tag pública |
| SemVer formal / branching policy | POST | prematuro com uma única tag; decidir quando houver uma segunda |
| SBOM / assinatura de imagem / provenance | POST | PRD já classifica isso como Epic 9 (rumo a 1.0.0), não v0.1.0 |
| Load tests / restore drill formal | POST | idem — Epic 9 |
| Exemplos de integração (Laravel/n8n/Python) | POST | idem — Epic 9 |
| Multi-arch (amd64+arm64) | POST | nenhuma evidência de demanda hoje; amd64 single-arch é suficiente para v0.1.0 |

---

## 2. Achados — auditoria por área

### 2.1 README (§3 do prompt)

`README.md` (raiz) está objetivamente desatualizado: diz "This repository is
currently in the foundation phase" e "PostgreSQL/Neo4j clients, migrations, and
worker behavior are implemented in later phases" — falso após GATE-B5. Não há
nenhuma seção sobre: Remember, Cognify, Recall, Improve, Forget, Dataset
management/delete, Runs/retry/cancel, `wait=true/false`, autenticação, exemplos de
uso, `/health/*`, limitações. ~70% do arquivo atual é sobre a stack de
desenvolvimento local de UM desenvolvedor (portas `5440`/`7688`, bug conhecido do
`NEO4J_AUTH`) — informação real e útil, mas não deveria ser o corpo principal do
README público (§19).

**Gap matrix (README):**

| Seção necessária | Existe hoje? | Nota |
|---|---|---|
| O que é o Sofias Memory | Parcial (1 frase) | precisa mencionar memória semântica + knowledge graph + single-user explicitamente |
| Status atual (v0.1.0) | Não — diz o oposto | bloqueador |
| Arquitetura (1 app + worker interno + PG autoridade + Neo4j projeção) | Não | ADRs existem mas não são resumidos aqui |
| Requisitos (Docker, chave OpenAI-compatible) | Não | |
| Quick start (compose up) | Não | existe só para dev local via `uv` |
| Configuração de ambiente | Parcial | existe para dev, falta para produção/compose |
| Docker Compose (uso, não só arquitetura) | Parcial | fala do arquivo, não do fluxo de subir |
| Health/readiness | Não | |
| Autenticação (`X-API-Key`) | Não | |
| Exemplos Remember/Recall/Cognify/Improve/Forget | Não | maior gap de usabilidade |
| Dataset management/delete | Não | |
| Runs/retry/cancel, `wait=true/false` | Não | |
| Volumes/persistência | Não | |
| Backup | Não | |
| Upgrade | Não | |
| Segurança | Não | |
| Limitações/fora de escopo | Não | |
| Acesso a OpenAPI | Não | |
| Desenvolvimento/testes | Sim (parcial, correto) | manter, mover parte para doc de dev se necessário |

### 2.2 Documentação de API para consumidor (§4)

OpenAPI **sozinho não é suficiente**: ele descreve schemas e rotas, mas não explica
semântica operacional (por que `wait=false` retorna `202` com run já commitado; o
que `Idempotency-Key` realmente garante; o significado de cada `ErrorCode`; que
`Forget Everything` exige literalmente `"DELETE EVERYTHING"`; o ciclo de vida de um
`PipelineRun`; a diferença entre `Forget Dataset` e `DELETE /datasets/{id}`).

**Recomendação:** um único `docs/api.md` complementar ao OpenAPI gerado (servido via
`/docs`/`/openapi.json` do próprio FastAPI, sem nada extra a construir). Não
introduzir Redoc customizado, Docusaurus, ou geração de site estático — não
justificado para um single-user MVP. `docs/api.md` deve cobrir exatamente os itens
listados na seção 4 do prompt original, com exemplos `curl` reais (o smoke real já
executado no GATE-B5 é a fonte primária desses exemplos).

### 2.3 Consistência de versão (§5)

Quatro superfícies carregam o literal `"0.1.0"`, sem mecanismo que as mantenha
sincronizadas. Classificadas por papel, não apenas listadas:

**Fonte canônica (deveria ser a única fonte de verdade):**

1. `pyproject.toml` → `version = "0.1.0"`.

**Metadado de deployment (deve ser derivado da fonte canônica, hoje é
independente):**

2. `sofias_memory/config.py` → `Settings.app_version` default `"0.1.0"` (via
   `APP_VERSION` env var) — isso é o que `/api/v1/info` e o `info.version` do
   OpenAPI (`FastAPI(version=resolved_settings.app_version)`) realmente refletem em
   runtime.
3. `compose.yaml` → `image: sofias-memory:0.1.0` (tag da imagem local) e
   `APP_VERSION: "${APP_VERSION:-0.1.0}"` (default do env var repassado ao
   container).

**Documentação/exemplo (não precisa ser mecanicamente sincronizado, só revisado a
cada release):**

4. `.env.example` → `APP_VERSION=0.1.0` (valor de exemplo copiado pelo operador
   para seu próprio `.env`; drift aqui é cosmético, não operacional, mas ainda deve
   ser verificado a cada release).

Hoje as quatro concordam por sincronização manual, não por design. Um operador que
mude `pyproject.toml` sem lembrar de `compose.yaml`/`.env.example` obtém `/info` e o
tag da imagem divergentes silenciosamente.

**Recomendação (não implementar agora, decisão de REL-002):** manter
`pyproject.toml` como fonte canônica única. Evitar uma refatoração grande de
runtime só por DRYness de versão — a estratégia mínima e suficiente é: (a)
`Settings.app_version` continua sendo um campo simples (lido de `APP_VERSION` ou,
alternativamente, de `importlib.metadata.version("sofias-memory")` se optar por
eliminar o env var duplicado); e (b) uma checagem de release/CI (REL-004/REL-005)
que falha se `pyproject.toml`, o `APP_VERSION` default em `compose.yaml`, e o valor
em `.env.example` divergirem no momento de publicar uma tag. Isso evita máquina de
versão em runtime sem deixar o drift sem guarda-corpo.

### 2.4 Dockerfile (§6)

Pontos fortes já corretos: `uv sync --frozen` (reprodutível a partir de
`uv.lock`), imagem `uv` pinada por versão exata (`0.8.17`), usuário não-root
dedicado, `HEALTHCHECK` presente, `PYTHONDONTWRITEBYTECODE`/`UV_COMPILE_BYTECODE`
configurados corretamente, sem arquivos de dev copiados para a imagem.

Gaps reais:

- **MAJOR — corrigido nesta revisão.** `docs/product/Sofias_Memory_PRD_SPECS.md`
  linha 238 exige explicitamente: *"Imagens Docker reproduzíveis e sem tags
  flutuantes."* `python:3.12-slim` **é** uma tag flutuante por definição — não
  fixa a patch version do Python nem a variante de base Debian (`bookworm` vs. a
  próxima), e recebe rebuilds upstream sem aviso prévio. A conclusão anterior
  deste discovery (que a tag de minor version já seria "suficiente" e pin por
  digest seria "desnecessariamente rígido") estava **incorreta** — contradiz o
  próprio texto do PRD, não uma leitura frouxa dele. Isto é MUST, não SHOULD.
  REL-002 deve fixar a identidade exata da base runtime (no mínimo uma tag de
  patch version completa e variante explícita disponível no momento da
  implementação, ex. no formato `python:3.12.<x>-slim-bookworm`; pin por digest
  pode ser adotado adicionalmente se não impuser fricção de manutenção
  desproporcional — a escolha exata fica para a implementação de REL-002, não
  para este documento). O mesmo princípio de não-flutuação já é respeitado hoje
  pelas imagens de banco (`pgvector/pgvector:0.8.1-pg17`, `neo4j:5.26-community`,
  ambas com versão explícita) — a base Python é a única imagem do stack que ainda
  viola a política.
- Nenhum metadado de versão embutido na imagem (nenhum `LABEL
  org.opencontainers.image.version`/`.revision`/`.source`). SHOULD para v0.1.0 —
  barato de adicionar, ajuda rastreabilidade de imagens publicadas.
- Nenhum `tini`/`dumb-init` como PID 1; `uvicorn` roda diretamente como processo 1.
  Uvicorn trata `SIGTERM` corretamente para desligamento gracioso de um único
  worker, então isso não é um bloqueador funcional, mas é uma prática mais segura
  para reaping de zumbis; classificar como SHOULD.
- `read_only: true` no `compose.yaml` já força o filesystem da imagem a ser
  imutável em runtime — isso já valida que o Dockerfile não escreve fora de
  `/data`/`tmpfs` esperados. Nenhuma mudança necessária aqui.
- Build single-stage (a partir de `python:3.12-slim`) é aceitável — não há
  toolchain de compilação pesada deixada na imagem final que justifique multi-stage
  agora; medir tamanho da imagem antes de assumir que multi-stage é necessário.

**Decisão revisada nesta correção (substitui a decisão anterior):** `python:3.12-slim`
**não** satisfaz a exigência de reprodutibilidade do PRD. REL-002 deve substituí-la
por uma tag de patch version explícita (e avaliar pin por digest como reforço
opcional). A política de release para as demais imagens do stack (Postgres/Neo4j,
já versionadas explicitamente, nunca `latest`) deve continuar sendo exigida
explicitamente na documentação de deployment (REL-006), não apenas observada como
já correta.

### 2.5 Compose (§7)

`compose.yaml` já define exatamente os três serviços exigidos
(`sofias-memory`, `postgres`, `neo4j`), apenas a aplicação publicada por padrão,
`read_only: true` + `cap_drop: [ALL]` + `security_opt: no-new-privileges:true` no
container da aplicação, healthchecks reais nos três serviços, `depends_on` com
`condition: service_healthy`, volumes nomeados persistentes para Postgres/Neo4j/
sources, e already-fixed o bug documentado em `AGENTS.md`§8 (`NEO4J_AUTH` vs
healthcheck usando a mesma senha `DB_NEO4J_PASSWORD`).

**Gap real e concreto encontrado (corrigido nesta revisão — contagem exata):**
`compose.yaml` repassa apenas um subconjunto das `Settings` documentadas em
`.env.example`. `WORKER_MAX_CONCURRENT_READS` **já está presente** em
`compose.yaml` (`WORKER_MAX_CONCURRENT_READS: "${WORKER_MAX_CONCURRENT_READS:-8}"`)
— a versão anterior deste discovery a listava como ausente por erro de auditoria.
Confirmado diretamente contra o arquivo: são exatamente **seis** variáveis
ausentes, não sete. Estas existem em `.env.example` (logo, um operador as espera
configuráveis) mas **não são repassadas** no bloco `environment:` do serviço
`sofias-memory` — ficam permanentemente presas ao default do código, mesmo que o
operador as defina no `.env` do host:

```text
ENTITY_DEDUP_SIMILARITY_THRESHOLD
ENTITY_MERGE_SIMILARITY_THRESHOLD
GRAPH_PATH_MAX_DEPTH
GRAPH_SUBGRAPH_MAX_DEPTH
GRAPH_SUBGRAPH_MAX_RELATIONS
PROVENANCE_MAX_EVIDENCE
```

(`DATABASE_URL`, `DATA_DIRECTORY`, `HTTP_HOST`, `HTTP_PORT`, `TEMP_DIRECTORY` estão
ausentes deliberadamente — são compostos/fixados pelo próprio `compose.yaml` e não
devem ser overridable pelo operador nesse caminho; isso está correto.
`WORKER_MAX_CONCURRENT_READS` já está corretamente presente e não deve ganhar uma
segunda entrada duplicada.)

**Recomendação:** REL-002 deve adicionar as seis variáveis acima ao bloco
`environment:` com os mesmos defaults de `.env.example`/`Settings`, e REL-004 (CI)
deve incluir um teste/`docker compose config` check que impeça essa lista de
divergir novamente no futuro (comparação automática `.env.example` × `compose.yaml`
como as feitas manualmente neste discovery).

**Caminho de produção recomendado (§7 pergunta A/B/C):** oferecer **ambos** (C), mas
com um canônico explícito: build-local via `compose.yaml` (`build: context: .`)
continua sendo o caminho testado/reprodutível a partir do source, e a imagem GHCR
(REL-005) é uma opção de conveniência para quem não quer buildar. Recomenda-se que
`compose.yaml` continue usando `build:` (já testável localmente sem GHCR) e que a
documentação (REL-006) explique como trocar `build:` por `image:
ghcr.io/kallbuloso/sofias-memory:0.1.0` para quem preferir a imagem publicada — sem
duplicar o arquivo.

### 2.6 Migração de banco / primeiro start (§8)

**Achado original:** não existe nenhum caminho automatizado do banco vazio até
`alembic head 0011`. `Dockerfile` `CMD` inicia `uvicorn` diretamente, sem etapa de
migração; `compose.yaml` não tem init container nem `command:` override rodando
`alembic upgrade head`; `sofias_memory/lifespan.py` só faz um *probe* de
conectividade Postgres (`_probe_postgres`) e cria constraints Neo4j — nunca aplica
migrations Alembic. `PostgresReadinessChecker` detecta uma revisão desatualizada e
reporta `not ready`, mas não corrige. Nenhuma documentação atual explica o
procedimento manual esperado.

**MAJOR — correção necessária, achado original estava incompleto.** A conclusão
anterior tratava isso como um gap puramente de documentação. Isso é verdade para a
*política* (migração explícita/manual continua correta, ver abaixo), mas é **falso**
para a *capacidade*: auditando `Dockerfile` diretamente, ele copia apenas:

```text
pyproject.toml
uv.lock
README.md
sofias_memory/
```

**Não copia `alembic.ini` nem `migrations/`.** Isso significa que a imagem
publicada (o artefato canônico de deploy via GHCR, REL-005) **não consegue
executar `alembic upgrade head` a partir de si mesma** — o comando
`docker compose run --rm sofias-memory alembic upgrade head` proposto abaixo
falharia contra a imagem real hoje, porque nem o `alembic.ini` nem os scripts de
migration existem dentro do container. Um consumidor que só tem acesso à imagem
publicada (não ao checkout do source) não tem como migrar o schema de jeito
nenhum com o estado atual do Dockerfile.

**Política de release (mantida — não implementar migração automática no
startup):** migração continua **explícita e manual**, consistente com
AGENTS.md/CLAUDE.md ("Alembic é a única autoridade de evolução do schema" e
nenhuma menção a auto-apply). Migração automática no boot é uma decisão
arquitetural com trade-offs reais (race entre múltiplas réplicas migrando
simultaneamente, migração silenciosa sem janela de manutenção) que o PRD/ADRs
atuais não endossam. `PostgresReadinessChecker` continua sendo o mecanismo que
detecta e recusa ficar `ready` com schema divergente — isso não muda.

**Correção obrigatória (REL-002, não apenas REL-003):** o Dockerfile deve copiar
`alembic.ini` e `migrations/` para dentro da imagem, para que a política acima
seja de fato executável a partir do artefato de release publicado, não apenas a
partir de um checkout de source. Sem isso, a política "explícita e manual" é
inexecutável para quem consome só a imagem GHCR.

Procedimento a documentar em REL-003/REL-006 (agora **suportável pela imagem real**
depois da correção de REL-002):

```bash
# primeiro start (schema vazio) — comando conceitual; caminho/entrypoint
# exatos a finalizar durante REL-002/REL-003 quando alembic.ini/migrations/
# já estiverem empacotados na imagem
docker compose run --rm sofias-memory alembic upgrade head
docker compose up -d
```

**Upgrade entre versões:** mesmo procedimento — antes de subir uma imagem `v0.1.x`
mais nova contra um schema mais antigo, o operador deve rodar `alembic upgrade
head` manualmente, usando a imagem de destino (que já contém suas próprias
migrations após a correção de REL-002). Isso deve ser explícito na documentação de
upgrade (§2.9).

### 2.7 Neo4j — primeiro start / rebuild (§9)

`lifespan.py` já cria constraints/índices Neo4j automaticamente a cada boot
(`ensure_neo4j_schema`/`_bootstrap_neo4j`, idempotente) — **nenhuma ação manual
necessária** para o schema Neo4j em si, diferente do Postgres. `graph_outbox` drena
autonomamente assim que o worker inicia. Um Neo4j vazio com PostgreSQL já povoado
(cenário de restore, §2.8) se recupera via `scripts/rebuild_graph.py --all` — este
script é a ferramenta de reconstrução de projeção correta para esse cenário (é uma
ferramenta operacional de reconstrução do grafo, não de backup/restore em si — ver
correção de escopo em §2.8) e **funciona corretamente contra um checkout do
source/ambiente de desenvolvimento**, onde foi auditado. Ele **não está hoje
empacotado na imagem de release** (`Dockerfile` não copia `scripts/`, ver §2.7.1
abaixo) — rodá-lo contra um deployment baseado apenas na imagem GHCR published
exige a correção de empacotamento decidida em REL-002. Nenhuma mudança de lógica
necessária no script em si — só decisão de empacotamento (REL-002) e documentação
do procedimento (REL-006).

#### 2.7.1 MAJOR — scripts operacionais também não estão na imagem (achado novo)

`Dockerfile` não copia `scripts/` para dentro da imagem. Os três scripts
existentes têm papéis distintos e não devem ser descritos de forma genérica como
"ferramentas de backup/restore":

- `scripts/generate_api_key.py` — utilitário de geração de credencial (`API_KEY`).
  Não lê nem escreve nenhum estado do sistema; é seguro incluir na imagem de
  release por simplicidade de empacotamento.
- `scripts/rebuild_graph.py` — utilitário de reconstrução da projeção Neo4j a
  partir do PostgreSQL. Útil **depois** de um restore ou de qualquer perda de
  projeção — não é, em si, uma ferramenta de backup ou de restore, é a ferramenta
  de reconciliação pós-recuperação.
- `scripts/verify_installation.py` — utilitário de verificação de instalação
  (versão de Python, importabilidade do pacote). Útil para diagnóstico de um
  deployment.

**Não existe hoje nenhum script de backup/restore completo** — nem automatizado
nem manual-assistido. O que existe é a base arquitetural (§2.8) sobre a qual um
procedimento manual pode ser documentado.

**Decisão para v0.1.0 (REL-002 empacotamento, REL-003 documentação):** incluir na
imagem de release, no mínimo, `scripts/rebuild_graph.py` e
`scripts/verify_installation.py` (ambos necessários para operação pós-deploy sem
exigir um checkout de source), e preferencialmente `scripts/generate_api_key.py`
como utilitário inofensivo adicional, se isso não complicar o empacotamento.
Alternativa aceitável: expor comandos de módulo equivalentes (`python -m
sofias_memory...`) em vez de copiar os arquivos de `scripts/` diretamente — mas o
deployment canônico (a imagem publicada) não deve exigir um checkout de source
para executar operações operacionais suportadas.

### 2.8 Backup / restore (§10)

**Correção de enquadramento (achado da revisão):** este documento não deve
descrever nenhuma "ferramenta de backup/restore" como já existente — não existe.
O que existe é a **base arquitetural** (ADR-0002/ADR-0008: PostgreSQL é autoridade,
Neo4j é projeção reconstruível) sobre a qual um procedimento **manual, apenas
documentado** pode ser definido para v0.1.0. Nenhum script de backup/restore é
implementado por este backlog.

Autoridade e reconstrutibilidade, aplicadas à pergunta de backup:

| Estado | Deve ser backupeado? | Por quê |
|---|---|---|
| PostgreSQL (`sofias_memory_postgres_data` volume, ou `pg_dump`) | **Sim, obrigatório** | única fonte de verdade — datasets, sources metadata, chunks, entities, relations, pipeline history |
| Filesystem de sources originais (`sofias_memory_sources` volume) | **Sim, obrigatório** | conteúdo original não é reconstruível a partir de nada — perdê-lo é perda permanente de proveniência |
| Neo4j (`sofias_memory_neo4j_data` volume) | **Opcional/conveniência** | 100% reconstruível via `rebuild_graph.py --all` a partir do PostgreSQL (depois de empacotado na imagem, §2.7.1); falta de backup dele nunca é perda de dados, só custo de tempo de rebuild |

**Procedimento mínimo de backup (documentar em REL-003, não implementar feature de
export/import — fora de escopo per PRD §301 "export/import: fase posterior"):**

1. Parar/quiesciar as escritas da aplicação `sofias-memory` (`WORKER_ENABLED=false`
   + reinício, ou parar o container) antes do backup, para uma cópia ponto-no-tempo
   consistente.
2. Manter o PostgreSQL disponível (não parar o serviço de banco).
3. `pg_dump` do PostgreSQL.
4. Arquivar/copiar o volume persistente de sources
   (`sofias_memory_sources`).
5. Retomar a aplicação.

(Neo4j é opcional no backup — ver tabela acima; se incluído, deve ser copiado
depois do PostgreSQL e do volume de sources, nunca antes.)

**Procedimento mínimo de restore (documentar em REL-003):**

1. Parar a aplicação.
2. Restaurar o PostgreSQL a partir do `pg_dump`.
3. Restaurar o volume de sources.
4. Garantir compatibilidade entre a versão da imagem e o schema restaurado
   (mesma revisão Alembic, ou aplicar as migrations pendentes conforme §2.6/§2.9
   antes de prosseguir).
5. Subir a infraestrutura necessária (PostgreSQL, Neo4j).
6. Reconstruir/reconciliar o Neo4j a partir do PostgreSQL usando
   `scripts/rebuild_graph.py --all` (a ferramenta de reconciliação suportada — não
   uma ferramenta de restore em si) — obrigatório se o volume de Neo4j não foi
   restaurado junto, recomendado mesmo se foi, para garantir convergência.
7. Verificar `/health/ready`.
8. Rodar o smoke de produção (§2.19/REL-006) antes de considerar o restore
   concluído.

### 2.9 Upgrade / rollback (§11)

Migration `0011` (`ALTER TYPE pipeline_type ADD VALUE 'dataset_delete'`) já
documenta em seu próprio arquivo que o downgrade é `NotImplementedError` —
limitação real do PostgreSQL (não existe `DROP VALUE` para enums nativos). Isso já
foi verificado fisicamente durante o GATE-B5.

**Política de rollback congelada:** não prometer rollback de schema arbitrário.
Rollback de uma release v0.1.x que não introduziu migration nova é seguro (apenas
trocar a imagem de volta). Rollback que exigiria desfazer uma migration com
downgrade não suportado (como `0011`) **não é suportado** — a política correta é
"rollback de aplicação sim, rollback de schema não além do que Alembic já permitir
downgrade real". Isso deve ser dito explicitamente na documentação de upgrade, não
deixado implícito.

Compatibilidade Neo4j/filesystem entre versões: como Neo4j é sempre reconstruível e
o formato de arquivo de `sources` não muda entre versões v0.1.x (nenhuma migration
toca nisso), essas duas camadas não impõem restrição adicional de upgrade — o
gargalo real é sempre o schema PostgreSQL.

### 2.10 Configuração de ambiente (§12)

Ver §2.5 (compose) para o gap concreto já encontrado (6 variáveis não repassadas).
Adicionalmente:

- `.env.example` está bem alinhado com `Settings` (nenhuma variável de aplicação
  ausente ou obsoleta encontrada além do gap do compose).
- Distinção `DB_PASSWORD`/`DB_NEO4J_PASSWORD` (interpolação de infraestrutura,
  usadas só dentro do `compose.yaml` para compor `DATABASE_URL`/`NEO4J_PASSWORD`/
  `NEO4J_AUTH`) versus `Settings` da aplicação (`DATABASE_URL`, `NEO4J_PASSWORD`,
  etc.) já está corretamente separada — README já documenta isso explicitamente
  (`extra="forbid"` continua válido). Nenhuma mudança necessária, só precisa
  sobreviver à reescrita do README (§2.1) sem se perder.
- Nenhum valor de exemplo inseguro encontrado em `.env.example` (todos os secrets
  usam placeholders `change-me`/`sk-change-me`, nunca um valor real).

### 2.11 CI (§13)

**Não existe CI hoje** — nenhum diretório `.github/workflows`, nenhum arquivo de CI
de outro provedor encontrado no repositório.

**Reclassificação (correção desta revisão): CI é MUST antes da release v0.1.0, não
SHOULD.** Isso não significa que CI seja um requisito funcional do MVP — GATE-B5 já
provou o produto sem CI. É um requisito do **processo de release** especificamente:
a sequência escolhida neste backlog publica uma imagem pública versionada (REL-005)
e a fecha com GATE-R1; nada garante hoje que o commit exato taggeado `v0.1.0` não
introduziu uma regressão silenciosa entre o baseline provado em GATE-B5 e o momento
da tag. CI mínima é o guarda-corpo que torna essa garantia real antes de publicar
algo que terceiros vão consumir.

Recomendação de CI mínima para v0.1.0 (pragmática, dois workflows):

1. **`ci.yml` (todo PR/push, sem infraestrutura externa):**
   `uv lock --check` → `ruff check .` → `ruff format --check .` → `mypy sofias_memory
   scripts` → `pytest` (apenas suíte unit, sem opt-ins de integração) → `pip-audit`
   → `bandit -r sofias_memory` → `docker build` (só validar que builda, sem push).
2. **`integration.yml` (opt-in/manual/nightly, com serviços reais):** usa
   *service containers* do próprio GitHub Actions para PostgreSQL (`pgvector/
   pgvector:0.8.1-pg17`) e Neo4j (`neo4j:5.26-community`) — mais simples e rápido
   que `testcontainers` dentro de CI, já que o runner já oferece isso nativamente;
   roda a suíte de integração completa com os `SOFIAS_MEMORY_RUN_*` opt-ins e o
   `alembic upgrade head` fresco (equivalente ao `test_postgres_migration_gate.py`),
   **e, após REL-002, também o teste de `alembic upgrade head` executado de
   dentro da imagem construída** (regressão direta do achado MAJOR §2.6 — garante
   que a correção de empacotamento nunca regride silenciosamente). Não usar
   `testcontainers` em CI (ele já é usado nos testes locais/`b3_neo4j_gate` para
   simular ambientes sem Docker Compose pré-existente — redundante e mais lento
   dentro de um runner que já pode rodar service containers nativamente).

Não criar os arquivos de workflow neste discovery — apenas recomendar (REL-004).

### 2.12 Artefato de release / GHCR (§14)

Recomendação:

- Tag Git: `v0.1.0` (prefixo `v`, convenção universal, compatível com
  `actions/create-release`/GoReleaser-style tooling se adotado depois).
- GitHub Release associada à tag, com o CHANGELOG resumido (§2.13).
- Imagem: `ghcr.io/kallbuloso/sofias-memory:0.1.0` como a **tag canônica,
  imutável, nunca reescrita** — é a única referência aceitável em documentação de
  deployment reproduzível, em instruções de verificação de release, e em
  instruções de rollback (rollback para uma versão anterior deve sempre citar a
  tag numérica exata daquela versão, nunca `latest`). `ghcr.io/kallbuloso/
  sofias-memory:latest` **pode** ser publicada adicionalmente como um alias de
  conveniência (mutável, apontando para a última release estável) para quem
  consome via Portainer/EasyPanel sem se importar em fixar versão — mas ela nunca
  substitui a tag imutável em nenhum contexto de reprodutibilidade, verificação,
  ou rollback. Isso não é uma nuance opcional: `latest` sem essa distinção
  explícita na documentação anularia a própria exigência de reprodutibilidade do
  PRD (§2.4).
- Nome da imagem usa hífen (`sofias-memory`) mesmo o repositório GitHub usando
  underscore (`sofias_memory`) — está consistente com o nome do pacote Python
  (`sofias-memory` em `pyproject.toml`) e com a imagem local já definida em
  `compose.yaml` (`sofias-memory:0.1.0`). Não introduzir um terceiro nome.
- Arquitetura: **amd64 apenas** para v0.1.0 — nenhuma evidência de demanda arm64
  hoje, e multi-arch multiplica o tempo de build/CI sem benefício comprovado.
  Reavaliar em uma versão futura se houver pedido real.

Nada disso é publicado neste discovery.

### 2.13 Supply chain (§15)

| Item | Classificação | Nota |
|---|---|---|
| Dependências Python lockadas (`uv.lock`) | MUST | já existe e já é verificado (`uv lock --check`) |
| Base image de containers com versão fixada (não `latest`, não flutuante) | MUST | `pgvector/pgvector:0.8.1-pg17` e `neo4j:5.26-community` já satisfazem isso; `python:3.12-slim` **não satisfaz** (§2.4, corrigido nesta revisão) — pendente em REL-002 |
| `pip-audit` no CI | MUST | ferramenta já é dev dependency; só falta rodar em CI (REL-004) |
| Scan de vulnerabilidade de container (ex. Trivy/Grype) | SHOULD | barato de adicionar a `integration.yml`/`ci.yml`, mas não bloqueia a primeira tag |
| SBOM | POST | Epic 9 do PRD, não v0.1.0 |
| Assinatura/provenance de imagem (cosign/SLSA) | POST | idem — ceremony desproporcional a um single-user MVP nesta fase |
| Dependabot/Renovate | SHOULD | barato, reduz drift de dependências entre releases; não bloqueia v0.1.0 |
| Permissões GHCR (visibilidade, quem pode publicar) | MUST decidir, não implementar ceremony | decisão de 1 configuração no GitHub, não uma tarefa de engenharia |

### 2.14 Checklist de segurança de release (§16)

Garantias já provadas em GATE-B5 e que **continuam válidas sem re-trabalho**:
`X-API-Key` obrigatório e comparado em tempo constante, guardas SSRF (32 testes
dedicados), limite de corpo de requisição, CORS restrito, guardas de path
traversal de storage, redaction de secrets em logs/erros, nenhuma rota proibida,
nenhuma telemetria externa, container non-root, `read_only`/`cap_drop: ALL`/
`no-new-privileges` no compose, apenas a porta da API publicada.

O que falta é **documentação operacional**, não código: REL-006 deve incluir uma
seção de produção explícita cobrindo responsabilidades que o repositório
deliberadamente não assume:

- TLS/reverse proxy é responsabilidade do operador (Traefik/Nginx/Caddy na frente
  de `sofias-memory:8000`, ou a própria camada TLS do Portainer/EasyPanel) — o
  container nunca deve ser exposto diretamente à internet sem TLS.
- Firewall/rede: apenas a porta da aplicação deve ser alcançável externamente;
  Postgres/Neo4j já ficam na rede interna do Compose por padrão — reforçar isso
  na doc para quem customizar.
- Geração de secrets: `scripts/generate_api_key.py` para `API_KEY`; `openssl rand
  -hex 32` (ou equivalente) para `DB_PASSWORD`/`DB_NEO4J_PASSWORD`.
- Permissões dos volumes: o container roda como usuário não-root dedicado —
  documentar que volumes bind-mounted (se o operador trocar de named volumes para
  bind mounts) precisam do UID/GID correto.
- Rotação de `API_KEY`: procedimento é reiniciar o container com um novo valor —
  não há endpoint de rotação em runtime (correto, por design — nenhuma settings
  API existe). Documentar como um passo manual esperado.
- Rotação de chave do provider LLM/embedding: mesmo procedimento (trocar env var,
  reiniciar).

### 2.15 Licença / NOTICE / atribuição upstream (§17)

`LICENSE` = Apache License 2.0 (texto completo presente e correto). `NOTICE.md`
referencia corretamente o baseline Cognee (`topoteretes/cognee`, branch `main`,
`v1.4.1`, commit `38eece5bbb0cb9f5706fed908abd16dba0f5505e`) e declara
explicitamente que **nenhum código-fonte do Cognee foi copiado até o momento** —
consistente com a arquitetura observada (reimplementação independente, não fork).
Nenhuma obrigação de terceiros identificada além do próprio Apache-2.0 do Cognee
(compatível, mesma licença). **Nada encontrado que bloqueie distribuição pública.**
Nenhuma revisão jurídica humana adicional identificada como necessária além da
verificação de rotina de que as dependências em `pyproject.toml` (todas licenças
permissivas comuns do ecossistema Python/FastAPI/PostgreSQL/Neo4j) continuam
compatíveis — isso é o que `pip-audit`/auditoria manual de licenças de terceiros
cobriria, mas nenhuma dependência atual é conhecida por ter licença restritiva.

### 2.16 CHANGELOG / release notes (§18)

Recomendação: **ambos, mas minimalistas**. Um `CHANGELOG.md` na raiz seguindo
"Keep a Changelog" simplificado, com uma única entrada inicial `## [0.1.0]`
resumindo capacidades por área funcional (Remember, Cognify, Recall, Improve,
Forget, Dataset lifecycle, Runs/retry/cancel, segurança) — **não** uma lista de
SM-401..SM-516. A GitHub Release da tag `v0.1.0` deve linkar para esse
`CHANGELOG.md` em vez de duplicar o texto. Isso é conteúdo de REL-006, não deste
discovery.

### 2.17 Fronteira dev vs. produção na documentação (§19)

Portas de desenvolvimento local (`5440`, `7688`), o bug conhecido de
`NEO4J_AUTH` do stack de dev, e detalhes de `sofias_memory_db` são **reais e
úteis**, mas pertencem a um documento de desenvolvimento (`docs/development.md` ou
uma seção claramente separada e secundária no README), não ao corpo principal do
README que um operador de produção lê primeiro. REL-001 deve mover esse conteúdo,
não descartá-lo.

### 2.18 Portainer / EasyPanel (§20)

A afirmação atual do README — que ambos consomem `compose.yaml` diretamente sem
arquivo de override — **já é uma decisão registrada e válida**, e continua
correta após este discovery: nenhuma diferença específica de plataforma foi
encontrada que justifique `compose.easypanel.yaml`/`compose.portainer.yaml`
(interessante: `AGENTS.md`/`CLAUDE.md` §6 listam esses dois arquivos na estrutura
esperada do repositório, mas eles nunca foram criados porque a necessidade nunca
se materializou — isso é uma divergência menor entre a estrutura aspiracional
documentada e a realidade já decidida no README; não é um bloqueador, apenas uma
nota para eventualmente alinhar `AGENTS.md`). Requisitos reais específicos de
plataforma a documentar (REL-006), sem criar arquivos novos:

- variáveis de ambiente obrigatórias (`API_KEY`, `LLM_API_KEY`, `DB_PASSWORD`,
  `DB_NEO4J_PASSWORD`) via UI da plataforma;
- volumes persistentes (os três nomeados já declarados);
- domínio/reverse proxy/TLS ficam na camada da plataforma (Portainer não tem
  proxy nativo — precisa de um adicional; EasyPanel já oferece isso);
- healthcheck já declarado no `compose.yaml` é lido nativamente por ambas as
  plataformas;
- processo de upgrade: trocar a tag da imagem (ou fazer `git pull` + rebuild, se
  build-local) e rodar a migration manual antes/depois conforme §2.6.

### 2.19 Smoke de produção (§21)

Definir como **script Python standalone** (não pytest, não exige suíte de dev
instalada no ambiente de produção) que qualquer operador pode rodar contra uma
instância recém-implantada: `/health/live` → `/health/ready` → `GET /info` →
`POST /datasets` → `POST /remember` (mode=full, wait=false) → poll `GET
/runs/{id}` até terminal → `POST /recall` → `DELETE /datasets/{id}?confirm=true`
(cleanup do próprio dataset de smoke, sempre seguro porque nunca usa `main`).
Determinístico e seguro por construção (cria e depois deleta seu próprio dataset;
nunca toca em dados existentes). Script único (`scripts/production_smoke.py`),
não workflow de CI nesta fase — pode ser chamado manualmente por SSH/exec após um
deploy, e opcionalmente promovido a um job de CI (`integration.yml`) mais tarde
sem mudança de design. Implementação é tarefa de REL-006, não deste discovery.

### 2.20 Nenhuma nova ADR é necessária

Avaliação explícita dos quatro tópicos sugeridos no prompt:

- **Migração automática vs. explícita:** já decidido implicitamente por
  `AGENTS.md`/ADR-0002 ("Alembic é a única autoridade de evolução do schema") — a
  política "explícita, documentada" (§2.6) é a única consistente com o que já
  está congelado, não uma nova escolha arquitetural.
- **Contrato de imagem publicada/versionamento:** é uma decisão operacional de
  release engineering (nomes de tag, `latest` ou não), não uma mudança na
  arquitetura do sistema — não afeta nenhum ADR existente.
- **Autoridade de backup/restore:** já decidida pelos ADR-0002/ADR-0008
  (PostgreSQL autoridade, Neo4j reconstruível, sources no filesystem como parte da
  autoridade) — backup/restore só aplica essa autoridade já congelada a um
  procedimento operacional, não introduz uma nova.
- **Política de upgrade/rollback:** decorre diretamente da limitação física já
  documentada na própria migration `0011` (PostgreSQL não suporta `DROP VALUE` em
  enum nativo) — não é uma escolha architectural nova, é reconhecer uma restrição
  técnica já existente.

**Conclusão: nenhuma ADR nova é necessária para v0.1.0.** Se uma REL-task descobrir
durante a implementação que alguma dessas áreas exige uma escolha genuinamente
nova e não coberta pelos ADRs 0001–0010, essa REL-task deve parar e propor a ADR
naquele momento — não deve ser assumida aqui.

---

## 3. Backlog executável

**Ordem revisada (corrigida nesta revisão — REL-002 passa a ser o task
fundacional de empacotamento, do qual REL-003 e REL-005 dependem
diretamente):**

```text
REL-001 (documentação) ──────────────┐
                                      ├──> REL-006 ──> GATE-R1
REL-002 (empacotamento) ──> REL-003 ─┘
        │
        └──> REL-004 (CI, pode iniciar em paralelo nas partes estáticas) ──> REL-005 ──> REL-006
```

REL-001 e as partes estáticas de REL-004 (lint/type/unit/security, sem depender
da imagem) podem começar imediatamente e em paralelo com REL-002. REL-003 não
pode começar a documentar comandos executáveis contra a imagem real antes de
REL-002 corrigir o empacotamento. REL-005 depende de REL-002 (imagem correta) e
REL-004 (CI validando antes de publicar). REL-006 depende de REL-001 e REL-003, e
idealmente valida contra a imagem já publicada por REL-005.

### REL-001 — Documentação canônica (README + docs/api.md + docs/development.md)

**Objetivo:** substituir o README stale por um README correto e centrado no
produto v0.1.0 real; extrair conteúdo de desenvolvimento local para um documento
separado; criar `docs/api.md` como complemento humano ao OpenAPI.

**Dependências:** nenhuma (documentação pura, pode começar imediatamente).

**Arquivos/superfícies afetados:** `README.md`, novo `docs/api.md`, novo
`docs/development.md` (ou `CONTRIBUTING.md`), `AGENTS.md`/`CLAUDE.md` (nota menor
sobre `compose.easypanel.yaml`/`compose.portainer.yaml` não existirem por decisão
válida, se optar por alinhar).

**Obrigações de implementação:**
- Reescrever `README.md` cobrindo todas as seções do gap matrix (§2.1) —
  o que é, status v0.1.0, arquitetura, requisitos, quick start via Compose,
  configuração, health/readiness, autenticação, exemplos por família de endpoint,
  wait=true/false, persistência, backup (resumo + link), upgrade (resumo + link),
  segurança (resumo + link), limitações, acesso a OpenAPI, link para
  `docs/development.md`.
- Criar `docs/api.md` cobrindo os itens do §4/§2.2 com exemplos `curl` reais
  (reaproveitar os comandos já executados e validados no smoke real do GATE-B5).
- Mover conteúdo de stack de desenvolvimento local (portas `5440`/`7688`, bug
  `NEO4J_AUTH`, `sofias_memory_db`) para `docs/development.md`.

**Não fazer:** não introduzir gerador de site de documentação; não duplicar o
conteúdo do OpenAPI dentro do `docs/api.md` (linkar, não copiar schemas).

**Testes:** nenhum teste automatizado aplicável; revisão manual/checklist contra o
gap matrix desta seção.

**Critérios de aceite:** README não menciona mais "foundation phase"; toda seção
do gap matrix §2.1 existe; `docs/api.md` cobre os 14 itens do §4 do prompt
original; nenhuma porta/detalhe de máquina de um desenvolvedor específico aparece
no corpo principal do README.

---

### REL-002 — Empacotamento reprodutível, consistência de versão e ativos de release

**Objetivo:** fechar os gaps concretos do Dockerfile/compose encontrados no
discovery — incluindo os dois achados MAJOR desta revisão (ativos de migração/
scripts ausentes da imagem, tag base flutuante) — e estabelecer uma fonte canônica
de versão. Este task cresceu de escopo nesta revisão: é o task fundamental do qual
REL-003 (documentação operacional) e REL-005 (publicação) dependem, porque ambos
precisam que a imagem real já seja capaz de fazer o que a documentação vai
descrever.

**Dependências:** nenhuma.

**Arquivos/superfícies afetados:** `Dockerfile`, `compose.yaml`,
`sofias_memory/config.py` (leitura de versão), `pyproject.toml`, `.env.example`.

**Obrigações de implementação:**
- **MUST — corrigido nesta revisão:** copiar `alembic.ini` e `migrations/` para
  dentro da imagem de release, para que `alembic upgrade head` seja executável a
  partir do próprio artefato publicado (§2.6). Sem isso a política de migração
  explícita/manual é inexecutável para quem só consome a imagem GHCR.
- **MUST — corrigido nesta revisão:** decidir e empacotar os scripts
  operacionais suportados na imagem — no mínimo `scripts/rebuild_graph.py` e
  `scripts/verify_installation.py`, preferencialmente também
  `scripts/generate_api_key.py` (§2.7.1). Alternativa aceitável: expor comandos
  de módulo equivalentes em vez de copiar os arquivos diretamente — mas o
  deployment canônico não deve exigir checkout de source para operações
  suportadas.
- **MUST — corrigido nesta revisão:** substituir `python:3.12-slim` por uma
  identidade de base runtime fixa e não-flutuante (tag de patch version explícita
  no mínimo; pin por digest opcional como reforço adicional), para satisfazer a
  exigência literal do PRD linha 238 (§2.4). A tag/digest exata a usar é decisão
  de implementação deste task, não deste documento de planejamento.
- Adicionar as **seis** variáveis de ambiente faltantes (§2.5, contagem
  corrigida) ao bloco `environment:` de `sofias-memory` em `compose.yaml`, com os
  mesmos defaults de `.env.example`.
- Adicionar `LABEL org.opencontainers.image.version`/`.source`/`.revision` ao
  Dockerfile (populado via `ARG` no build, ex. `--build-arg
  APP_VERSION=$(git describe --tags)` ou lido de `pyproject.toml` no build).
- Decidir e implementar a fonte canônica de versão (§2.3 revisado):
  `pyproject.toml` como fonte canônica; `Settings.app_version` derivado dela
  (via `importlib.metadata.version()` ou um `APP_VERSION` formalizado como único
  override intencional documentado); e uma checagem de consistência entre
  `pyproject.toml`, o default de `APP_VERSION` em `compose.yaml`, e o valor em
  `.env.example` (a checagem em si pode viver em REL-004/REL-005, mas a decisão
  de estratégia é deste task).
- Avaliar `tini`/`dumb-init` como PID 1 (SHOULD — pode ser adiado se avaliação
  concluir que não há benefício mensurável).

**Não fazer:** não criar múltiplos `Dockerfile`s por ambiente; não inventar a
tag/digest exata de pin neste documento de planejamento (decisão de
implementação).

**Testes:** `docker compose config` validando as três variantes de compose
existentes continuam válidas; build real da imagem confirmando que
`/api/v1/info` reflete a versão esperada; **novo teste obrigatório:** rodar
`alembic upgrade head` a partir de dentro da própria imagem construída (não do
checkout de source) contra um banco descartável, provando que a correção MAJOR
funciona; confirmar que `scripts/rebuild_graph.py`/`verify_installation.py`
executam de dentro da imagem sem depender de arquivos fora dela.

**Critérios de aceite:** as seis variáveis passam a ser configuráveis via
`compose.yaml`; a imagem contém `alembic.ini`+`migrations/` e consegue migrar a
si mesma; os scripts operacionais decididos estão presentes e executáveis na
imagem; a base runtime não usa mais uma tag flutuante; existe exatamente uma
fonte de verdade documentada para a versão; `docker build` continua funcionando
sem mudança de comportamento funcional para o operador.

---

### REL-003 — Contrato operacional de migração, upgrade e backup/restore

**Objetivo:** documentar (não automatizar) os procedimentos de primeiro start,
upgrade entre versões, e backup/restore, incluindo o comportamento não-reversível
conhecido da migration `0011`.

**Dependências:** **REL-002** (corrigido nesta revisão — antes tratado como sem
dependências; na verdade, os comandos que este task documenta só funcionam contra
a imagem real depois que REL-002 empacota `alembic.ini`/`migrations/` e os
scripts operacionais dentro dela. Documentar um comando que a imagem publicada
ainda não consegue executar seria documentação enganosa).

**Arquivos/superfícies afetados:** novo `docs/operations.md` (ou seção dedicada
em `docs/api.md`/README, a decidir em REL-001), sem mudança de código.

**Obrigações de implementação:**
- Documentar o procedimento de primeiro start (§2.6): `docker compose run --rm
  sofias-memory alembic upgrade head` antes do primeiro `up -d` — válido a partir
  da imagem publicada somente após a correção de empacotamento de REL-002.
- Documentar o procedimento de upgrade entre versões v0.1.x (rodar `alembic
  upgrade head`, usando a imagem de destino, antes de trocar para ela quando essa
  versão introduzir migration nova).
- Documentar a política de rollback (§2.9): rollback de imagem é seguro;
  rollback de schema não é garantido além do que Alembic suportar downgrade real,
  e `0011` especificamente não suporta downgrade; instruções de rollback devem
  sempre citar a tag imutável exata da versão-alvo (§2.12), nunca `latest`.
- Documentar backup/restore (§2.8, procedimentos de 5 e 8 passos corrigidos nesta
  revisão): o que é obrigatório (PostgreSQL + sources), o que é opcional/
  reconstruível (Neo4j via `scripts/rebuild_graph.py --all`), a ordem correta de
  backup (Postgres antes do volume de sources), e o procedimento completo de
  restore terminando em smoke de produção.
- Documentar `scripts/rebuild_graph.py`, `scripts/verify_installation.py`, e
  `scripts/generate_api_key.py` com seus papéis corretos e distintos (§2.7.1) —
  nenhum deles deve ser descrito como uma "ferramenta de backup/restore".

**Não fazer:** não implementar migração automática no startup; não implementar
feature de export/import (fora de escopo do PRD para esta fase); não prometer
rollback de schema não suportado pela ferramenta; não documentar nenhum comando
que dependa de ativos que REL-002 ainda não tiver empacotado na imagem.

**Testes:** nenhum teste de código; validar manualmente o procedimento de
primeiro start e o ciclo completo de backup/restore documentados contra um
ambiente descartável (mesmo padrão de verificação já usado no GATE-B5 com bancos
dedicados), usando a imagem real pós-REL-002, não um checkout de source.

**Critérios de aceite:** um operador seguindo apenas a documentação e a imagem
publicada (não um checkout de source) consegue ir de banco vazio a stack
funcional, e completar um ciclo de backup→restore→rebuild→smoke; a limitação de
rollback de `0011` está explícita, não implícita.

---

### REL-004 — CI mínima de qualidade e integração (MUST antes da release)

**Objetivo:** introduzir a primeira automação de CI do repositório, cobrindo
qualidade estática sempre e integração real sob demanda. **Reclassificado nesta
revisão de SHOULD para MUST antes de v0.1.0** (§2.11/§1) — não é requisito
funcional do MVP, é requisito do processo de release: nada garante hoje que o
commit taggeado não regrediu silenciosamente em relação ao baseline provado em
GATE-B5.

**Dependências:** beneficia-se de REL-002 estar concluído primeiro (o `docker
build`/teste de migração-a-partir-da-imagem deste workflow só faz sentido validar
a imagem já corrigida), mas pode começar em paralelo com REL-002 nas partes que
não dependem dela (lint/type/unit/security estática).

**Arquivos/superfícies afetados:** novos `.github/workflows/ci.yml` e
`.github/workflows/integration.yml`.

**Obrigações de implementação (conforme §2.11):**
- `ci.yml`: `uv lock --check`, `ruff check .`, `ruff format --check .`, `mypy
  sofias_memory scripts`, `pytest` (suíte unit, sem opt-ins), `pip-audit`,
  `bandit -r sofias_memory`, `docker build` (sem push).
- `integration.yml`: service containers de PostgreSQL (`pgvector/
  pgvector:0.8.1-pg17`) e Neo4j (`neo4j:5.26-community`) nativos do GitHub
  Actions; roda a suíte de integração completa com os opt-ins
  `SOFIAS_MEMORY_RUN_*` necessários e o teste de migração fresca a partir de
  banco vazio; gatilho manual/nightly, não em todo PR (para não pagar o custo de
  infraestrutura real a cada commit).
- Adicionar ao `ci.yml` uma checagem simples que compare as chaves de
  `.env.example` contra o bloco `environment:` de `compose.yaml` (regressão do
  gap encontrado em §2.5), para impedir que a lista divirja de novo
  silenciosamente.

**Não fazer:** não usar `testcontainers` dentro do CI (service containers
nativos são mais simples e mais rápidos no contexto do GitHub Actions); não
tentar cobertura 100% de todos os cenários reais de provider LLM em CI (custo
real de API) — reservar isso para execução manual/local como já é feito hoje.

**Testes:** os próprios workflows são o artefato testável (validar rodando via
`act` localmente ou observando a primeira execução real no GitHub).

**Critérios de aceite:** todo PR roda `ci.yml` automaticamente e falha em
qualquer regressão de lint/tipo/teste/segurança estática; `integration.yml`
consegue ser disparado manualmente e reproduz os resultados já obtidos
manualmente no GATE-B5.

---

### REL-005 — Automação de release e publicação de imagem (GHCR)

**Objetivo:** publicar a primeira imagem versionada e a primeira tag/release
pública.

**Dependências:** REL-002 (empacotamento correto), REL-004 (CI validando o build
antes de publicar).

**Arquivos/superfícies afetados:** novo `.github/workflows/release.yml`, novo
`CHANGELOG.md`.

**Obrigações de implementação:**
- Workflow disparado por tag `v*` que builda e publica
  `ghcr.io/kallbuloso/sofias-memory:<versão>` (tag canônica imutável) e,
  opcionalmente, atualiza `ghcr.io/kallbuloso/sofias-memory:latest` como alias
  de conveniência (§2.12) — `latest` nunca é a referência usada em nenhuma
  instrução de reprodutibilidade/rollback deste próprio workflow ou da
  documentação que o consome.
- `CHANGELOG.md` com uma entrada inicial `## [0.1.0]` resumida por área funcional
  (§2.16), sem histórico interno de stories.
- Criação da tag `v0.1.0` e da GitHub Release correspondente linkando o
  changelog (execução real da publicação é o ato de fechar este backlog, não
  parte deste discovery).

**Não fazer:** não publicar múltiplas arquiteturas (amd64 apenas, §2.12); não
adicionar SBOM/assinatura de imagem (POST, Epic 9 do PRD); não deixar nenhuma
documentação de rollback/reprodutibilidade referenciar `latest`.

**Testes:** dry-run do workflow contra uma tag de teste antes de usar `v0.1.0`
de verdade.

**Critérios de aceite:** `docker pull ghcr.io/kallbuloso/sofias-memory:0.1.0`
funciona publicamente; a tag é imutável; se `latest` for publicada, aponta para
a mesma imagem, mas nenhum critério deste gate depende dela existir.

---

### REL-006 — Guias de deployment + smoke de produção

**Objetivo:** fechar a lacuna entre "a imagem existe" e "um operador consegue
rodar em produção com confiança", incluindo o smoke test pós-deploy.

**Dependências:** REL-001 (estrutura de documentação já existe), REL-003
(contrato operacional já documentado), idealmente REL-005 (imagem publicada para
referenciar nos exemplos).

**Arquivos/superfícies afetados:** `README.md`/`docs/operations.md` (seções de
produção, Portainer, EasyPanel, segurança), novo `scripts/production_smoke.py`.

**Obrigações de implementação:**
- Seção de segurança de produção (§2.14): TLS/reverse proxy, firewall, geração de
  secrets via `scripts/generate_api_key.py`, permissões de volume, rotação de
  `API_KEY`/chave do provider.
- Seção Portainer/EasyPanel (§2.18) com os requisitos reais já identificados
  (env vars via UI, volumes nomeados, healthcheck nativo, processo de upgrade) —
  sem criar arquivos de compose adicionais.
- `scripts/production_smoke.py`: script standalone determinístico e seguro
  (§2.19) cobrindo `/health/live` → `/health/ready` → `GET /info` → criar dataset
  de smoke → `Remember` full → poll de `Runs` → `Recall` → `DELETE
  /datasets/{id}?confirm=true` do próprio dataset de smoke.

**Não fazer:** não criar `compose.easypanel.yaml`/`compose.portainer.yaml` (§2.18
— decisão já validada de não precisar deles); não tornar o smoke um requisito de
todo deploy automatizado nesta fase (fica manual/opcional, pode virar job de CI
depois sem redesenho).

**Testes:** executar `scripts/production_smoke.py` manualmente contra uma stack
`compose.yaml` local real como validação final deste task.

**Critérios de aceite:** o smoke roda do zero ao fim sem intervenção manual além
de configurar `.env`; a documentação de produção não deixa nenhuma
responsabilidade implícita (TLS, secrets, backup) sem menção explícita.

---

### GATE-R1 — Sofias Memory v0.1.0 Release

**Tipo:** RELEASE READINESS GATE (não funcional/não recovery — GATE-B5 já provou
isso). Não declarado passado por este documento.

**Critérios de aceite:**

1. **Documentação:** README correto (REL-001 completo, gap matrix §2.1 fechado);
   `docs/api.md` publicado e cobrindo os 14 itens do §4; `docs/development.md`
   separando conteúdo de dev do README principal.
2. **Build de container:** `docker build` reproduzível a partir de `uv.lock`;
   base runtime fixada em identidade não-flutuante (§2.4); metadados de versão
   embutidos na imagem; as seis variáveis de Compose faltantes configuráveis
   (REL-002).
3. **Imagem auto-suficiente para operação:** a imagem publicada contém
   `alembic.ini`+`migrations/` e consegue rodar `alembic upgrade head` a partir
   de si mesma; os scripts operacionais decididos (`rebuild_graph.py`,
   `verify_installation.py`, e preferencialmente `generate_api_key.py`) estão
   presentes e executáveis na imagem sem exigir checkout de source (REL-002 —
   corrigido nesta revisão, antes ausente como critério explícito).
4. **Deploy do zero:** um operador seguindo apenas `README.md` +
   `docs/operations.md` e a imagem publicada (não um checkout de source)
   consegue ir de repositório/imagem a stack funcional via `compose.yaml`,
   incluindo a etapa de migração manual documentada (REL-002 + REL-003).
5. **Migração:** procedimento de primeiro start e de upgrade entre versões
   documentado e validado manualmente contra um banco descartável, usando a
   imagem real (REL-003).
6. **Upgrade/rollback:** política explícita, incluindo a limitação conhecida de
   `0011`, documentada e sem promessa não suportada; instruções de rollback
   sempre referenciam a tag imutável exata, nunca `latest` (REL-003).
7. **Backup/restore:** o procedimento completo de 5 passos de backup e 8 passos
   de restore (§2.8) documentado e validado manualmente pelo menos uma vez,
   terminando em smoke de produção (REL-003 + REL-006).
8. **Segurança:** checklist de produção (§2.14) completo na documentação; nenhuma
   regressão nas garantias já provadas em GATE-B5 (reexecutar a suíte de
   segurança existente como confirmação, não como trabalho novo).
9. **CI (MUST, reclassificado nesta revisão):** `ci.yml` verde em todo PR;
   `integration.yml` executado manualmente pelo menos uma vez reproduzindo os
   resultados do GATE-B5, incluindo o teste de auto-migração da imagem (REL-004).
10. **E2E real:** `scripts/production_smoke.py` executado com sucesso contra uma
    stack `compose.yaml` real, incluindo provider LLM/embedding real (REL-006).
11. **Artefato de release:** tag `v0.1.0` criada; imagem publicada em GHCR na
    tag imutável `0.1.0` (canônica) e opcionalmente também em `latest`
    (conveniência, nunca referenciada em documentação de reprodutibilidade/
    rollback — §2.12); `CHANGELOG.md`/GitHub Release publicados (REL-005).
12. **Versionamento:** fonte canônica de versão (`pyproject.toml`) implementada
    e consistente entre `pyproject.toml`, `/api/v1/info`, o default de
    `compose.yaml`, `.env.example`, e o tag da imagem publicada (REL-002).
13. **Licença/NOTICE:** confirmados corretos (já validado neste discovery, §2.15
    — nenhuma ação pendente, apenas re-confirmar no fechamento do gate).

Somente após todos os treze itens acima, um relatório de fechamento futuro pode
declarar `GATE-R1 PASSED`.

**Nota de execução — as duas fases de GATE-R1 (resolve a circularidade, não
altera os treze critérios acima):** o critério 11 exige a tag `v0.1.0`, a
imagem GHCR `:0.1.0` e a GitHub Release stable — artefatos que só existem
*depois* que este mesmo gate autoriza a publicação. Para não exigir a tag
antes de auditar a prontidão para publicá-la, o fechamento deste gate ocorre
em duas fases:

- **FASE A — PRE-PUBLICATION READINESS:** audita os critérios 1–10, 12 e 13
  (tudo que pode ser verificado antes de qualquer publicação), e confirma que
  `release.yml` está pronto para publicar a versão stable (tag exata,
  `IMAGE_VERSION`, `--latest`/`--latest=false`, verificação de labels OCI,
  prova de proveniência da tag, exigência de CI verde no mesmo SHA, lógica de
  não-sobrescrita, `--verify-tag`). O critério 11 permanece
  `PENDING FINAL PUBLICATION` nesta fase — isso é esperado, não um bloqueio.
  Se 1–10/12/13 = PASS e 11 = PENDING FINAL PUBLICATION, o estado declarado é
  `GATE-R1 = PRE-PUBLICATION PASS / final artifact verification pending`, e
  **não** `GATE-R1 PASSED`. Esse estado autoriza a criação manual da tag
  final `v0.1.0` (disparando `release.yml`), mas não a substitui.
- **FASE B — FINAL ARTIFACT VERIFICATION:** depois que a tag dispara
  `release.yml` e a publicação stable é concluída, esta fase valida
  literalmente o critério 11 contra os artefatos reais publicados (tag,
  imagem GHCR, GitHub Release) e repete qualquer checagem de
  identidade/versionamento que dependa do artefato publicado (§2.12,
  digest/OCI). Somente ao final da FASE B o relatório de fechamento pode
  declarar `GATE-R1 PASSED`.

---

## 4. Fora de escopo (explícito)

Confirmado POST-v0.1.0 por este discovery, alinhado ao Epic 9 do PRD:

- SemVer/branching policy formal (decidir na segunda release real);
- SBOM;
- assinatura/provenance de imagem (cosign, SLSA);
- scanning de vulnerabilidade de container além de `pip-audit` (Trivy/Grype —
  SHOULD, não bloqueia);
- load tests formais;
- restore drill formal/agendado;
- exemplos de integração (Laravel/n8n/Python) mencionados no PRD;
- multi-arch (arm64);
- feature de export/import (fora do PRD para esta fase);
- migração automática de schema no startup;
- `compose.easypanel.yaml`/`compose.portainer.yaml` dedicados (decisão já
  validada como desnecessária).

---

## 5. Decisão de ADR

**Nenhuma ADR nova é necessária para v0.1.0** — ver justificativa completa em
§2.20. Todas as quatro áreas potencialmente arquiteturais (migração
automática/explícita, contrato de imagem publicada, autoridade de backup,
política de upgrade/rollback) já decorrem diretamente de ADRs já aceitos
(ADR-0002, ADR-0008) ou de limitações técnicas já documentadas (migration
`0011`), não de escolhas novas.
