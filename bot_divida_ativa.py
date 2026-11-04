"""
BOT DÍVIDA ATIVA (PGFN — Dívida Aberta) -> Firebase -> Orion

Como funciona
-------------
* O Orion tem a tela "Dívida Ativa". Quando um usuário LIGA a captação, o
  Orion grava em  divida_ativa_controle/atual  {ativo, uf, heartbeatEm, ...}.
* Este robô fica olhando esse documento. Só trabalha enquanto
  ativo == True  E  o "sinal de vida" (heartbeatEm) tem menos de 3 minutos
  (o Orion renova a cada 60s; se a aba for fechada, o robô para sozinho).
* A cada ciclo (padrão: 5 min) ele pesquisa no site
  https://www.dividaaberta.pgfn.gov.br/consultar-devedores com valor mínimo
  R$ 100.000,00 (e a UF escolhida), lê a lista (10 por página, do maior para
  o menor), IGNORA CPFs (só CNPJ), pula quem já está no Firebase, e abre o
  detalhe (seta) dos novos para pegar:
      Nome Empresarial, CNPJ, Domicílio do Devedor, Atividade Econômica,
      Valor Total da dívida
  Se TODOS os CNPJs de uma página já foram coletados, passa para a próxima
  página. Em cada ciclo coleta no máximo NOVOS_POR_CICLO empresas (padrão 5),
  para ir "aos poucos".
* Grava em  divida_ativa/{cnpj só números}  e o progresso (página) em
  divida_ativa_progresso/{UF}  para continuar de onde parou.

Uso
---
    python bot_divida_ativa.py                  # modo normal (obedece o Orion)
    python bot_divida_ativa.py --diagnostico    # só abre o site, filtra e
                                                # mostra o que enxergou (sem Firebase)
    python bot_divida_ativa.py --teste --uf RJ  # roda UM ciclo agora, ignorando
                                                # o Orion (grava no Firebase)

.env (opcional)
---------------
    DIVIDA_HEADLESS=false        # true = sem janela
    DIVIDA_NOVOS_POR_CICLO=5
    DIVIDA_INTERVALO_MIN=5
    DIVIDA_PAGINAS_POR_CICLO=40  # máx. de páginas "já coletadas" puladas por ciclo
    DIVIDA_FIREBASE_KEY=firebase-service-account.json   # MESMO projeto do Orion
"""
import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

URL_CONSULTA = "https://www.dividaaberta.pgfn.gov.br/consultar-devedores"
VALOR_MINIMO = 100000  # R$ 100.000,00

HEADLESS = os.getenv("DIVIDA_HEADLESS", "false").strip().lower() in ("1", "true", "sim", "yes")
NOVOS_POR_CICLO = int(os.getenv("DIVIDA_NOVOS_POR_CICLO", "5"))
INTERVALO_MIN = float(os.getenv("DIVIDA_INTERVALO_MIN", "5"))
PAGINAS_POR_CICLO = int(os.getenv("DIVIDA_PAGINAS_POR_CICLO", "40"))
CHAVE_FIREBASE = os.getenv("DIVIDA_FIREBASE_KEY", "firebase-service-account.json")
# No GitHub Actions: o JSON da chave vem num secret (variável de ambiente)
JSON_FIREBASE = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
# Tempo máximo de execução (o Actions gratuito corta em 6h) e saída por inatividade
MAX_HORAS = float(os.getenv("DIVIDA_MAX_HORAS", "0"))  # 0 = sem limite
SAIR_INATIVO_MIN = float(os.getenv("DIVIDA_SAIR_INATIVO_MIN", "0"))  # 0 = nunca sai

HEARTBEAT_MAX_SEGUNDOS = 180
POLL_CONTROLE_SEGUNDOS = 15

COL_DIVIDA = "divida_ativa"
COL_CONTROLE = "divida_ativa_controle"
COL_PROGRESSO = "divida_ativa_progresso"

PADRAO_CNPJ = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")

ESTADOS = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia",
    "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe",
    "TO": "Tocantins",
}


def log(*args):
    print(datetime.now().strftime("[%H:%M:%S]"), *args, flush=True)


