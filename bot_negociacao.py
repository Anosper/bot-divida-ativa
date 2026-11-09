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


def _foto(pagina, nome):
    """Guarda um print da tela do painel (o workflow envia a pasta debug/ como artefato)."""
    try:
        os.makedirs("debug", exist_ok=True)
        pagina.screenshot(path=f"debug/{datetime.now().strftime('%H%M%S')}_{nome}.png")
    except Exception:
        pass


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

    # 0) tira o foco do campo de CNPJ / fecha listas abertas (um clique solto no topo da página)
    pagina.keyboard.press("Escape")
    pagina.mouse.click(box["x"] + box["width"] / 2, max(box["y"] - 70, 5))
    pagina.wait_for_timeout(500)
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
    for _, bb in melhores[:6]:
        if abrir_e_escolher(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, "botão Menu de Contexto"):
            return True
    # 2) posições ao redor do fim do título (faixa vermelha do filtro)
    for dx in (14, 22, 6, 30, -2, -10, -18, -26, 40):
        if abrir_e_escolher(fim + dx, cy, f"posição {dx:+d}"):
            return True
    log("NEGOCIAÇÃO: não consegui abrir o menu (⋮) de", nome, "| botões achados:", len(melhores),
        "| título em", {k: round(v) for k, v in box.items()})
    _foto(pagina, f"falha_menu_{nome}")
    for k, fr in enumerate(pagina.frames):
        try:
            r = fr.evaluate("""() => [...document.querySelectorAll('[title*="Contexto" i],[aria-label*="Contexto" i]')].map(e => {
              const b = e.getBoundingClientRect(); return e.tagName + '@' + Math.round(b.x) + ',' + Math.round(b.y) + ' ' + Math.round(b.width) + 'x' + Math.round(b.height) + ' vis=' + !!(e.offsetWidth||e.offsetHeight); })""")
            if r:
                log(f"NEGOCIAÇÃO botões de contexto (frame {k}):", r)
        except Exception:
            pass
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


def _input_perto(pagina, box):
    """Campo de texto visível mais próximo (abaixo) do título do filtro."""
    melhor, dist = None, 1e9
    alvo = (box["x"] + box["width"] / 2, box["y"] + box["height"] + 30)
    for fr in pagina.frames:
        try:
            ins = fr.locator("input:visible")
            for i in range(min(ins.count(), 30)):
                bb = ins.nth(i).bounding_box()
                if not bb:
                    continue
                d = abs(bb["x"] + bb["width"] / 2 - alvo[0]) + abs(bb["y"] + bb["height"] / 2 - alvo[1])
                if d < dist and bb["y"] >= box["y"]:
                    melhor, dist = (ins.nth(i), bb), d
        except Exception:
            continue
    return melhor


_JS_MARCAR = """(dig) => {
  const v = e => !!(e.offsetWidth || e.offsetHeight);
  document.querySelectorAll('[data-neg-alvo]').forEach(e => e.removeAttribute('data-neg-alvo'));
  let n = 0;
  for (const e of document.querySelectorAll('body *')) {
    if (!v(e) || ['SCRIPT','STYLE','INPUT','TEXTAREA'].includes(e.tagName)) continue;
    const t = (e.innerText || e.textContent || '');
    if (t.length > 60 || t.replace(/\\D/g, '') !== dig) continue;       // só o texto do CNPJ, nada mais
    if ([...e.children].some(c => (c.innerText || c.textContent || '').replace(/\\D/g, '') === dig)) continue;  // o mais fundo
    e.setAttribute('data-neg-alvo', '1'); n++;
  }
  return n;
}"""

_JS_DUMP = """(dig) => [...document.querySelectorAll('body *')].filter(e => {
    const t = (e.innerText || e.textContent || '');
    return (e.offsetWidth || e.offsetHeight) && t.length < 120 && t.replace(/\\D/g, '').includes(dig.slice(0, 8));
  }).slice(0, 10).map(e => { const r = e.getBoundingClientRect();
    return e.tagName + '.' + String(e.className).slice(0, 40) + '@' + Math.round(r.x) + ',' + Math.round(r.y) + ' "' + (e.innerText || '').slice(0, 40).replace(/\\n/g, ' ') + '"'; })"""


