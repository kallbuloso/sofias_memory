# Sofias Memory — Backlog Técnico Executável B3

**Documento:** Backlog técnico — B3 Neo4j Foundation  
**Escopo:** B3 Neo4j Foundation  
**Status:** Pronto para execução pelo Codex  
**Pré-requisitos:** GATE-B2 aprovado + ADR-0008 accepted/revisado; `AGENTS.md`; `docs/product/Sofias_Memory_PRD_SPECS.md`; ADRs aceitos  
**Regra:** executar uma task por vez, respeitando dependências e gates.  
**Base de elaboração:** estado do repositório após GATE-B2, com `main` em `aaf4436` antes da execução de SM-301.

---

## 1. Objetivo deste backlog

Este backlog transforma a fundação Neo4j do Sofias Memory em unidades pequenas de trabalho
que podem ser entregues individualmente ao Codex sem permitir que o agente invente
contratos de projeção, outbox, rebuild ou worker durante a implementação.

B3 não implementa graph-RAG completo, Cognify, Recall, Improve, Forget ou o pipeline engine.
B3 fornece somente a infraestrutura mínima e comprovável necessária para que essas fases
possam utilizar Neo4j posteriormente sem torná-lo uma segunda fonte de verdade.

Ao final deste backlog devemos possuir:

- contrato arquitetural explícito da projeção Neo4j;
- driver Neo4j assíncrono com lifecycle correto;
- constraints e índices mínimos do grafo;
- Neo4j integrado ao readiness;
- boundary/port concreto de projeção;
- operações de projeção idempotentes;
- mecanismo unitário de processamento da `graph_outbox`, sem polling/worker completo;
- rebuild de projeção a partir do PostgreSQL;
- testes reais da projeção e recovery;
- gate que prove que Neo4j continua descartável e reconstruível.

---

## 2. Fontes de verdade e precedência

Para toda task B3, respeitar a precedência definida no `AGENTS.md`:

1. instrução explícita do usuário na task atual;
2. `AGENTS.md` mais específico;
3. `AGENTS.md` raiz;
4. ADR aceito;
5. `docs/product/Sofias_Memory_PRD_SPECS.md`;
6. contratos, migrations e testes versionados;
7. Cognee upstream.

B3 deve respeitar obrigatoriamente os ADRs já aceitos, especialmente:

- ADR-0001 — modular monolith;
- ADR-0002 — PostgreSQL source of truth + Neo4j rebuildable projection;
- ADR-0006 — pgvector 3072/halfvec;
- ADR-0007 — PostgreSQL enums/FK/delete policies;
- ADR-0008 — Neo4j Projection and Rebuild Contract.

---

## 3. Invariantes B3

Nunca introduzir, nem temporariamente:

- conhecimento exclusivo no Neo4j;
- dual-write transacional PostgreSQL + Neo4j;
- transação distribuída;
- provider selecionável de graph database;
- Kuzu/Qdrant/Redis ou outra alternativa ao Neo4j;
- dependência de Neo4j dentro de `sofias_memory/domain`;
- Cypher arbitrário exposto por API;
- APOC como dependência do core;
- GDS como dependência do core;
- users;
- `owner_id`;
- `tenant_id`;
- roles;
- permissions;
- ACL;
- organizations;
- multitenancy;
- sync entre instâncias;
- cloud client;
- plugin system;
- MCP;
- múltiplas réplicas como requisito do MVP;
- worker/polling completo antes de B5.

PostgreSQL permanece a única fonte de verdade.

Neo4j é uma projeção reconstruível:

- idempotente;
- derivada de estado PostgreSQL confirmado;
- acionável por outbox;
- recuperável;
- reconstruível;
- descartável.

Queries futuras de grafo retornam IDs; conteúdo e evidências são hidratados do PostgreSQL.

---

## 4. Modelo de grafo mínimo do PRD

### Nós

```text
(:Entity {
  id,
  dataset_id,
  name,
  entity_type,
  description,
  importance_weight,
  generation
})

(:Chunk {
  id,
  dataset_id,
  source_id,
  document_id,
  ordinal,
  generation
})
```