# ============================================================
# FIREBASE (isolado em funções simples — fácil de trocar em testes)
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
    return snap.to_dict() or {} if snap.exists else {}


def escrever_status(estado, mensagem, **extra):
    """Escreve o status do robô em controle/atual.bot (o Orion mostra na tela)."""
    from firebase_admin import firestore

    bot = {"estado": estado, "mensagem": mensagem, "atualizadoEm": firestore.SERVER_TIMESTAMP}
    bot.update(extra)
    try:
        db.collection(COL_CONTROLE).document("atual").set({"bot": bot}, merge=True)
    except Exception as erro:
        log("Aviso: não consegui escrever o status:", erro)


def ids_existentes(ids):
    """Devolve o conjunto de ids (CNPJ só números) que já estão no Firebase."""
    if not ids:
        return set()
    refs = [db.collection(COL_DIVIDA).document(i) for i in ids]
    return {s.id for s in db.get_all(refs) if s.exists}


def salvar_divida(dados):
    db.collection(COL_DIVIDA).document(dados["cnpjNumeros"]).set(dados)


def ler_progresso(uf):
    snap = db.collection(COL_PROGRESSO).document(uf or "TODAS").get()
    if snap.exists:
        return int((snap.to_dict() or {}).get("pagina", 1) or 1)
    return 1


def salvar_progresso(uf, pagina):
    db.collection(COL_PROGRESSO).document(uf or "TODAS").set(
        {"pagina": int(pagina), "atualizadoEm": datetime.now(timezone.utc)}, merge=True
    )


# ============================================================
# UTILITÁRIOS DE TEXTO
# ============================================================
def so_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def valor_para_float(texto):
    """'R$ 38.673.421.095,65' -> 38673421095.65"""
    if not texto:
        return None
    t = re.sub(r"[^\d,.]", "", texto).replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_detalhe(texto):
    """Lê o texto da janela de detalhe (pelos rótulos) — não depende de seletor."""
    texto = (texto or "").replace("\xa0", " ")

    def pegar(rotulo_regex):
        m = re.search(rotulo_regex + r"\s*:\s*(.+)", texto, re.IGNORECASE)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else None

    nome = pegar(r"Nome\s+Empresarial")
    cnpj_m = re.search(r"CNPJ\s*:\s*(" + PADRAO_CNPJ.pattern + ")", texto, re.IGNORECASE)
    cnpj = cnpj_m.group(1) if cnpj_m else None
    domicilio = pegar(r"Domic[ií]lio\s+do\s+Devedor")
    atividade = pegar(r"Atividade\s+Econ[ôo]mica")
    valor_m = re.search(r"Valor\s+Total\s+da\s+d[ií]vida\s*:\s*(R\$\s*[\d.,]+)", texto, re.IGNORECASE)
    valor_txt = valor_m.group(1).strip() if valor_m else None

    uf = None
    if domicilio:
        m = re.search(r"-\s*([A-Z]{2})\s*$", domicilio)
        uf = m.group(1) if m else None

    return {
        "nomeEmpresarial": nome,
        "cnpj": cnpj,
        "cnpjNumeros": so_digitos(cnpj) if cnpj else None,
        "domicilio": domicilio,
        "uf": uf,
        "atividadeEconomica": atividade,
        "valorTotalTexto": valor_txt,
        "valorTotal": valor_para_float(valor_txt),
    }


# ============================================================
# NAVEGAÇÃO NO SITE
# ============================================================
class Sessao:
    def __init__(self, p):
        self.p = p
        self.navegador = None
        self.pagina = None
        self.uf = None
        self.pagina_num = 1

    def abrir(self):
        self.fechar()
        self.navegador = self.p.chromium.launch(headless=HEADLESS, args=["--disable-http2"])
        ctx = self.navegador.new_context(viewport={"width": 1366, "height": 900})
        self.pagina = ctx.new_page()
        self.uf = None
        self.pagina_num = 1

    def fechar(self):
        try:
            if self.navegador:
                self.navegador.close()
        except Exception:
            pass
        self.navegador = None
        self.pagina = None
        self.uf = None