def _dump_cnpj(pagina, cnpj):
    for k, fr in enumerate(pagina.frames):
        try:
            r = fr.evaluate(_JS_DUMP, cnpj)
            if r:
                log(f"NEGOCIAÇÃO dump frame {k}:", r)
        except Exception:
            continue


def _seletor_cnpj(pagina):
    """Filtro 'CPF/CNPJ do Optante' do MicroStrategy: <div class="mstrmojo-DocSelector" nm="...">.
    O <input> real fica escondido até clicar na caixa visível dentro dele."""
    for fr in pagina.frames:
        try:
            sels = fr.locator('.mstrmojo-DocSelector[nm*="CNPJ" i], .mstrmojo-DocSelector[nm*="CPF" i]')
            for i in range(min(sels.count(), 4)):
                sel = sels.nth(i)
                bb = sel.bounding_box()
                if bb and sel.is_visible():
                    return sel, bb
        except Exception:
            continue
    return None, None


def _listar_seletores(pagina):
    for k, fr in enumerate(pagina.frames):
        try:
            r = fr.evaluate("""() => [...document.querySelectorAll('.mstrmojo-DocSelector')].map(e => {
              const b = e.getBoundingClientRect(); return (e.getAttribute('nm') || '?') + '@' + Math.round(b.x) + ',' + Math.round(b.y) + ' ' + Math.round(b.width) + 'x' + Math.round(b.height); })""")
            if r:
                log(f"NEGOCIAÇÃO seletores (frame {k}):", r)
        except Exception:
            pass


def _chip_ou_sugestao(pagina, fmt, abaixo, profundo=True):
    """Elemento visível com o texto do CNPJ abaixo do campo (sugestão da lista ou o 'chip' já aplicado)."""
    padrao = re.compile(re.escape(fmt))
    dig = re.sub(r"\D", "", fmt)
    for fr in pagina.frames:
        try:
            alvo = fr.get_by_text(padrao)
            for i in range(min(alvo.count(), 8)):
                el = alvo.nth(i)
                bb = el.bounding_box()
                if bb and el.is_visible() and bb["y"] > abaixo - 4:
                    return el, bb
        except Exception:
            continue
    if not profundo:
        return None, None
    for fr in pagina.frames:          # busca ampla: texto quebrado em vários <span>, qualquer formatação
        try:
            if fr.evaluate(_JS_MARCAR, dig):
                alvo = fr.locator("[data-neg-alvo='1']")
                for i in range(min(alvo.count(), 8)):
                    el = alvo.nth(i)
                    bb = el.bounding_box()
                    if bb and bb["y"] > abaixo - 4:
                        return el, bb
        except Exception:
            continue
    return None, None


