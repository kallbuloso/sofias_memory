# PRD e Especificação Técnica — Sofias Memory

**Produto:** Sofias Memory  
**Organização:** Sofias Tech  
**Documento:** Product Requirements Document + Software Requirements Specification  
**Versão:** 0.1.0  
**Status:** Proposta técnica para implementação  
**Data:** 05 de agosto de 2026  
**Baseline analisada:** `topoteretes/cognee`, branch `main`, versão `1.4.1`, commit `38eece5bbb0cb9f5706fed908abd16dba0f5505e`

---

## 1. Resumo executivo

O **Sofias Memory** será uma reimplementação focada do núcleo de memória semântica e grafo de conhecimento do Cognee, distribuída como uma **aplicação modular monolítica**, single-user, operada por uma única API HTTP e protegida por uma chave estática definida no ambiente:

```env
API_KEY=sf-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

A aplicação não possuirá domínio de usuários, login, registro, cookies, JWT, roles, permissions, ACL, tenants, organizações, gerenciamento de API keys, settings persistidos, rotas de configuração, cloud client, remote client, sync, push, integrações, plugins ou seleção dinâmica de bancos e provedores.

A arquitetura terá um único caminho oficial de execução:

- FastAPI como API e processo principal;
- worker interno no mesmo processo/aplicação;
- PostgreSQL com pgvector como banco relacional, índice lexical, armazenamento vetorial, estado dos pipelines e fonte de verdade;
- Neo4j como projeção obrigatória do grafo de conhecimento;
- filesystem local persistente para arquivos originais;
- API OpenAI-compatible para LLM e embeddings;
- configuração exclusivamente por `.env`, carregada e validada no startup;
- apenas a porta HTTP da aplicação exposta publicamente.

O produto preservará as capacidades centrais:

1. ingerir textos, arquivos e URLs;
2. extrair e normalizar conteúdo;
3. dividir documentos em chunks;
4. gerar embeddings;
5. extrair entidades, relações e resumos por LLM;
6. persistir o conhecimento em vetores e grafo;
7. recuperar contexto por busca vetorial, lexical, híbrida e graph-RAG;
8. enriquecer e consolidar a memória explicitamente;
9. apagar uma fonte, a memória derivada, um dataset ou toda a base;
10. manter proveniência entre resposta, entidade, relação, chunk e documento original.

O objetivo não é remover arquivos aleatoriamente de um fork. O objetivo é reconstruir um núcleo coerente, menor, testável e previsível. Um fork amputado herdaria acoplamentos de usuário, ACL, configuração dinâmica, providers, sessões, cloud e integrações; seria uma dívida técnica fantasiada de atalho.

---

## 2. Decisões mandatórias

### 2.1 Tipo de aplicação

O Sofias Memory será um **modular monolith**: uma única base de código, uma única aplicação Python, um único processo de API/worker e uma única versão implantável.

O deployment oficial conterá três serviços obrigatórios:

1. `sofias-memory`: API, pipelines e worker interno;
2. `postgres`: PostgreSQL + pgvector;
3. `neo4j`: projeção e travessia do grafo.

“Monolítico” descreve a aplicação, não significa colocar PostgreSQL e Neo4j dentro do mesmo processo ou imagem. Empacotar bancos dentro do container da API pioraria backup, atualização, observabilidade e recuperação.

### 2.2 Single-user

Single-user significa:

- não existe tabela `users`;
- não existe `owner_id`;
- não existe `tenant_id`;
- não existe ACL;
- não existe compartilhamento;
- não existe resolução de dataset autorizado;
- todo dataset pertence à única instância;
- `dataset` é somente um namespace lógico;
- `session_id`, quando informado, é metadado de contexto e nunca uma fronteira de autorização.

### 2.3 Autenticação simplificada

Todas as rotas, exceto health checks, exigem:

```http
X-API-Key: sf-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

A chave:

- é definida somente por `API_KEY` no `.env`;
- não é persistida no banco;
- não possui CRUD;
- não possui endpoint de rotação;
- é comparada com operação constant-time;
- nunca aparece em logs;
- deve começar com `sf-`;
- deve possuir ao menos 32 caracteres aleatórios após o prefixo;
- é rotacionada alterando o ambiente e reiniciando a aplicação.

### 2.4 Credenciais do provedor de IA

`API_KEY=sf-...` é a única credencial de acesso ao **Sofias Memory**, mas não elimina a necessidade de credenciais do LLM/embedding quando um serviço externo for utilizado.

O runtime poderá exigir também:

```env
LLM_API_KEY=...
EMBEDDING_API_KEY=...
```

Essas credenciais são configuração de infraestrutura, não autenticação da API e não serão administradas por rotas.

A afirmação “somente uma chave total para tudo” só seria verdadeira com LLM e embeddings locais incorporados, o que aumentaria drasticamente imagem, memória, GPU e complexidade operacional. Isso não faz parte da versão 1.

### 2.5 Stack única e sem opcionais

Não haverá extras de instalação como `[postgres]`, `[neo4j]`, `[docs]`, `[scraping]` ou adapters comunitários. Todas as dependências suportadas estarão na instalação principal.

Não haverá seleção entre LanceDB, pgvector, Kuzu, Neo4j, SQLite, Turso, Qdrant ou outros. A stack oficial será fixa:

- PostgreSQL + pgvector;
- Neo4j;
- armazenamento local;
- OpenAI-compatible API.

### 2.6 Configuração imutável em runtime

Toda configuração será carregada uma vez no startup. Não haverá:

- `/settings`;
- `/configuration`;
- configuração por dataset;
- atualização de provider em runtime;
- gravação de secrets no banco;
- parâmetros arbitrários para substituir banco, LLM ou embedding em uma requisição.

Alterações de configuração exigem restart.

### 2.7 Sem Sync

Não haverá:

- rota `/sync`;
- replicação entre instâncias;
- cliente cloud;
- envio de dados para Cognee Cloud;
- sincronização de cache de sessão com memória permanente;
- sincronização automática oculta disparada por término de sessão.

Poderá existir **reconciliação interna de consistência** entre PostgreSQL e Neo4j, executada pelo worker, sem endpoint público chamado Sync e sem conexão com outra instância.

---

## 3. Contexto e problema

O Cognee moderno combina biblioteca Python, API HTTP, CLI, memória de sessão, autenticação, permissões, datasets por usuário, múltiplos bancos, providers opcionais, integrações, cloud client, agent memory, skills, proposals, observabilidade, migrações e duas superfícies de API.

Essas capacidades são válidas para um produto multiusuário e extensível, mas são desnecessárias e prejudiciais para o objetivo do Sofias Memory:

- aumentam o grafo de dependências;
- espalham verificações de usuário e autorização em pipelines centrais;
- exigem `owner_id`, `tenant_id` e ACL nos modelos;
- permitem configurações divergentes por requisição/dataset;
- multiplicam caminhos de execução;
- tornam testes e migrations mais complexos;
- ampliam superfície de ataque;
- dificultam prever o comportamento operacional.

O Sofias Memory resolverá esse problema adotando uma regra simples: **uma instância, uma identidade, uma stack, uma configuração e um caminho oficial para cada operação**.

---

## 4. Visão do produto

> Tornar a memória semântica e relacional de aplicações e agentes uma infraestrutura simples, privada, previsível e fácil de integrar, sem carregar a complexidade de uma plataforma SaaS multiusuário.

### 4.1 Proposta de valor

O Sofias Memory deverá permitir que aplicações Laravel, n8n, agentes e serviços internos:

- enviem informações em texto ou documento;
- transformem essas informações em memória persistente;
- recuperem respostas contextualizadas com referências;
- naveguem relações entre conceitos;
- atualizem a relevância do conhecimento por feedback;
- removam dados e seus derivados de forma confiável;
- operem tudo por uma API simples e uma única chave.

### 4.2 Público-alvo

- aplicações internas da Sofias Tech;
- agentes de IA single-user;
- automações n8n;
- SaaS que utilizem uma instância isolada por cliente;
- ferramentas pessoais ou empresariais self-hosted;
- integrações Laravel/PHP que precisem de memória semântica.

### 4.3 Princípios do produto

1. **Explicit is better than automatic:** não haverá sincronizações ou melhorias escondidas.
2. **Proveniência antes de eloquência:** toda resposta deve poder apontar para suas fontes.
3. **Idempotência antes de velocidade aparente:** reenvios não podem duplicar memória.
4. **Configuração no startup:** nenhum endpoint altera infraestrutura.
5. **Um caminho oficial:** não haverá dezenas de providers e estratégias intercambiáveis.
6. **Falhar cedo:** configuração inválida impede startup.
7. **Deleção é produto:** apagar memória derivada deve ser tão confiável quanto criá-la.
8. **Sem telemetria externa:** nenhuma informação de uso é enviada para terceiros, exceto as chamadas necessárias ao provedor de IA configurado.

---

## 5. Objetivos

### 5.1 Objetivos funcionais

