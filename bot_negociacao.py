"""
Robô "Negociação na PGFN" — roda no GitHub Actions (mais memória que o Render gratuito).

Fluxo:
  Orion (botão "Consultar negociação") grava o pedido em botorion2two
  (coleção consulta_negociacao/{cnpj}, status "pendente").
  Este robô é ligado de tempos em tempos pelo agendamento do GitHub, pega os pedidos
  pendentes, abre o painel público da PGFN (guest, sem login), tira os filtros que
  escondem resultados e grava a situação da negociação e a qtde de parcelas no mesmo documento.

Variável de ambiente (Secret do GitHub):
  PROCESSOS_FIREBASE_KEY   JSON da conta de serviço do projeto botorion2two
  (opcional) NEG_MAX_POR_EXECUCAO (padrão 6), NEG_SO_CHECAR=1 (só informa se há pendentes)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore

COLECAO_NEG = "consulta_negociacao"
PGFN_DW_URL = os.getenv("PGFN_DW_URL") or "https://dw.pgfn.fazenda.gov.br/dwsigpgfn/servlet/mstrWeb?server=161.148.236.209&Project=SIGPGFN+Historiado&Port=0&evt=3140&src=mstrWeb.3140&documentID=6C86F33847E71AE8ACC292A5E08B3B43&ConnMode=8&guest=Guest+User"
MAX_POR_EXECUCAO = int(os.getenv("NEG_MAX_POR_EXECUCAO", "6"))

_bruto = os.getenv("PROCESSOS_FIREBASE_KEY") or os.getenv("FIREBASE_SERVICE_ACCOUNT")
if not _bruto:
    sys.exit("Defina o Secret PROCESSOS_FIREBASE_KEY (JSON do projeto botorion2two).")
firebase_admin.initialize_app(credentials.Certificate(json.loads(_bruto)))
db = firestore.client()


def agora():
    return datetime.now(timezone.utc)


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def mem():
    """Memória usada pelo contêiner (MB) / limite — para diagnosticar quedas por falta de memória."""
    try:
        base = "/sys/fs/cgroup/"
        usado = int(open(base + "memory.current").read()) / 1048576
        lim = open(base + "memory.max").read().strip()
        lim = "sem limite" if lim == "max" else f"{int(lim) / 1048576:.0f}"
        return f"[mem {usado:.0f}/{lim} MB]"
    except Exception:
        return ""


def tem_captcha(pagina):
    try:
        return pagina.locator(
            "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .g-recaptcha, .h-captcha"
        ).count() > 0
    except Exception:
        return False




def diagnostico_frames(pagina):
    """Resumo de TODOS os frames (o MicroStrategy usa vários): campos, listas e botões."""
    out = []
    for fr in pagina.frames:
        try:
            r = fr.evaluate("""() => {
              const v = e => !!(e.offsetWidth || e.offsetHeight);
              const sels = [...document.querySelectorAll('select')].filter(v).slice(0, 6).map(e =>
                (e.id || e.name || 'select') + ':[' + [...e.options].slice(0, 8).map(o => o.text.trim()).join('|') + ']');
              const ins = [...document.querySelectorAll('input')].filter(v).slice(0, 8).map(e => (e.id || e.name || '') + ':' + e.type);
              const bts = [...document.querySelectorAll('button,input[type=submit],input[type=button],a')].filter(v)
                .slice(0, 14).map(e => (e.innerText || e.value || e.title || '').trim().slice(0, 25)).filter(Boolean);
              return { u: location.pathname.slice(-40), sels, ins, bts, tabelas: document.querySelectorAll('table').length };
            }""")
            out.append(r)
        except Exception:
            continue
    return json.dumps(out, ensure_ascii=False)[:1400]


def formatar_cnpj(c):
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


def _norm(t):
    import unicodedata
    return re.sub(r"\s+", " ", "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c))).strip().lower()


def _chave_coluna(n):
    """Nome normalizado do cabeçalho -> chave fixa (o que o site mostra)."""
    n = re.sub(r"\s+", " ", n)
    if "mes/ano" in n or ("requerimento" in n):
        return "mesAno"
    if "numero da conta" in n or n.startswith("numero da"):
        return "conta"
    if "tipo de negociacao" in n:
        return "tipo"
    if "modalidade" in n:
        return "modalidade"
    if "situacao da negociacao" in n:
        return "situacao"
    if "parcelas concedidas" in n:
        return "parcelas"
    if "parcelas em atraso" in n:
        return "atraso"
    for rot, chave in (("consolidado", "consolidado"), ("principal", "principal"), ("multa", "multa"),
                       ("juros", "juros"), ("encargo", "encargo")):
        if rot in n:
            if "apos" in n:
                return chave + "Apos"
            if "adesao" in n:
                return chave + "Adesao"
    return None


def extrair_negociacao(pagina, cnpj):
    """Procura, em todos os frames, uma tabela com as colunas Situação da Negociação e
    Qtde de Parcelas Concedidas. Linhas com células mescladas (rowspan: mês, UF, CNPJ e nome só
    aparecem na 1ª linha) são realinhadas pelo tamanho do cabeçalho."""
    achados = []
    cnpj_fmt = formatar_cnpj(cnpj)
    for fr in pagina.frames:
        try:
            tabelas = fr.evaluate("""() => [...document.querySelectorAll('table')].map(t =>
                [...t.querySelectorAll('tr')].map(r => [...r.querySelectorAll('th,td')].map(c => (c.innerText || '').trim())))""")
        except Exception:
            continue
        for linhas in tabelas:
            for i, lin in enumerate(linhas):
                nl = [_norm(c) for c in lin]
                i_s = next((k for k, c in enumerate(nl) if "situacao" in c and ("negocia" in c or "parcelamento" in c or "acordo" in c)), None)
                i_p = next((k for k, c in enumerate(nl) if "parcelas concedidas" in c), None)
                if i_s is None or i_p is None:
                    continue
                larg = len(lin)
                chaves = [_chave_coluna(c) for c in nl]
                empresa_atual = False
                for l2 in linhas[i + 1:]:
                    if not l2 or all(not c for c in l2):
                        continue
                    falta = max(larg - len(l2), 0)   # colunas mescladas que não vieram nesta linha
                    texto = " ".join(l2)
                    if falta == 0:
                        empresa_atual = cnpj in re.sub(r"\D", "", texto) or cnpj_fmt in texto
                    js, jp = i_s - falta, i_p - falta
                    if js < 0 or jp < 0 or js >= len(l2) or jp >= len(l2):
                        continue
                    campos = {}
                    for k, ch in enumerate(chaves):
                        j = k - falta
                        if ch and 0 <= j < len(l2) and l2[j]:
                            campos[ch] = re.sub(r"\s+", " ", l2[j]).strip()
                    achados.append({"situacao": l2[js], "parcelas": l2[jp], "campos": campos,
                                    "da_empresa": empresa_atual})
                break
    return achados


def _achar(pagina, padrao):
    """Localiza (em qualquer frame) o elemento cujo texto bate com o padrão; devolve (locator, box)."""
    for fr in pagina.frames:
        try:
            loc = fr.get_by_text(re.compile(padrao, re.I)).first
            if loc.count():
                box = loc.bounding_box()
                if box:
                    return loc, box
        except Exception:
            continue
    return None, None


def _aguardar_painel(pagina, maximo=60):
    """Espera os indicadores 'Pesquisando...'/'Carregando...' sumirem (o painel é lento)."""
    pagina.wait_for_timeout(1500)
    for _ in range(int(maximo / 2)):
        ocupado = False
        for fr in pagina.frames:
            try:
                l = fr.get_by_text(re.compile(r"^\s*(Pesquisando|Carregando)", re.I)).first
                if l.count() and l.is_visible():
                    ocupado = True
                    break
            except Exception:
                continue
        if not ocupado:
            return
        pagina.wait_for_timeout(2000)


def _item_visivel(pagina, regex):
    for fr in pagina.frames:
        try:
            it = fr.get_by_text(re.compile(regex, re.I)).first
            if it.count() and it.is_visible():
                return it
        except Exception:
            continue
    return None


def _menu_filtro(pagina, padrao_titulo, nome, acao="Excluir"):
    """Abre o menu de contexto (⋮) do filtro e escolhe Incluir/Excluir.
    O ⋮ só aparece com o mouse sobre o título; por isso passamos o mouse antes e procuramos o botão
    pelo nome ('Menu de Contexto'); se não achar, tentamos várias posições ao lado do título."""
    loc, box = _achar(pagina, padrao_titulo)
    if not box:
        log("NEGOCIAÇÃO: filtro não encontrado:", nome)
        return False
    cy = box["y"] + box["height"] / 2
    fim = box["x"] + box["width"]
    en = {"Excluir": "Exclude", "Incluir": "Include"}[acao]
    regex_item = rf"^\s*({acao}|{en})\s*$"

    def abrir_e_escolher(cx, cy_, como):
        pagina.mouse.move(cx, cy_)
        pagina.wait_for_timeout(500)
        pagina.mouse.click(cx, cy_)
        pagina.wait_for_timeout(1000)
        it = _item_visivel(pagina, regex_item)
        if it:
            it.click()
            log("NEGOCIAÇÃO: filtro", acao, nome, "(" + como + ")")
            _aguardar_painel(pagina)
            return True
        pagina.keyboard.press("Escape")
        pagina.wait_for_timeout(300)
        return False

    # 1) passa o mouse no título e procura o botão pelo nome/tooltip
    pagina.mouse.move(box["x"] + box["width"] / 2, cy)
    pagina.wait_for_timeout(600)
    melhores = []
    for fr in pagina.frames:
        try:
            els = fr.locator('[title*="Contexto" i], [aria-label*="Contexto" i]')
            for i in range(min(els.count(), 12)):
                bb = els.nth(i).bounding_box()
                if bb and abs((bb["y"] + bb["height"] / 2) - cy) < 22:
                    melhores.append((abs(bb["x"] + bb["width"] / 2 - fim), bb))
        except Exception:
            continue
    melhores.sort(key=lambda t: t[0])
    for _, bb in melhores[:3]:
        if abrir_e_escolher(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, "botão Menu de Contexto"):
            return True
    # 2) posições ao redor do fim do título (faixa vermelha do filtro)
    for dx in (14, 22, 6, 30, -2, -10, -18, -26, 40):
        if abrir_e_escolher(fim + dx, cy, f"posição {dx:+d}"):
            return True
    log("NEGOCIAÇÃO: não consegui abrir o menu (⋮) de", nome, "| botões achados:", len(melhores))
    return False


def _ano_sem_dados(pagina):
    """Seleciona um ano antigo (2001, ou o mais antigo da lista) no filtro de ano, para que 'Excluir'
    esse ano deixe TODOS os outros anos visíveis (inclusive o ano atual)."""
    loc, box = _achar(pagina, r"Selecione o ano de concess")
    if not box:
        return False
    pagina.mouse.click(box["x"] + 90, box["y"] + box["height"] + 26)   # abre a lista
    pagina.wait_for_timeout(900)
    for fr in pagina.frames:
        try:
            it = fr.get_by_text("2001", exact=True).first
            if it.count() and it.is_visible():
                it.click(); pagina.wait_for_timeout(1500); log("NEGOCIAÇÃO: ano 2001 escolhido"); return True
        except Exception:
            continue
    for fr in pagina.frames:      # sem 2001: usa a última (mais antiga) opção da lista
        try:
            ops = fr.get_by_role("option")
            n = ops.count()
            if n:
                ops.nth(n - 1).click(); pagina.wait_for_timeout(1500); log("NEGOCIAÇÃO: ano mais antigo escolhido"); return True
        except Exception:
            continue
    pagina.keyboard.press("Escape")
    return False


def _digitar_cnpj(pagina, cnpj):
    """Digita o CNPJ; o painel mostra 'Pesquisando...' e depois lista o mesmo CNPJ — clicamos nele."""
    loc, box = _achar(pagina, r"Preencha o CPF\s*/?\s*CNPJ")
    if not box:
        return False
    abaixo = box["y"] + box["height"]
    pagina.mouse.click(box["x"] + box["width"] / 2, abaixo + 28)
    pagina.wait_for_timeout(400)
    fmt = formatar_cnpj(cnpj)
    pagina.keyboard.type(fmt, delay=40)
    padrao = re.compile(re.escape(fmt))
    for _ in range(60):                       # até ~2 min (a lista demora mesmo: 'Pesquisando...')
        pagina.wait_for_timeout(2000)
        for fr in pagina.frames:
            try:
                alvo = fr.get_by_text(padrao)
                for i in range(min(alvo.count(), 8)):
                    el = alvo.nth(i)
                    bb = el.bounding_box()
                    if bb and el.is_visible() and bb["y"] > abaixo:       # a sugestão fica abaixo do campo
                        el.click()
                        log("NEGOCIAÇÃO: CNPJ selecionado na lista", mem())
                        _aguardar_painel(pagina)
                        return True
            except Exception:
                continue
    # plano B: seta para baixo + Enter seleciona o primeiro item da lista
    pagina.keyboard.press("ArrowDown")
    pagina.wait_for_timeout(300)
    pagina.keyboard.press("Enter")
    log("NEGOCIAÇÃO: sugestão não apareceu; usei ArrowDown+Enter")
    _aguardar_painel(pagina)
    return True


def consultar_negociacao(cnpj, uf=""):
    from playwright.sync_api import sync_playwright

    ref = db.collection(COLECAO_NEG).document(cnpj)
    ref.set({"cnpjNumeros": cnpj, "status": "consultando", "mensagem": "Abrindo o DW da PGFN...",
             "atualizadoEm": agora()}, merge=True)
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking", "--mute-audio", "--disable-software-rasterizer",
            "--disable-features=site-per-process,IsolateOrigins,Translate,BackForwardCache",
            "--disable-site-isolation-trials", "--renderer-process-limit=1", "--no-zygote",
            "--disable-blink-features=AutomationControlled"])
        contexto = navegador.new_context(locale="pt-BR", viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        # só bloqueia mídia (imagens e fontes ficam: os ícones ⋮ do painel dependem delas)
        contexto.route("**/*", lambda rota: rota.abort() if rota.request.resource_type == "media" else rota.continue_())
        pagina = contexto.new_page()
        try:
            log("NEGOCIAÇÃO: iniciada", cnpj, "UF", uf or "-")
            pagina.goto(PGFN_DW_URL, timeout=90000, wait_until="domcontentloaded")
            log("NEGOCIAÇÃO: página aberta", mem())
            try:       # o painel faz requisições o tempo todo: 'networkidle' pode nunca chegar — não é erro
                pagina.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                log("NEGOCIAÇÃO: seguindo sem esperar a rede acalmar")
            pagina.wait_for_timeout(3000)
            if tem_captcha(pagina):
                ref.set({"status": "concluido", "mensagem": "O DW da PGFN pediu captcha — consulte manualmente pelo link.",
                         "linhas": [], "atualizadoEm": agora()}, merge=True)
                return
            log("NEGOCIAÇÃO tela inicial:", mem(), diagnostico_frames(pagina))
            # 1) tira os filtros padrão (ano e UF) pelo menu ⋮ → Excluir; 2) digita o CNPJ
            # espera (até ~60 s) o painel desenhar os filtros — no começo só aparecem botões "[ ... ]"
            for _ in range(20):
                if _achar(pagina, r"Selecione o ano de concess")[1] and _achar(pagina, r"Preencha o CPF")[1]:
                    break
                pagina.wait_for_timeout(3000)
            log("NEGOCIAÇÃO: painel pronto?", bool(_achar(pagina, r"Selecione o ano de concess")[1]), mem())
            def esperar(tentativas):
                """True = achou linhas da empresa. Sai antes (False) quando o painel mostra
                'Nenhum dado retornado' — aí é hora de ampliar o filtro."""
                for n in range(tentativas):
                    pagina.wait_for_timeout(3000)
                    if any(a_["da_empresa"] for a_ in extrair_negociacao(pagina, cnpj)):
                        return True
                    if n >= 2 and _item_visivel(pagina, r"Nenhum dado retornado"):
                        log("NEGOCIAÇÃO: painel sem dados para esse filtro")
                        return False
                return False

            # 1) só o CNPJ (com os filtros padrão do painel: ano atual + RJ)
            preenchido = _digitar_cnpj(pagina, cnpj)
            ref.set({"mensagem": "CNPJ informado, aguardando o painel...", "atualizadoEm": agora()}, merge=True)
            achou = esperar(10)
            ok_ano = ok_uf = False
            # 2) nada? escolhe o ano 2001 e o EXCLUI → todos os outros anos aparecem
            if not achou:
                ref.set({"mensagem": "Sem resultado; ampliando para todos os anos...", "atualizadoEm": agora()}, merge=True)
                ok_ano = _ano_sem_dados(pagina)
                if ok_ano:
                    _aguardar_painel(pagina)
                    ok_ano = _menu_filtro(pagina, r"Selecione o ano de concess", "ano", "Excluir")
                achou = esperar(10)
            # 3) ainda nada? exclui o RJ (filtro de UF) → todas as outras UFs
            if not achou:
                ref.set({"mensagem": "Sem resultado; ampliando para todas as UFs...", "atualizadoEm": agora()}, merge=True)
                ok_uf = _menu_filtro(pagina, r"Selecione a UF do Devedor", "UF", "Excluir")
                esperar(14)
            achados = extrair_negociacao(pagina, cnpj)
            minhas = [a for a in achados if a["da_empresa"]] or []
            debug = diagnostico_frames(pagina)
            if not achados:      # ajuda a ajustar a leitura: mostra o começo de cada tabela vista
                for fr in pagina.frames:
                    try:
                        amostra = fr.evaluate("""() => [...document.querySelectorAll('table')].slice(0,6).map(t =>
                            [...t.querySelectorAll('tr')].slice(0,3).map(r => [...r.querySelectorAll('th,td')].map(c => (c.innerText||'').trim().slice(0,25)).join(' | ')))""")
                        if any(amostra):
                            log("NEGOCIAÇÃO amostra de tabelas:", amostra)
                    except Exception:
                        pass
            log("NEGOCIAÇÃO", cnpj, "ano excl.:", ok_ano, "| UF excl.:", ok_uf, "| cnpj digitado:", preenchido, "| linhas:", len(achados), "| da empresa:", len(minhas), debug)
            if minhas:
                msg = f"{len(minhas)} negociação(ões) encontrada(s)."
            elif achados:
                minhas, msg = [], "Tabela encontrada, mas sem linha deste CNPJ (provável: sem negociação)."
            else:
                msg = "Não consegui ler a tabela do relatório (a tela pode ter mudado). Consulte manualmente pelo link."
            ref.set({"status": "concluido", "mensagem": msg, "linhas": [{"situacao": m["situacao"], "parcelas": m["parcelas"], "campos": m.get("campos", {})} for m in minhas[:150]],
                     "debug": debug, "concluidoEm": agora(), "atualizadoEm": agora()}, merge=True)
        finally:
            navegador.close()




def pendentes():
    """Pedidos 'pendente' + pedidos 'consultando' parados há mais de 25 min (execução anterior interrompida)."""
    achados = {}
    for d in db.collection(COLECAO_NEG).where("status", "==", "pendente").stream():
        achados[d.id] = d.to_dict() or {}
    limite = agora() - timedelta(minutes=25)
    for d in db.collection(COLECAO_NEG).where("status", "==", "consultando").stream():
        dados = d.to_dict() or {}
        t = dados.get("atualizadoEm")
        if t is None or t < limite:
            achados[d.id] = dados
    return achados


def main():
    fila = pendentes()
    log(f"{len(fila)} pedido(s) de negociação pendente(s).")
    if os.getenv("NEG_SO_CHECAR") == "1":
        # usado pelo workflow: avisa se vale a pena instalar o navegador
        saida = os.getenv("GITHUB_OUTPUT")
        if saida:
            with open(saida, "a") as f:
                f.write(f"pendentes={len(fila)}\n")
        return
    for cnpj, dados in list(fila.items())[:MAX_POR_EXECUCAO]:
        cnpj = re.sub(r"\D", "", cnpj)
        if len(cnpj) != 14:
            continue
        uf = str(dados.get("uf") or "").strip().upper()
        try:
            consultar_negociacao(cnpj, uf)
        except Exception as e:
            log("ERRO negociação", cnpj, type(e).__name__, e)
            try:
                db.collection(COLECAO_NEG).document(cnpj).set(
                    {"status": "erro", "mensagem": f"Falha do robô: {type(e).__name__}", "atualizadoEm": agora()}, merge=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