def fechar_avisos(pagina):
    for texto in ["Aceitar", "Concordo", "Concordar", "Entendi", "Ok", "Fechar"]:
        try:
            b = pagina.get_by_role("button", name=re.compile(rf"^\s*{texto}\s*$", re.IGNORECASE))
            if b.count() > 0 and b.first.is_visible():
                b.first.click(timeout=2000)
                pagina.wait_for_timeout(300)
        except Exception:
            pass


def _campo_valor_minimo(pagina):
    cand = pagina.get_by_label(re.compile(r"^\s*M[ií]nimo", re.IGNORECASE))
    try:
        if cand.count() > 0:
            return cand.first
    except Exception:
        pass
    rotulo = pagina.get_by_text(re.compile(r"Faixa de Valor", re.IGNORECASE))
    if rotulo.count() > 0:
        c = rotulo.first.locator("xpath=following::input[1]")
        if c.count() > 0:
            return c.first
    return None


def preencher_valor_minimo(pagina, valor=VALOR_MINIMO):
    campo = _campo_valor_minimo(pagina)
    if campo is None:
        log("ERRO: não achei o campo 'Mínimo' da Faixa de Valor.")
        return False
    alvo_digitos = str(int(valor)) + "00"  # centavos (máscara monetária)
    try:
        campo.click()
        campo.press("Control+A")
        campo.press("Backspace")
        campo.type(alvo_digitos, delay=40)
        campo.press("Tab")
        pagina.wait_for_timeout(300)
        atual = campo.input_value()
        if so_digitos(atual) != alvo_digitos:
            # sem máscara automática: escreve já formatado
            campo.fill("100.000,00")
            campo.press("Tab")
            atual = campo.input_value()
        log("Valor mínimo no campo:", atual)
        return so_digitos(atual) in (alvo_digitos, str(int(valor)))
    except Exception as erro:
        log("ERRO ao preencher valor mínimo:", erro)
        return False


def selecionar_uf(pagina, uf):
    if not uf:
        return True  # "Todas as UFs" já é o padrão
    nome_estado = ESTADOS.get(uf, uf)
    campo = pagina.get_by_label(re.compile(r"Estado\s*\(UF\)|^\s*UF\s*$", re.IGNORECASE))
    if campo.count() == 0:
        r = pagina.get_by_text(re.compile(r"Estado\s*\(UF\)", re.IGNORECASE))
        campo = r.first.locator("xpath=following::select[1]") if r.count() > 0 else campo
    if campo.count() == 0:
        log("ERRO: não achei o seletor de UF.")
        return False
    alvo = campo.first
    # 1) <select> nativo: tenta value (UF), depois rótulo
    for tentativa in (
        lambda: alvo.select_option(value=uf),
        lambda: alvo.select_option(label=uf),
        lambda: alvo.select_option(label=nome_estado),
        lambda: alvo.select_option(label=re.compile(rf"(^|\W){uf}(\W|$)|{re.escape(nome_estado)}", re.IGNORECASE)),
    ):
        try:
            tentativa()
            log("UF selecionada:", uf)
            return True
        except Exception:
            continue
    # 2) lista customizada: abre e clica na opção
    try:
        alvo.click()
        pagina.wait_for_timeout(400)
        opc = pagina.get_by_role("option", name=re.compile(rf"^\s*({uf}|{re.escape(nome_estado)})\b", re.IGNORECASE))
        if opc.count() > 0:
            opc.first.click()
            log("UF selecionada (lista customizada):", uf)
            return True
    except Exception as erro:
        log("Erro ao selecionar UF:", erro)
    log("ERRO: não consegui selecionar a UF", uf)
    return False


def clicar_filtrar(pagina):
    b = pagina.get_by_role("button", name=re.compile(r"filtrar\s+devedores", re.IGNORECASE))
    if b.count() == 0:
        b = pagina.get_by_text(re.compile(r"filtrar\s+devedores", re.IGNORECASE))
    if b.count() == 0:
        log("ERRO: botão 'Filtrar devedores' não encontrado.")
        return False
    b.first.click()
    return True


def _linhas_locator(pagina):
    linhas = pagina.locator("table tbody tr")
    if linhas.count() == 0:
        linhas = pagina.locator("[role='row']").filter(has_text=re.compile(r"\d"))
    return linhas