### Relações

```text
(:Entity)-[:RELATES_TO {
  relation_id,
  predicate,
  description,
  confidence,
  importance_weight,
  generation
}]->(:Entity)

(:Entity)-[:MENTIONED_IN {
  confidence
}]->(:Chunk)

(:Chunk)-[:NEXT]->(:Chunk)
```

### Constraints/índices mínimos do PRD

- `Entity.id` unique;
- `Chunk.id` unique;
- índice em `Entity.dataset_id`;
- índice em `Chunk.dataset_id`;
- índice em `Entity.name`.

Não criar nós `Dataset`, `Source`, `Document`, `Summary` ou `MemoryEntry` somente por
conveniência. Se uma task futura precisar ampliar o modelo Neo4j, exige justificativa
contra PRD/ADR e, quando estrutural, novo ADR.

---

## 5. Contrato congelado pelo ADR-0008

ADR-0008 — Neo4j Projection and Rebuild Contract é a autoridade B3 para projeção,
outbox, rebuild e readiness Neo4j.

### Aggregate types

`graph_outbox.aggregate_type` usa exatamente:

```text
entity
chunk
relation
entity_mention
chunk_next
```

### Identidades de projeção

```text
Entity        = entities.id
Chunk         = chunks.id
RELATES_TO    = relations.id / relation_id
MENTIONED_IN  = entity_mentions.id / mention_id
NEXT          = (from_chunk_id, to_chunk_id)
```

`MENTIONED_IN.mention_id` é propriedade técnica de identidade/idempotência e corresponde
exatamente a `entity_mentions.id`. Múltiplas mentions do mesmo Entity no mesmo Chunk não
podem colapsar.

`NEXT` liga somente chunks projetáveis consecutivos no mesmo dataset, document e generation,
com `to.ordinal = from.ordinal + 1`. `NEXT` nunca cruza dataset, document ou generation.
Para `aggregate_type = chunk_next`, `aggregate_id = from_chunk_id`, e a identidade completa
permanece no payload: `from_chunk_id + to_chunk_id`.

### Schema Neo4j

Constraints/indexes canônicos:

```text
entity_id_unique
chunk_id_unique
entity_dataset_id_index
chunk_dataset_id_index
entity_name_index
```

### Outbox

`graph_outbox.payload` é um projection command snapshot versionado. Delete não depende da
existência posterior do row PostgreSQL original. Replay deve ser idempotente e não pode
duplicar nodes ou relationships.

### Rebuild

Rebuild usa o estado PostgreSQL atual, nunca o histórico da outbox.

Escopos:

- global;
- por dataset.

Seleção authoritative:

- Dataset: `datasets.status = 'active'`;
- Chunk: `chunks.generation = datasets.active_generation` e `chunks.is_active IS TRUE`;
- Entity: `entities.generation = datasets.active_generation` e `entities.is_active IS TRUE`;
- Relation: `relations.generation = datasets.active_generation`, `relations.is_active IS TRUE`
  e source/target Entity no conjunto projetável;
- EntityMention: sem generation/is_active próprios; projetar somente se Entity e Chunk
  correspondentes estiverem nos conjuntos projetáveis;
- NEXT: derivado somente de Chunks projetáveis consecutivos conforme regra acima.

Nodes são projetados antes dos relationships.

---

# 6. Ordem de execução

```text
B3
SM-301  Architecture Gate — contrato de projeção e rebuild
  ↓
SM-302  Driver Neo4j async e lifecycle
  ↓
SM-303  Constraints e índices Neo4j
  ↓
SM-304  Readiness Neo4j
  ↓
SM-305  Projection port + aplicação idempotente
  ↓
SM-306  Graph outbox processor unitário
  ↓
SM-307  Rebuild da projeção
  ↓
SM-308  Integração real e recovery gate
  ↓
GATE-B3
```

Tasks independentes podem ser paralelizadas somente quando não alterarem os mesmos
contratos ou arquivos. Em B3, a ordem acima é a recomendada porque SM-301 congela os
contratos usados por todas as seguintes.

