# Sofias Memory — Feature Contract v0.4.0: Skills

**Status:** Implemented\
**Target release:** v0.4.0\
**Feature:** First-class durable procedural Skills

---

## 1. Objetivo

O v0.4.0 introduz **Skills first-class e duráveis** no Sofias Memory.

Uma Skill representa **conhecimento procedural reutilizável sobre como executar uma tarefa**.

Skill não é:

- runtime de agente;
- tool executor;
- authorization principal;
- Dataset;
- Session;
- semantic knowledge Document;
- PipelineRun;
- plugin system.

Sofias Memory armazena, versiona, busca, resolve e devolve Skills. O caller — especialmente Sofia's Assistant — decide quando selecionar uma Skill, interpreta suas instruções e executa quaisquer tools necessárias.

PostgreSQL permanece como fonte de verdade.

---

## 2. Princípios

### 2.1 Skill é procedural memory, não semantic memory

```text
Skill
    ↓
conhecimento sobre "como fazer"

Remember/Cognify
    ↓
conhecimento semântico extraído de conteúdo
```

Skills nunca passam por Remember/Cognify, nunca geram Source, nunca geram graph_outbox.

### 2.2 Skill é global, não Dataset/Session/Agent-owned

Skill não pertence a um Dataset, a uma Session, nem (no v0.4.0) a um Agent.

Resolução de Skill não altera Recall, não altera Session Context RAG, não cria SessionEntry, não cria Query.

### 2.3 Identidade e revisão são conceitos separados

`Skill` é identidade lógica durável e imutável (`name`). Conteúdo procedural vive em `SkillRevision`, imutável e append-only. `Skill.current_revision_id` é um ponteiro explícito, nunca conteúdo duplicado.

### 2.4 Progressive disclosure é obrigatório

Discovery/list/resolve devolvem apenas metadata de seleção. O procedure completo só aparece em leitura explícita de detail/revision.

### 2.5 Explicit is better than automatic

Não existem no v0.4.0:

- seleção automática de Skill dentro de Recall;
- invocação automática de Skill;
- criação automática de Skill a partir de execuções bem-sucedidas;
- auto-melhoria ou auto-edição de Skill;
- promoção automática de Session/Query para Skill;
- SkillRun ou qualquer rastreamento de execução.

### 2.6 Compatibilidade é obrigatória

`/skills` deixa de ser um prefixo proibido. `/agents` e `/proposals` continuam proibidos. Nenhuma outra superfície do contrato existente muda de comportamento.

---

# 3. Identidade da Skill

Cada Skill possui exatamente duas identidades, nunca três:

- `id`: UUID PRIMARY KEY interno, gerado pelo sistema. Exposto publicamente sob o nome de campo `skill_uuid` — não existe uma coluna `skill_uuid` separada duplicando `id`. Mesmo padrão já usado por `Session` (ADR-0012): `Session.id` é serializado publicamente como `session_uuid`, sem coluna duplicada.
- `name`: identificador lógico portable, externo, imutável.

## 3.1 Formato de `name`

`name` segue o subset portable do Agent Skills format:

- 1 a 64 caracteres;
- somente `a-z`, `0-9` e hyphen;
- não inicia nem termina com hyphen;
- não contém `--`.

`name` é:

- globally unique dentro de uma instância Sofias Memory;
- case-sensitive por contrato, embora uppercase seja inválido (o subset portable é lowercase-only, então maiúsculas são rejeitadas na validação, não normalizadas silenciosamente);
- imutável após criação;
- independente de path de filesystem.

Renomear uma Skill não é uma operação suportada — significa criar outra identidade (`POST /api/v1/skills` com um `name` diferente). Não existe endpoint de rename.

## 3.2 `skill_uuid` (= `id`) vs `name`

```text
skill_uuid  → serialização pública de Skill.id; identidade estrutural, usada em toda
              a API de management/revision (path parameters)
name        → identidade lógica portable, usada em resolve/import/export e em SKILL.md
```

Ambos identificam a mesma Skill; nenhum dos dois é derivado do outro. `skill_uuid` não é uma coluna adicional — é `id` sob outro nome de campo na camada pública.

## 3.3 Identidade de SkillRevision na URL

Uma `SkillRevision` também possui um `id` UUID interno (chave primária, útil para FKs e para joins internos), mas **nunca aparece na URL pública**. A rota de leitura/export de uma revisão específica é sempre endereçada por `{skill_uuid}/revisions/{revision}` — o inteiro monotônico da revisão, nunca o UUID interno da linha `SkillRevision`.

---

# 4. Modelo conceitual

## 4.1 Skill

Campos conceituais:

```text
id                    UUID PK, exposto publicamente como skill_uuid (não há coluna
                       skill_uuid separada)
name                  (public portable identity, unique, immutable)
status                (active | archived)
current_revision_id   FK para SkillRevision.id; nunca NULL em uma Skill committed (§4.4)
created_at
updated_at
archived_at
```

`name` é validado e persistido com a mesma disciplina de normalização única e explícita usada para `Session.session_id` (ADR-0012) — uma única função de normalização/validação compartilhada entre create e import.

## 4.2 SkillRevision

Campos conceituais mínimos:

```text
id                    UUID PK interno — nunca aparece em URL pública (§3.3)
skill_id              FK para Skill.id
revision              (monotonic integer, 1-based, per Skill)
description
procedure             (Markdown body)
license
compatibility
metadata              map<string, string> — subset portable do Agent Skills format
                       (ver §12.6 para a extensão namespaced usada por tags)
tags                  Sofias Memory extension, não portable top-level (ver §12.6)
declared_tools        list[string] (ver §12.2)
content_sha256        hash do conteúdo semântico canônico (§12.7), não dos bytes
                       originais importados
resolution_embedding  (pgvector, indexes name+description+tags — never procedure)
created_at
```