- Ingerir texto, arquivos e URLs.
- Detectar duplicidade por hash.
- Extrair conteúdo textual de formatos suportados.
- Criar chunks estáveis e reproduzíveis.
- Gerar embeddings e índices lexicais.
- Extrair entidades, relações, atributos e resumos.
- Manter grafo consultável no Neo4j.
- Oferecer busca vetorial, lexical, híbrida, RAG e graph-RAG.
- Retornar referências de chunks e fontes.
- Enriquecer explicitamente a memória.
- Permitir feedback em resultados.
- Remover fonte, memória derivada, dataset ou tudo.
- Expor estado e histórico dos pipelines.
- Recuperar pipelines interrompidos após reinício.

### 5.2 Objetivos técnicos

- Código Python 3.12 fortemente tipado.
- API OpenAPI estável e versionada.
- Migrations Alembic determinísticas.
- PostgreSQL como fonte de verdade.
- Neo4j como projeção reconstruível.
- Worker interno com estado persistido.
- Testes unitários, integração, contratos e E2E.
- Imagens Docker reproduzíveis e sem tags flutuantes.
- Apenas uma porta pública.
- Logs JSON estruturados.

### 5.3 Métricas de sucesso

- 100% das rotas privadas rejeitam chave ausente ou inválida.
- 0 tabelas ou colunas de usuário, tenant ou ACL.
- 0 rotas de auth, permissions, settings ou sync.
- Reenvio da mesma fonte com a mesma idempotency key não duplica dados.
- 100% das respostas RAG/graph-RAG possuem referências quando houver contexto recuperado.
- Exclusão de fonte remove seus chunks, evidências, vetores e projeções órfãs.
- Reinício durante pipeline não perde o estado e permite retomada/retry seguro.
- Nenhum conteúdo de documento é registrado em logs por padrão.

---

## 6. Fora do escopo da versão 1

- frontend web;
- MCP server;
- CLI completa;
- autenticação de usuários;
- registro, login, reset de senha e verificação;
- JWT, cookies e OAuth;
- roles, groups, permissions e ACL;
- multitenancy;
- múltiplas API keys;
- billing e quotas por usuário;
- providers de banco alternativos;
- S3 e object storage remoto;
- Slack, Google Drive, Notion, GitHub e outras integrações;
- cloud client, `serve`, `push` e sync entre instâncias;
- agentic search e execução de tools;
- skills e proposals;
- code graph especializado;
- entrada de áudio e transcrição;
- OCR de imagens;
- pipelines Python arbitrários enviados pelo cliente;
- endpoint de Cypher arbitrário;
- mudança de modelo ou dimensão de embeddings sem reindexação;
- alta disponibilidade multi-réplica;
- cluster de workers;
- GPU/modelos locais embutidos.

Esses itens não devem permanecer “desabilitados”. Devem estar ausentes do código de produção.

---

## 7. Matriz de paridade funcional

| Capacidade Cognee | Sofias Memory | Decisão |
|---|---|---|
| `add` | `remember` com modo `ingest` | Mantida e simplificada |
| `cognify` | pipeline de cognificação e rota explícita | Mantida |
| `search` | `recall` | Mantida com conjunto reduzido de modos |
| `remember` | rota principal de entrada | Mantida |
| `recall` | rota principal de consulta | Mantida |
| `memify` | núcleo interno de `improve` | Mantido sem tasks externas |
| `improve` | enriquecimento explícito | Mantido e simplificado |
| `forget`, `delete`, `prune` | uma operação `forget` | Unificadas |
| datasets | namespaces globais | Mantidos sem ownership |
| graph schema/provenance | rotas read-only | Mantidas |
| export/import | backup operacional, fase posterior | Não bloqueia v1 |
| sessions | `session_id` como metadado | Sem cache separado e sem sync |
| users/auth | inexistente | Removido |
| permissions/ACL | inexistente | Removido |
| API key management | inexistente | Removido; chave vem do ambiente |
| settings/configuration routes | inexistente | Removido |
| sync/cloud/serve/push | inexistente | Removido |
| agents/skills/proposals | inexistente | Removido |
| integrations | inexistente | Removido |
| múltiplos DB providers | inexistente | Stack fixa |
| custom pipeline público | inexistente | Engine interno apenas |
| telemetry externa | inexistente | Removida |
| OpenTelemetry | logs/métricas locais | Simplificado |

---

## 8. Terminologia

### Dataset

Namespace lógico global que agrupa fontes, documentos, chunks, entidades e relações. Não representa tenant, usuário ou permissão.

### Source

Entrada original recebida: texto, arquivo ou URL.

### Document

Representação textual normalizada de uma fonte.

### Chunk

Segmento estável de um documento, com offsets, tokens, embedding e metadados.

### Entity

Conceito canônico extraído do conteúdo: pessoa, organização, produto, tecnologia, local, evento ou outro tipo.

### Relation

Ligação dirigida entre duas entidades, acompanhada de predicado, confiança e evidências.

### Evidence

Referência ao chunk que sustenta uma entidade ou relação.

### Summary

Resumo de documento, entidade, cluster ou dataset.

### Run

Execução persistida de um pipeline.

### Memory generation

Versão ativa dos artefatos derivados de uma fonte. Permite reconstruir a memória sem expor resultados parciais.

---

## 9. Casos de uso principais

### UC-01 — Memorizar texto

Uma aplicação envia texto e metadados. O Sofias Memory persiste a fonte, cria chunks, embeddings, entidades, relações e resumos e retorna o run concluído ou em processamento.

### UC-02 — Memorizar documento

O cliente envia PDF, DOCX, Markdown, TXT, CSV, JSON ou HTML. O sistema extrai conteúdo, registra metadados e executa o pipeline completo.

### UC-03 — Memorizar URL

O cliente informa uma URL HTTPS. O downloader valida SSRF, tamanho e tipo, extrai o conteúdo e processa a fonte.

### UC-04 — Recuperar resposta

O cliente envia pergunta, dataset e modo de busca. O sistema recupera contexto, monta resposta e devolve referências.

### UC-05 — Recuperar apenas contexto

O cliente solicita chunks, entidades ou relações sem geração de resposta pelo LLM.

### UC-06 — Melhorar memória

O cliente dispara consolidação, reponderação por feedback, deduplicação de entidades, embeddings de relações e regeneração de resumos.

### UC-07 — Esquecer fonte

O cliente remove uma fonte específica e todos os seus derivados, preservando artefatos ainda sustentados por outras fontes.

### UC-08 — Limpar somente memória derivada

O cliente remove chunks derivados, vetores, entidades, relações e resumos, mas mantém a fonte original para nova cognificação.

### UC-09 — Esquecer dataset

O cliente remove um dataset inteiro.

### UC-10 — Acompanhar pipeline

O cliente consulta status, etapa, progresso, métricas e erro de um run.

---

## 10. Requisitos funcionais

## FR-001 — Inicialização e configuração

1. A aplicação deve carregar `.env` no startup.
2. Deve validar todas as configurações antes de abrir a porta HTTP.
3. Deve falhar com mensagem objetiva quando faltar segredo ou conexão obrigatória.
4. Deve executar migrations PostgreSQL.
5. Deve validar extensões `vector` e `pg_trgm`.
6. Deve validar conectividade e constraints do Neo4j.
7. Deve iniciar o worker interno somente após readiness dos bancos.
8. Deve recuperar runs abandonados.
9. Não deve criar usuário padrão.
10. Não deve gravar settings no banco.

## FR-002 — API key

1. `/health/live` e `/health/ready` não exigem API key.
2. Todas as demais rotas exigem `X-API-Key`.
3. Chave ausente retorna `401`.
4. Chave inválida retorna `403`.
5. Comparação deve usar `hmac.compare_digest` ou equivalente.
6. Chaves nunca são registradas.
7. Chaves não podem ser recebidas por query string.
8. Swagger deve declarar somente o esquema `ApiKeyAuth`.

## FR-010 — Datasets

O sistema deve permitir:

- criar dataset;
- listar datasets;
- obter dataset;
- renomear dataset;
- consultar contadores;
- apagar dataset;
- reconstruir a memória de um dataset.

Regras:

- nome único global;
- slug normalizado para comparação;
- nome entre 1 e 120 caracteres;
- exclusão deve ser assíncrona quando houver artefatos;
- dataset padrão `main` é criado no primeiro uso, não no startup;
- dataset não possui owner ou tenant.

## FR-020 — Ingestão

### Entradas obrigatórias da versão 1

- texto UTF-8;
- TXT;
- Markdown;
- JSON;
- CSV;
- HTML;
- PDF textual;
- DOCX;
- URL HTTPS.

### Não suportado na versão 1

- OCR de PDF imagem;
- imagens;
- áudio;
- vídeo;
- PPTX;
- XLSX;
- ZIP;
- repositório Git;
- path arbitrário do host informado por HTTP.

### Regras de ingestão

1. Calcular SHA-256 dos bytes originais.
2. Detectar MIME por conteúdo, nunca apenas extensão.
3. Validar tamanho antes e durante streaming.
4. Salvar arquivo em caminho controlado pela aplicação.
5. Não aceitar caminhos com traversal.
6. Normalizar texto para UTF-8 e LF.
7. Preservar arquivo original.
8. Registrar hash do conteúdo normalizado.
9. Deduplicar por `(dataset_id, content_sha256)`.
10. Permitir `force=true` para nova versão da mesma fonte.
11. Aceitar `Idempotency-Key` no header.
12. Nunca duplicar pipeline concluído para mesma chave e payload.
13. Rejeitar reuse da mesma idempotency key com payload diferente.
14. Registrar metadados JSON com limite de tamanho.
15. Não registrar conteúdo em logs.