---

# 7. B3 — Neo4j Foundation

---

## SM-301 — Architecture Gate: congelar contrato da projeção Neo4j

**Status:** DONE
**Prioridade:** P0  
**Dependências:** GATE-B2  
**Tipo:** ARCHITECTURE GATE  
**PRD:** ADR-0002; seção 13 Modelo de grafo Neo4j; graph outbox; FR-110; FR-120

### Objetivo

Congelar antes da implementação:

- aggregate types da `graph_outbox`;
- contrato mínimo do payload;
- identidades dos nodes/relationships;
- semântica de upsert/delete;
- replay/idempotência;
- comportamento quando o row PostgreSQL já não existe no consumo de delete;
- fronteira projection port;
- fronteira B3 consumer vs B5 worker;
- rebuild global/dataset;
- generation/is_active aplicáveis ao rebuild;
- nomes canônicos de constraints/índices;
- contrato de readiness.

### Entregável

Um novo ADR em `docs/adr/`, com próximo número disponível, cobrindo o contrato completo
de projeção e rebuild.

### Regras

- não implementar driver;
- não implementar Cypher de produção;
- não implementar worker;
- não implementar polling;
- não alterar migrations PostgreSQL já congeladas;
- não adicionar artefatos Neo4j fora do modelo do PRD sem decisão explícita;
- não depender de APOC/GDS.

### Gate

SM-301 foi marcada `DONE` após a revisão do ADR-0008.

---

## SM-302 — Implementar driver Neo4j assíncrono e lifecycle

**Status:** DONE
**Prioridade:** P0  
**Dependências:** SM-301  
**PRD:** configuração Neo4j; startup/shutdown; FR-120  
**ADR:** ADR-0008 — Neo4j Projection and Rebuild Contract

### Objetivo

Criar a infraestrutura Neo4j mínima usando o driver oficial assíncrono, sem conectar no
import e sem introduzir abstração de provider.

### Configuração existente a reutilizar

```text
NEO4J_URI
NEO4J_USERNAME
NEO4J_PASSWORD
NEO4J_DATABASE
```

A configuração deve continuar vindo exclusivamente de `load_settings()` / `Settings`.

### Implementação esperada

Criar módulo pequeno em:

```text
sofias_memory/infrastructure/neo4j/
```

Responsabilidades:

- construir `AsyncDriver` oficial;
- autenticar com username/password configurados;
- manter database explícito nas operações;
- disponibilizar fechamento assíncrono;
- permitir `verify_connectivity`/consulta leve;
- não executar schema bootstrap no construtor;
- não executar query no import;
- não logar URI com credentials;
- nunca logar password.

### Lifecycle

Integrar ao registry/lifespan existente de forma compatível com a arquitetura atual:

- inicialização controlada;
- driver compartilhado pelo processo;
- fechamento no shutdown;
- erro de conexão tratado como dependência indisponível, não como `/health/live` morto.

### Não fazer

- provider interface genérica;
- service locator;
- singleton global mutável;
- retry infinito;
- APOC/GDS;
- graph schema bootstrap nesta task;
- readiness completo nesta task;
- projeção nesta task.

### Testes

Unitários:

- criação usa `Settings`;
- password não aparece em repr/log/exceptions controladas;
- database configurado é preservado;
- driver fecha exatamente uma vez;
- import não abre conexão;
- lifecycle fecha recurso no shutdown;
- erro de conectividade é representável sem derrubar live.