def aguardar_resultados(pagina, timeout_s=120):
    for _ in range(timeout_s):
        try:
            if len(extrair_linhas(pagina)) > 0:
                return True
            corpo = pagina.locator("body").inner_text()
            if re.search(r"devedores?\s+encontrados?", corpo, re.IGNORECASE) and _linhas_locator(pagina).count() > 0:
                return True
        except Exception:
            pass
        pagina.wait_for_timeout(1000)
    return False


def salvar_debug(pagina, motivo):
    """Guarda print + texto + html da tela para eu poder ver o que o site mostrou."""
    try:
        os.makedirs("debug_divida", exist_ok=True)
        marca = datetime.now().strftime("%H%M%S")
        pagina.screenshot(path=f"debug_divida/erro_{marca}.png", full_page=True)
        with open(f"debug_divida/erro_{marca}.txt", "w", encoding="utf-8") as f:
            f.write(f"MOTIVO: {motivo}\nURL: {pagina.url}\n\n")
            f.write(pagina.locator("body").inner_text())
        with open(f"debug_divida/erro_{marca}.html", "w", encoding="utf-8") as f:
            f.write(pagina.content())
        log("Debug salvo em debug_divida/ (erro_%s.*)" % marca)
    except Exception as e:
        log("Não consegui salvar o debug:", e)


def total_encontrado(pagina):
    try:
        m = re.search(r"([\d.]+)\s+devedores?\s+encontrados?", pagina.locator("body").inner_text(), re.IGNORECASE)
        return int(m.group(1).replace(".", "")) if m else None
    except Exception:
        return None


def extrair_linhas(pagina):
    """Lista as linhas da página: [{indice, cnpj, cnpjNumeros, nome}] (só CNPJ)."""
    resultado = []
    linhas = _linhas_locator(pagina)
    for i in range(linhas.count()):
        try:
            txt = linhas.nth(i).inner_text()
        except Exception:
            continue
        compacto = re.sub(r"\s+", "", txt)  # CNPJ costuma quebrar linha na tabela
        m = PADRAO_CNPJ.search(compacto)
        if not m:
            continue  # CPF mascarado (***.018.954-**) ou linha de cabeçalho
        partes = [p.strip() for p in txt.split("\n") if p.strip()]
        nome = partes[2] if len(partes) > 2 else (partes[1] if len(partes) > 1 else "")
        resultado.append({"indice": i, "cnpj": m.group(0), "cnpjNumeros": so_digitos(m.group(0)), "nome": nome})
    return resultado


def abrir_e_ler_detalhe(pagina, indice):
    """Clica na seta da linha, lê a janela de detalhe e fecha. Devolve dict ou None."""
    linhas = _linhas_locator(pagina)
    if indice >= linhas.count():
        return None
    linha = linhas.nth(indice)
    celulas = linha.locator("td")
    alvo_cel = celulas.last if celulas.count() > 0 else linha
    clicavel = alvo_cel.locator("a, button, [role='button'], svg, i, span, img")
    try:
        if clicavel.count() > 0:
            clicavel.first.click(timeout=5000)
        else:
            alvo_cel.click(timeout=5000)
    except Exception as erro:
        log("Não consegui clicar na seta da linha:", erro)
        return None

    # espera a janela de detalhe abrir (o texto "Nome Empresarial:" aparece)
    texto = ""
    for _ in range(20):
        pagina.wait_for_timeout(500)
        try:
            texto = pagina.locator("body").inner_text()
        except Exception:
            continue
        if re.search(r"Nome\s+Empresarial\s*:", texto, re.IGNORECASE):
            break
    else:
        log("A janela de detalhe não abriu a tempo.")
        return None

    dados = parse_detalhe(texto)
    fechar_detalhe(pagina)
    return dados