## FR-030 — Chunking

1. Chunking deve ser determinístico para mesma configuração e texto.
2. Deve usar tokenizer compatível com o embedding model.
3. Cada chunk deve guardar offsets no documento.
4. Chunks devem ter overlap configurado no `.env`.
5. O hash do chunk deve considerar texto normalizado e versão do algoritmo.
6. A ordem dos chunks deve ser preservada.
7. Chunks vazios devem ser descartados.
8. Tabelas, headings e parágrafos devem ser preservados quando possível.
9. CSV/JSON devem gerar chunks com contexto de estrutura.

Configuração inicial recomendada:

```env
CHUNK_MAX_TOKENS=900
CHUNK_OVERLAP_TOKENS=120
CHUNK_MIN_TOKENS=40
```

## FR-040 — Embeddings

1. Embeddings devem ser gerados em lotes.
2. O modelo e a dimensão devem ser fixos por deployment.
3. A dimensão deve ser validada no startup.
4. Cada embedding deve registrar modelo e versão de configuração.
5. Falha transitória deve usar retry com backoff e jitter.
6. Falha definitiva deve falhar a etapa, não gravar vetor incompleto.
7. Chunks idênticos podem reutilizar embedding por hash.
8. Query embeddings não devem ser persistidos, salvo em observabilidade agregada.

## FR-050 — Cognificação

O pipeline padrão deve executar:

1. classificação do documento;
2. extração de chunks;
3. resumo por chunk;
4. extração de entidades e relações;
5. validação do JSON estruturado;
6. normalização de nomes e tipos;
7. resolução/deduplicação de entidades;
8. persistência dos artefatos no PostgreSQL;
9. geração de embeddings dos chunks, entidades e relações configuradas;
10. criação da projeção Neo4j;
11. resumo do documento;
12. ativação atômica da nova generation;
13. registro de métricas e custos estimados.

### Extração estruturada

A saída do LLM deve seguir schema Pydantic equivalente a:

```json
{
  "summary": "string",
  "entities": [
    {
      "local_id": "e1",
      "name": "string",
      "type": "string",
      "description": "string",
      "aliases": ["string"],
      "confidence": 0.0
    }
  ],
  "relations": [
    {
      "source_local_id": "e1",
      "target_local_id": "e2",
      "predicate": "string",
      "description": "string",
      "confidence": 0.0,
      "evidence": "string"
    }
  ]
}
```

Regras:

- saída fora do schema deve ser reparada no máximo uma vez;
- após segunda falha, a etapa deve falhar;
- IDs locais nunca são persistidos como IDs canônicos;
- entidade deve possuir evidência em ao menos um chunk;
- relação deve apontar para entidades válidas;
- confidence deve ficar entre `0` e `1`;
- prompts devem ser versionados em arquivos do repositório;
- conteúdo da fonte deve ser delimitado como dado não confiável;
- instruções presentes no documento não podem substituir o system prompt.

## FR-060 — Recall

### Modos suportados

- `chunks`: busca vetorial + lexical, retorna contexto bruto;
- `summaries`: busca em resumos;
- `rag`: contexto de chunks + resposta LLM;
- `graph`: sementes semânticas + vizinhança do grafo + resposta LLM;
- `hybrid`: fusão de chunks, resumos, entidades e grafo + resposta LLM;
- `triplets`: retorna relações relevantes sem resposta gerada.

### Modos explicitamente ausentes

- raw Cypher;
- agentic completion;
- code search;
- temporal engine especializado;
- feeling lucky baseado em roteador LLM na versão 1.

### Pipeline de recuperação híbrida

1. validar datasets;
2. gerar embedding da query;
3. recuperar top-N vetorial;
4. recuperar top-N lexical;
5. fundir com Reciprocal Rank Fusion;
6. extrair sementes de entidades;
7. consultar vizinhança no Neo4j;
8. buscar evidências no PostgreSQL;
9. aplicar filtros e deduplicação;
10. montar contexto dentro do orçamento de tokens;
11. gerar resposta quando o modo exigir;
12. anexar referências;
13. persistir query e metadados mínimos.

### Referências

Cada referência deve conter:

- `source_id`;
- `source_name`;
- `document_id`;
- `chunk_id`;
- `chunk_ordinal`;
- `quote` limitada;
- `start_char` e `end_char` quando disponíveis;
- score agregado;
- URL original quando a fonte for URL.

### Ausência de evidência

Quando não houver contexto suficiente, a resposta deve declarar ausência de evidência em vez de inventar conteúdo.

## FR-070 — Improve

`improve` será explícito e deverá suportar:

1. recalcular pesos a partir de feedback;
2. gerar embedding de relações ainda não indexadas;
3. detectar possíveis entidades duplicadas;
4. mesclar entidades somente acima de limiar configurado;
5. reconstruir resumos de documento/dataset;
6. remover relações sem evidência ativa;
7. recalcular centralidade e importância;
8. reconstruir projeções Neo4j inconsistentes;
9. registrar relatório de alterações.

Não fará:

- sync de sessão;
- importação automática de traces;
- skill improvement;
- truth subspace;
- auto-improve invisível;
- execução contínua por cron interno na v1.

## FR-080 — Feedback

O cliente poderá registrar feedback sobre uma resposta ou referência:

- score inteiro `-1`, `0` ou `1`;
- comentário opcional;
- query/result alvo;
- data/hora.

Feedback não altera memória imediatamente. Ele é aplicado em `improve`.

## FR-090 — Forget

A operação unificada deve suportar:

- apagar uma fonte e seus derivados;
- apagar apenas memória derivada de uma fonte;
- apagar dataset inteiro;
- apagar apenas memória derivada de dataset;
- apagar tudo.

### Regras de deleção

1. Deleção deve adquirir lock por dataset.
2. Dataset/fonte deve ser marcado como `deleting`.
3. Novas consultas não devem usar artefatos marcados.
4. Projeções Neo4j devem ser removidas.
5. Embeddings e chunks devem ser removidos.
6. Entidades/relações sem outra evidência devem ser removidas.
7. Arquivo original deve ser removido somente quando `memory_only=false`.
8. Falha parcial deve permanecer recuperável.
9. `everything=true` exige campo `confirm="DELETE EVERYTHING"`.
10. Todas as deleções devem produzir resumo de contagem.

## FR-100 — Runs e worker

1. Toda operação de escrita deve possuir `run_id`.
2. Runs devem ser persistidos antes da execução.
3. Estados: `queued`, `running`, `succeeded`, `failed`, `cancelling`, `cancelled`.
4. Etapas devem possuir estado e número da tentativa.
5. Worker deve processar fila do PostgreSQL.
6. Apenas um pipeline de escrita por dataset pode executar simultaneamente.
7. Reads podem executar durante writes, usando apenas generation ativa.
8. O worker deve usar `FOR UPDATE SKIP LOCKED`.
9. O processo deve enviar heartbeat do run.
10. Run sem heartbeat deve ser marcado `stale` no startup e reavaliado.
11. Retry deve ser idempotente por etapa.
12. Cancelamento deve ocorrer entre etapas, nunca no meio de transação crítica.

## FR-110 — Proveniência e grafo

O sistema deve permitir:

- obter schema de tipos e predicados;
- listar entidades relacionadas a uma fonte;
- obter evidências de uma relação;
- obter caminho entre duas entidades com limites;
- visualizar subgrafo em JSON;
- rastrear uma referência até o arquivo original.

Não haverá endpoint de Cypher arbitrário.

## FR-120 — Health e informações

### `/health/live`

Confirma que o processo está vivo. Não consulta dependências pesadas.

### `/health/ready`

Confirma:

- migrations aplicadas;
- PostgreSQL acessível;
- pgvector acessível;
- Neo4j acessível;
- worker iniciado;
- configuração válida.

### `/api/v1/info`

Retorna:

- nome e versão;
- commit/build;
- status dos componentes sem secrets;
- modelos configurados;
- dimensão de embedding;
- fingerprint de configuração.

---

## 11. Especificação da API

## 11.1 Convenções

Base path:

```text
/api/v1
```

Headers:

```http
X-API-Key: sf-...
Content-Type: application/json
X-Request-Id: opcional
Idempotency-Key: opcional em writes
```