Uma SkillRevision, uma vez criada, é imutável. Nenhum campo de conteúdo (`description`, `procedure`, `license`, `compatibility`, `metadata`, `tags`, `declared_tools`) pode ser alterado in-place. Qualquer mudança semântica nesses campos cria uma nova SkillRevision, exceto quando o conteúdo é semanticamente idêntico a uma revisão já existente da mesma Skill — nesse caso a operação é um safe replay (§9.1), não uma nova revisão.

A revisão deve preservar informação suficiente para reconstruir/exportar um `SKILL.md` standalone semanticamente válido a partir do conteúdo persistido (ver §12). Export é determinístico *sobre o conteúdo persistido*, não uma promessa de bytes originais preservados (§12.7).

## 4.3 Revision numbering

Dentro de uma Skill, revisões são numeradas `1, 2, 3, ...` monotonicamente, nunca por timestamp.

Concorrência de criação de revisões para a mesma Skill deve produzir ordinais únicos e coerentes — mesma disciplina de `PipelineRun.attempt` sob concorrência, e serializada pelo lock por-Skill descrito em §6.

Um possível `metadata.version` vindo de um `SKILL.md` importado é metadata externa preservada como dado (dentro de `metadata`), e nunca substitui o `revision` integer interno.

## 4.4 Invariante da primeira revisão

Uma Skill publicamente observável nunca existe sem conteúdo procedural.

`POST /api/v1/skills` cria `Skill` + `SkillRevision revision=1` + `current_revision_id → revision 1` na mesma transação atômica. O payload estruturado mínimo é, portanto, `name` + `description` + `procedure` (mais os campos opcionais de §8).

Se a chamada ao embedding provider (necessária para `resolution_embedding`, §7) falhar antes da persistência, o resultado observável é:

```text
Skill rows criadas     = 0
SkillRevision rows criadas = 0
```

Nenhuma Skill incompleta (sem revisão, ou com `current_revision_id` nulo) é jamais observável por um `GET`. É aceitável que, internamente, o schema torne `current_revision_id` tecnicamente nullable para quebrar o ciclo de FK (`Skill` referencia `SkillRevision`, `SkillRevision` referencia `Skill`) durante a construção da transação — desde que nenhuma linha de `Skill` **committed** jamais tenha `current_revision_id = NULL`, e isso seja coberto por teste de invariante (SM-701, ver Backlog).

---

# 5. Lifecycle

Estados possíveis de `Skill.status`:

```text
active
archived
```

## 5.1 Archive

```text
active → archived
```

Efeitos:

- remove a Skill de resolução semântica (`POST /skills/resolve` nunca retorna uma Skill archived);
- preserva a Skill;
- preserva toda SkillRevision existente;
- preserva leitura de management (`GET /skills/{skill_uuid}`, `GET .../revisions`) — uma Skill archived continua legível por id/name, apenas não é descoberta por resolve;
- não impede criação de nova SkillRevision nem mudança de `current_revision_id` via `PATCH` — archive é um filtro de descoberta, não um admission barrier sobre escrita (diferença deliberada em relação a Session archive, ver §17).

Archivar uma Skill já archived é idempotente (200, sem mudança de estado).

## 5.2 Restore

```text
archived → active
```

Restaura elegibilidade para resolução semântica. Restaurar uma Skill já active é idempotente.

## 5.3 Sem hard delete público

Não existe endpoint de delete físico de Skill no v0.4.0. Uma Skill indesejada é archived, nunca apagada.

---

# 6. Concurrency

## 6.1 Criação de Skill

`POST /api/v1/skills` com um `name` já existente (em qualquer status, active ou archived) é conflito (409), nunca upsert silencioso — mesmo princípio de `SessionCreateRequest` (ADR-0012 §3, `name` já existente é conflito, não silent upsert).

## 6.2 Lock por-Skill

Toda operação que muta revisão ou o ponteiro `current_revision_id` de uma Skill é serializada por essa Skill: criar revisão (estruturada ou via import), `PATCH current_revision` (rollback), archive, restore. Conceitualmente, cada uma dessas operações adquire um lock sobre a linha `Skill` (`SELECT ... FOR UPDATE` ou equivalente) antes de decidir seu efeito — implementação exata é decisão do SM-701/702, não deste contrato.

Duas Skills diferentes nunca se bloqueiam entre si.

## 6.3 Criação de revisão concorrente — conteúdo diferente

Duas requisições concorrentes com conteúdo semanticamente diferente, para a mesma Skill, devem resultar em dois ordinais distintos e coerentes, sem colisão:

```text
primeira admitida  → revision N
segunda admitida   → revision N+1
current_revision_id → N+1 (a última commitada, dentro do lock)
```

## 6.4 Criação de revisão concorrente — conteúdo idêntico

Duas requisições concorrentes com o mesmo `content_sha256` (§12.7) para a mesma Skill produzem **uma única** SkillRevision persistida; a segunda requisição resolve como safe replay (§9.1), devolvendo a revisão já existente, sem criar uma nova e sem alterar `current_revision_id` além do que a primeira já fez.

## 6.5 Rollback vs nova revisão concorrentes

Uma criação de nova revisão e um rollback explícito (`PATCH current_revision`) concorrentes, sobre a mesma Skill, são linearizados pelo mesmo lock (§6.2) — o resultado depende estritamente da ordem de aquisição do lock, nunca de last-write-wins não determinístico:

```text
rollback adquire o lock primeiro, nova revisão depois
→ nova revisão torna-se current (a criação sempre aponta current para si mesma)

nova revisão adquire o lock primeiro, rollback depois
→ o alvo explícito do rollback torna-se current
```

Nenhum lost update silencioso é aceitável em nenhuma ordem — cada operação lê o estado corrente da Skill dentro do próprio lock antes de decidir seu efeito.

## 6.6 Rollback de current_revision

Selecionar novamente uma revisão histórica como `current_revision_id` (rollback) é uma mudança de ponteiro dentro de uma transação curta e sob o lock de §6.2, nunca cópia ou reconstrução de conteúdo. O mecanismo explícito é o `PATCH` descrito em §8.

---

# 7. Embedding e resolution boundary

O texto usado para indexação de resolução é `name` + `description` + `tags` — nunca o `procedure` completo.

Isso preserva a separação:

```text
metadata  = activation/discovery
procedure = execution guidance, disponível somente após seleção explícita
```

Reutiliza o embedding provider/configuração já existentes (mesmo client OpenAI-compatible usado por Cognify/Recall). Nenhuma nova vector database. Nenhum uso de Neo4j.

## 7.1 Boundary de escrita (create Skill / create SkillRevision / import)

```text
1. validar/normalizar payload (sem transação PostgreSQL aberta)
2. chamar o embedding provider (sem transação PostgreSQL aberta, sem lock por-Skill retido)
3. abrir transação PostgreSQL curta
4. adquirir o lock por-Skill (§6.2) dentro dessa transação
5. revalidar identidade/estado corrente (name ainda livre, ou Skill ainda existe e
   content_sha256 ainda não presente, conforme o caso)
6. inserir/atualizar atomicamente (Skill + SkillRevision + current_revision_id)
7. commit
```

Nenhuma transação PostgreSQL permanece aberta durante a chamada ao embedding provider — mesma disciplina já aplicada a Cognify e à resolução de Session. Falha do provider entre os passos 1–2 resulta em zero linhas persistidas (§4.4) — nenhuma Skill ou SkillRevision parcial.

A revalidação no passo 5 é o que torna a concorrência de §6.3–§6.5 correta: o resultado do embedding (calculado fora do lock) é aplicado dentro do lock contra o estado mais recente, não contra o estado lido antes da chamada ao provider.

## 7.2 Boundary de leitura (resolve)

```text
1. embed da query do caller (sem transação PostgreSQL aberta)
2. leitura PostgreSQL curta (SELECT via pgvector, sem lock)
```

`resolve` nunca adquire o lock por-Skill de §6.2 — é uma operação somente-leitura.

---

# 8. API — Skill management

## `POST /api/v1/skills`

Cria uma Skill e sua primeira SkillRevision atomicamente (uma Skill sem nenhuma revisão não é um estado válido observável publicamente).

Request mínimo:

```text
name            required, portable subset
description     required
procedure       required (Markdown body)
license         optional
compatibility   optional
metadata        optional
tags            optional
declared_tools  optional
```

`201` com a Skill criada (`current_revision = 1`).

`409` se `name` já existir.

## `GET /api/v1/skills`

Lista Skills com paginação (`limit`/`offset`, mesmo shape de `DatasetListResult`/`SessionListResult`). Suporta filtro por `status`. Devolve apenas metadata de seleção (§11), nunca `procedure`.

## `GET /api/v1/skills/{skill_uuid}`

Detalhe de management de uma Skill (identidade, status, `current_revision`, timestamps). Não devolve `procedure` diretamente — aponta para `current_revision` e o caller busca o conteúdo via `GET .../revisions/{revision}` quando precisar.

## `PATCH /api/v1/skills/{skill_uuid}`

Estritamente administrativo. Único campo mutável:

```text
current_revision
```

`current_revision` deve referenciar uma revisão existente da mesma Skill; caso contrário, `422`/`INVALID_REQUEST`.

`PATCH` nunca aceita `name`. `PATCH` nunca aceita conteúdo de revisão (`description`/`procedure`/etc.) — esses campos só existem via `POST .../revisions`.

## `POST /api/v1/skills/{skill_uuid}/archive`

Ver §5.1. Idempotente.

## `POST /api/v1/skills/{skill_uuid}/restore`

Ver §5.2. Idempotente.

---

# 9. API — SkillRevision

## `POST /api/v1/skills/{skill_uuid}/revisions`

Cria uma nova SkillRevision e atualiza `current_revision_id` atomicamente. Mesmo shape de campos de conteúdo do create de Skill (§8), exceto `name` (imutável, não aceito aqui).

`201` com a nova revisão quando o conteúdo é semanticamente novo. Revision number é o próximo inteiro monotônico da Skill (§4.3).

## 9.1 Safe replay (conteúdo duplicado)

Se o `content_sha256` (§12.7) calculado do payload já corresponder a uma SkillRevision existente da mesma Skill, a operação é um safe replay:

```text
200  -- devolve a SkillRevision existente (mesmo revision number de antes)
current_revision_id -- NUNCA é alterado por um safe replay
```

Um safe replay nunca é tratado como rollback. Reenviar o conteúdo de uma revisão histórica antiga não a torna `current` — apenas confirma que ela já existe. A única forma de tornar uma revisão histórica `current` é o `PATCH` explícito de §8.

Esta mesma regra (`201` para conteúdo novo, `200` para replay semanticamente idêntico, `current_revision_id` nunca alterado por replay) aplica-se igualmente à criação de revisão estruturada acima e à importação de revisão via `SKILL.md` (§12.4).