### Validação

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory scripts
uv run pytest
git diff --check
```

---

## SM-303 — Implementar constraints e índices Neo4j

**Status:** DONE
**Prioridade:** P0  
**Dependências:** SM-302  
**PRD:** seção 13 Modelo de grafo Neo4j  
**ADR:** ADR-0008 — Neo4j Projection and Rebuild Contract

### Objetivo

Instalar de forma idempotente somente o schema Neo4j mínimo congelado pelo ADR.

### Mínimo obrigatório

O bootstrap deve prover, conforme nomes exatos congelados no ADR-0008:

- unique constraint para `Entity.id`;
- unique constraint para `Chunk.id`;
- index para `Entity.dataset_id`;
- index para `Chunk.dataset_id`;
- index para `Entity.name`.

Não adicionar índice composto sem requisito congelado.

### Regras

- usar Cypher padrão compatível com Neo4j 5.x;
- preferir `IF NOT EXISTS` quando suportado;
- nomes determinísticos de constraints/indexes;
- bootstrap idempotente;
- readiness não cria schema;
- schema bootstrap é operação explícita de startup/bootstrap;
- não executar `DROP CONSTRAINT`/`DROP INDEX` automaticamente;
- não depender de APOC/GDS.

### Testes unitários

- conjunto de schema statements é exato;
- nomes batem com ADR;
- não existem statements APOC/GDS;
- segunda aplicação não altera o contrato;
- nenhum schema além do autorizado é criado.

### Integração real

Adicionar teste opt-in contra Neo4j real para:

- aplicar schema;
- aplicar novamente;
- verificar constraints;
- verificar indexes;
- confirmar database configurado.

O teste local não deve destruir dados existentes.

---

## SM-304 — Integrar Neo4j ao readiness

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-302, SM-303  
**PRD:** FR-120; startup/readiness; NFR-001  
**ADR:** ADR-0008 — Neo4j Projection and Rebuild Contract

### Objetivo

Substituir o placeholder Neo4j do readiness por uma verificação real e leve.

### `/health/live`

Permanece:

- sem consulta Neo4j;
- sem consulta PostgreSQL;
- rápido;
- independente de falha Neo4j.

### `/health/ready`

Neo4j deve ser `ready` somente quando:

- conectividade funciona;
- database configurado é acessível;
- constraints mínimas esperadas existem;
- indexes mínimos esperados existem.

Falha Neo4j:

- deixa readiness false;
- não derruba `/health/live`;
- não cria schema;
- não executa query pesada;
- não faz scan de nodes/relationships;
- não expõe secrets.

### Startup

Respeitar a ordem arquitetural já congelada tanto quanto aplicável à fase atual:

```text
driver
→ connectivity
→ schema bootstrap
→ readiness
```

Worker ainda permanece fora do escopo B3 até a task específica de processor unitário e B5.

### Testes

- Neo4j ready;
- conexão recusada;
- database inexistente/inacessível;
- constraint ausente;
- index ausente;
- live não consulta Neo4j;
- resposta não contém credentials.

### Integração real

Teste opt-in com Neo4j real, sem operações destrutivas globais.

---

## SM-305 — Implementar Projection Port e projeção Neo4j idempotente

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-301, SM-302, SM-303  
**PRD:** seção 13; ADR-0002  
**ADR:** ADR-0008 — Neo4j Projection and Rebuild Contract

### Objetivo

Criar o boundary concreto entre aplicação e Neo4j e implementar a aplicação de UMA
operação de projeção já validada.

### Arquitetura

O port não pode importar `neo4j`.

O implementation Neo4j fica em `infrastructure`.

Não criar `GraphDatabaseProvider`, registry de providers ou adapter genérico para bancos
que não existem.

### Contratos

Usar exatamente:

- aggregate types;
- payload;
- identidades;
- upsert;
- delete;
- replay;

congelados pelo ADR-0008.

Não reinterpretar o payload nesta task.

### Idempotência

Aplicar duas vezes o mesmo comando deve resultar no mesmo grafo lógico:

- sem node duplicado;
- sem `RELATES_TO` duplicado;
- sem `MENTIONED_IN` duplicado;
- sem `NEXT` duplicado.

### Upsert

Usar identidades PostgreSQL estáveis e Cypher padrão.

### Delete

Deve funcionar conforme snapshot/identidade congelada no ADR mesmo se o registro
PostgreSQL correspondente já não existir.

### Segurança

- queries parametrizadas;
- não interpolar conteúdo em Cypher;
- labels/types são constantes do código, nunca input arbitrário;
- sem endpoint público;
- sem Cypher fornecido pelo usuário.

### Testes

Unitários:

- comando → Cypher/params esperados;
- replay determinístico;
- IDs corretos;
- delete não exige row já apagado quando ADR assim exigir;
- payload inválido falha antes da escrita;
- nenhum conteúdo desnecessário é enviado ao Neo4j.

Integração:

- upsert de cada aggregate type congelado;
- replay;
- update de properties;
- delete;
- ausência de duplicatas.

---

## SM-306 — Implementar graph outbox processor unitário

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-305; SM-213/B2 repositories/UoW  
**PRD:** graph outbox; ADR-0002  
**ADR:** ADR-0008 — Neo4j Projection and Rebuild Contract

### Objetivo

Implementar o mecanismo para processar UMA entrada já selecionada da `graph_outbox`,
sem implementar o polling/worker completo de B5.

### Boundary B3

B3 pode:

- receber um outbox id/evento concreto;
- validar aggregate type/operation/payload;
- aplicar a projeção via projection port;
- registrar resultado `done`;
- registrar tentativa/falha conforme contrato persistido;
- ser seguro para retry/replay.

B3 não pode:

- implementar loop de polling;
- scheduler;
- heartbeat de worker;
- stale recovery;
- concorrência por dataset;
- `FOR UPDATE SKIP LOCKED` da fila de pipelines;
- lifecycle completo do worker.

Esses itens pertencem a B5.

### Semântica de entrega

Como não existe transação distribuída:

- PostgreSQL outbox e Neo4j são consistentes por retry;
- operação Neo4j deve ser idempotente;
- crash após Neo4j e antes de marcar `done` deve ser recuperável por replay;
- falha Neo4j não desfaz o estado autoritativo PostgreSQL.

### Repositories

Estender somente os repositories concretos necessários.

Não criar repository genérico.

### Testes

- sucesso marca `done`;
- attempt coerente;
- falha preserva possibilidade de retry;
- replay após "Neo4j aplicado, PostgreSQL ainda não marcado done";
- aggregate type desconhecido falha de forma determinística;
- payload inválido não gera escrita parcial;
- nenhum polling é criado.

---

## SM-307 — Implementar rebuild da projeção a partir do PostgreSQL

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-305, SM-306  
**PRD:** ADR-0002; critério MVP "Neo4j pode ser reconstruído a partir do PostgreSQL"  
**ADR:** ADR-0008 — Neo4j Projection and Rebuild Contract

### Objetivo

Provar operacionalmente que Neo4j é descartável e reconstruível sem depender do histórico
da `graph_outbox`.

### Rebuild authoritative source

O rebuild deve ler o estado PostgreSQL atual e projetável.

Não deve reconstruir consumindo eventos históricos da outbox.

### Escopos

Implementar conforme ADR-0008:

- rebuild por dataset;
- rebuild global.

### Regras de seleção

Usar exatamente as regras de:

- generation;
- `is_active`;
- artefatos válidos;
- relações/evidências ativas;

congeladas no ADR.

Não inventar uma segunda regra de "ativo" dentro do Neo4j.

### Ordem de rebuild

A ordem deve evitar edges órfãs. Conceitualmente:

```text
schema
→ Entity nodes
→ Chunk nodes
→ MENTIONED_IN
→ RELATES_TO
→ NEXT
```

A ordem final deve seguir o ADR.

### Cleanup

Dataset rebuild:

- remove/substitui somente projeção daquele dataset.

Global rebuild:

- somente em contexto explicitamente autorizado;
- nunca executar destruição global implicitamente no startup;
- não executar automaticamente no readiness.

### Operação

Pode existir serviço interno e/ou script operacional enxuto em `scripts/`.

Não criar endpoint público de rebuild nesta fase.

Se houver script destrutivo, exigir confirmação explícita.

### Testes

- rebuild de database Neo4j vazio;
- rebuild repetido é idempotente;
- rebuild por dataset não afeta outro dataset;
- grafo resultante equivale ao estado PostgreSQL projetável;
- outbox histórica não é necessária;
- artefatos inativos/deletados não reaparecem;
- relationships sem endpoints não são criados.

---

## SM-308 — Criar gate real Neo4j/projection/recovery

**Status:** TODO  
**Prioridade:** P0  
**Dependências:** SM-302..SM-307  
**Tipo:** INTEGRATION GATE  
**PRD:** testes de integração; recovery; rebuild; E2E crash PostgreSQL→Neo4j

### Objetivo

Criar o gate automatizado de B3 com infraestrutura Neo4j real.

### Backend CI

Preferência:

```text
Testcontainers Neo4j
```

CI não pode depender da porta local `7688`.

A imagem usada pelo gate deve acompanhar a imagem Neo4j canônica do `compose.yaml` ou
uma política versionada equivalente.

### Backend local

Pode usar explicitamente `Settings`/`.env` e o Neo4j local configurado.

O backend local:

- não deve executar wipe global;
- não deve usar `MATCH (n) DETACH DELETE n` sem isolamento comprovado;
- deve gerar `dataset_id`/IDs exclusivos de teste;
- deve limpar somente nodes/relationships criados pelo próprio teste;
- constraints/indexes globais só podem ser criados de forma idempotente;
- nunca imprimir password ou URI com credentials.

### Cenários mínimos compartilhados

1. connectivity;
2. database configurado;
3. schema bootstrap;
4. constraints/indexes;
5. Entity upsert;
6. Chunk upsert;
7. RELATES_TO;
8. MENTIONED_IN;
9. NEXT;
10. replay/idempotência;
11. update de properties;
12. delete;
13. dataset isolation;
14. outbox process success;
15. outbox retry/replay;
16. simular crash lógico após Neo4j e antes de `done`;
17. rebuild por dataset;
18. readiness true;
19. readiness false quando Neo4j indisponível/schema incompleto;
20. `/health/live` independente.

### Cenários destrutivos exclusivos de backend descartável/Testcontainers

- rebuild global a partir de Neo4j vazio;
- apagar completamente a projeção e reconstruí-la;
- confirmar equivalência com PostgreSQL;
- recovery após reset total do grafo.

Não executar esses cenários contra Neo4j local persistente por fallback.

### Segurança do gate

- opt-in explícito;
- backend explícito;
- sem fallback silencioso;
- credentials redigidas em repr/log/tracebacks controláveis;
- cleanup em `finally`;
- local cleanup somente por IDs gerados pelo gate.

### Não fazer

- depender de Docker Desktop;
- hard-code `7688`;
- fallback automático de Testcontainers para local;
- depender de APOC/GDS;
- criar worker B5;
- criar graph-RAG.

---

# GATE-B3 — Neo4j Foundation concluída

Antes de iniciar B4:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory scripts
uv run pytest
```

