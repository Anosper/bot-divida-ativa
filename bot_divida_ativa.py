"""
BOT DÍVIDA ATIVA (PGFN — Dados Abertos) -> Firebase -> Orion

SEM navegador e SEM captcha: usa os arquivos oficiais e gratuitos de
"Dados Abertos" da PGFN (https://dadosabertos.pgfn.gov.br/), os mesmos dados
do site Dívida Aberta.

Como funciona
-------------
* O Orion grava em  divida_ativa_controle/atual  {ativo, uf, valorMinimo,
  heartbeatEm}. O robô só trabalha com ativo == True e sinal de vida < 3 min.
* Na primeira vez que for preciso, o robô baixa os zips do trimestre mais
  recente (Não Previdenciário, Previdenciário e FGTS), soma a dívida de cada
  CNPJ (CPFs são ignorados) e guarda uma base local (SQLite) só com as
  empresas com dívida >= R$ 10.000, ordenadas da MENOR para a MAIOR (pula as já salvas).
* A cada ciclo (padrão 5 min) ele anda nessa lista (filtrada pela UF do
  Orion), pula quem já está no Firebase e, para as empresas NOVAS, completa
  Atividade Econômica e Domicílio consultando o CNPJ (BrasilAPI, grátis).
  Salva em  divida_ativa/{cnpj só números}  no mesmo formato de antes.
* O ponto onde parou fica em  divida_ativa_progresso/{UF}.

Uso
---
    python bot_divida_ativa.py                  # modo normal (obedece o Orion)
    python bot_divida_ativa.py --diagnostico    # baixa/monta a base e mostra
                                                # colunas, situações e top 5 (sem Firebase)
    python bot_divida_ativa.py --teste --uf RJ  # roda UM ciclo agora (grava no Firebase)

Variáveis (opcionais)
---------------------
    FIREBASE_SERVICE_ACCOUNT   JSON da chave (no GitHub Actions)
    DIVIDA_FIREBASE_KEY        caminho do .json (padrão firebase-service-account.json)
    DIVIDA_NOVOS_POR_CICLO=5   DIVIDA_INTERVALO_MIN=5   DIVIDA_MAX_HORAS=0
    DIVIDA_SAIR_INATIVO_MIN=0  DIVIDA_DADOS_DIR=dados_pgfn
    DIVIDA_FONTES=Nao_Previdenciario,Previdenciario,FGTS
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

BASE_URL = "https://dadosabertos.pgfn.gov.br"
VALOR_MINIMO = 10000.0  # piso da base (R$ 10.000,00)
ORDEM = "ASC"  # do MENOR para o MAIOR valor

NOVOS_POR_CICLO = int(os.getenv("DIVIDA_NOVOS_POR_CICLO", "5"))
INTERVALO_MIN = float(os.getenv("DIVIDA_INTERVALO_MIN", "5"))
MAX_HORAS = float(os.getenv("DIVIDA_MAX_HORAS", "0"))
SAIR_INATIVO_MIN = float(os.getenv("DIVIDA_SAIR_INATIVO_MIN", "0"))
DADOS_DIR = os.getenv("DIVIDA_DADOS_DIR", "dados_pgfn")
FONTES = [f.strip() for f in os.getenv("DIVIDA_FONTES", "Nao_Previdenciario,Previdenciario,FGTS").split(",") if f.strip()]
CHAVE_FIREBASE = os.getenv("DIVIDA_FIREBASE_KEY", "firebase-service-account.json")
JSON_FIREBASE = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()

HEARTBEAT_MAX_SEGUNDOS = 180
POLL_CONTROLE_SEGUNDOS = 15
TAMANHO_BLOCO = 50  # empresas lidas por vez da base local

COL_DIVIDA = "divida_ativa"
COL_CONTROLE = "divida_ativa_controle"
COL_PROGRESSO = "divida_ativa_progresso"

UA = {"User-Agent": "Mozilla/5.0 (compatible; OrionDividaAtiva/1.0)"}


def log(*args):
    print(datetime.now().strftime("[%H:%M:%S]"), *args, flush=True)


# ============================================================
# FIREBASE
# ============================================================
db = None


def iniciar_firebase():
    global db
    import firebase_admin
    from firebase_admin import credentials, firestore

    if JSON_FIREBASE:
        cred = credentials.Certificate(json.loads(JSON_FIREBASE))
    else:
        cred = credentials.Certificate(CHAVE_FIREBASE)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    log("Firebase conectado.")


def ler_controle():
    snap = db.collection(COL_CONTROLE).document("atual").get()
    return (snap.to_dict() or {}) if snap.exists else {}


def escrever_status(estado, mensagem, **extra):
    from firebase_admin import firestore

    bot = {"estado": estado, "mensagem": mensagem, "atualizadoEm": firestore.SERVER_TIMESTAMP}
    bot.update(extra)
    try:
        db.collection(COL_CONTROLE).document("atual").set({"bot": bot}, merge=True)
    except Exception as erro:
        log("Aviso: não consegui escrever o status:", erro)


def ids_existentes(ids):
    if not ids:
        return set()
    refs = [db.collection(COL_DIVIDA).document(i) for i in ids]
    return {s.id for s in db.get_all(refs) if s.exists}


def salvar_divida(dados):
    db.collection(COL_DIVIDA).document(dados["cnpjNumeros"]).set(dados)


# progresso separado do antigo (que ia do maior para o menor, piso 100 mil)
PREFIXO_PROGRESSO = "asc10k_"


def ler_progresso(uf, trimestre):
    snap = db.collection(COL_PROGRESSO).document(PREFIXO_PROGRESSO + (uf or "TODAS")).get()
    if snap.exists:
        d = snap.to_dict() or {}
        if d.get("trimestre") == trimestre:
            return int(d.get("posicao", 0) or 0)
    return 0


def salvar_progresso(uf, trimestre, posicao):
    db.collection(COL_PROGRESSO).document(PREFIXO_PROGRESSO + (uf or "TODAS")).set(
        {"posicao": int(posicao), "trimestre": trimestre, "atualizadoEm": datetime.now(timezone.utc)},
        merge=True,
    )


# ============================================================
# UTILITÁRIOS
# ============================================================
def sem_acento(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c))


def norm(t):
    return re.sub(r"\s+", " ", sem_acento(t).upper().strip())


def so_digitos(t):
    return re.sub(r"\D", "", t or "")


def formatar_cnpj(d):
    return f"{d[0:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}"


def valor_para_float(t):
    t = (t or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return 0.0
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def brl(v):
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def http_status(url, metodo="HEAD", timeout=60):
    req = urllib.request.Request(url, method=metodo, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ============================================================
# DOWNLOAD E BASE LOCAL
# ============================================================
def descobrir_trimestre():
    """Acha a pasta mais recente com arquivos (ex.: 2026_trimestre_03)."""
    hoje = datetime.now()
    ano, tri = hoje.year, (hoje.month - 1) // 3 + 1
    for _ in range(12):
        nome = f"{ano}_trimestre_{tri:02d}"
        if http_status(f"{BASE_URL}/{nome}/Dados_abertos_{FONTES[0]}.zip") == 200:
            return nome
        tri -= 1
        if tri == 0:
            tri, ano = 4, ano - 1
    raise RuntimeError("não encontrei nenhuma pasta de dados abertos da PGFN")


def baixar(url, destino):
    log("Baixando", url)
    req = urllib.request.Request(url, headers=UA)
    ultimo = time.time()
    with urllib.request.urlopen(req, timeout=120) as r, open(destino, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        lido = 0
        while True:
            bloco = r.read(1024 * 1024)
            if not bloco:
                break
            f.write(bloco)
            lido += len(bloco)
            if time.time() - ultimo > 30:
                log(f"  ... {lido // 1048576} MB" + (f" de {total // 1048576} MB" if total else ""))
                ultimo = time.time()
    log(f"  concluído ({os.path.getsize(destino) // 1048576} MB).")


def detectar_encoding(zf, nome):
    with zf.open(nome) as f:
        amostra = f.read(200000)
    for fim in range(0, 5):  # tolera corte no meio de um caractere
        try:
            (amostra[: len(amostra) - fim] if fim else amostra).decode("utf-8")
            return "utf-8-sig"
        except UnicodeDecodeError:
            continue
    return "latin-1"


ALIASES = {
    "doc": ["CPF_CNPJ", "CNPJ_CPF", "CPF/CNPJ", "NI", "CPF_CNPJ_DEVEDOR"],
    "nome": ["NOME_DEVEDOR", "NOME", "NOME_RAZAO_SOCIAL", "RAZAO_SOCIAL"],
    "uf": ["UF_DEVEDOR", "UF_UNIDADE_RESPONSAVEL", "UF", "UF_INSCRICAO"],
    "valor": ["VALOR_CONSOLIDADO", "VALOR_ATUAL_INSCRITO", "VALOR"],
    "situacao": ["SITUACAO_INSCRICAO", "SITUACAO", "TIPO_SITUACAO_INSCRICAO"],
}


def mapear_colunas(cabecalho):
    cab = [norm(c).replace(" ", "_") for c in cabecalho]
    mapa = {}
    for chave, nomes in ALIASES.items():
        for n in nomes:
            if n in cab:
                mapa[chave] = cab.index(n)
                break
    return mapa, cab


def situacao_ativa(s):
    s = norm(s)
    return s.startswith("ATIVA") or s.startswith("EM COBRANCA")


def processar_zip(caminho_zip, con, diag=None):
    linhas_pj = 0
    with zipfile.ZipFile(caminho_zip) as zf:
        for nome in zf.namelist():
            if not nome.lower().endswith(".csv"):
                continue
            enc = detectar_encoding(zf, nome)
            with zf.open(nome) as bruto:
                texto = io.TextIOWrapper(bruto, encoding=enc, errors="replace", newline="")
                leitor = csv.reader(texto, delimiter=";", quotechar='"')
                try:
                    cabecalho = next(leitor)
                except StopIteration:
                    continue
                mapa, cab = mapear_colunas(cabecalho)
                if diag is not None and not diag.get("cabecalho"):
                    diag["cabecalho"] = cab
                    diag["arquivo"] = nome
                    diag["encoding"] = enc
                    diag["mapa"] = mapa
                if "doc" not in mapa or "valor" not in mapa:
                    log(f"  Arquivo {nome}: colunas não reconhecidas {cab} — ignorado.")
                    continue
                i_doc, i_val = mapa["doc"], mapa["valor"]
                i_nome, i_uf, i_sit = mapa.get("nome"), mapa.get("uf"), mapa.get("situacao")
                lote = {}
                for linha in leitor:
                    if len(linha) <= max(i_doc, i_val):
                        continue
                    d = so_digitos(linha[i_doc])
                    if len(d) != 14 or "*" in linha[i_doc]:
                        continue  # CPF (mascarado) ou lixo
                    v = valor_para_float(linha[i_val])
                    sit = linha[i_sit] if i_sit is not None and i_sit < len(linha) else ""
                    ativa = situacao_ativa(sit) if i_sit is not None else True
                    if diag is not None:
                        diag.setdefault("situacoes", {})
                        diag["situacoes"][sit] = diag["situacoes"].get(sit, 0) + 1
                    uf = (linha[i_uf].strip().upper() if i_uf is not None and i_uf < len(linha) else "")
                    nm = linha[i_nome].strip() if i_nome is not None and i_nome < len(linha) else ""
                    reg = lote.get(d)
                    if reg is None:
                        lote[d] = [nm, uf, v if ativa else 0.0, v]
                    else:
                        reg[3] += v
                        if ativa:
                            reg[2] += v
                    linhas_pj += 1
                    if len(lote) >= 50000:
                        gravar_lote(con, lote)
                        lote = {}
                gravar_lote(con, lote)
            log(f"  {nome}: ok ({linhas_pj} inscrições de CNPJ até agora)")
    return linhas_pj


def gravar_lote(con, lote):
    if not lote:
        return
    con.executemany(
        "INSERT INTO agg(cnpj,nome,uf,total_ativa,total_all) VALUES(?,?,?,?,?) "
        "ON CONFLICT(cnpj) DO UPDATE SET total_ativa=total_ativa+excluded.total_ativa, "
        "total_all=total_all+excluded.total_all",
        [(k, v[0], v[1], v[2], v[3]) for k, v in lote.items()],
    )
    con.commit()


def caminho_base():
    return os.path.join(DADOS_DIR, "base.sqlite")


def ler_meta():
    if not os.path.exists(caminho_base()):
        return {}
    try:
        con = sqlite3.connect(caminho_base())
        meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
        con.close()
        return meta
    except Exception:
        return {}


def montar_base(trimestre, diag=None):
    os.makedirs(DADOS_DIR, exist_ok=True)
    temp = caminho_base() + ".tmp"
    if os.path.exists(temp):
        os.remove(temp)
    con = sqlite3.connect(temp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(
        "CREATE TABLE agg(cnpj TEXT PRIMARY KEY, nome TEXT, uf TEXT, total_ativa REAL, total_all REAL) WITHOUT ROWID"
    )
    for fonte in FONTES:
        url = f"{BASE_URL}/{trimestre}/Dados_abertos_{fonte}.zip"
        zip_local = os.path.join(DADOS_DIR, f"{fonte}.zip")
        try:
            baixar(url, zip_local)
            processar_zip(zip_local, con, diag)
        except Exception as erro:
            log(f"ERRO ao processar {fonte}: {type(erro).__name__} {erro}")
            con.close()
            raise
        finally:
            if os.path.exists(zip_local):
                os.remove(zip_local)
    log("Filtrando empresas com dívida >= R$ 10.000 ...")
    con.execute("DELETE FROM agg WHERE total_all < ?", (VALOR_MINIMO,))
    con.execute("CREATE INDEX i_ativa ON agg(total_ativa DESC)")
    con.execute("CREATE INDEX i_all ON agg(total_all DESC)")
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    n_ativa = con.execute("SELECT COUNT(*) FROM agg WHERE total_ativa >= ?", (VALOR_MINIMO,)).fetchone()[0]
    n_all = con.execute("SELECT COUNT(*) FROM agg").fetchone()[0]
    # se o filtro de situação não bater com os dados, usa o total geral
    usar = "total_ativa" if n_ativa > 0 else "total_all"
    con.executemany(
        "INSERT INTO meta VALUES(?,?)",
        [("trimestre", trimestre), ("minimo", str(VALOR_MINIMO)), ("coluna", usar), ("n_ativa", str(n_ativa)), ("n_all", str(n_all)),
         ("criado", datetime.now().isoformat())],
    )
    con.commit()
    con.close()
    os.replace(temp, caminho_base())
    log(f"Base pronta: {n_all} empresas >= R$ 10 mil ({n_ativa} em cobrança ativa). Trimestre {trimestre}. Coluna usada: {usar}.")


def garantir_base(ultimo_trimestre_checado):
    """Garante que a base local existe e está no trimestre mais recente."""
    meta = ler_meta()
    base_ok = meta.get("minimo") == str(VALOR_MINIMO)
    if meta and base_ok and ultimo_trimestre_checado.get("ok"):
        return meta
    mais_recente = descobrir_trimestre()
    ultimo_trimestre_checado["ok"] = True
    if meta.get("trimestre") == mais_recente and base_ok:
        return meta
    log(f"Montando base local do trimestre {mais_recente} (demora alguns minutos)...")
    montar_base(mais_recente)
    return ler_meta()


def ler_bloco(coluna, uf, minimo, posicao, quantidade):
    con = sqlite3.connect(caminho_base())
    sql = f"SELECT cnpj,nome,uf,{coluna} FROM agg WHERE {coluna} >= ?"
    args = [minimo]
    if uf:
        sql += " AND uf = ?"
        args.append(uf)
    sql += f" ORDER BY {coluna} {ORDEM}, cnpj LIMIT ? OFFSET ?"
    args += [quantidade, posicao]
    linhas = con.execute(sql, args).fetchall()
    con.close()
    return linhas


# ============================================================
# CONSULTA DO CNPJ (Atividade Econômica e Domicílio) — APIs gratuitas
# ============================================================
def _get_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def consultar_cnpj(cnpj):
    for tentativa in range(3):
        try:
            d = _get_json(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}")
            municipio = (d.get("municipio") or "").strip()
            uf = (d.get("uf") or "").strip()
            return {
                "razaoSocial": d.get("razao_social"),
                "atividadeEconomica": (d.get("cnae_fiscal_descricao") or "").strip() or None,
                "domicilio": f"{municipio} - {uf}" if municipio else None,
                "uf": uf or None,
                "situacaoCadastral": d.get("descricao_situacao_cadastral"),
            }
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(8 * (tentativa + 1))
                continue
            if e.code == 404:
                break
        except Exception:
            time.sleep(3)
    try:  # plano B
        d = _get_json(f"https://publica.cnpj.ws/cnpj/{cnpj}")
        est = d.get("estabelecimento") or {}
        ativ = (est.get("atividade_principal") or {}).get("descricao")
        cidade = (est.get("cidade") or {}).get("nome")
        uf = (est.get("estado") or {}).get("sigla")
        return {
            "razaoSocial": d.get("razao_social"),
            "atividadeEconomica": ativ,
            "domicilio": f"{cidade} - {uf}" if cidade else None,
            "uf": uf,
            "situacaoCadastral": est.get("situacao_cadastral"),
        }
    except Exception:
        return {}


# ============================================================
# CICLO
# ============================================================
def executar_ciclo(uf, ctrl_min, ainda_ativo, estado_base):
    meta = garantir_base(estado_base)
    coluna = meta.get("coluna", "total_ativa")
    trimestre = meta.get("trimestre", "")
    minimo = VALOR_MINIMO  # fixo: 10 mil (ignora o valor antigo gravado no controle)

    posicao = ler_progresso(uf, trimestre)
    novos = 0
    lidos = 0
    while novos < NOVOS_POR_CICLO and ainda_ativo():
        bloco = ler_bloco(coluna, uf, minimo, posicao, TAMANHO_BLOCO)
        if not bloco:
            log(f"Fim da lista ({uf or 'todas as UFs'}): nada mais a coletar neste trimestre.")
            break
        ja = ids_existentes([b[0] for b in bloco])
        pendentes = [b for b in bloco if b[0] not in ja]
        log(f"Posição {posicao}: {len(bloco)} empresa(s) lidas, {len(pendentes)} nova(s).")

        consumidos = 0
        for cnpj, nome, uf_emp, valor in bloco:
            if novos >= NOVOS_POR_CICLO or not ainda_ativo():
                break
            consumidos += 1
            if cnpj in ja:
                continue
            extra = consultar_cnpj(cnpj)
            time.sleep(1.2)
            dados = {
                "cnpj": formatar_cnpj(cnpj),
                "cnpjNumeros": cnpj,
                "nomeEmpresarial": (nome or extra.get("razaoSocial") or "").strip(),
                "domicilio": extra.get("domicilio") or (f"{uf_emp}" if uf_emp else None),
                "uf": extra.get("uf") or uf_emp or None,
                "atividadeEconomica": extra.get("atividadeEconomica"),
                "situacaoCadastral": extra.get("situacaoCadastral"),
                "valorTotal": round(valor, 2),
                "valorTotalTexto": brl(valor),
                "capturadoEm": datetime.now(timezone.utc),
                "origem": f"PGFN - Dados Abertos ({trimestre})",
                "ufBusca": uf or "",
                "posicaoNaLista": posicao + consumidos,
            }
            salvar_divida(dados)
            novos += 1
            log(f"✔ Salvo: {dados['nomeEmpresarial']} | {dados['cnpj']} | {dados['valorTotalTexto']} | {dados['atividadeEconomica']}")

        posicao += consumidos
        lidos += consumidos
        salvar_progresso(uf, trimestre, posicao)
    return novos


# ============================================================
# LOOP PRINCIPAL
# ============================================================
def controle_ativo(ctrl):
    if not ctrl or ctrl.get("ativo") is not True:
        return False
    hb = ctrl.get("heartbeatEm")
    if hb is None:
        return False
    try:
        idade = (datetime.now(timezone.utc) - hb).total_seconds()
    except Exception:
        return False
    return idade <= HEARTBEAT_MAX_SEGUNDOS


def motivo_inativo(ctrl):
    if not ctrl:
        return "documento divida_ativa_controle/atual não existe (o Orion ainda não ligou, ou grava em outro projeto)"
    if ctrl.get("ativo") is not True:
        return "captação DESLIGADA no Orion (ativo != true)"
    hb = ctrl.get("heartbeatEm")
    if hb is None:
        return "sem sinal de vida (heartbeatEm) do Orion"
    try:
        idade = int((datetime.now(timezone.utc) - hb).total_seconds())
    except Exception:
        return "heartbeat ilegível"
    return f"sinal de vida do Orion antigo ({idade}s) — a aba do Orion precisa ficar aberta"


def loop_principal():
    from firebase_admin import firestore

    inicio = time.time()
    ultimo_ativo = time.time()
    ultimo_aviso = 0.0
    ultimo_ciclo = 0.0
    uf_anterior = None
    estado_base = {}
    log("Robô de Dívida Ativa (dados abertos PGFN) iniciado. Aguardando o Orion ligar a captação...")
    escrever_status("aguardando", "Aguardando a captação ser ligada no Orion.")

    while True:
        try:
            if MAX_HORAS and (time.time() - inicio) >= MAX_HORAS * 3600:
                log("Tempo máximo atingido — encerrando (o próximo agendamento continua).")
                escrever_status("pausado", "Robô reiniciando (limite de tempo do servidor).")
                break
            ctrl = ler_controle()
            ativo = controle_ativo(ctrl)
            if ativo:
                ultimo_ativo = time.time()
            elif SAIR_INATIVO_MIN and (time.time() - ultimo_ativo) >= SAIR_INATIVO_MIN * 60:
                log("Inativo por muito tempo — encerrando para economizar minutos.")
                break

            if not ativo:
                if time.time() - ultimo_aviso >= 120:
                    log("Aguardando... motivo:", motivo_inativo(ctrl))
                    ultimo_aviso = time.time()
                time.sleep(POLL_CONTROLE_SEGUNDOS)
                continue

            uf = (ctrl.get("uf") or "").strip().upper()
            vencido = (time.time() - ultimo_ciclo) >= INTERVALO_MIN * 60
            if uf != uf_anterior or vencido:
                uf_anterior = uf
                log(f"=== Novo ciclo | UF: {uf or 'todas'} ===")
                precisa_base = not ler_meta()
                escrever_status(
                    "coletando",
                    "Baixando a base da PGFN (só na primeira vez, leva alguns minutos)..." if precisa_base
                    else f"Coletando ({uf or 'todas as UFs'})...",
                )
                try:
                    novos = executar_ciclo(uf, ctrl.get("valorMinimo"), lambda: controle_ativo(ler_controle()), estado_base)
                    escrever_status(
                        "aguardando",
                        f"Ciclo concluído: {novos} nova(s) empresa(s). Próximo em {INTERVALO_MIN:g} min.",
                        ultimoCicloEm=firestore.SERVER_TIMESTAMP,
                        totalColetados=firestore.Increment(novos),
                    )
                except Exception as erro:
                    texto = f"{type(erro).__name__} {erro}"
                    log("ERRO no ciclo:", texto)
                    if "Quota" in texto or "ResourceExhausted" in texto:
                        raise
                    escrever_status("erro", f"Erro no ciclo: {erro}. Tentando de novo no próximo ciclo.")
                ultimo_ciclo = time.time()

            time.sleep(POLL_CONTROLE_SEGUNDOS)

        except KeyboardInterrupt:
            log("Encerrado pelo usuário.")
            escrever_status("pausado", "Robô desligado.")
            break
        except Exception as erro:
            texto = f"{type(erro).__name__} {erro}"
            if "429" in texto or "Quota" in texto or "ResourceExhausted" in texto:
                log("Cota do Firebase esgotada neste projeto — aguardando 10 min (zera por volta das 05h de Brasília).")
                time.sleep(600)
            else:
                log("ERRO no loop principal:", texto)
                time.sleep(POLL_CONTROLE_SEGUNDOS)


# ============================================================
# MODOS DE TESTE
# ============================================================
def modo_diagnostico():
    trimestre = descobrir_trimestre()
    log("Trimestre mais recente:", trimestre)
    diag = {}
    montar_base(trimestre, diag)
    log("Arquivo de exemplo:", diag.get("arquivo"), "| encoding:", diag.get("encoding"))
    log("Colunas:", diag.get("cabecalho"))
    log("Mapeamento:", diag.get("mapa"))
    sits = sorted((diag.get("situacoes") or {}).items(), key=lambda x: -x[1])[:15]
    log("Situações mais comuns:", sits)
    meta = ler_meta()
    log("Meta:", meta)
    log("Top 5 (todas as UFs):")
    for b in ler_bloco(meta.get("coluna", "total_ativa"), "", VALOR_MINIMO, 0, 5):
        log("  ", b[0], b[1], b[2], brl(b[3]))
    log("Exemplo consulta CNPJ:", consultar_cnpj(ler_bloco(meta.get("coluna"), "", VALOR_MINIMO, 0, 1)[0][0]))


def modo_teste(uf):
    iniciar_firebase()
    novos = executar_ciclo((uf or "").upper(), VALOR_MINIMO, lambda: True, {})
    log(f"Teste concluído: {novos} empresa(s) nova(s) salva(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnostico", action="store_true")
    ap.add_argument("--teste", action="store_true")
    ap.add_argument("--uf", default="")
    args = ap.parse_args()

    if args.diagnostico:
        modo_diagnostico()
    elif args.teste:
        modo_teste(args.uf)
    else:
        iniciar_firebase()
        loop_principal()