## `GET /api/v1/skills/{skill_uuid}/revisions`

Lista as revisões de uma Skill, paginado, ordenado por `revision` ascendente. Devolve metadata de cada revisão (sem o corpo completo de `procedure`, para manter a listagem leve — mesma lógica de progressive disclosure de §11).

## `GET /api/v1/skills/{skill_uuid}/revisions/{revision}`

Leitura completa de uma revisão específica, incluindo `procedure`. Este é o único ponto onde o conteúdo procedural completo é devolvido.

---

# 10. API — Semantic resolve

## `POST /api/v1/skills/resolve`

Request:

```text
query   required
top_k   optional, default 5, max 20
```

Somente Skills `active` participam.

Response: ranking de metadata (§11), nunca decisão. Cada item inclui `score`, onde **maior valor = maior similaridade semântica** (direção fixa, documentada, nunca invertida silenciosamente entre versões).

Sem matches: `200` com `matches=[]` — nunca `404`, nunca erro.

Não aplica threshold oculto de corte — todo ranking até `top_k` é devolvido, ordenado por score decrescente.

`resolve` nunca:

- escolhe uma Skill em nome do caller;
- carrega o `procedure` automaticamente;
- cria SessionEntry;
- cria Query de Recall;
- cria SkillRun (não existe SkillRun);
- executa qualquer tool.

---

# 11. Progressive disclosure — shape de metadata

Discovery/list/resolve devolvem, no mínimo:

```text
skill_uuid
name
description
current_revision
tags
declared_tools
compatibility
score            (apenas em resolve)
```

`procedure` nunca aparece nesse shape. Só aparece em `GET .../revisions/{revision}`.

---

# 12. SKILL.md interoperability

## 12.1 Subset portable suportado

```text
name           required, 1..64 chars, subset portable (§3.1)
description    required, 1..1024 chars
license        optional
compatibility  optional, 1..500 chars quando presente
metadata       optional, map<string, string> (§12.6)
corpo Markdown required, non-empty, mapeado para procedure (limite explícito, §12.8)
```

A recomendação externa do Agent Skills format de "`procedure` com menos de 500 linhas" é apenas uma recomendação de origem, nunca vira constraint normativa de validação nesta API.

## 12.2 `allowed-tools` → `declared_tools`

Mapping congelado, sem ambiguidade entre camadas:

```text
SKILL.md frontmatter  allowed-tools: <string separada por espaços>
                            ↕ (import / export, mapping determinístico)
Sofias Memory          declared_tools: list[string]
```

- Import: `allowed-tools` é tokenizado por espaço em uma lista de strings e persistido em `declared_tools`.
- Export: `declared_tools` é serializado de volta como uma única string separada por espaço em `allowed-tools`.
- API pública (create/list/detail/resolve): `declared_tools` é sempre `list[string]` — nunca string em um endpoint e lista em outro.
- Canonicalização para `content_sha256` (§12.7): a lista é ordenada (sort lexicográfico) antes do hash, para que duas listas com o mesmo conjunto de tools em ordem diferente produzam o mesmo hash. A ordem de armazenamento/retorno na API, fora do cálculo do hash, preserva a ordem informada na escrita mais recente.

Em todos os casos, `allowed-tools`/`declared_tools`:

- é preservado apenas como metadata descritiva/declarativa;
- nunca é interpretado como autorização;
- nunca concede acesso a tool;
- nunca faz bypass de permissões do caller.

Authorization continua responsabilidade exclusiva do runtime caller.

## 12.3 `metadata` é `map<string, string>`

O subset portable do Agent Skills format define `metadata` como um mapa string→string — não um JSON arbitrário aninhado.

Sofias Memory pode armazenar `metadata` internamente em uma coluna JSONB, mas a forma pública/interoperável (request/response da API, e o mapeamento de/para frontmatter YAML) é sempre `dict[str, str]`. Nenhum valor de `metadata` é um objeto ou array aninhado na superfície pública.

`metadata` persistida nunca contém a chave reservada `sofias-memory.tags` (§12.6) — rejeitada em toda superfície de escrita, estruturada ou futura-import.

## 12.4 Import

Duas rotas distintas, congeladas nesta task:

```text
POST /api/v1/skills/import
    -- importa um SKILL.md standalone como NOVA Skill.
    -- name já existente (qualquer status) → 409 INVALID_REQUEST. Nunca upsert.

POST /api/v1/skills/{skill_uuid}/revisions/import
    -- importa um SKILL.md standalone como NOVA SkillRevision de uma Skill existente.
    -- o `name` do frontmatter deve corresponder exatamente ao Skill.name do
       {skill_uuid} alvo. Divergência → 422 INVALID_REQUEST. Nunca renomeação
       implícita, nunca redirecionamento para outra Skill.
```

As duas rotas têm semânticas de idempotência **diferentes** — não confundir uma com a outra:

```text
POST /api/v1/skills/import
    -- name já existente → SEMPRE 409 INVALID_REQUEST, independentemente do
       conteúdo/hash. content_sha256 nunca é consultado para decidir o
       resultado desta rota. Nunca safe replay, nunca upsert, nunca cria
       revisão implicitamente, nunca resolve para a Skill existente.

POST /api/v1/skills/{skill_uuid}/revisions/import
    -- segue a regra de safe replay de §9.1: conteúdo semanticamente idêntico
       (mesmo content_sha256) a uma revisão já existente DESSA Skill resolve
       como replay seguro (200, revisão existente, current_revision_id
       inalterado). Conteúdo novo → 201, nova revisão.
```