Executar também os gates reais Neo4j definidos pelas tasks B3.

## Critérios obrigatórios

- ADR de projeção/rebuild aceito;
- driver Neo4j oficial async com lifecycle correto;
- nenhuma conexão no import;
- secrets Neo4j não aparecem em logs/errors;
- constraints mínimas instaladas e verificáveis;
- indexes mínimos instalados e verificáveis;
- `/health/live` não consulta Neo4j;
- `/health/ready` falha quando Neo4j está indisponível;
- readiness não cria schema;
- projection port não depende do pacote `neo4j`;
- `sofias_memory/domain` não importa Neo4j;
- nenhuma abstração de graph provider;
- Cypher parametrizado;
- labels/relationship types não vêm de input arbitrário;
- operações de projeção idempotentes;
- replay não duplica nodes/edges;
- delete recuperável;
- outbox processor funciona sem polling/worker completo;
- crash lógico após Neo4j/antes de `done` é recuperável;
- rebuild não depende do histórico da outbox;
- rebuild por dataset é isolado;
- rebuild total funciona em backend descartável;
- PostgreSQL continua sendo a fonte de verdade;
- nenhuma informação essencial existe somente no Neo4j;
- nenhuma dependência APOC/GDS no core;
- nenhum graph-RAG antecipado.

