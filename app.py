"""E-Shop Brasil: gestão e análise de dados com MongoDB e Streamlit.

Funcionalidades:
  - Inserção de dados (fictícios com Faker, CSV/Kaggle, cadastro manual)
  - Edição e exclusão de dados
  - Concatenação de dados ($lookup, $concat e empilhamento de CSV com pandas)
  - Consultas com visualização (vendas, logística, estoque, recomendação)
"""
import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from bson import ObjectId
from bson import json_util
from bson.errors import InvalidId
from faker import Faker
from pymongo import DeleteOne, InsertOne, MongoClient, UpdateOne

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "eshop_brasil")

# ---------------------------------------------------------------------------
# Parâmetros do domínio (regiões, prazos, catálogo)
# ---------------------------------------------------------------------------
REGIOES = {
    "Sudeste": ["SP", "RJ", "MG", "ES"],
    "Sul": ["PR", "SC", "RS"],
    "Nordeste": ["BA", "SE", "AL", "PE", "PB", "RN", "CE", "PI", "MA"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Norte": ["AM", "PA", "AC", "RO", "RR", "AP", "TO"],
}
ESTADO_REGIAO = {uf: reg for reg, ufs in REGIOES.items() for uf in ufs}
PESO_REGIAO = {"Sudeste": 45, "Sul": 15, "Nordeste": 22, "Centro-Oeste": 8, "Norte": 10}
PRAZO_BASE = {"Sudeste": 3, "Sul": 5, "Centro-Oeste": 6, "Nordeste": 8, "Norte": 12}
# Probabilidade de atraso extra de 0, 1, 2, 4 ou 7 dias (regiões remotas atrasam mais)
PESO_ATRASO = {
    "Sudeste": [70, 20, 7, 2, 1],
    "Sul": [60, 22, 12, 4, 2],
    "Centro-Oeste": [50, 25, 15, 7, 3],
    "Nordeste": [40, 25, 18, 10, 7],
    "Norte": [30, 25, 20, 15, 10],
}
CENTROS = {
    "Sudeste": "CD Cajamar-SP",
    "Sul": "CD Curitiba-PR",
    "Nordeste": "CD Recife-PE",
    "Centro-Oeste": "CD Goiânia-GO",
    "Norte": "CD Manaus-AM",
}
STATUS = ["Entregue", "Em transporte", "Processando", "Cancelado"]
CATALOGO = {
    "Eletrônicos": (["Smartphone", "Notebook", "Fone Bluetooth", "Smart TV", "Tablet"], (150, 6000)),
    "Moda": (["Camiseta", "Tênis", "Jaqueta", "Calça Jeans", "Vestido"], (40, 600)),
    "Casa": (["Air Fryer", "Aspirador", "Jogo de Cama", "Cafeteira", "Liquidificador"], (80, 1200)),
    "Beleza": (["Perfume", "Kit Skincare", "Secador", "Maquiagem"], (30, 500)),
    "Esporte": (["Bicicleta", "Halteres", "Tênis de Corrida", "Bola"], (50, 2500)),
}
MARCAS = ["Aurora", "Nativa", "Vetor", "Brasa", "Orion", "Maré", "Ipê", "Tupã"]

CHAVE = {"clientes": "codigo_cliente", "produtos": "sku", "pedidos": "numero", "estoque": "id_estoque"}
PREFIXO = {"clientes": "CLI-", "produtos": "SKU-", "pedidos": "PED-", "estoque": "EST-"}
DADOS_PESSOAIS = {"clientes": ["nome", "email", "cpf"]}


# ---------------------------------------------------------------------------
# Conexão e índices
# ---------------------------------------------------------------------------
def criar_indices(db):
    db.clientes.create_index("codigo_cliente")
    db.clientes.create_index("estado")
    db.produtos.create_index("sku")
    db.produtos.create_index("categoria")
    db.pedidos.create_index("numero")
    db.pedidos.create_index("codigo_cliente")
    db.pedidos.create_index("data_pedido")
    db.pedidos.create_index("regiao")
    db.pedidos.create_index("itens.sku")
    db.estoque.create_index([("sku", 1), ("regiao", 1)])


@st.cache_resource(show_spinner="Conectando ao MongoDB...")
def get_db():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[DB_NAME]
    criar_indices(db)
    return db


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def py(valor):
    """Converte valores do pandas/numpy em tipos que o MongoDB aceita."""
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(valor, pd.Timestamp):
        return valor.to_pydatetime()
    if hasattr(valor, "item"):
        return valor.item()
    return valor


def iguais(a, b):
    if pd.isna(a) and pd.isna(b):
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


def brl(valor):
    texto = f"{valor or 0:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def milhar(n):
    return f"{n:,}".replace(",", ".")


def novo_codigo(colecao):
    return PREFIXO.get(colecao, "") + uuid.uuid4().hex[:8].upper()


def _oid(texto):
    try:
        return ObjectId(texto)
    except (InvalidId, TypeError):
        return texto


def mascarar_valor(coluna, valor):
    """Mascara dados pessoais para exibição (princípio da minimização da LGPD)."""
    if not isinstance(valor, str) or not valor:
        return valor
    if coluna == "cpf":
        return "***." + valor[4:7] + ".***-**" if len(valor) == 14 else "***"
    if coluna == "email":
        usuario, _, dominio = valor.partition("@")
        return usuario[:1] + "***@" + dominio
    if coluna == "nome":
        return valor.split()[0] + " ***"
    return valor


def mascarar_df(df, colecao, ativo):
    if not ativo or df.empty:
        return df
    df = df.copy()
    for coluna in DADOS_PESSOAIS.get(colecao, []):
        if coluna in df.columns:
            df[coluna] = df[coluna].map(lambda v, c=coluna: mascarar_valor(c, v))
    return df


def docs_para_df(docs):
    """Converte documentos em DataFrame. Listas/dicts viram JSON (somente leitura)."""
    linhas, aninhadas = [], set()
    for doc in docs:
        linha = {}
        for chave, valor in doc.items():
            if chave == "_id":
                linha[chave] = str(valor)
            elif isinstance(valor, (list, dict)):
                aninhadas.add(chave)
                linha[chave] = json.dumps(valor, ensure_ascii=False, default=str)
            else:
                linha[chave] = valor
        linhas.append(linha)
    return pd.DataFrame(linhas), aninhadas


def inserir_em_lotes(colecao, docs, tamanho=5000):
    db = get_db()
    for i in range(0, len(docs), tamanho):
        db[colecao].insert_many(docs[i:i + tamanho], ordered=False)


def agregar(colecao, pipeline):
    inicio = time.perf_counter()
    resultado = list(get_db()[colecao].aggregate(pipeline, allowDiskUse=True))
    return pd.DataFrame(resultado), (time.perf_counter() - inicio) * 1000


def ler_csv(arquivo, chave):
    sep = st.selectbox("Separador", [",", ";", "\t"], key=f"sep_{chave}")
    arquivo.seek(0)
    return pd.read_csv(arquivo, sep=sep, encoding_errors="replace")


# ---------------------------------------------------------------------------
# Geração de dados fictícios
# ---------------------------------------------------------------------------
def data_aleatoria(dias):
    return datetime.now() - timedelta(days=random.randint(0, dias), seconds=random.randint(0, 86399))


def novo_produto():
    categoria = random.choice(list(CATALOGO))
    bases, (minimo, maximo) = CATALOGO[categoria]
    marca = random.choice(MARCAS)
    return {
        "sku": novo_codigo("produtos"),
        "nome": f"{random.choice(bases)} {marca} {random.randint(100, 999)}",
        "categoria": categoria,
        "marca": marca,
        "preco": round(random.uniform(minimo, maximo), 2),
    }


def estoque_do_produto(produto):
    docs = []
    for regiao, centro in CENTROS.items():
        quantidade = random.choices([0, random.randint(1, 19), random.randint(20, 500)], [5, 15, 80])[0]
        docs.append({
            "id_estoque": f"{produto['sku']}@{regiao}",
            "sku": produto["sku"],
            "centro_distribuicao": centro,
            "regiao": regiao,
            "quantidade": quantidade,
            "minimo": 20,
        })
    return docs


def novo_cliente(fake, estados, pesos):
    uf = random.choices(estados, pesos)[0]
    return {
        "codigo_cliente": novo_codigo("clientes"),
        "nome": fake.name(),
        "email": fake.email(),
        "cpf": fake.cpf(),
        "cidade": fake.city(),
        "estado": uf,
        "regiao": ESTADO_REGIAO[uf],
        "data_cadastro": data_aleatoria(1460),
        "consentimento_lgpd": random.random() < 0.93,
    }


def novo_pedido(clientes, produtos, por_categoria):
    cliente = random.choice(clientes)
    regiao = cliente["regiao"]
    primeiro = random.choice(produtos)
    escolhidos = {primeiro["sku"]: primeiro}
    for _ in range(random.choices([0, 1, 2, 3], [55, 25, 15, 5])[0]):
        # 70% das vezes o cliente compra itens da mesma categoria (gera padrões p/ recomendação)
        origem = por_categoria[primeiro["categoria"]] if random.random() < 0.7 else produtos
        extra = random.choice(origem)
        escolhidos[extra["sku"]] = extra
    itens = [
        {"sku": p["sku"], "nome": p["nome"], "qtd": random.choices([1, 2, 3], [80, 15, 5])[0], "preco": p["preco"]}
        for p in escolhidos.values()
    ]
    status = random.choices(STATUS, [70, 15, 10, 5])[0]
    prometido = PRAZO_BASE[regiao] + random.randint(0, 2)
    extra_dias = random.choices([0, 1, 2, 4, 7], PESO_ATRASO[regiao])[0]
    real = max(1, prometido - random.randint(0, 2) + extra_dias) if status == "Entregue" else None
    return {
        "numero": novo_codigo("pedidos"),
        "codigo_cliente": cliente["codigo_cliente"],
        "itens": itens,
        "total": round(sum(i["qtd"] * i["preco"] for i in itens), 2),
        "status": status,
        "canal": random.choices(["Entrega", "Retirada em loja"], [85, 15])[0],
        "data_pedido": data_aleatoria(364),
        "estado": cliente["estado"],
        "regiao": regiao,
        "centro_distribuicao": CENTROS[regiao],
        "prazo_prometido_dias": prometido,
        "prazo_real_dias": real,
    }


def gerar_dados(n_clientes, n_produtos, n_pedidos, limpar):
    db = get_db()
    fake = Faker("pt_BR")
    barra = st.progress(0.0, text="Preparando...")

    if limpar:
        for nome in ["clientes", "produtos", "pedidos", "estoque", "pedidos_enriquecidos"]:
            db[nome].drop()
        criar_indices(db)

    if n_produtos:
        barra.progress(0.05, text="Gerando produtos e estoque...")
        produtos = [novo_produto() for _ in range(n_produtos)]
        estoque = [e for p in produtos for e in estoque_do_produto(p)]
        inserir_em_lotes("produtos", produtos)
        inserir_em_lotes("estoque", estoque)

    if n_clientes:
        estados = list(ESTADO_REGIAO)
        pesos = [PESO_REGIAO[ESTADO_REGIAO[e]] / len(REGIOES[ESTADO_REGIAO[e]]) for e in estados]
        tamanho = 5000
        for inicio in range(0, n_clientes, tamanho):
            lote = [novo_cliente(fake, estados, pesos) for _ in range(min(tamanho, n_clientes - inicio))]
            db.clientes.insert_many(lote, ordered=False)
            barra.progress(0.05 + 0.35 * min(1.0, (inicio + tamanho) / n_clientes),
                           text=f"Gerando clientes... {min(inicio + tamanho, n_clientes):,}".replace(",", "."))

    if n_pedidos:
        clientes = list(db.clientes.find({}, {"_id": 0, "codigo_cliente": 1, "estado": 1, "regiao": 1}))
        produtos = list(db.produtos.find({}, {"_id": 0, "sku": 1, "nome": 1, "preco": 1, "categoria": 1}))
        if not clientes or not produtos:
            barra.empty()
            st.error("Para gerar pedidos é preciso ter clientes e produtos no banco.")
            return
        por_categoria = {}
        for p in produtos:
            por_categoria.setdefault(p["categoria"], []).append(p)
        tamanho = 5000
        for inicio in range(0, n_pedidos, tamanho):
            lote = [novo_pedido(clientes, produtos, por_categoria) for _ in range(min(tamanho, n_pedidos - inicio))]
            db.pedidos.insert_many(lote, ordered=False)
            barra.progress(0.4 + 0.6 * min(1.0, (inicio + tamanho) / n_pedidos),
                           text=f"Gerando pedidos... {min(inicio + tamanho, n_pedidos):,}".replace(",", "."))

    barra.progress(1.0, text="Concluído")
    st.success("Dados inseridos no MongoDB. Veja o resultado na página Visão geral.")


# ---------------------------------------------------------------------------
# Página: Visão geral
# ---------------------------------------------------------------------------
def pagina_visao_geral():
    db = get_db()
    st.header("Visão geral")
    colunas = st.columns(4)
    for coluna, nome in zip(colunas, ["clientes", "produtos", "pedidos", "estoque"]):
        coluna.metric(nome.capitalize(), milhar(db[nome].estimated_document_count()))

    estatisticas = db.command("dbstats")
    st.caption(
        f"Banco {DB_NAME}: {estatisticas.get('dataSize', 0) / 1024 / 1024:.1f} MB de dados e "
        f"{estatisticas.get('indexSize', 0) / 1024 / 1024:.1f} MB de índices."
    )

    if db.pedidos.estimated_document_count() == 0:
        st.info("O banco está vazio. Abra Inserir dados e gere dados fictícios para começar.")
        return

    resumo, _ = agregar("pedidos", [
        {"$match": {"status": {"$ne": "Cancelado"}}},
        {"$group": {"_id": None, "faturamento": {"$sum": "$total"}, "ticket": {"$avg": "$total"},
                    "pedidos": {"$sum": 1}}},
    ])
    if not resumo.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("Faturamento (sem cancelados)", brl(resumo.loc[0, "faturamento"]))
        c2.metric("Ticket médio", brl(resumo.loc[0, "ticket"]))
        c3.metric("Pedidos válidos", milhar(int(resumo.loc[0, "pedidos"])))

    esquerda, direita = st.columns(2)
    with esquerda:
        st.subheader("Pedidos por status")
        df, _ = agregar("pedidos", [{"$group": {"_id": "$status", "pedidos": {"$sum": 1}}}])
        st.bar_chart(df.rename(columns={"_id": "status"}).set_index("status")["pedidos"])
    with direita:
        st.subheader("Faturamento por região")
        df, _ = agregar("pedidos", [
            {"$match": {"status": {"$ne": "Cancelado"}}},
            {"$group": {"_id": "$regiao", "faturamento": {"$sum": "$total"}}},
            {"$sort": {"faturamento": -1}},
        ])
        st.bar_chart(df.rename(columns={"_id": "regiao"}).set_index("regiao")["faturamento"])


# ---------------------------------------------------------------------------
# Página: Inserir dados
# ---------------------------------------------------------------------------
def aba_gerar():
    st.write("Gera clientes, produtos, estoque por centro de distribuição e pedidos com dados fictícios "
             "(Faker pt_BR). Quanto maior o volume, mais evidente o ganho dos índices e das agregações.")
    with st.form("form_gerar"):
        c1, c2, c3 = st.columns(3)
        n_clientes = c1.number_input("Clientes", 0, 100_000, 5_000, step=1_000)
        n_produtos = c2.number_input("Produtos", 0, 5_000, 300, step=50)
        n_pedidos = c3.number_input("Pedidos", 0, 200_000, 20_000, step=5_000)
        limpar = st.checkbox("Apagar os dados existentes antes de gerar")
        enviado = st.form_submit_button("Gerar e inserir no MongoDB", type="primary")
    if enviado:
        gerar_dados(int(n_clientes), int(n_produtos), int(n_pedidos), limpar)


def aba_csv():
    st.write("Importe um arquivo CSV (por exemplo, um dataset do Kaggle). Cada linha vira um documento.")
    arquivo = st.file_uploader("Arquivo CSV", type=["csv"], key="up_importar")
    if not arquivo:
        return
    df = ler_csv(arquivo, "importar")
    st.dataframe(df.head(20))
    st.caption(f"{milhar(len(df))} linhas e {len(df.columns)} colunas.")
    destino = st.text_input("Coleção de destino", "kaggle_dados")
    if st.button("Inserir no MongoDB", type="primary", key="btn_importar"):
        if not destino.strip():
            st.error("Informe o nome da coleção.")
            return
        registros = [{k: py(v) for k, v in linha.items() if py(v) is not None} for linha in df.to_dict("records")]
        inserir_em_lotes(destino.strip(), registros)
        st.success(f"{milhar(len(registros))} documentos inseridos em {destino.strip()}.")


def aba_manual():
    db = get_db()
    tipo = st.radio("O que cadastrar?", ["Cliente", "Produto"], horizontal=True)
    if tipo == "Cliente":
        with st.form("form_cliente", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nome = c1.text_input("Nome completo")
            email = c2.text_input("E-mail")
            cpf = c1.text_input("CPF", placeholder="000.000.000-00")
            cidade = c2.text_input("Cidade")
            uf = c1.selectbox("Estado", sorted(ESTADO_REGIAO))
            consentimento = c2.checkbox("O cliente consentiu com o tratamento dos dados (LGPD)")
            if st.form_submit_button("Salvar cliente", type="primary"):
                if not nome.strip():
                    st.error("Informe o nome do cliente.")
                else:
                    db.clientes.insert_one({
                        "codigo_cliente": novo_codigo("clientes"), "nome": nome.strip(), "email": email.strip(),
                        "cpf": cpf.strip(), "cidade": cidade.strip(), "estado": uf, "regiao": ESTADO_REGIAO[uf],
                        "data_cadastro": datetime.now(), "consentimento_lgpd": consentimento,
                    })
                    st.success("Cliente salvo.")
    else:
        with st.form("form_produto", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nome = c1.text_input("Nome do produto")
            categoria = c2.selectbox("Categoria", list(CATALOGO))
            marca = c1.text_input("Marca")
            preco = c2.number_input("Preço (R$)", min_value=0.0, value=100.0, step=10.0)
            if st.form_submit_button("Salvar produto", type="primary"):
                if not nome.strip():
                    st.error("Informe o nome do produto.")
                else:
                    produto = {"sku": novo_codigo("produtos"), "nome": nome.strip(), "categoria": categoria,
                               "marca": marca.strip(), "preco": float(preco)}
                    db.produtos.insert_one(produto)
                    inserir_em_lotes("estoque", estoque_do_produto(produto))
                    st.success(f"Produto salvo com o código {produto['sku']} e estoque inicial nos 5 centros.")


def pagina_inserir():
    st.header("Inserir dados")
    aba1, aba2, aba3 = st.tabs(["Gerar dados fictícios", "Importar CSV", "Cadastro manual"])
    with aba1:
        aba_gerar()
    with aba2:
        aba_csv()
    with aba3:
        aba_manual()


# ---------------------------------------------------------------------------
# Página: Editar e excluir
# ---------------------------------------------------------------------------
def aplicar_alteracoes(colecao, original, editado, ignorar):
    col = get_db()[colecao]
    chave = CHAVE.get(colecao)
    ops = []
    contagem = {"alterados": 0, "inseridos": 0, "excluídos": 0}

    ids_editado = set(editado["_id"].dropna())
    for _id in set(original["_id"]) - ids_editado:
        ops.append(DeleteOne({"_id": _oid(_id)}))
        contagem["excluídos"] += 1

    antigos = original.set_index("_id")
    campos = [c for c in editado.columns if c != "_id" and c not in ignorar]
    for _, linha in editado.iterrows():
        _id = linha["_id"]
        if pd.isna(_id):
            doc = {c: py(linha[c]) for c in campos if py(linha[c]) is not None}
            if not doc:
                continue
            if chave and not doc.get(chave):
                doc[chave] = novo_codigo(colecao)
            ops.append(InsertOne(doc))
            contagem["inseridos"] += 1
        else:
            antigo = antigos.loc[_id]
            mudancas = {c: py(linha[c]) for c in campos if c in antigo.index and not iguais(antigo[c], linha[c])}
            if mudancas:
                ops.append(UpdateOne({"_id": _oid(_id)}, {"$set": mudancas}))
                contagem["alterados"] += 1

    if ops:
        col.bulk_write(ops, ordered=False)
    return contagem


def pagina_editar(mascarar):
    db = get_db()
    st.header("Editar e excluir dados")
    colecoes = sorted(db.list_collection_names())
    if not colecoes:
        st.info("Nenhuma coleção encontrada. Insira dados primeiro.")
        return

    c1, c2, c3 = st.columns([2, 1, 3])
    colecao = c1.selectbox("Coleção", colecoes, index=colecoes.index("clientes") if "clientes" in colecoes else 0)
    limite = c2.number_input("Máx. de linhas", 10, 2000, 200, step=50)
    filtro_txt = c3.text_input("Filtro (JSON do MongoDB)", "{}",
                               help='Exemplos: {"estado": "AM"}  ou  {"preco": {"$gt": 1000}}')
    try:
        filtro = json_util.loads(filtro_txt or "{}")
    except Exception:
        st.error("O filtro não é um JSON válido.")
        return

    docs = list(db[colecao].find(filtro).limit(int(limite)))
    if not docs:
        st.info("Nenhum documento encontrado com esse filtro.")
        return
    original, aninhadas = docs_para_df(docs)
    original = mascarar_df(original, colecao, mascarar)

    bloqueadas = set(aninhadas)
    if mascarar:
        bloqueadas |= set(DADOS_PESSOAIS.get(colecao, []))
        if colecao in DADOS_PESSOAIS:
            st.caption("Dados pessoais estão mascarados e bloqueados para edição. "
                       "Desative a máscara na barra lateral para alterá-los.")
    st.caption("Edite as células, adicione linhas no fim da tabela ou selecione linhas e use a lixeira para excluir. "
               "As mudanças só vão para o banco ao clicar em Salvar alterações.")

    versao = st.session_state.setdefault("versao_editor", 0)
    editado = st.data_editor(
        original, num_rows="dynamic", disabled=list(bloqueadas), column_config={"_id": None},
        key=f"editor_{colecao}_{versao}",
    )
    if st.button("Salvar alterações", type="primary"):
        resultado = aplicar_alteracoes(colecao, original, editado, bloqueadas)
        st.session_state["versao_editor"] = versao + 1
        st.session_state["aviso"] = (
            f"{resultado['alterados']} alterados, {resultado['inseridos']} inseridos, "
            f"{resultado['excluídos']} excluídos."
        )
        st.rerun()
    if "aviso" in st.session_state:
        st.success(st.session_state.pop("aviso"))

    with st.expander("Exclusão em massa por filtro"):
        total = db[colecao].count_documents(filtro)
        st.write(f"O filtro atual atinge {milhar(total)} documentos em {colecao}.")
        confirmar = st.checkbox("Entendo que a exclusão não pode ser desfeita")
        if st.button("Excluir documentos do filtro", disabled=not confirmar or total == 0):
            apagados = db[colecao].delete_many(filtro).deleted_count
            st.session_state["versao_editor"] = versao + 1
            st.session_state["aviso"] = f"{milhar(apagados)} documentos excluídos."
            st.rerun()


# ---------------------------------------------------------------------------
# Página: Concatenar dados
# ---------------------------------------------------------------------------
def pipeline_enriquecido():
    """Une pedidos, clientes e produtos ($lookup) e concatena cidade e estado ($concat).

    O nome do cliente fica de fora de propósito: a coleção resultante usa apenas o código do
    cliente (pseudonimização), reduzindo o risco ao compartilhar a base com marketing e BI.
    """
    return [
        {"$lookup": {"from": "clientes", "localField": "codigo_cliente",
                     "foreignField": "codigo_cliente", "as": "cliente"}},
        {"$unwind": "$cliente"},
        {"$unwind": "$itens"},
        {"$lookup": {"from": "produtos", "localField": "itens.sku", "foreignField": "sku", "as": "produto"}},
        {"$unwind": "$produto"},
        {"$project": {
            "_id": 0, "numero": 1, "data_pedido": 1, "status": 1, "canal": 1, "codigo_cliente": 1,
            "localizacao": {"$concat": ["$cliente.cidade", " - ", "$cliente.estado"]},
            "sku": "$itens.sku", "produto": "$produto.nome", "categoria": "$produto.categoria",
            "qtd": "$itens.qtd", "subtotal": {"$multiply": ["$itens.qtd", "$itens.preco"]},
        }},
    ]


def aba_lookup():
    db = get_db()
    st.write("Cruza **pedidos**, **clientes** e **produtos** em uma única visão, uma linha por item comprado. "
             "O campo `localizacao` é a concatenação de cidade e estado.")
    with st.expander("Ver o pipeline de agregação"):
        st.code(json.dumps(pipeline_enriquecido(), indent=2, ensure_ascii=False), language="json")
    amostra = st.slider("Linhas da pré-visualização", 10, 500, 100, step=10)
    if db.pedidos.estimated_document_count() == 0:
        st.info("Não há pedidos. Gere dados em Inserir dados.")
        return
    df, ms = agregar("pedidos", [{"$limit": amostra}] + pipeline_enriquecido())
    st.dataframe(df)
    st.caption(f"Consulta executada em {ms:.0f} ms.")
    if st.button("Salvar tudo na coleção pedidos_enriquecidos", type="primary"):
        with st.spinner("Processando todos os pedidos..."):
            _, ms = agregar("pedidos", pipeline_enriquecido() + [{"$out": "pedidos_enriquecidos"}])
        total = db.pedidos_enriquecidos.estimated_document_count()
        st.success(f"{milhar(total)} linhas salvas em pedidos_enriquecidos ({ms / 1000:.1f} s).")


def aba_empilhar():
    db = get_db()
    st.write("Empilha (concatena linhas) um CSV com os dados que já estão em uma coleção e insere só o que ainda não existe.")
    colecoes = sorted(db.list_collection_names())
    if not colecoes:
        st.info("Nenhuma coleção encontrada.")
        return
    colecao = st.selectbox("Coleção existente", colecoes, key="empilhar_colecao")
    arquivo = st.file_uploader("CSV com os novos registros", type=["csv"], key="up_empilhar")
    if not arquivo:
        return
    df_csv = ler_csv(arquivo, "empilhar")
    docs = list(db[colecao].find().limit(1000))
    df_db, _ = docs_para_df(docs)
    if "_id" in df_db.columns:
        df_db = df_db.drop(columns="_id")
    colunas_comuns = [c for c in df_csv.columns if c in df_db.columns] or list(df_csv.columns)
    chave = st.selectbox("Coluna-chave para evitar duplicatas", colunas_comuns,
                         index=colunas_comuns.index(CHAVE[colecao]) if CHAVE.get(colecao) in colunas_comuns else 0)

    combinado = pd.concat([df_db.assign(origem="MongoDB"), df_csv.assign(origem="CSV")], ignore_index=True)
    st.write(f"Resultado da concatenação: {milhar(len(df_db))} linhas do MongoDB (amostra) + {milhar(len(df_csv))} do CSV.")
    st.dataframe(combinado.tail(50))

    chaves_csv = [py(v) for v in df_csv[chave].dropna().unique()]
    existentes = {d[chave] for d in db[colecao].find({chave: {"$in": chaves_csv}}, {chave: 1})}
    novos = df_csv[~df_csv[chave].isin(existentes)].drop_duplicates(subset=chave)
    st.write(f"{milhar(len(novos))} registros novos e {milhar(len(df_csv) - len(novos))} já existentes ou repetidos.")
    if st.button("Inserir apenas os registros novos", type="primary", disabled=novos.empty):
        registros = [{k: py(v) for k, v in linha.items() if py(v) is not None} for linha in novos.to_dict("records")]
        inserir_em_lotes(colecao, registros)
        st.success(f"{milhar(len(registros))} registros inseridos em {colecao}.")


def pagina_concatenar():
    st.header("Concatenar dados")
    aba1, aba2 = st.tabs(["Pedidos + clientes + produtos", "Empilhar CSV com uma coleção"])
    with aba1:
        aba_lookup()
    with aba2:
        aba_empilhar()


# ---------------------------------------------------------------------------
# Página: Consultas
# ---------------------------------------------------------------------------
CONSULTAS = {
    "Faturamento por região": ("pedidos", [
        {"$match": {"status": {"$ne": "Cancelado"}}},
        {"$group": {"_id": "$regiao", "faturamento": {"$sum": "$total"}, "pedidos": {"$sum": 1}}},
        {"$sort": {"faturamento": -1}},
    ], "regiao", "faturamento"),
    "Faturamento por mês": ("pedidos", [
        {"$match": {"status": {"$ne": "Cancelado"}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$data_pedido"}},
                    "faturamento": {"$sum": "$total"}, "pedidos": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ], "mes", "faturamento"),
    "10 produtos mais vendidos": ("pedidos", [
        {"$unwind": "$itens"},
        {"$group": {"_id": "$itens.sku", "produto": {"$first": "$itens.nome"},
                    "unidades": {"$sum": "$itens.qtd"},
                    "receita": {"$sum": {"$multiply": ["$itens.qtd", "$itens.preco"]}}}},
        {"$sort": {"unidades": -1}},
        {"$limit": 10},
    ], "sku", "unidades"),
    "Atraso médio de entrega por região": ("pedidos", [
        {"$match": {"status": "Entregue"}},
        {"$group": {"_id": "$regiao",
                    "prazo_prometido_medio": {"$avg": "$prazo_prometido_dias"},
                    "prazo_real_medio": {"$avg": "$prazo_real_dias"},
                    "atraso_medio_dias": {"$avg": {"$subtract": ["$prazo_real_dias", "$prazo_prometido_dias"]}},
                    "entregas": {"$sum": 1}}},
        {"$sort": {"atraso_medio_dias": -1}},
    ], "regiao", "atraso_medio_dias"),
    "Estoque crítico por região": ("estoque", [
        {"$match": {"$expr": {"$lt": ["$quantidade", "$minimo"]}}},
        {"$group": {"_id": "$regiao", "skus_criticos": {"$sum": 1},
                    "deficit_total": {"$sum": {"$subtract": ["$minimo", "$quantidade"]}}}},
        {"$sort": {"skus_criticos": -1}},
    ], "regiao", "skus_criticos"),
    "Omnichannel: entrega x retirada em loja": ("pedidos", [
        {"$match": {"status": {"$ne": "Cancelado"}}},
        {"$group": {"_id": "$canal", "pedidos": {"$sum": 1}, "faturamento": {"$sum": "$total"}}},
        {"$sort": {"pedidos": -1}},
    ], "canal", "pedidos"),
    "Clientes com e sem consentimento (LGPD)": ("clientes", [
        {"$group": {"_id": "$consentimento_lgpd", "clientes": {"$sum": 1}}},
        {"$sort": {"clientes": -1}},
    ], "consentimento_lgpd", "clientes"),
}


def mostrar_resultado(df, ms, x, y):
    if df.empty:
        st.info("A consulta não retornou resultados. Verifique se há dados na coleção.")
        return
    df = df.rename(columns={"_id": x})
    df[x] = df[x].astype(str)
    df = df.round(2)
    st.dataframe(df)
    st.bar_chart(df.set_index(x)[y])
    st.caption(f"Consulta executada em {ms:.0f} ms.")


def aba_consultas_prontas():
    nome = st.selectbox("Consulta", list(CONSULTAS))
    colecao, pipeline, x, y = CONSULTAS[nome]
    df, ms = agregar(colecao, pipeline)
    mostrar_resultado(df, ms, x, y)
    with st.expander("Ver o pipeline de agregação"):
        st.code(json.dumps(pipeline, indent=2, ensure_ascii=False), language="json")


def aba_recomendacao():
    db = get_db()
    st.write("Personalização: produtos mais comprados **no mesmo pedido** que o produto escolhido. "
             "É a base de um bloco de 'quem comprou isto também comprou'.")
    produtos = list(db.produtos.find({}, {"_id": 0, "sku": 1, "nome": 1}).limit(500))
    if not produtos:
        st.info("Não há produtos cadastrados.")
        return
    escolhido = st.selectbox("Produto", produtos, format_func=lambda p: f"{p['nome']} ({p['sku']})")
    pipeline = [
        {"$match": {"itens.sku": escolhido["sku"], "status": {"$ne": "Cancelado"}}},
        {"$unwind": "$itens"},
        {"$match": {"itens.sku": {"$ne": escolhido["sku"]}}},
        {"$group": {"_id": "$itens.sku", "produto": {"$first": "$itens.nome"}, "vezes_juntos": {"$sum": 1}}},
        {"$sort": {"vezes_juntos": -1}},
        {"$limit": 10},
    ]
    df, ms = agregar("pedidos", pipeline)
    if df.empty:
        st.info("Esse produto ainda não aparece em pedidos com outros itens. Escolha outro ou gere mais pedidos.")
        return
    mostrar_resultado(df, ms, "sku", "vezes_juntos")


def aba_consulta_livre(mascarar):
    db = get_db()
    colecoes = sorted(db.list_collection_names())
    if not colecoes:
        st.info("Nenhuma coleção encontrada.")
        return
    c1, c2, c3 = st.columns([2, 1, 1])
    colecao = c1.selectbox("Coleção", colecoes, key="livre_colecao")
    limite = c2.number_input("Limite", 1, 5000, 100, key="livre_limite")
    campo_ordem = c3.text_input("Ordenar por (campo)", "", key="livre_ordem")
    filtro_txt = st.text_area("Filtro (JSON)", "{}", height=80, key="livre_filtro",
                              help='Ex.: {"regiao": "Norte", "status": "Entregue"}. '
                                   'Datas: {"data_pedido": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}')
    try:
        filtro = json_util.loads(filtro_txt or "{}")
    except Exception:
        st.error("O filtro não é um JSON válido.")
        return
    inicio = time.perf_counter()
    cursor = db[colecao].find(filtro)
    if campo_ordem.strip():
        cursor = cursor.sort(campo_ordem.strip(), -1)
    docs = list(cursor.limit(int(limite)))
    ms = (time.perf_counter() - inicio) * 1000
    df, _ = docs_para_df(docs)
    st.dataframe(mascarar_df(df, colecao, mascarar))
    st.caption(f"{len(docs)} documentos retornados em {ms:.0f} ms. "
               f"Total que atende ao filtro: {milhar(db[colecao].count_documents(filtro))}.")


def pagina_consultas(mascarar):
    st.header("Consultas")
    aba1, aba2, aba3 = st.tabs(["Análises prontas", "Recomendação de produtos", "Consulta livre"])
    with aba1:
        aba_consultas_prontas()
    with aba2:
        aba_recomendacao()
    with aba3:
        aba_consulta_livre(mascarar)


# ---------------------------------------------------------------------------
# Aplicação
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="E-Shop Brasil - Gestão de dados", page_icon="📦", layout="wide")

    try:
        get_db()
    except Exception as erro:
        st.error("Não foi possível conectar ao MongoDB.")
        st.code(str(erro))
        st.write("Verifique se o contêiner `mongo` está de pé (`docker compose ps`) e se a variável MONGO_URI está correta.")
        st.stop()

    with st.sidebar:
        st.title("E-Shop Brasil")
        st.caption("Gestão e análise de dados com MongoDB")
        pagina = st.radio("Seções", ["Visão geral", "Inserir dados", "Editar e excluir", "Concatenar dados", "Consultas"])
        st.divider()
        mascarar = st.toggle("Mascarar dados pessoais (LGPD)", value=True,
                             help="Oculta parte do nome, e-mail e CPF dos clientes nas telas.")

    if pagina == "Visão geral":
        pagina_visao_geral()
    elif pagina == "Inserir dados":
        pagina_inserir()
    elif pagina == "Editar e excluir":
        pagina_editar(mascarar)
    elif pagina == "Concatenar dados":
        pagina_concatenar()
    else:
        pagina_consultas(mascarar)


if __name__ == "__main__":
    main()
