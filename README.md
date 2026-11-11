# E-Shop Brasil: gestão de dados com MongoDB, Streamlit e Docker

Aplicação prática da disciplina de Bancos de Dados Avançados e Big Data. Simula a camada de dados de uma grande plataforma de e-commerce, com inserção, edição, concatenação e análise de grandes volumes de dados em MongoDB, usando uma interface em Streamlit.

## 1. Introdução

A E-Shop Brasil tem mais de 5 milhões de clientes ativos e processa cerca de 100 mil pedidos por dia. Com esse volume, a equipe de TI enfrenta dois problemas centrais:

- **Gestão de dados:** proteger dados pessoais (LGPD) e usar o histórico de compras para personalizar a experiência.
- **Logística:** controlar estoque e prazos de entrega, principalmente nas regiões mais distantes (Norte e Nordeste), e integrar o online com a retirada em loja.

**Objetivo:** construir um ambiente Dockerizado com MongoDB e uma interface em Streamlit que mostre, na prática, como um banco NoSQL apoia esses problemas: documentos flexíveis (pedidos com itens aninhados), agregações para análise e índices para manter as consultas rápidas conforme o volume cresce.

## 2. Tecnologias utilizadas

| Tecnologia | Papel no projeto | Por que foi escolhida |
|---|---|---|
| **MongoDB 7** | Banco de dados principal | Um pedido com seus itens é um único documento, sem JOINs para lê-lo. Escala horizontalmente (sharding) e tem agregações poderosas |
| **Streamlit** | Interface gráfica | Permite criar telas de dados em Python, sem front-end separado |
| **Docker e Docker Compose** | Ambiente reproduzível | Sobe banco e aplicação com um único comando, em qualquer máquina |
| **PyMongo** | Driver Python do MongoDB | Acesso nativo a consultas e pipelines de agregação |
| **Pandas** | Tabelas, edição e concatenação de CSV | Integra bem com o Streamlit (`st.data_editor`) |
| **Faker (pt_BR)** | Geração de dados fictícios | Nomes, CPFs e cidades brasileiras sem expor dados reais |

## 3. Arquitetura

```
 Navegador ──► Streamlit (app.py, contêiner "app", porta 8501)
                      │  PyMongo
                      ▼
               MongoDB 7 (contêiner "mongo", volume "mongo_data")
```

### Modelo de dados

| Coleção | Chave de negócio | Conteúdo |
|---|---|---|
| `clientes` | `codigo_cliente` | nome, e-mail, CPF, cidade, estado, região, data de cadastro, `consentimento_lgpd` |
| `produtos` | `sku` | nome, categoria, marca, preço |
| `pedidos` | `numero` | `codigo_cliente`, **`itens` (lista aninhada)**, total, status, canal, região, prazo prometido e real |
| `estoque` | `id_estoque` | `sku`, centro de distribuição, região, quantidade, mínimo |
| `pedidos_enriquecidos` | (gerada pelo app) | pedidos + clientes + produtos, uma linha por item |

Índices criados automaticamente: `codigo_cliente`, `sku`, `numero`, `data_pedido`, `regiao`, `itens.sku`, entre outros.

## 4. Descrição da aplicação

### Inserção de dados no MongoDB (seção "Inserir dados")
- **Gerar dados fictícios:** define quantos clientes, produtos e pedidos criar (até 100 mil clientes e 200 mil pedidos). A inserção é feita em lotes (`insert_many`). Os dados simulam o contexto brasileiro: regiões remotas têm prazos maiores e mais atrasos.
- **Importar CSV:** carrega qualquer arquivo (por exemplo, datasets do Kaggle) para uma coleção à escolha.
- **Cadastro manual:** formulários de cliente e produto. O cliente registra o consentimento LGPD.

### Edição e exclusão (seção "Editar e excluir")
- Tabela editável (`st.data_editor`) para qualquer coleção, com filtro em JSON do MongoDB.
- Editar células, adicionar linhas e excluir linhas. As mudanças só são gravadas ao clicar em **Salvar alterações** (`UpdateOne`, `InsertOne` e `DeleteOne` em um único `bulk_write`).
- Exclusão em massa por filtro, com confirmação.

### Manipulação e concatenação de dados (seção "Concatenar dados")
1. **Pedidos + clientes + produtos:** pipeline com `$lookup` que cruza as três coleções. O campo `localizacao` é a concatenação de cidade e estado com `$concat`. O resultado pode ser salvo na coleção `pedidos_enriquecidos` com `$out`.
2. **Empilhar CSV com uma coleção:** `pd.concat` entre os dados do MongoDB e um CSV, com detecção de duplicatas por chave. Só entram os registros novos.