## Verificações arquiteturais sugeridas

```bash
git grep -n -i "neo4j" -- sofias_memory/domain
git grep -n -E "GraphDatabaseProvider|GraphProvider|DatabaseProvider" -- sofias_memory
git grep -n -E "apoc\.|gds\." -- sofias_memory
```

Esperado para imports Neo4j no domínio e dependências APOC/GDS:

```text
<sem saída>
```

A busca por nomes de provider deve ser revisada manualmente caso retorne algum uso
legítimo não relacionado.

---

# 8. Política de testes reais local vs CI

## Local

Ambiente conhecido de desenvolvimento pode utilizar:

```text
Neo4j host: 127.0.0.1
Bolt host port: 7688
database: neo4j
```

Sempre através de `Settings` e `.env`.

Nunca copiar credentials para código, tests, prompts versionados ou relatórios.

Operações locais devem ser dataset-scoped e auto-limpáveis.

## CI

Preferir Neo4j descartável via Testcontainers.

CI:

- não depende de `127.0.0.1:7688`;
- usa porta dinâmica;
- começa em estado conhecido;
- permite testes destrutivos de reset/rebuild;
- encerra container no cleanup.

---

# 9. Limites explícitos de B3

B3 NÃO implementa:

- contratos OpenAPI finais de B4;
- pipeline queue/worker B5;
- chunking/ingestion B6;
- extração LLM/Cognify B7;
- graph traversal/Recall B8;
- Improve B9;
- Forget completo B10;
- graph-RAG;
- endpoint arbitrário Cypher;
- APIs públicas novas de grafo além do que uma task futura congelar;
- scheduler;
- cron interno;
- múltiplos workers;
- HA/multi-réplica.