Envelope de sucesso:

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid",
    "timestamp": "2026-08-05T23:00:00Z"
  }
}
```

Envelope de erro:

```json
{
  "error": {
    "code": "DATASET_NOT_FOUND",
    "message": "Dataset not found.",
    "details": {},
    "request_id": "uuid"
  }
}
```

Códigos HTTP:

- `200`: sucesso síncrono;
- `201`: recurso criado;
- `202`: pipeline aceito;
- `400`: request inválido;
- `401`: chave ausente;
- `403`: chave inválida;
- `404`: recurso inexistente;
- `409`: conflito/idempotência/estado;
- `413`: payload grande;
- `415`: tipo não suportado;
- `422`: validação semântica;
- `429`: limite de requisições;
- `500`: erro interno;
- `502`: falha no provedor externo;
- `503`: dependência indisponível.

## 11.2 Rotas

### Health

```http
GET /health/live
GET /health/ready
```

### Info

```http
GET /api/v1/info
```

### Datasets

```http
POST   /api/v1/datasets
GET    /api/v1/datasets
GET    /api/v1/datasets/{dataset_id}
PATCH  /api/v1/datasets/{dataset_id}
DELETE /api/v1/datasets/{dataset_id}
GET    /api/v1/datasets/{dataset_id}/sources
GET    /api/v1/datasets/{dataset_id}/stats
```

### Remember — texto

```http
POST /api/v1/remember
```

Request:

```json
{
  "dataset": "main",
  "content": "Sofias Memory mantém memória persistente.",
  "name": "nota-inicial",
  "metadata": {
    "origin": "laravel",
    "external_id": "note-123"
  },
  "session_id": "chat-42",
  "mode": "full",
  "wait": true,
  "force": false
}
```

`mode`:

- `ingest`: extrai/persiste, sem cognify;
- `full`: ingest + cognify.

Response concluído:

```json
{
  "data": {
    "run_id": "uuid",
    "status": "succeeded",
    "dataset_id": "uuid",
    "source_id": "uuid",
    "document_id": "uuid",
    "content_hash": "sha256",
    "chunks": 8,
    "entities": 17,
    "relations": 24,
    "deduplicated": false
  },
  "meta": {}
}
```

Response assíncrono:

```json
{
  "data": {
    "run_id": "uuid",
    "status": "queued"
  },
  "meta": {}
}
```

### Remember — arquivo

```http
POST /api/v1/remember/file
Content-Type: multipart/form-data
```

Campos:

- `file`: obrigatório;
- `dataset`: default `main`;
- `metadata`: JSON string opcional;
- `session_id`: opcional;
- `mode`: `ingest|full`;
- `wait`: boolean;
- `force`: boolean.

### Remember — URL

```http
POST /api/v1/remember/url
```

```json
{
  "dataset": "main",
  "url": "https://example.com/article",
  "metadata": {},
  "mode": "full",
  "wait": false
}
```

### Cognify

```http
POST /api/v1/cognify
```

```json
{
  "dataset": "main",
  "source_ids": ["uuid"],
  "rebuild": false,
  "wait": false
}
```

Regras:

- sem `source_ids`, processa fontes pendentes;
- `rebuild=true` cria nova generation;
- configurações do pipeline não podem ser substituídas pelo request.

### Recall

```http
POST /api/v1/recall
```

```json
{
  "query": "O que é o Sofias Memory?",
  "datasets": ["main"],
  "mode": "hybrid",
  "top_k": 12,
  "only_context": false,
  "include_references": true,
  "session_id": "chat-42",
  "filters": {
    "source_ids": [],
    "created_after": null,
    "created_before": null,
    "metadata": {}
  }
}
```

Response:

```json
{
  "data": {
    "query_id": "uuid",
    "mode": "hybrid",
    "answer": "...",
    "references": [
      {
        "source_id": "uuid",
        "source_name": "nota-inicial",
        "document_id": "uuid",
        "chunk_id": "uuid",
        "chunk_ordinal": 0,
        "quote": "Sofias Memory mantém memória persistente.",
        "score": 0.91,
        "url": null
      }
    ],
    "entities": [],
    "relations": [],
    "timings_ms": {
      "embedding": 80,
      "retrieval": 120,
      "graph": 40,
      "generation": 1800,
      "total": 2040
    }
  },
  "meta": {}
}
```

### Feedback

```http
POST /api/v1/feedback
```

```json
{
  "query_id": "uuid",
  "target_type": "answer",
  "target_id": "uuid",
  "score": 1,
  "comment": "Resposta correta e bem fundamentada."
}
```

### Improve

```http
POST /api/v1/improve
```

```json
{
  "dataset": "main",
  "stages": [
    "feedback_weights",
    "entity_deduplication",
    "relation_embeddings",
    "summaries",
    "graph_reconciliation"
  ],
  "wait": false
}
```

`stages` omitido executa o conjunto padrão definido no código/configuração.

### Forget

```http
POST /api/v1/forget
```

Fonte:

```json
{
  "dataset": "main",
  "source_id": "uuid",
  "memory_only": false,
  "wait": false
}
```

Dataset:

```json
{
  "dataset": "main",
  "memory_only": true,
  "wait": false
}
```

Tudo:

```json
{
  "everything": true,
  "confirm": "DELETE EVERYTHING",
  "wait": false
}
```

### Runs

```http
GET  /api/v1/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/retry
POST /api/v1/runs/{run_id}/cancel
```

### Grafo e proveniência

```http
GET /api/v1/graph/schema?dataset=main
GET /api/v1/graph/subgraph?dataset=main&entity_id=...&depth=2
GET /api/v1/graph/path?dataset=main&from=...&to=...&max_depth=4
GET /api/v1/provenance/source/{source_id}
GET /api/v1/provenance/relation/{relation_id}
GET /api/v1/provenance/query/{query_id}
```

## 11.3 Rotas proibidas

A aplicação não deve registrar rotas com os seguintes prefixes:

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

Um teste automatizado deve inspecionar o OpenAPI e falhar caso qualquer prefixo proibido apareça.

---

## 12. Modelo de dados PostgreSQL

PostgreSQL é a fonte de verdade. Neo4j é uma projeção reconstruível.

## 12.1 `datasets`

| Coluna | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `name` | CITEXT | unique, not null |
| `slug` | TEXT | unique, not null |
| `description` | TEXT | nullable |
| `status` | ENUM | active, deleting, deleted |
| `active_generation` | INTEGER | default 0 |
| `created_at` | TIMESTAMPTZ | not null |
| `updated_at` | TIMESTAMPTZ | not null |

Não existem `owner_id`, `tenant_id`, ACL ou configuration por dataset.

## 12.2 `sources`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `kind` | ENUM(text,file,url) |
| `name` | TEXT |
| `mime_type` | TEXT |
| `original_uri` | TEXT nullable |
| `storage_uri` | TEXT nullable |
| `content_sha256` | CHAR(64) |
| `normalized_sha256` | CHAR(64) nullable |
| `byte_size` | BIGINT |
| `metadata` | JSONB |
| `status` | ENUM |
| `version` | INTEGER |
| `created_at` | TIMESTAMPTZ |
| `updated_at` | TIMESTAMPTZ |

Índices/constraints:

- unique `(dataset_id, content_sha256, version)`;
- GIN em `metadata`;
- índice em `status`.

## 12.3 `documents`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `source_id` | UUID FK |
| `generation` | INTEGER |
| `title` | TEXT |
| `language` | VARCHAR(16) |
| `normalized_text` | TEXT |
| `text_sha256` | CHAR(64) |
| `token_count` | INTEGER |
| `metadata` | JSONB |
| `is_active` | BOOLEAN |
| `created_at` | TIMESTAMPTZ |

## 12.4 `chunks`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `document_id` | UUID FK |
| `source_id` | UUID FK |
| `generation` | INTEGER |
| `ordinal` | INTEGER |
| `text` | TEXT |
| `content_sha256` | CHAR(64) |
| `token_count` | INTEGER |
| `start_char` | INTEGER |
| `end_char` | INTEGER |
| `section_path` | TEXT[] |
| `metadata` | JSONB |
| `embedding` | VECTOR(3072) |
| `lexical` | TSVECTOR |
| `is_active` | BOOLEAN |
| `created_at` | TIMESTAMPTZ |

Índices:

- unique `(document_id, generation, ordinal)`;
- HNSW ou IVFFlat em `embedding`;
- GIN em `lexical`;
- índice `(dataset_id, is_active)`;
- índice `(source_id, is_active)`.

A dimensão `3072` é a baseline recomendada para `text-embedding-3-large`. Se outro modelo for escolhido antes da primeira release, a dimensão deve ser alterada em migration e congelada para a major version.

## 12.5 `entities`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `generation` | INTEGER |
| `canonical_key` | TEXT |
| `name` | TEXT |
| `entity_type` | TEXT |
| `description` | TEXT |
| `aliases` | TEXT[] |
| `properties` | JSONB |
| `confidence` | REAL |
| `importance_weight` | REAL |
| `embedding` | VECTOR(3072) nullable |
| `is_active` | BOOLEAN |
| `created_at` | TIMESTAMPTZ |
| `updated_at` | TIMESTAMPTZ |

Constraint recomendada:

- unique parcial `(dataset_id, canonical_key)` where `is_active=true`.

## 12.6 `entity_mentions`

| Coluna | Tipo |
|---|---|
| `entity_id` | UUID FK |
| `chunk_id` | UUID FK |
| `surface_text` | TEXT |
| `start_char` | INTEGER nullable |
| `end_char` | INTEGER nullable |
| `confidence` | REAL |

PK composta ou UUID próprio.

## 12.7 `relations`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `generation` | INTEGER |
| `source_entity_id` | UUID FK |
| `target_entity_id` | UUID FK |
| `predicate` | TEXT |
| `description` | TEXT |
| `properties` | JSONB |
| `confidence` | REAL |
| `importance_weight` | REAL |
| `embedding` | VECTOR(3072) nullable |
| `is_active` | BOOLEAN |
| `created_at` | TIMESTAMPTZ |
| `updated_at` | TIMESTAMPTZ |

## 12.8 `relation_evidence`

| Coluna | Tipo |
|---|---|
| `relation_id` | UUID FK |
| `chunk_id` | UUID FK |
| `quote` | TEXT |
| `confidence` | REAL |

Uma relação permanece ativa enquanto possuir ao menos uma evidência ativa.

## 12.9 `summaries`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `generation` | INTEGER |
| `target_type` | ENUM(document,entity,dataset,cluster) |
| `target_id` | UUID nullable |
| `level` | INTEGER |
| `text` | TEXT |
| `embedding` | VECTOR(3072) |
| `is_active` | BOOLEAN |
| `created_at` | TIMESTAMPTZ |

## 12.10 `memory_entries`

Tabela leve para conteúdo com `session_id`, sem cache separado.

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `dataset_id` | UUID FK |
| `source_id` | UUID FK nullable |
| `session_id` | TEXT nullable |
| `entry_type` | ENUM(text,qa,feedback,note) |
| `content` | TEXT |
| `metadata` | JSONB |
| `created_at` | TIMESTAMPTZ |

## 12.11 `queries`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `query_text` | TEXT |
| `dataset_ids` | UUID[] |
| `mode` | TEXT |
| `answer` | TEXT nullable |
| `references` | JSONB |
| `timings` | JSONB |
| `model` | TEXT nullable |
| `created_at` | TIMESTAMPTZ |

O armazenamento integral de query/answer poderá ser desabilitado por `STORE_QUERY_CONTENT=false`, mantendo apenas métricas e hashes.

## 12.12 `feedback`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `query_id` | UUID FK |
| `target_type` | TEXT |
| `target_id` | UUID nullable |
| `score` | SMALLINT check -1..1 |
| `comment` | TEXT nullable |
| `applied_at` | TIMESTAMPTZ nullable |
| `created_at` | TIMESTAMPTZ |

## 12.13 `pipeline_runs`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `pipeline_type` | ENUM |
| `dataset_id` | UUID nullable |
| `source_id` | UUID nullable |
| `status` | ENUM |
| `idempotency_key` | TEXT nullable |
| `payload_hash` | CHAR(64) |
| `input` | JSONB |
| `progress` | REAL |
| `current_step` | TEXT nullable |
| `attempt` | INTEGER |
| `worker_id` | TEXT nullable |
| `heartbeat_at` | TIMESTAMPTZ nullable |
| `config_fingerprint` | CHAR(64) |
| `error_code` | TEXT nullable |
| `error_message` | TEXT nullable |
| `metrics` | JSONB |
| `created_at` | TIMESTAMPTZ |
| `started_at` | TIMESTAMPTZ nullable |
| `finished_at` | TIMESTAMPTZ nullable |

## 12.14 `pipeline_steps`

| Coluna | Tipo |
|---|---|
| `id` | UUID PK |
| `run_id` | UUID FK |
| `name` | TEXT |
| `ordinal` | INTEGER |
| `status` | ENUM |
| `attempt` | INTEGER |
| `input_hash` | CHAR(64) nullable |
| `output` | JSONB |
| `metrics` | JSONB |
| `error` | JSONB nullable |
| `started_at` | TIMESTAMPTZ nullable |
| `finished_at` | TIMESTAMPTZ nullable |

## 12.15 `graph_outbox`

| Coluna | Tipo |
|---|---|
| `id` | BIGSERIAL PK |
| `dataset_id` | UUID |
| `aggregate_type` | TEXT |
| `aggregate_id` | UUID |
| `operation` | ENUM(upsert,delete) |
| `payload` | JSONB |
| `status` | ENUM(pending,processing,done,failed) |
| `attempt` | INTEGER |
| `created_at` | TIMESTAMPTZ |
| `processed_at` | TIMESTAMPTZ nullable |

A outbox evita considerar o Neo4j como fonte de verdade e permite reparar falhas sem transação distribuída.

---

## 13. Modelo de grafo Neo4j

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

### Constraints

- `Entity.id` unique;
- `Chunk.id` unique;
- índices em `dataset_id`;
- índice em `Entity.name`;
- índices compostos conforme suporte da versão escolhida.

### Regra de autoridade

- PostgreSQL é autoritativo.
- Neo4j pode ser descartado e reconstruído.
- Nenhuma informação existirá somente no Neo4j.
- Queries do Neo4j retornam IDs; conteúdo e evidências são hidratados do PostgreSQL.

---

## 14. Arquitetura de software

## 14.1 Estrutura recomendada

```text
sofias-memory/
├── AGENTS.md
├── README.md
├── pyproject.toml
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
└── scripts/
    ├── generate_api_key.py
    ├── rebuild_graph.py
    └── verify_installation.py