### Consultas e visualização (seção "Consultas")
- **Análises prontas:** faturamento por região e por mês, 10 produtos mais vendidos, atraso médio de entrega por região, estoque crítico por região, entrega x retirada em loja, clientes com e sem consentimento. Cada uma mostra tabela, gráfico, tempo de execução e o pipeline usado.
- **Recomendação de produtos:** "quem comprou isto também comprou", calculado a partir dos itens dos pedidos.
- **Consulta livre:** filtro JSON em qualquer coleção.

### Segurança e LGPD
- Dados pessoais (nome, e-mail e CPF) aparecem **mascarados** por padrão, com a opção na barra lateral.
- `pedidos_enriquecidos` usa apenas o código do cliente, sem nome (pseudonimização).
- O campo `consentimento_lgpd` permite filtrar quem pode receber personalização.
- O MongoDB exige usuário e senha e a porta 27017 fica exposta só em `127.0.0.1`.
- A senha é configurável por `.env`, que não vai para o Git.

## 5. Como executar

Pré-requisito: [Docker](https://docs.docker.com/get-docker/) com Docker Compose.

```bash
git clone https://github.com/hemerxz/eshop-brasil-mongodb.git
cd eshop-brasil-mongodb

# (opcional, recomendado) definir uma senha própria para o MongoDB
cp .env.example .env

docker compose up --build
```
Em versões antigas do Docker, use `docker-compose up --build`.

Depois abra **http://localhost:8501**. O Streamlit (`app.py`) já sobe automaticamente dentro do contêiner `app`.

Alternativa sem Compose (`docker build` + `docker run`):

```bash
docker network create eshop
docker run -d --name eshop-mongo --network eshop -p 127.0.0.1:27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=admin -e MONGO_INITDB_ROOT_PASSWORD=eshop_dev_2025 \
  -v mongo_data:/data/db mongo:7
docker build -t eshop-app .
docker run --rm --name eshop-app --network eshop -p 8501:8501 \
  -e MONGO_URI="mongodb://admin:eshop_dev_2025@eshop-mongo:27017/?authSource=admin" eshop-app
```

Comandos úteis:

```bash
docker compose ps            # estado dos contêineres
docker compose logs -f app   # logs do Streamlit
docker compose down          # para tudo (os dados continuam no volume)
docker compose down -v       # para tudo e apaga os dados
```

## 6. Testes e exemplos

Roteiro para testar todas as funcionalidades (os prints e GIFs ficam na pasta [`exemplos/`](exemplos/)):

1. **Inserir dados:** vá em *Inserir dados*, mantenha 5.000 clientes, 300 produtos e 20.000 pedidos e clique em *Gerar e inserir no MongoDB*. Confira os totais em *Visão geral*.
2. **Editar e excluir:** abra *Editar e excluir*, escolha `produtos`, altere um preço, exclua uma linha e clique em *Salvar alterações*. Confirme a mudança recarregando a página.
3. **Concatenar:** em *Concatenar dados*, veja a união de pedidos, clientes e produtos e salve em `pedidos_enriquecidos`. Depois, suba um CSV com produtos novos na aba *Empilhar CSV*.
4. **Consultar:** em *Consultas*, abra *Atraso médio de entrega por região* (o Norte deve aparecer com o maior atraso) e *Estoque crítico por região*. Em *Recomendação de produtos*, escolha um produto e veja o que mais é comprado com ele.
5. **LGPD:** desative e ative o interruptor *Mascarar dados pessoais* e veja o efeito em *Editar e excluir*, coleção `clientes`.
### Capturas de tela

**Visão geral**

![Visão geral](exemplos/01-visao-geral.png)

**Inserção de dados**

![Inserir dados](exemplos/02-inserir-dados.png)

**Edição e exclusão**

![Editar e excluir](exemplos/03-editar-excluir.png)

**Concatenação de dados**

![Concatenação](exemplos/04-concatenacao.png)

**Consultas**

![Consultas](exemplos/05-consultas.png)

## 7. Estrutura do repositório

```
├── README.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── app.py
├── .env.example
├── .streamlit/config.toml
└── exemplos/        (prints e GIFs)
```

## 8. Caminho de evolução (Big Data)

Esta aplicação roda em uma única instância do MongoDB. Em produção, o caminho natural seria:

- **Replica set e sharding** no MongoDB para alta disponibilidade e escala horizontal (por exemplo, shard key por `regiao` ou `codigo_cliente`).
- **Spark** (via MongoDB Spark Connector) para processar o histórico completo, como treinar modelos de recomendação ou prever demanda por região.
- **Hadoop/HDFS ou data lake** para o armazenamento frio de eventos de navegação.
- **PostgreSQL** (relacional) para dados transacionais e financeiros que exigem ACID estrito, em arquitetura de persistência poliglota.