def fechar_detalhe(pagina):
    for _ in range(3):
        try:
            b = pagina.get_by_role("button", name=re.compile(r"^\s*Fechar\s*$", re.IGNORECASE))
            if b.count() > 0 and b.last.is_visible():
                b.last.click(timeout=3000)
            else:
                pagina.keyboard.press("Escape")
        except Exception:
            try:
                pagina.keyboard.press("Escape")
            except Exception:
                pass
        pagina.wait_for_timeout(500)
        try:
            if not re.search(r"Nome\s+Empresarial\s*:", pagina.locator("body").inner_text(), re.IGNORECASE):
                return True
        except Exception:
            return True
    return False


# ---------------- paginação ----------------
def detectar_pagina_atual(pagina):
    try:
        m = re.search(r"P[áa]gina\s+(\d+)\s+de\s+\d+", pagina.locator("body").inner_text(), re.IGNORECASE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    for sel in ["[aria-current='page']", ".pagination .active", ".page-item.active", "li.active", "button.active", ".current-page"]:
        try:
            loc = pagina.locator(sel)
            for i in range(min(loc.count(), 5)):
                t = loc.nth(i).inner_text().strip()
                if t.isdigit():
                    return int(t)
        except Exception:
            continue
    return None


def _assinatura_pagina(pagina):
    try:
        linhas = _linhas_locator(pagina)
        return "|".join(linhas.nth(i).inner_text()[:60] for i in range(min(linhas.count(), 3)))
    except Exception:
        return ""


def _aguardar_mudanca(pagina, assinatura_antes, timeout_s=25):
    for _ in range(timeout_s * 2):
        pagina.wait_for_timeout(500)
        if _assinatura_pagina(pagina) != assinatura_antes:
            return True
    return False


def proxima_pagina(pagina):
    antes = _assinatura_pagina(pagina)
    candidatos = [
        pagina.get_by_role("button", name=re.compile(r"pr[óo]xim|next|seguinte", re.IGNORECASE)),
        pagina.get_by_role("link", name=re.compile(r"pr[óo]xim|next|seguinte", re.IGNORECASE)),
        pagina.locator("[aria-label*='róxim' i], [aria-label*='next' i], [title*='róxim' i]"),
        pagina.get_by_role("button", name=re.compile(r"^\s*[›>»]\s*$")),
        pagina.get_by_role("link", name=re.compile(r"^\s*[›>»]\s*$")),
    ]
    for loc in candidatos:
        try:
            if loc.count() == 0:
                continue
            el = loc.last
            if not el.is_visible():
                continue
            if el.is_disabled() or "disabled" in (el.get_attribute("class") or ""):
                continue
            el.click(timeout=5000)
            return _aguardar_mudanca(pagina, antes)
        except Exception:
            continue
    return False


def _numeros_visiveis(pagina):
    nums = {}
    for loc in (pagina.get_by_role("button"), pagina.get_by_role("link")):
        try:
            for i in range(min(loc.count(), 60)):
                el = loc.nth(i)
                t = (el.inner_text() or "").strip()
                if t.isdigit() and el.is_visible():
                    nums[int(t)] = el
        except Exception:
            continue
    return nums


def ir_para_pagina(pagina, alvo):
    """Vai até a página `alvo` (1-based). Devolve a página em que realmente parou."""
    atual = detectar_pagina_atual(pagina) or 1
    guarda = 0
    while atual < alvo and guarda < alvo + 60:
        guarda += 1
        nums = _numeros_visiveis(pagina)
        melhores = [n for n in nums if atual < n <= alvo]
        antes = _assinatura_pagina(pagina)
        if melhores:
            n = max(melhores)
            try:
                nums[n].click(timeout=5000)
                _aguardar_mudanca(pagina, antes)
                atual = detectar_pagina_atual(pagina) or n
                continue
            except Exception:
                pass
        if not proxima_pagina(pagina):
            log(f"Não consegui avançar além da página {atual}.")
            break
        atual = detectar_pagina_atual(pagina) or (atual + 1)
    return atual


# ============================================================
# CICLO
# ============================================================
def montar_pesquisa(sessao, uf):
    """Abre o site, aplica filtros e vai até a página salva."""
    sessao.abrir()
    pagina = sessao.pagina
    pagina.goto(URL_CONSULTA, wait_until="domcontentloaded", timeout=120000)
    pagina.wait_for_timeout(3000)
    fechar_avisos(pagina)

    if not preencher_valor_minimo(pagina):
        salvar_debug(pagina, "valor mínimo")
        raise RuntimeError("não consegui preencher o valor mínimo")
    if not selecionar_uf(pagina, uf):
        salvar_debug(pagina, "uf")
        raise RuntimeError(f"não consegui selecionar a UF {uf}")
    if not clicar_filtrar(pagina):
        salvar_debug(pagina, "filtrar")
        raise RuntimeError("botão filtrar não encontrado")
    if not aguardar_resultados(pagina):
        salvar_debug(pagina, "resultados não carregaram")
        raise RuntimeError("os resultados não carregaram")

    log("Total encontrado no site:", total_encontrado(pagina))
    sessao.uf = uf
    alvo = ler_progresso(uf)
    if alvo > 1:
        log(f"Retomando da página {alvo}...")
        sessao.pagina_num = ir_para_pagina(pagina, alvo)
    else:
        sessao.pagina_num = 1


def executar_ciclo(sessao, uf, ainda_ativo):
    """Coleta até NOVOS_POR_CICLO empresas novas. `ainda_ativo()` -> bool."""
    if sessao.pagina is None or sessao.uf != uf:
        montar_pesquisa(sessao, uf)
    pagina = sessao.pagina

    novos = 0
    paginas_puladas = 0
    falhas = set()

    while novos < NOVOS_POR_CICLO and paginas_puladas < PAGINAS_POR_CICLO:
        linhas = extrair_linhas(pagina)
        ids = [l["cnpjNumeros"] for l in linhas]
        ja = ids_existentes(ids)
        pendentes = [l for l in linhas if l["cnpjNumeros"] not in ja and l["cnpjNumeros"] not in falhas]
        log(f"Página {sessao.pagina_num}: {len(linhas)} CNPJ(s), {len(pendentes)} novo(s).")

        for l in pendentes:
            if novos >= NOVOS_POR_CICLO or not ainda_ativo():
                break
            dados = abrir_e_ler_detalhe(pagina, l["indice"])
            if not dados or not dados.get("cnpjNumeros"):
                falhas.add(l["cnpjNumeros"])
                log("Falha ao ler o detalhe de", l["cnpj"])
                continue
            dados["cnpjNumeros"] = dados["cnpjNumeros"] or l["cnpjNumeros"]
            dados["capturadoEm"] = datetime.now(timezone.utc)
            dados["origem"] = "PGFN - Divida Aberta"
            dados["ufBusca"] = uf or ""
            dados["paginaOrigem"] = sessao.pagina_num
            salvar_divida(dados)
            novos += 1
            log(f"✔ Salvo: {dados['nomeEmpresarial']} | {dados['cnpj']} | {dados['valorTotalTexto']}")

        if novos >= NOVOS_POR_CICLO or not ainda_ativo():
            break

        # todos desta página já coletados (ou só CPFs) -> próxima página
        salvar_progresso(uf, sessao.pagina_num + 1)
        if not proxima_pagina(pagina):
            log("Fim da lista (ou não consegui avançar).")
            break
        sessao.pagina_num = detectar_pagina_atual(pagina) or (sessao.pagina_num + 1)
        paginas_puladas += 1

    salvar_progresso(uf, sessao.pagina_num)
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


def loop_principal():
    from firebase_admin import firestore

    with sync_playwright() as p:
        sessao = Sessao(p)
        ultimo_ciclo = 0.0
        uf_anterior = None
        estava_ativo = False
        inicio = time.time()
        ultimo_ativo = time.time()
        log("Robô de Dívida Ativa iniciado. Aguardando o Orion ligar a captação...")
        escrever_status("aguardando", "Aguardando a captação ser ligada no Orion.")

        while True:
            try:
                if MAX_HORAS and (time.time() - inicio) >= MAX_HORAS * 3600:
                    log("Tempo máximo atingido — encerrando (o próximo agendamento continua).")
                    sessao.fechar()
                    escrever_status("pausado", "Robô reiniciando (limite de tempo do servidor).")
                    break
                ctrl = ler_controle()
                ativo = controle_ativo(ctrl)
                if ativo:
                    ultimo_ativo = time.time()
                elif SAIR_INATIVO_MIN and (time.time() - ultimo_ativo) >= SAIR_INATIVO_MIN * 60:
                    log("Inativo por muito tempo — encerrando para economizar minutos.")
                    sessao.fechar()
                    break
                uf = (ctrl.get("uf") or "").strip().upper()

                if not ativo:
                    if estava_ativo:
                        log("Captação pausada/sem sinal do Orion — fechando o navegador.")
                        sessao.fechar()
                        escrever_status("pausado", "Captação pausada.")
                        estava_ativo = False
                    time.sleep(POLL_CONTROLE_SEGUNDOS)
                    continue

                estava_ativo = True
                mudou_uf = uf != uf_anterior
                vencido = (time.time() - ultimo_ciclo) >= INTERVALO_MIN * 60

                if mudou_uf or vencido:
                    uf_anterior = uf
                    log(f"=== Novo ciclo | UF: {uf or 'todas'} ===")
                    escrever_status("coletando", f"Coletando ({uf or 'todas as UFs'})...", paginaAtual=sessao.pagina_num)
                    try:
                        novos = executar_ciclo(sessao, uf, lambda: controle_ativo(ler_controle()))
                        escrever_status(
                            "aguardando",
                            f"Ciclo concluído: {novos} nova(s) empresa(s). Próximo em {INTERVALO_MIN:g} min.",
                            ultimoCicloEm=firestore.SERVER_TIMESTAMP,
                            paginaAtual=sessao.pagina_num,
                            totalColetados=firestore.Increment(novos),
                        )
                    except Exception as erro:
                        log("ERRO no ciclo:", type(erro).__name__, erro)
                        if "429" in str(erro) or "Quota" in str(erro):
                            raise
                        sessao.fechar()  # recomeça a pesquisa do zero no próximo ciclo
                        escrever_status("erro", f"Erro no ciclo: {erro}. Tentando de novo no próximo ciclo.")
                    ultimo_ciclo = time.time()

                time.sleep(POLL_CONTROLE_SEGUNDOS)

            except KeyboardInterrupt:
                log("Encerrado pelo usuário.")
                sessao.fechar()
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
    """Abre o site, filtra e mostra o que o robô enxerga (sem Firebase)."""
    os.makedirs("debug_divida", exist_ok=True)
    with sync_playwright() as p:
        sessao = Sessao(p)
        sessao.abrir()
        pagina = sessao.pagina
        pagina.goto(URL_CONSULTA, wait_until="domcontentloaded", timeout=120000)
        pagina.wait_for_timeout(4000)
        fechar_avisos(pagina)
        log("Valor mínimo OK?", preencher_valor_minimo(pagina))
        log("Filtrar clicado?", clicar_filtrar(pagina))
        log("Resultados carregaram?", aguardar_resultados(pagina))
        log("Total encontrado:", total_encontrado(pagina))
        linhas = extrair_linhas(pagina)
        log(f"{len(linhas)} linha(s) com CNPJ na página 1:")
        for l in linhas:
            log("  ", l)
        log("Página atual detectada:", detectar_pagina_atual(pagina))
        log("Números de página visíveis:", sorted(_numeros_visiveis(pagina)))
        if linhas:
            dados = abrir_e_ler_detalhe(pagina, linhas[0]["indice"])
            log("Detalhe da 1ª linha:", dados)
        pagina.screenshot(path="debug_divida/diagnostico.png", full_page=True)
        with open("debug_divida/texto_pagina.txt", "w", encoding="utf-8") as f:
            f.write(pagina.locator("body").inner_text())
        with open("debug_divida/html_pagina.html", "w", encoding="utf-8") as f:
            f.write(pagina.content())
        log("Arquivos salvos em debug_divida/ (screenshot, texto e html).")
        input("ENTER para fechar... ")
        sessao.fechar()


def modo_teste(uf):
    iniciar_firebase()
    with sync_playwright() as p:
        sessao = Sessao(p)
        novos = executar_ciclo(sessao, (uf or "").upper(), lambda: True)
        log(f"Teste concluído: {novos} empresa(s) nova(s) salva(s).")
        input("ENTER para fechar... ")
        sessao.fechar()


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