```

## 14.2 Dependências entre camadas

- `api` depende de `services` e schemas públicos;
- `services` orquestra domínio e pipelines;
- `domain` não depende de FastAPI, SQLAlchemy ou Neo4j;
- `pipelines` usa ports/interfaces de infraestrutura;
- `infrastructure` implementa ports;
- loaders não conhecem API;
- nenhuma camada importa módulos de usuário, permission, auth, settings ou sync.

## 14.3 Engine de pipeline

Interface conceitual:

```python
class PipelineStep(Protocol):
    name: str
    async def execute(self, context: PipelineContext) -> StepResult: ...
    async def compensate(self, context: PipelineContext, result: StepResult) -> None: ...
```

Requisitos:

- steps registrados no código;
- nenhum import dinâmico por string externa;
- input/output serializável;
- retries por step;
- progresso persistido;
- logs com `run_id`, `step`, `dataset_id`, `source_id`;
- cada step deve ser idempotente ou possuir chave de idempotência;
- geração nova permanece inativa até commit final;
- compensation é usada somente quando necessária; preferir artefatos versionados e cleanup posterior.

---

## 15. Pipelines

## 15.1 Remember/full

```text
validate_request
→ resolve_or_create_dataset
→ persist_source
→ extract_text
→ normalize_document
→ chunk_document
→ embed_chunks
→ summarize_chunks
→ extract_graph
→ resolve_entities
→ persist_graph_records
→ project_to_neo4j
→ summarize_document
→ activate_generation
→ finalize_run
```

## 15.2 Remember/ingest

```text
validate_request
→ resolve_or_create_dataset
→ persist_source
→ extract_text
→ normalize_document
→ finalize_run
```

## 15.3 Cognify pendentes

```text
select_pending_sources
→ para cada fonte: chunk + embeddings + graph + summaries
→ activate generations
→ update dataset generation
```

## 15.4 Recall

```text
validate_query
→ embed_query
→ vector_retrieve
→ lexical_retrieve
→ rank_fusion
→ graph_seed
→ graph_expand
→ hydrate_evidence
→ context_budgeting
→ optional_generate_answer
→ build_references
→ persist_query_metrics
```

## 15.5 Improve

```text
load_feedback
→ apply_weights
→ find_duplicate_candidates
→ validate_merges
→ merge_entities
→ embed_relations
→ rebuild_summaries
→ reconcile_graph_projection
→ cleanup_orphans
→ finalize_report
```

## 15.6 Forget

```text
validate_target
→ acquire_dataset_lock
→ mark_deleting
→ identify_affected_artifacts
→ delete_graph_projection
→ delete_or_deactivate_derived_records
→ cleanup_orphan_entities_relations
→ optionally_delete_raw_source
→ finalize_counts
```

---

## 16. LLM e structured output

## 16.1 Provider suportado

A versão 1 suporta uma API **OpenAI-compatible** via SDK `openai`.

Configuração:

```env
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-5-mini
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=3
```

Não haverá LiteLLM, Instructor, BAML ou SDKs específicos de Anthropic, Gemini, Mistral e Azure na versão 1.

Um endpoint local compatível com OpenAI poderá ser usado alterando o `.env`, desde que suporte os recursos exigidos.

## 16.2 Structured output nativo

- usar JSON Schema/Pydantic;
- validar toda saída;
- nenhuma entidade é persistida antes de validação;
- uma tentativa de repair é permitida;
- respostas inválidas devem ser armazenadas apenas em diagnóstico redigido, nunca em logs comuns;
- prompts e schemas devem possuir versão;
- `config_fingerprint` inclui versão de prompt, modelos e chunking.

## 16.3 Rate limit e retry

- limite de concorrência global de LLM;
- backoff exponencial com jitter;
- respeitar `Retry-After`;
- distinguir `429`, timeout, `5xx` e erro permanente;
- circuit breaker simples após sequência configurada de falhas;
- não repetir requisição não idempotente sem request ID quando o provider suportar.

## 16.4 Segurança contra prompt injection

- conteúdo é inserido em seção delimitada;
- system prompt declara que documentos são dados não confiáveis;
- nenhuma instrução do documento pode solicitar tool call;
- a aplicação não oferece tools ao LLM;
- URLs e arquivos não alteram configuração;
- respostas estruturadas são validadas e limitadas.

---

## 17. Embeddings

Baseline:

```env
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_DIMENSIONS=3072
EMBEDDING_BATCH_SIZE=64
EMBEDDING_TIMEOUT_SECONDS=60
```

Regras:

- `EMBEDDING_API_KEY` pode herdar `LLM_API_KEY`;
- a dimensão deve ser exatamente a da migration;
- mudança de modelo com dimensão diferente exige migration e reindexação;
- cache por `(model, dimensions, content_sha256)`;
- vetores são normalizados conforme estratégia definida;
- índices HNSW devem ser criados após volume mínimo ou de forma concorrente em produção;
- busca deve filtrar dataset e generation ativa.

---

## 18. Loaders nativos

## 18.1 Texto e Markdown

- decodificação UTF-8 com detecção controlada;
- preservar headings;
- remover NUL;
- normalizar line endings.

## 18.2 JSON

- limite de profundidade;
- serialização estável;
- preservar paths de chaves no contexto;
- arrays grandes devem ser quebrados por itens.

## 18.3 CSV

- detectar delimiter com limite;
- primeira linha como header quando válida;
- chunks devem repetir nomes de colunas;
- limitar número de colunas e tamanho de célula.

## 18.4 HTML e URL

- remover scripts, styles e elementos invisíveis;
- preservar title, headings, links e canonical URL;
- user-agent próprio;
- apenas HTTPS por padrão;
- bloquear loopback, link-local, RFC1918, metadata endpoints e redirecionamentos para redes privadas;
- limitar redirects;
- timeout e tamanho máximo;
- resolver DNS novamente antes da conexão para mitigar DNS rebinding.

## 18.5 PDF

- usar `pypdf`;
- registrar página por chunk;
- detectar PDF sem texto e retornar erro `OCR_REQUIRED`;
- limitar páginas e tamanho.

## 18.6 DOCX

- usar `python-docx`;
- preservar headings, parágrafos e tabelas;
- ignorar macros e objetos incorporados;
- limitar quantidade de elementos.

---

## 19. Configuração `.env`

Exemplo oficial:

```env
# Application
APP_NAME=Sofias Memory
APP_ENV=production
APP_VERSION=0.1.0
API_KEY=sf-change-me-with-at-least-32-random-characters
HTTP_HOST=0.0.0.0
HTTP_PORT=8000
LOG_LEVEL=INFO
CORS_ALLOWED_ORIGINS=
MAX_REQUEST_BODY_MB=50
REQUEST_WAIT_TIMEOUT_SECONDS=30