Safe replay por `content_sha256` é uma propriedade de **criação de revisão em uma Skill já identificada por `skill_uuid`** (`POST .../revisions` estruturado e `POST .../revisions/import`), nunca de **criação de Skill** (`POST /skills` e `POST /skills/import`). A identidade de Skill é decidida exclusivamente por `name`; hash de conteúdo nunca participa dessa decisão.

## 12.5 Export

```text
GET /api/v1/skills/{skill_uuid}/revisions/{revision}/export
```

Para preservar o invariante global de envelope de resposta, o export retorna, dentro do `SuccessEnvelope` padrão, um payload conceitualmente equivalente a:

```json
{
  "format": "skill_md",
  "content": "...texto canônico SKILL.md...",
  "content_sha256": "..."
}
```

Não existe resposta raw fora do envelope só porque é export. O caller salva `content` como `SKILL.md` do lado dele, se quiser. A API estruturada continua sendo o recurso autoritativo.

Garantia de determinismo: exportar a mesma `revision` repetidamente produz sempre o mesmo `content` e o mesmo `content_sha256`, dado o mesmo conteúdo persistido — nunca uma promessa de que `content` é byte-idêntico ao arquivo `SKILL.md` originalmente importado (§12.7).

## 12.6 `tags` — extensão Sofias Memory, não portable

`tags` não é um campo top-level do subset portable do Agent Skills format. É uma extensão do Sofias Memory usada para discovery/filtering.

`SkillRevision.tags` é a **única fonte de verdade semântica** para tags. A chave reservada abaixo é **somente representação de transporte SKILL.md** — ela nunca faz parte do `metadata` semântico persistido de uma SkillRevision, e as duas nunca coexistem como fontes concorrentes.

Round-trip via `SKILL.md` usa uma chave reservada e namespaced (constante `SOFIAS_MEMORY_TAGS_METADATA_KEY = "sofias-memory.tags"`), presente apenas no documento `SKILL.md` externo (que é `map<string,string>`, §12.3):

```text
metadata:
  sofias-memory.tags: '["pdf","forms"]'
```

O valor é uma string contendo um array JSON serializado de tags (para caber no tipo `map<string,string>` do documento externo).

- **Import** (SM-703): se `metadata["sofias-memory.tags"]` estiver presente no `SKILL.md` importado, ela é **parseada como a lista de `tags`** e então **consumida e removida** — a chave nunca chega à `metadata` semântica persistida. Essa remoção não é silenciosa: é o comportamento explicitamente documentado e testado desta chave reservada, não um efeito colateral acidental. `tags` e `metadata` persistidos, a partir desse ponto, nunca contêm a mesma informação duas vezes.
- **Export** (SM-703): `metadata` persistida e `tags` persistida são combinadas apenas na hora de sintetizar o `SKILL.md` exportado — `metadata["sofias-memory.tags"]` é gerada nesse momento, exclusivamente no documento externo. O `metadata` persistido nunca é modificado por export.
- **API estruturada** (SM-702): o caller usa `tags=[...]` diretamente. Um payload estruturado cujo `metadata` contenha a chave `sofias-memory.tags` é rejeitado como metadata inválida (`INVALID_REQUEST`) — a chave é reservada e nunca pode ser usada como um canal alternativo para `tags` na API estruturada.
- Nenhum campo YAML top-level não-portable chamado `tags` é criado no `SKILL.md` exportado.

Determinismo de `tags`: a lista é deduplicada e ordenada lexicograficamente na escrita (mesma disciplina de `declared_tools`, §12.2), tanto para a representação persistida/retornada quanto para o cálculo de `content_sha256`.

Defesa em duas camadas (já implementada na foundation, SM-701): a rejeição de `metadata["sofias-memory.tags"]` é aplicada tanto pela validação de domínio (`validate_metadata`) quanto, de forma authoritative, por um `CHECK` PostgreSQL em `skill_revisions.metadata` — nenhum caminho de escrita, estruturado ou futuro-import, pode persistir a chave reservada dentro de `metadata`.

## 12.7 Canonicalização e `content_sha256`

Sofias Memory não é um archive byte-a-byte de `SKILL.md`. `content_sha256` representa o **conteúdo semântico canônico** de uma revisão, não os bytes originais de um upload.

Fluxo:

```text
SKILL.md importado
    → parse
    → normalização dos campos semânticos validados
    → persistência da revisão estruturada
```

O hash é calculado sobre uma representação canônica independente de formatação YAML:

```text
JSON UTF-8 canônico
sort_keys=true
separadores estáveis
procedure normalizado para LF (CRLF/CR → LF)
```

## 12.7.1 Objeto canônico (congelado)

O objeto canônico usado para `content_sha256` contém sempre exatamente estas oito chaves, nesta forma:

```text
name             string
description      string
procedure        string (após normalização de newline)
license          string | null
compatibility    string | null
metadata         object (map<string,string>, chaves ordenadas)
tags             array[string] (ordenado, deduplicado — §12.6)
declared_tools   array[string] (ordenado — §12.2)
```

`metadata`, dentro do objeto canônico, **nunca contém a chave `sofias-memory.tags`** — essa chave existe somente no documento `SKILL.md` externo (transporte), nunca na `metadata` semântica persistida ou hasheada. `tags` é a única representação canônica de tags; `metadata` e `tags` nunca duplicam a mesma informação dentro do objeto canônico. Consequência direta: a representação estruturada (`metadata={}`, `tags=[...]`) e a representação já normalizada de uma importação futura (mesmo `tags`, `metadata` sem a chave reservada) produzem exatamente o mesmo objeto canônico e o mesmo `content_sha256` — comprovado por teste (SM-701).