---

# 10. Definition of Done mínima por task

Uma task B3 só pode ser marcada `DONE` quando:

- escopo implementado;
- ADR-0008 respeitado;
- testes aplicáveis criados/atualizados;
- `uv lock --check` passa;
- `ruff` passa;
- `ruff format --check` passa;
- `mypy` passa;
- `pytest` aplicável passa;
- integração real passa quando a task exigir;
- secrets não foram expostos;
- documentação afetada foi atualizada;
- nenhum conceito proibido do `AGENTS.md` foi violado;
- diff foi revisado;
- nenhum commit/push/PR foi feito pelo Codex.

---

# 11. Template de entrega de task B3 ao Codex

```text
Implemente somente a task SM-3XX do backlog técnico B3 do Sofias Memory.

Antes de alterar código:
1. leia AGENTS.md;
2. leia a task SM-3XX completa;
3. leia docs/product/Sofias_Memory_PRD_SPECS.md nas seções referenciadas;
4. leia ADR-0002;
5. leia ADR-0008 — Neo4j Projection and Rebuild Contract;
6. inspecione código, migrations e testes atuais.

Regras:
- PostgreSQL continua source of truth;
- Neo4j é projeção reconstruível;
- não implemente tasks posteriores;
- não reintroduza conceitos proibidos;
- não crie provider abstractions;
- não dependa de APOC/GDS;
- não faça commit/push/PR;
- mantenha o diff restrito ao escopo;
- adicione testes aplicáveis;
- execute os checks exigidos pelo AGENTS.md.

Ao terminar informe:
- arquivos criados/alterados;
- decisões tomadas;
- contratos do ADR utilizados;
- testes executados e resultados;
- integração real executada ou não;
- riscos/divergências reais;
- git status final.
```

---

# 12. O que vem depois

Somente após `GATE-B3`:

```text
B4 — Pydantic/OpenAPI Contracts
B5 — Pipeline Engine
B6 — Ingestion
B7 — Cognify
B8 — Recall
B9 — Improve/Feedback
B10 — Forget
B11 — Hardening/Release
```

Não detalhar ou antecipar implementação dessas fases dentro de B3.

---

# 13. Reconciliação após SM-301

Este backlog já foi reconciliado com ADR-0008 — Neo4j Projection and Rebuild
Contract.

SM-301 está `DONE`.

SM-302 permanece `TODO`.

Nenhuma task SM-302+ foi iniciada durante esta reconciliação.