# PostgreSQL + pgvector
DATABASE_URL=postgresql+asyncpg://sofias_memory:change-me@postgres:5432/sofias_memory
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=10

# Neo4j
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j

# Local storage
DATA_DIRECTORY=/data/sources
TEMP_DIRECTORY=/data/tmp
MAX_SOURCE_SIZE_MB=50

# LLM
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-change-me
LLM_MODEL=gpt-5-mini
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=3
LLM_MAX_CONCURRENCY=4

# Embeddings
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_DIMENSIONS=3072
EMBEDDING_BATCH_SIZE=64
EMBEDDING_MAX_CONCURRENCY=4

# Chunking
CHUNK_MAX_TOKENS=900
CHUNK_OVERLAP_TOKENS=120
CHUNK_MIN_TOKENS=40

# Retrieval
RECALL_VECTOR_TOP_K=50
RECALL_LEXICAL_TOP_K=50
RECALL_GRAPH_SEED_TOP_K=10
RECALL_GRAPH_DEPTH=2
RECALL_GRAPH_MAX_NODES=100
RECALL_DEFAULT_TOP_K=12
RECALL_MAX_TOP_K=100
RECALL_RRF_K=60

# Worker
WORKER_ENABLED=true
WORKER_POLL_INTERVAL_MS=500
WORKER_STALE_AFTER_SECONDS=300
WORKER_MAX_CONCURRENT_DATASETS=1
WORKER_MAX_CONCURRENT_READS=8