## 12.7.2 Normalização de campo ausente/opcional

Um campo opcional ausente e seu estado neutro equivalente produzem sempre a mesma representação canônica — nenhum caller/import path pode inventar uma representação diferente:

```text
license omitido ou null                → null
compatibility omitido ou null          → null
metadata omitido ou null               → {}
tags omitido ou null                   → []
declared_tools omitido ou null         → []
```

`name`, `description` e `procedure` são sempre required (§8, §12.1, §12.8) — nunca ausentes no objeto canônico.

Para campos presentes, aplicam-se as validações/normalizações já congeladas em outras seções (§3.1 para `name`, §12.6 para `tags`, §12.2 para `declared_tools`, §12.8 para limites de tamanho).

Garantia:

```text
mesmo conteúdo semântico
+ formatação YAML diferente
+ ordem de chaves diferente
+ CRLF vs LF
→ mesmo content_sha256
```

Não garantido:

```text
bytes do export == bytes do SKILL.md originalmente importado
```

Import → export é **semanticamente equivalente**, não necessariamente textualmente idêntico ao arquivo original.

## 12.8 Limites de validação

```text
name           1..64      (subset portable, §3.1)
description    1..1024
compatibility  1..500     (quando presente)
procedure      1..65536   (Unicode chars, após normalização CRLF/CR → LF;
                            mínimo é ao menos 1 caractere não-whitespace)
```

Este limite de `procedure` é validado independentemente do limite global de tamanho de request body — nenhum ticket de implementação decide esse número, ele já está congelado aqui.

A validação aplica-se ao texto já normalizado para LF. Nenhum trim destrutivo é aplicado ao conteúdo persistido apenas para fins de validação — whitespace significativo do Markdown (indentação de listas, blocos de código, etc.) é preservado no `procedure` armazenado.

A recomendação externa do Agent Skills format de `procedure` com menos de 500 linhas (§12.1) continua sendo apenas recomendação de origem, nunca um hard limit de validação desta API.

## 12.9 Fora de escopo do v0.4.0

```text
scripts/
references/
assets/
arquivos arbitrários agrupados
zip skill packages
recursos binários
execução de script
```

Um `SKILL.md` standalone é suficiente para o v0.4.0. Source object storage não é usado para esses recursos nesta release. Essa limitação é deliberada e documentada, não um gap acidental.

---

# 13. Global scope

Skills são globais à instância single-user Sofias Memory.

Nenhuma das seguintes colunas existe em `skills` ou `skill_revisions`:

```text
dataset_id
session_id
agent_id
owner_id
tenant_id
user_id
```

v0.4.0 não implementa associação Agent↔Skill — isso pertence ao v0.5 Agent Management (§18).

---

# 14. PostgreSQL / Neo4j

PostgreSQL é authoritative para `skills` e `skill_revisions`.

Skills e SkillRevisions não são Document, Chunk, Entity ou Relation. Skills nunca passam por Remember/Cognify. Skills nunca geram Source. Skills nunca geram evento em `graph_outbox`.

Nenhum nó `(:Skill)` ou `(:SkillRevision)` é criado no Neo4j. Graph reconciliation não ganha nenhuma responsabilidade sobre Skills.

pgvector fica dentro do PostgreSQL, exclusivamente para semantic resolution de Skills (mesma extensão já usada para embeddings de Chunk).

---

# 15. Forget / Dataset Delete

Forget de memória semântica não remove Skills. Dataset Delete não remove Skills. Forget `everything` não significa Skill purge — Skills não são Dataset-owned e portanto estão estruturalmente fora do escopo de qualquer scope de Forget/Dataset Delete existente.

Testes de compatibilidade cross-feature (equivalentes aos de SM-606 para Sessions) devem provar isso explicitamente no SM-705.

---

# 16. Sessions

Skill não pertence a Session. `skill_uuid`/`skill_id` nunca aparecem em `Session` ou `SessionEntry`.

Skill resolution pode futuramente ser usada por um runtime que possui Session, mas v0.4.0 não persiste associação Session↔Skill, nem qual Skill foi selecionada, nem se/como foi executada.

`POST /skills/resolve` nunca cria SessionEntry. Nenhuma alteração ocorre no Session Context RAG (ADR-0012 §Context selection permanece exatamente como está).

---

# 17. Comparação com Session archive

Session archive é um **admission barrier**: bloqueia nova atividade (`SessionEntry`, Recall, Remember) enquanto archived (ADR-0012 §Admission semantics).

Skill archive é um **discovery filter**: remove a Skill de `resolve`, mas não bloqueia nenhuma operação de management. Uma Skill `archived` continua permitindo, exatamente como se estivesse `active`:

```text
GET detail
GET revisions (list e leitura individual)
POST .../revisions          (criar nova revisão, estruturada ou via import)
PATCH current_revision      (rollback)
GET .../export
POST .../restore
```

Criar uma nova revisão em uma Skill `archived` funciona normalmente: `current_revision_id` é atualizado, a Skill permanece `archived`, e `resolve` continua excluindo-a até um `restore` explícito.

`archive` e `restore` nunca modificam `current_revision_id` por si mesmos — nenhum dos dois é uma operação que muta o ponteiro. `restore` reativa a mesma Skill, com o mesmo histórico, preservando exatamente o `current_revision_id` que a Skill possui **no instante do restore** — que pode ser diferente do que era no instante do archive, se uma nova revisão foi criada (ou um rollback via `PATCH` foi feito) enquanto a Skill estava `archived` (§5.1). `restore` não busca nem reconstrói um ponteiro histórico anterior ao archive; ele simplesmente não toca o ponteiro.