def _digitar_cnpj(pagina, cnpj, espera=60):
    """Digita o CNPJ; o painel mostra 'Pesquisando...' e depois lista o mesmo CNPJ — clicamos nele."""
    loc, box = _achar(pagina, r"Preencha o CPF\s*/?\s*CNPJ")
    if not box:
        return False
    fmt = formatar_cnpj(cnpj)
    sel, sbb = _seletor_cnpj(pagina)
    if sel:
        log("NEGOCIAÇÃO: filtro do CNPJ encontrado em", {k: round(v) for k, v in sbb.items()})
        try:
            sel.locator(".mstrmojo-SimpleObjectInputBox").first.click(timeout=5000)
        except Exception:
            pagina.mouse.click(sbb["x"] + sbb["width"] / 2, sbb["y"] + sbb["height"] / 2)
        pagina.wait_for_timeout(600)
        inp = sel.locator("input[type=text]").first
        try:
            if not inp.is_visible():           # o MicroStrategy troca a caixa pelo input ao clicar; força se não trocou
                inp.evaluate("e => { e.style.display = 'block'; e.focus(); }")
        except Exception:
            pass
        pagina.keyboard.type(fmt, delay=70)
        try:
            log("NEGOCIAÇÃO: valor no campo do CNPJ:", repr(inp.input_value()))
        except Exception:
            pass
        ibb = sbb
        abaixo = sbb["y"] + sbb["height"]
    else:
        _listar_seletores(pagina)
        campo = _input_perto(pagina, box)
        if campo:
            el, ibb = campo
            log("NEGOCIAÇÃO: campo do CNPJ encontrado em", {k: round(v) for k, v in ibb.items()})
            el.click()
        else:
            log("NEGOCIAÇÃO: campo do CNPJ não localizado; clicando abaixo do título")
            ibb = {"x": box["x"], "y": box["y"] + box["height"] + 8, "width": max(box["width"], 240), "height": 30}
            pagina.mouse.click(ibb["x"] + ibb["width"] / 2, ibb["y"] + ibb["height"] / 2)
        pagina.wait_for_timeout(400)
        pagina.keyboard.type(fmt, delay=60)
        abaixo = ibb["y"] + ibb["height"]
    _foto(pagina, "01_cnpj_digitado")
    pagina.wait_for_timeout(3000)             # NÃO mexe em nada enquanto aparece 'Pesquisando...' (qualquer clique cancela a busca)
    for n in range(max(espera // 2, 5)):      # a lista demora mesmo
        pagina.wait_for_timeout(2000)
        el2, bb = _chip_ou_sugestao(pagina, fmt, abaixo, profundo=(n % 3 == 2))   # só leitura; busca ampla a cada 3ª vez
        if el2:
            el2.click()
            log("NEGOCIAÇÃO: CNPJ selecionado na lista", mem())
            _foto(pagina, "02_cnpj_selecionado")
            _aguardar_painel(pagina)
            return True
        if n == 14:
            _foto(pagina, "01b_cnpj_aguardando")
            _dump_cnpj(pagina, cnpj)
    # plano B: clique logo abaixo do campo (onde a sugestão aparece); só vale se o CNPJ ficar visível
    pagina.mouse.click(ibb["x"] + 40, abaixo + 18)
    pagina.wait_for_timeout(2000)
    el2, _ = _chip_ou_sugestao(pagina, fmt, abaixo)
    log("NEGOCIAÇÃO: sugestão não apareceu; cliquei abaixo do campo | CNPJ visível:", bool(el2))
    _foto(pagina, "02b_cnpj_planoB")
    _dump_cnpj(pagina, cnpj)
    if el2:
        _aguardar_painel(pagina)
        return True
    return False


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

            def abrir_painel():
                """Abre (ou reabre) o painel e espera os filtros aparecerem. False = captcha."""
                pagina.goto(PGFN_DW_URL, timeout=90000, wait_until="domcontentloaded")
                log("NEGOCIAÇÃO: página aberta", mem())
                try:       # o painel faz requisições o tempo todo: 'networkidle' pode nunca chegar — não é erro
                    pagina.wait_for_load_state("networkidle", timeout=25000)
                except Exception:
                    log("NEGOCIAÇÃO: seguindo sem esperar a rede acalmar")
                pagina.wait_for_timeout(3000)
                if tem_captcha(pagina):
                    return False
                log("NEGOCIAÇÃO tela inicial:", mem(), diagnostico_frames(pagina))
                for _ in range(20):    # até ~60 s: no começo só aparecem botões "[ ... ]"
                    if _achar(pagina, r"Selecione o ano de concess")[1] and _achar(pagina, r"Preencha o CPF")[1]:
                        break
                    pagina.wait_for_timeout(3000)
                log("NEGOCIAÇÃO: painel pronto?", bool(_achar(pagina, r"Selecione o ano de concess")[1]), mem())
                pagina.wait_for_timeout(2000)
                return True

            if not abrir_painel():
                ref.set({"status": "concluido", "mensagem": "O DW da PGFN pediu captcha — consulte manualmente pelo link.",
                         "linhas": [], "atualizadoEm": agora()}, merge=True)
                return
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

            # 1) só o CNPJ (com os filtros padrão do painel: ano atual + RJ).
            #    O painel é instável: se a sugestão do CNPJ não aparecer, recarrega e tenta de novo.
            preenchido = False
            for tentativa in (1, 2):
                if tentativa == 2:
                    log("NEGOCIAÇÃO: recarregando o painel (2ª tentativa)")
                    ref.set({"mensagem": "O painel não respondeu; tentando de novo...", "atualizadoEm": agora()}, merge=True)
                    if not abrir_painel():
                        break
                preenchido = _digitar_cnpj(pagina, cnpj, espera=60 if tentativa == 1 else 90)
                if preenchido:
                    break
            if not preenchido:
                ref.set({"status": "concluido", "linhas": [], "concluidoEm": agora(), "atualizadoEm": agora(),
                         "mensagem": "Este CNPJ não apareceu na lista do painel da PGFN (após 2 tentativas) — provavelmente não há negociação. Se quiser conferir, use o link do painel."}, merge=True)
                _foto(pagina, "98_cnpj_nao_apareceu")
                return
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
            _foto(pagina, "99_final")
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
    for d in db.collection(COLECAO_NEG).where(filter=firestore.FieldFilter("status", "==", "pendente")).stream():
        achados[d.id] = d.to_dict() or {}
    limite = agora() - timedelta(minutes=25)
    for d in db.collection(COLECAO_NEG).where(filter=firestore.FieldFilter("status", "==", "consultando")).stream():
        dados = d.to_dict() or {}
        t = dados.get("atualizadoEm")
        if t is None or t < limite:
            achados[d.id] = dados
    return achados


def _pode_processar(cnpj):
    """Relê o documento: só processa se ainda estiver 'pendente' (ou 'consultando' parado há 25+ min)."""
    snap = db.collection(COLECAO_NEG).document(cnpj).get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    st = d.get("status")
    if st == "pendente":
        return d
    if st == "consultando":
        t = d.get("atualizadoEm")
        if t is None or t < agora() - timedelta(minutes=25):
            return d
    return None


def processar(cnpj):
    cnpj = re.sub(r"\D", "", cnpj)
    if len(cnpj) != 14:
        return
    dados = _pode_processar(cnpj)
    if dados is None:
        return
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


def main():
    """Fica ligado (como o robô da Dívida Ativa): escuta o Firebase em tempo real e atende os
    pedidos em segundos. Termina sozinho após NEG_MAX_HORAS; o agendamento religa em seguida."""
    import queue
    import time

    if os.getenv("NEG_UMA_VEZ") == "1":           # modo antigo: uma passada e sai
        fila = pendentes()
        log(f"{len(fila)} pedido(s) pendente(s).")
        for cnpj in list(fila)[:MAX_POR_EXECUCAO]:
            processar(cnpj)
        return

    limite = time.time() + float(os.getenv("NEG_MAX_HORAS", "5.8")) * 3600
    fila = queue.Queue()

    def ao_mudar(_docs, mudancas, _t):            # roda em outra thread: só enfileira
        for m in mudancas:
            if m.type.name in ("ADDED", "MODIFIED"):
                fila.put(m.document.id)

    escuta = db.collection(COLECAO_NEG).where(filter=firestore.FieldFilter("status", "==", "pendente")).on_snapshot(ao_mudar)
    log("Robô ligado: escutando pedidos de negociação em tempo real.")
    ultima_varredura = 0.0
    try:
        while time.time() < limite:
            try:
                cnpj = fila.get(timeout=30)
            except queue.Empty:
                cnpj = None
            if cnpj:
                processar(cnpj)
                continue
            if time.time() - ultima_varredura > 300:   # rede de segurança: pega pedidos perdidos/parados
                ultima_varredura = time.time()
                for c in pendentes():
                    fila.put(c)
    finally:
        escuta.unsubscribe()
    log("Tempo máximo atingido; encerrando (o agendamento religa o robô).")


if __name__ == "__main__":
    main()