# Privacy
STORE_QUERY_CONTENT=true
LOG_DOCUMENT_CONTENT=false
LOG_LLM_PAYLOADS=false
```

### Regras de configuração

- secrets sem valor impedem startup;
- valores inválidos impedem startup;
- `CORS_ALLOWED_ORIGINS` vazio significa CORS desabilitado;
- nenhuma configuração é retornada integralmente por API;
- `/info` retorna somente valores não secretos e fingerprints;
- `.env` nunca entra no Git;
- configuração de DB e Neo4j é obrigatória;
- a aplicação não cria providers alternativos.

---

## 20. Dependências Python propostas

Versão Python:

```toml
requires-python = ">=3.12,<3.13"
```

Dependências runtime, todas obrigatórias:

```toml
dependencies = [
  "fastapi>=0.116,<1",
  "uvicorn[standard]>=0.35,<1",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "sqlalchemy[asyncio]>=2.0.40,<3",
  "asyncpg>=0.30,<1",
  "alembic>=1.16,<2",
  "pgvector>=0.4,<1",
  "neo4j>=5.28,<6",
  "openai>=1.100,<2",
  "httpx>=0.28,<1",
  "tenacity>=9,<10",
  "structlog>=25,<26",
  "orjson>=3.10,<4",
  "python-multipart>=0.0.20,<1",
  "aiofiles>=24,<25",
  "filetype>=1.2,<2",
  "tiktoken>=0.9,<1",
  "pypdf>=6,<7",
  "python-docx>=1.1,<2",
  "beautifulsoup4>=4.13,<5",
  "lxml>=5,<7",
  "charset-normalizer>=3.4,<4",
  "prometheus-client>=0.22,<1"
]
```

Dependências de desenvolvimento:

```toml
[dependency-groups]
dev = [
  "pytest>=8,<9",
  "pytest-asyncio>=0.24,<1",
  "pytest-cov>=6,<7",
  "httpx>=0.28,<1",
  "testcontainers[postgres,neo4j]>=4.10,<5",
  "ruff>=0.12,<1",
  "mypy>=1.17,<2",
  "pip-audit>=2.9,<3",
  "bandit>=1.8,<2"
]
```

Não deve existir `[project.optional-dependencies]` na versão 1.

As versões finais deverão ser congeladas por lockfile e atualizadas por PRs automatizados com testes.

---

## 21. Deployment

## 21.1 Docker Compose obrigatório

O projeto deverá possuir três definições de Docker Compose:

- `compose.yaml`: definição canônica e portável da stack;
- `compose.easypanel.yaml`: definição destinada ao deployment pelo EasyPanel;
- `compose.portainer.yaml`: definição destinada ao deployment pelo Portainer.

Os três arquivos deverão representar a mesma arquitetura funcional e utilizar as mesmas imagens, serviços, volumes, variáveis de ambiente e regras fundamentais de persistência.

O `compose.yaml` será a referência canônica da infraestrutura e deverá poder ser executado diretamente com Docker Compose, independentemente de EasyPanel ou Portainer.

Todos os deployments deverão conter obrigatoriamente os serviços:

```text
sofias-memory
postgres
neo4j
```

Sem profiles opcionais.

### Rede

- `sofias-memory` expõe `8000`;
- PostgreSQL e Neo4j não publicam portas no host em produção;
- comunicação ocorre por rede interna;
- reverse proxy fornece TLS.

### Volumes

- `sofias_memory_postgres_data`;
- `sofias_memory_neo4j_data`;
- `sofias_memory_sources`.

### Startup

1. PostgreSQL healthy;
2. Neo4j healthy;
3. app inicia;
4. migrations PostgreSQL;
5. constraints Neo4j;
6. verificação de configuração;
7. worker;
8. readiness true.

### Shutdown

- parar aceitação de writes;
- concluir ou pausar etapa atual;
- atualizar heartbeat/status;
- fechar conexões;
- encerrar worker;
- finalizar API.

## 21.2 Réplicas

Versão 1 suporta exatamente uma réplica da aplicação. O worker interno e o objetivo single-user tornam múltiplas réplicas desnecessárias.

A fila usa locking no PostgreSQL para não impedir evolução futura, mas multi-réplica não faz parte do suporte oficial.

## 21.3 EasyPanel

O arquivo `compose.easypanel.yaml` será destinado ao deployment do Sofias Memory pelo EasyPanel.

A stack deve:

- utilizar as mesmas imagens e versões definidas pelo `compose.yaml`;
- usar imagens com tag imutável;
- expor somente a aplicação;
- definir health checks;
- usar volumes persistentes;
- não colocar secrets no compose versionado;
- permitir domínio e TLS pelo proxy do EasyPanel;
- manter PostgreSQL e Neo4j acessíveis somente pela rede interna;
- executar backup separado de PostgreSQL, Neo4j e sources.

O EasyPanel não deverá introduzir dependências funcionais no Sofias Memory. A aplicação deverá continuar podendo ser executada sem EasyPanel utilizando o `compose.yaml` canônico.

## 21.4 Portainer

O arquivo `compose.portainer.yaml` será destinado ao deployment do Sofias Memory como Stack no Portainer.

A stack deve:

- utilizar as mesmas imagens e versões definidas pelo `compose.yaml`;
- usar imagens com tag imutável;
- expor somente a aplicação;
- definir health checks;
- usar volumes persistentes;
- não colocar secrets no compose versionado;
- manter PostgreSQL e Neo4j acessíveis somente pela rede interna;
- permitir integração com reverse proxies externos, como Traefik ou Nginx Proxy Manager, sem tornar nenhum deles dependência obrigatória;
- permitir deployment diretamente pelo editor de Stack ou por repositório Git;
- executar backup separado de PostgreSQL, Neo4j e sources.

O Portainer não deverá introduzir dependências funcionais no Sofias Memory. A aplicação deverá continuar podendo ser executada sem Portainer utilizando o `compose.yaml` canônico.

## 22. Segurança

## 22.1 Controles obrigatórios

- TLS no proxy;
- API key com alta entropia;
- comparação constant-time;
- rate limit em memória por IP e rota;
- request size limit;
- timeouts;
- CORS desabilitado por padrão;
- proteção SSRF;
- MIME sniffing;
- validação de nomes e metadados;
- queries parametrizadas;
- nenhuma rota de SQL/Cypher arbitrário;
- secrets redigidos;
- sem logs de documentos;
- containers non-root;
- filesystem read-only, exceto diretórios de dados/tmp;
- capabilities Linux removidas;
- dependências auditadas;
- SBOM e scan de imagem;
- banco sem porta pública;
- endpoint `/docs` configurável por ambiente.

## 22.2 Limitação da chave única

Uma API key estática é adequada para uma instância single-user em rede controlada. Quando exposta à internet, deve ser combinada com:

- TLS;
- allowlist de IP quando possível;
- reverse proxy;
- rate limit;
- rotação periódica;
- logs de request sem secrets.

A chave única não deve ser vendida como equivalente a autenticação multiusuário. Ela é deliberadamente simples.

## 22.3 Conteúdo malicioso

- arquivos executáveis são rejeitados;
- ZIP não é aceito;
- XML external entities desabilitadas;
- HTML scripts não são executados;
- DOCX é lido como pacote de dados, sem macros;
- PDFs não executam JavaScript;
- URLs privadas são bloqueadas;
- texto do documento não controla tools ou configuração.

---

## 23. Observabilidade

### Logs

JSON estruturado com:

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

Não incluir:

- API keys;
- LLM keys;
- passwords;
- conteúdo completo;
- embeddings;
- payload bruto do LLM por padrão.

### Métricas

- requests por rota/status;
- latência;
- runs por estado;
- duração de steps;
- tokens de LLM;
- quantidade de embeddings;
- filas pendentes;
- heartbeat atrasado;
- erros por provider;
- divergências de projeção;
- chunks/entidades/relações por dataset.

### Tracing

Tracing distribuído não é obrigatório na v1. `request_id` e `run_id` devem permitir rastreio suficiente dentro do monólito.

---

## 24. Requisitos não funcionais

## NFR-001 — Disponibilidade

- health live deve responder em p95 menor que 100 ms;
- readiness deve responder em p95 menor que 500 ms em condições normais;
- falha do LLM não deve derrubar health live;
- falha do Neo4j deixa readiness false e bloqueia pipelines de grafo;
- modo `chunks` poderá ser configurado para continuar disponível sem geração LLM, mas não é uma stack alternativa.

## NFR-002 — Performance

Baseline para uma instância com 4 vCPU, 16 GB RAM, bancos em SSD e provedor externo saudável:

- listagem de dataset p95 < 300 ms;
- recall `chunks` em 100 mil chunks p95 < 1,5 s;
- recall `triplets` p95 < 2 s;
- recuperação de contexto `hybrid` antes do LLM p95 < 2,5 s;
- latência total RAG/graph depende do provider e deve ser medida separadamente;
- aceitar ao menos 8 recalls concorrentes;
- um pipeline de escrita por dataset;
- ingestão de arquivo de 10 MB sem manter o arquivo inteiro duplicado em memória.

## NFR-003 — Capacidade inicial

- 100 datasets;
- 100 mil fontes;
- 1 milhão de chunks;
- 2 milhões de entidades;
- 10 milhões de relações/evidências;
- 50 GB de sources por instância.

Esses são objetivos de engenharia, não promessa sem benchmark. Testes de carga devem validar ou ajustar os números.

## NFR-004 — Confiabilidade

- escrita idempotente;
- geração ativa atômica;
- retries controlados;
- stale run recovery;
- Neo4j reconstruível;
- backups testados;
- migrations com rollback documentado;
- nenhuma exclusão silenciosamente parcial.

## NFR-005 — Privacidade

- sem telemetria externa;
- conteúdo enviado apenas ao provider configurado;
- possibilidade de desabilitar storage de query/answer;
- logs sem conteúdo;
- deleção remove dados e derivados.

## NFR-006 — Manutenibilidade

- cobertura mínima de 80% no domínio/pipelines;
- 100% dos contratos públicos testados;
- typing obrigatório nas APIs internas;
- lint e format obrigatórios;
- imports proibidos testados;
- módulos menores e responsabilidade única;
- ADR para decisões arquiteturais.

---

## 25. Testes

## 25.1 Unitários

- normalização;
- hashing;
- chunking;
- schemas;
- canonicalização de entidade;
- rank fusion;
- context budgeting;
- API key middleware;
- validações de forget;
- idempotência por step.

## 25.2 Integração

Com PostgreSQL+pgvector e Neo4j reais via Testcontainers:

- migrations;
- índices vetoriais;
- CRUD de datasets;
- ingestão por formato;
- pipeline completo;
- outbox;
- projeção Neo4j;
- busca híbrida;
- deleção e orphan cleanup;
- recovery após falha.

## 25.3 Contrato/OpenAPI

- snapshots do OpenAPI;
- rotas proibidas ausentes;
- exemplos válidos;
- códigos de erro estáveis;
- headers obrigatórios;
- backward compatibility dentro da major version.

## 25.4 E2E

Cenários mínimos:

1. remember texto → recall graph → referência correta;
2. upload PDF → chunks com páginas → recall;
3. envio duplicado → deduplicated true;
4. crash após PostgreSQL e antes do Neo4j → outbox recupera;
5. forget source → evidências removidas e entidade compartilhada preservada;
6. memory-only → source preservada e nova cognify possível;
7. API key inválida → nenhum acesso;
8. URL para `127.0.0.1` → bloqueada;
9. LLM retorna JSON inválido → repair/retry e erro controlado;
10. restart durante run → recovery.

## 25.5 Segurança

- path traversal;
- SSRF;
- DNS rebinding;
- oversized payload;
- MIME spoofing;
- malformed PDF/DOCX;
- JSON profundo;
- CSV gigante;
- prompt injection fixture;
- secret redaction;
- brute force/rate limit.

## 25.6 Performance

- 10 mil, 100 mil e 1 milhão de chunks;
- latência vetorial;
- latência lexical;
- fusão híbrida;
- graph expansion por depth;
- ingestão de lotes;
- concorrência de recall;
- tempo de delete/rebuild;
- crescimento dos índices.

---

## 26. Critérios de aceite do MVP

O MVP só é considerado pronto quando:

1. `docker compose up` utilizando o `compose.yaml` canônico inicia os três serviços obrigatórios.
2. `compose.easypanel.yaml` e `compose.portainer.yaml` devem passar em `docker compose config` e implementar a mesma arquitetura funcional do Compose canônico.
3. Apenas a porta da aplicação está publicada.
4. API sem `API_KEY` não inicia.
5. Rotas privadas validam `X-API-Key`.
6. OpenAPI não contém rotas proibidas.
7. Banco não contém tabelas `users`, `roles`, `permissions`, `acl`, `api_keys`, `settings` ou `tenants`.
8. Nenhuma tabela de domínio contém `owner_id` ou `tenant_id`.
9. Text, TXT, MD, JSON, CSV, HTML, PDF textual e DOCX passam em E2E.
10. Remember/full gera chunks, embeddings, entidades, relações e resumos.
11. Recall `chunks`, `rag`, `graph`, `hybrid` e `triplets` funciona.
12. Respostas possuem referências.
13. Improve aplica feedback e reconcilia projeção.
14. Forget source/dataset/memory-only/everything funciona.
15. Reenvio idempotente não duplica dados.
16. Restart recupera run interrompido.
17. Neo4j pode ser reconstruído a partir do PostgreSQL.
18. Nenhum evento de telemetria externa é emitido.
19. Testes de integração passam em CI.
20. Imagem não roda como root.
21. LICENSE e atribuições estão presentes.

---

## 27. Epics de implementação

## Epic 0 — Baseline, licença e ADRs

Entregas:

- congelar commit upstream usado como referência;
- inventário de arquivos/conceitos reaproveitados;
- LICENSE Apache-2.0;
- NOTICE/attribution;
- ADR-001 modular monolith;
- ADR-002 PostgreSQL source of truth + Neo4j projection;
- ADR-003 single API key;
- ADR-004 OpenAI-compatible only;
- ADR-005 no optional dependencies.

## Epic 1 — Fundação

- projeto Python;
- config startup;
- logging;
- request ID;
- API key middleware;
- health/info;
- Dockerfile;
- `compose.yaml` canônico;
- `compose.easypanel.yaml`;
- `compose.portainer.yaml`;

## Epic 2 — Persistência

- models;
- migrations;
- pgvector;
- repositories;
- Neo4j client;
- constraints;
- graph outbox;
- dataset locks.

## Epic 3 — Runs e pipeline engine

- fila PostgreSQL;
- worker interno;
- run/step models;
- retries;
- heartbeats;
- cancellation;
- stale recovery;
- idempotency.

## Epic 4 — Ingestão

- storage local;
- loaders;
- hashing;
- MIME;
- dedup;
- SSRF-safe URL downloader;
- normalized document.

## Epic 5 — Cognify

- tokenizer/chunker;
- embeddings;
- prompts;
- structured graph extraction;
- entity resolution;
- persistence;
- Neo4j projection;
- summaries;
- generations.

## Epic 6 — Recall

- vector retrieval;
- lexical retrieval;
- RRF;
- graph traversal;
- evidence hydration;
- context budget;
- answer generation;
- references.

## Epic 7 — Improve e feedback

- feedback API;
- weights;
- duplicate candidates;
- merge;
- relation embeddings;
- summary rebuild;
- reconciliation report.

## Epic 8 — Forget

- source;
- memory-only;
- dataset;
- everything;
- orphan cleanup;
- recovery de delete parcial.

## Epic 9 — Hardening e release

- security suite;
- load tests;
- backups;
- restore drill;
- SBOM;
- dependency audit;
- docs;
- examples Laravel/n8n/Python;
- release 1.0.0.

---

## 28. Migração a partir do Cognee

Não é recomendado copiar diretamente bancos do Cognee para o Sofias Memory porque:

- modelos de dataset possuem ownership/tenant/ACL;
- providers e schemas podem divergir;
- o Sofias Memory usa generations e outbox próprias;
- as projeções de grafo não são a fonte de verdade;
- relações de usuário e sessão serão removidas.

Estratégia recomendada:

1. exportar fontes originais e metadados úteis;
2. mapear datasets por nome;
3. ignorar users, ACL, api keys, settings e tenants;
4. importar sources;
5. executar cognify novamente;
6. comparar amostras de recall;
7. manter Cognee read-only durante período de validação;
8. desligar após backup final.

Uma ferramenta de migração poderá aceitar manifest JSONL:

```json
{"dataset":"docs","source_path":"/export/a.pdf","metadata":{}}
```

Importar diretamente nodes/edges é uma otimização posterior e não deve bloquear o MVP.

---

## 29. Licença e propriedade intelectual

O baseline do Cognee está sob Apache License 2.0. A implementação pode reutilizar e modificar código conforme a licença, mas o rename para Sofias Memory não remove obrigações.

Requisitos:

- incluir cópia da Apache License 2.0;
- preservar notices e copyrights aplicáveis nos arquivos derivados;
- documentar modificações significativas;
- adicionar atribuição ao projeto Cognee na documentação técnica;
- revisar dependências e suas licenças;
- não reutilizar marcas/logotipos do Cognee como identidade do produto;
- manter inventário de componentes de terceiros;
- gerar SBOM.

Uma abordagem clean-room não é juridicamente obrigatória pela Apache-2.0, mas uma reconstrução arquitetural limpa é tecnicamente recomendada. Este documento não substitui revisão jurídica.

---

## 30. Riscos e mitigação

### Risco 1 — Tentar obter paridade total

**Problema:** O Cognee atual contém recursos além de memória essencial.  
**Mitigação:** matriz explícita de escopo e rotas proibidas.

### Risco 2 — Dupla persistência PostgreSQL/Neo4j

**Problema:** não existe transação distribuída simples.  
**Mitigação:** PostgreSQL autoritativo, graph outbox, operations idempotentes e rebuild.

### Risco 3 — Entity resolution imperfeito

**Problema:** merges incorretos destroem qualidade.  
**Mitigação:** canonical key conservadora, limiar alto, evidências, merge auditável e improve explícito.

### Risco 4 — Custo e latência do LLM

**Problema:** cognify domina custo.  
**Mitigação:** cache por hash, batch, métricas de tokens, dedup e prompts compactos.

### Risco 5 — “Somente API_KEY” mal interpretado

**Problema:** provider externo também exige segredo.  
**Mitigação:** documentação distingue access key da aplicação e credenciais de infraestrutura.

### Risco 6 — Worker dentro do monólito

**Problema:** crash interrompe run.  
**Mitigação:** estado persistido, heartbeat, idempotência e stale recovery.

### Risco 7 — URL ingestion/SSRF

**Problema:** serviço poderia acessar rede interna.  
**Mitigação:** bloqueio de IPs privados, validação de redirects, DNS pinning/recheck e HTTPS only.

### Risco 8 — Deleção incompleta

**Problema:** artefatos podem sobreviver em vetor/grafo/storage.  
**Mitigação:** workflow persistido, estados deleting, contagens, retry e testes de órfãos.

### Risco 9 — Modelo de embedding alterado

**Problema:** dimensões incompatíveis.  
**Mitigação:** dimensão congelada por major version e reindexação explícita.

### Risco 10 — Fork mutilado

**Problema:** imports e comportamentos residuais de auth/settings/sync.  
**Mitigação:** novo package `sofias_memory`, allowlist de módulos e testes de imports/rotas proibidos.

---

## 31. Decisões que não devem ser reabertas durante o MVP

1. Não haverá usuários.
2. Não haverá multitenancy.
3. Não haverá ACL.
4. Não haverá gerenciamento de API keys.
5. Não haverá settings por rota.
6. Não haverá Sync.
7. Não haverá providers opcionais.
8. PostgreSQL+pgvector e Neo4j são obrigatórios.
9. OpenAI-compatible é o único protocolo de modelo.
10. PostgreSQL é a fonte de verdade.
11. Neo4j é reconstruível.
12. Apenas uma réplica da aplicação é suportada.
13. Improve é explícito.
14. Sem telemetria externa.
15. Sem frontend e MCP no repositório principal.

---

## 32. Pontos de extensão futuros, sem plugin system

A arquitetura pode permitir futuras features por código versionado, sem criar um mercado de adapters:

- loader PPTX/XLSX;
- OCR;
- áudio;
- local OpenAI-compatible model;
- export/import;
- MCP em repositório separado;
- SDK PHP;
- SDK TypeScript;
- temporal graph;
- code graph;
- Postgres-only graph experimental.

Essas evoluções devem ser features explícitas de release, não optional dependencies carregadas dinamicamente.

---

## 33. Definition of Done por feature

Uma feature está pronta somente quando:

- implementação concluída;
- typing e lint passam;
- unit tests;
- integration tests reais;
- contrato OpenAPI atualizado;
- erros documentados;
- logs e métricas definidos;
- migration criada quando aplicável;
- segurança revisada;
- cenário de rollback/recovery testado;
- documentação e exemplo de uso entregues;
- nenhuma rota/módulo proibido introduzido.

---

## 34. Recomendação final

A implementação deve nascer como projeto novo, `sofias-memory`, usando o Cognee como referência funcional e, onde vantajoso, como fonte de código sob Apache-2.0. Não deve nascer dentro do package `cognee` com dezenas de módulos desativados.

O melhor corte inicial é:

- datasets globais;
- remember texto/arquivo/URL;
- cognify padrão;
- recall chunks/rag/graph/hybrid/triplets;
- feedback;
- improve explícito;
- forget completo;
- run tracking;
- proveniência;
- PostgreSQL+pgvector+Neo4j;
- API key fixa;
- `.env` imutável;
- Docker Compose obrigatório.

Esse produto terá menos features que o Cognee, mas será mais correto para o objetivo real: uma memória self-hosted, single-user, integrável e operacionalmente previsível. Remover complexidade não é perder capacidade; neste caso, é finalmente escolher um produto.

---

## 35. Referências técnicas auditadas

Baseline consultada no repositório `topoteretes/cognee`:

- `pyproject.toml` — versão, dependências e extras;
- `.env.template` — providers e configurações atuais;
- `cognee/__init__.py` — superfícies V1/V2 e imports globais;
- `cognee/api/client.py` — routers e autenticação atuais;
- `cognee/api/v1/add/add.py` — pipeline de ingestão e acoplamentos;
- `cognee/api/v1/cognify/cognify.py` — pipeline padrão de cognificação;
- `cognee/api/v1/search/search.py` — tipos e fluxo de busca;
- `cognee/modules/memify/memify.py` — enriquecimento;
- `cognee/api/v1/improve/improve.py` — improve e integração com sessões;
- `cognee/api/v1/forget/forget.py` — deleção unificada;
- `cognee/modules/data/models/Dataset.py` — owner, tenant e ACL;
- `cognee/modules/data/models/Data.py` — ownership e pipeline status;
- `LICENSE` — Apache License 2.0.