Exemplo normativo:

```text
Skill active, current_revision_id → revision 2
archive                            → current_revision_id ainda → revision 2 (archive não move o ponteiro)
create revision 3 (enquanto archived) → current_revision_id → revision 3 (management continua mutando o ponteiro normalmente)
restore                            → current_revision_id ainda → revision 3 (restore não move o ponteiro)
```

`restore` apenas volta a tornar a Skill elegível para `resolve`.

Archive e restore são ambos idempotentes (§5.1, §5.2).

Essa diferença é deliberada: Session archive protege contra nova atividade contextual indesejada; Skill archive protege apenas contra descoberta indesejada de conhecimento procedural potencialmente obsoleto, sem impedir manutenção administrativa contínua.

---

# 18. Agent boundary (v0.5, referência futura)

v0.4.0 prepara procedural memory para v0.5, mas não implementa Agent.

Futuro esperado:

```text
Agent Profile
    ↕ associação explícita
Skill
```

Essa futura associação não deve exigir mudar a identidade (`id`/`skill_uuid`/`name`) ou o modelo de revisão da Skill — mesma garantia de não-redesenho que ADR-0012 deu para uma futura associação Agent↔Session.

Nenhuma tabela `agents` é criada nesta release.

---

# 19. Security boundary

Sofias Memory mantém sua única chave estática de aplicação (ADR-0003). Skills não introduzem nenhum novo princípio de autorização.

`declared_tools`/`allowed-tools` importado nunca autorizam execução (§12.2). Nenhuma Skill pode, por si só, conceder acesso a um tool, bypassar a permissão do caller, ou instruir Sofias Memory a executar algo.

Conteúdo de `procedure` é dado não confiável do ponto de vista de prompt injection quando eventualmente interpretado por um LLM externo — Sofias Memory não interpreta `procedure`, apenas o armazena e devolve; a responsabilidade de tratamento seguro do conteúdo pertence ao caller que o executa.

---

# 20. Error behavior

Reutilizar ErrorCodes existentes sempre que suficientes:

```text
INVALID_REQUEST          -- Skill/revision não encontrada (404); name já existe em
                             POST /skills ou POST /skills/import (409); current_revision
                             inexistente em PATCH (422); name mismatch entre frontmatter
                             e Skill alvo em POST .../revisions/import (422); validação
                             de formato/limites de name/description/compatibility/procedure
DEPENDENCY_UNAVAILABLE   -- embedding provider indisponível durante create/revision/import
INTERNAL_ERROR           -- falha inesperada
IDEMPOTENCY_CONFLICT     -- reuso de Idempotency-Key com payload divergente, se aplicável
```

Um safe replay (§9.1) nunca é um erro — é `200` com a revisão existente, não um `409`/`INVALID_REQUEST`.

Nenhum novo ErrorCode é introduzido no v0.4.0 a menos que o SM-701/702/703 encontre um caso genuinamente não coberto pelos códigos acima — decisão a ser tomada durante implementação, não antecipada aqui.

---

# 21. Legacy compatibility

## 21.1 Rotas proibidas

O contrato atual (`AGENTS.md` §12, PRD §11.3) proíbe `/skills`. v0.4.0 remove deliberadamente **somente** `/skills` dessa proibição.

Continuam proibidos:

```text
/agents
/proposals
/integrations
```

e as demais superfícies já listadas fora do produto (`/auth`, `/users`, `/permissions`, `/api-keys`, `/settings`, `/configuration`, `/sync`, `/cloud`, `/serve`, `/push`, `/slack`).

O teste de contrato `tests/contract/test_openapi_forbidden_routes.py` deve ser atualizado no ticket de API correspondente (SM-702) para remover `/skills` de `FORBIDDEN_PATH_PREFIXES`. Nenhuma alteração de teste ocorre neste documento nem nesta task de definição.

## 21.2 `AGENTS.md` / `CLAUDE.md`

`AGENTS.md` §17 (Recall MVP) lista `skills/tools` entre o que não é implementado — essa referência permanece válida sem alteração: ela descreve que Recall não ganha seleção/invocação automática de Skill, o que continua verdadeiro no v0.4.0 (§2.5, §16).

`AGENTS.md` §12 (rotas proibidas) precisa de amendment mínimo para remover `/skills`. Esse amendment é registrado como escopo do ticket de API (SM-702), não executado nesta task de definição normativa.

`CLAUDE.md` (raiz do repositório) duplica `AGENTS.md` quase integralmente, incluindo a mesma lista de rotas proibidas e a mesma referência a `skills/tools`, mas já está fora de sincronia com `AGENTS.md` (não reflete as atualizações da era Sessions). O amendment de `/skills` no SM-702 deve tocar os dois arquivos juntos, não apenas `AGENTS.md` — deixar `CLAUDE.md` desatualizado reintroduziria a mesma contradição que este release está removendo.

Amendment mínimo esperado, para referência futura:

```text
Skills storage/resolution = allowed
tool/runtime execution     = still forbidden
```

## 21.3 PRD original

`docs/product/Sofias_Memory_PRD_SPECS.md` §7 (matriz de paridade) e §11.3 (rotas proibidas) continuam descrevendo o baseline original. Este Feature Contract é o amendment específico de Skills; o PRD não é reescrito.

---

# 22. Out of scope — v0.4.0

Explicitamente fora deste release:

- SkillRun;
- SkillExecution;
- tool execution;
- tool authorization;
- tool registry;
- agent runtime;
- Agent;
- AgentConnection;
- seleção automática de Skill dentro de Recall;
- invocação automática de Skill;
- criação automática de Skill;
- melhoria automática de Skill;
- Skills auto-editáveis;
- proposals;
- aprendizado a partir de execuções bem-sucedidas;
- ratings/evals de Skill;
- provenance Session→Skill;
- projeção Neo4j de Skill;
- scripts/references/assets agrupados;
- skill marketplace;
- sync remoto;
- registro de skills baseado em Git;
- filesystem watcher;
- plugins;
- MCP.

---

# 23. Invariants obrigatórios

1. PostgreSQL é authoritative para Skills e SkillRevisions.
2. Skill nunca pertence a Dataset, Session ou Agent.
3. `name` é globally unique, imutável e segue o subset portable Agent Skills.
4. `skill_uuid` é a serialização pública de `Skill.id` — não existe coluna `skill_uuid` separada; `skill_uuid` e `name` nunca são derivados um do outro nem de path de filesystem.
5. Uma Skill committed nunca tem `current_revision_id = NULL`; falha do embedding provider durante criação nunca deixa Skill ou SkillRevision parcial (zero linhas em ambas).
6. SkillRevision, uma vez criada, nunca é editada in-place.
7. Revision number é inteiro monotônico por Skill, nunca timestamp, nunca `metadata.version` externo; concorrência de criação é serializada por um lock por-Skill.
8. `current_revision_id` é ponteiro explícito; rollback nunca copia ou modifica conteúdo; um safe replay de conteúdo duplicado nunca altera `current_revision_id`.
9. Resolution embedding indexa apenas `name`+`description`+`tags`, nunca `procedure`.
10. `resolve` nunca escolhe, nunca carrega procedure automaticamente, nunca cria SessionEntry/Query/SkillRun; `score` maior sempre significa mais similar; sem matches é `200`/`matches=[]`, nunca erro.
11. Discovery/list/resolve nunca devolvem `procedure` completo.
12. Skill archive é discovery filter — toda operação de management (`GET`, criar revisão, `PATCH current_revision`, `export`) permanece disponível em uma Skill archived; apenas `resolve` a exclui.
13. `declared_tools`/`allowed-tools` nunca autorizam execução; `declared_tools` é sempre `list[string]` na API pública; `metadata` é sempre `map<string,string>` na API pública e no mapeamento SKILL.md.
14. `POST /skills/import` com `name` já existente é sempre `409`, nunca safe replay, nunca upsert, nunca cria revisão implicitamente — hash de conteúdo nunca decide identidade de Skill. `POST .../revisions/import` para uma Skill já identificada por `skill_uuid` cria revisão explícita, ou resolve como safe replay se o conteúdo for semanticamente idêntico a uma revisão já existente dessa Skill.
15. Import de revisão exige correspondência exata de `name` entre frontmatter e Skill alvo.
16. `content_sha256` é calculado sobre uma representação canônica do conteúdo semântico, nunca sobre os bytes originais importados; export é deterministicamente equivalente ao conteúdo persistido, nunca uma promessa de bytes originais preservados.
17. Skills nunca são projetadas para Neo4j.
18. Forget e Dataset Delete nunca removem Skill/SkillRevision.
19. Nenhuma transação PostgreSQL longa permanece aberta durante chamada ao embedding provider, em escrita ou em `resolve`.
20. Skill não é authorization boundary.
21. `metadata` semântica persistida nunca contém a chave reservada `sofias-memory.tags` — `tags` é a única fonte de verdade para tags; a chave existe apenas como representação de transporte no documento `SKILL.md` externo, consumida e removida no import, sintetizada apenas no export, e rejeitada (domínio + `CHECK` PostgreSQL) em qualquer tentativa de persisti-la.

---

# 24. Critério de conclusão do v0.4.0

A feature é considerada concluída quando:

- Skills e SkillRevisions possuem persistência PostgreSQL e lifecycle aprovado, com a identidade única `id`/`skill_uuid` (§3) comprovada em schema/testes;
- a API de management funciona com paginação e contratos estáveis;
- criação de Skill e de revisão são concurrency-safe, incluindo a matriz de concorrência de §6 (conteúdo diferente, conteúdo idêntico, e rollback-vs-nova-revisão) comprovada por teste real PostgreSQL;
- `current_revision` rollback funciona como mudança de ponteiro comprovada por teste, e safe replay (§9.1) nunca altera esse ponteiro, comprovado por teste;
- resolve funciona com pgvector, respeita `active`-only, nunca ultrapassa o boundary de metadata, e a direção de `score`/comportamento sem matches estão comprovados;
- import/export SKILL.md são semanticamente equivalentes (canonical hash, §12.7) e sem upsert silencioso, com as duas rotas de import (§12.4) e o mapping `declared_tools`/`tags` (§12.2/§12.6) comprovados por teste;
- `declared_tools`/`allowed-tools` são comprovadamente nunca interpretados como autorização;
- Forget/Dataset Delete/Session/Neo4j compatibility está comprovada por teste;
- `/skills` é removido do forbidden-route contract sem reabrir `/agents`/`/proposals`;
- documentação (`AGENTS.md`, `CLAUDE.md`, `docs/api.md`, README) reflete a nova superfície, sincronizada entre `AGENTS.md` e `CLAUDE.md`;
- suite completa (unit/integration/contract/security) está verde;
- smoke real end-to-end está verde;
- nenhum item de §22 foi implementado antecipadamente.
