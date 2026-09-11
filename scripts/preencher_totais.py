#!/usr/bin/env python3
"""REFINO 2 — preenche o `total_esperado` REAL de cada curso HOTMART do YAML doméstico.

PROBLEMA: hoje todo curso do YAML tem `total_esperado: 0`. O 0 é FAIL-CLOSED de
propósito (o owner NUNCA declara 'concluído' sem denominador — anti-falso-pronto), mas
com 33 cursos em 0 o disjuntor/stall vira RUÍDO: nenhum curso pode fechar a completude-
por-Notion, então todos parecem eternamente incompletos. Este script troca o 0 pelo
total REAL de aulas de cada curso, lido da FONTE que a própria captura usa.

DE ONDE VEM O TOTAL (mesma verdade da captura): a SPA do Hotmart Club busca a árvore
inteira do curso via `GET /v1/navigation`. A captura enumera EXATAMENTE por aí
(`motor.hotmart.enumerate.parse_course_index`: filtra `type=='CONTENT'`, deduplica por
`hash`). Reusamos essa MESMA função aqui — assim o DENOMINADOR (total_esperado) conta
pela idêntica régra do NUMERADOR (aulas no Notion, uma por página CONTENT). Contar de
outro jeito descasaria as duas pontas da completude.

COMO AUTENTICA (read-only): injeta a sessão viva (`.hotmart-session.json`, o storage_state
salvo — que INCLUI o cookie de sessão TGC) num contexto Playwright NOVO e HEADLESS, navega
até o curso (é o que dispara o re-mint silencioso do token do club) e INTERCEPTA a resposta
de `/v1/navigation` via `capture_navigation` — o mesmíssimo mecanismo do motor. NUNCA
escreve a sessão de volta (não corre com o daemon, que é quem persiste o re-mint): é
LEITURA pura.

INVIOLÁVEL ANTI-BAN (o daemon está capturando na MESMA conta AGORA):
  - READ-ONLY: só GET de navegação; nada é baixado, nada é escrito na plataforma nem na
    sessão.
  - PAÇADO e SEQUENCIAL: um curso por vez, com 2-4s de pausa entre cursos. Nunca paralelo,
    nunca martelo — para não somar carga automatizada pesada por cima da captura ativa.
  - PARA NA 1ª PAREDE: ao primeiro sinal de sessão morta / redirect SSO / sem-resposta
    (que tanto pode ser sessão expirada quanto rate-limit — ambos são 'pare'), INTERROMPE.
    Os cursos já lidos são persistidos; os que faltaram FICAM em 0 (fail-closed, honesto —
    NÃO invento total). Melhor subpreencher que arriscar a conta.
  - UMA VEZ: roda uma passada e sai. Sem retry-loop.

Execução:
  cd /Users/guilhermerodrigues/teste/aula   # p/ o motor.config achar o .env do motor
  PYTHONPATH=/Users/guilhermerodrigues/teste/aula \\
    /Users/guilhermerodrigues/teste/aula/.venv/bin/python \\
    /Users/guilhermerodrigues/teste/maestro-athlocal/scripts/preencher_totais.py
"""
import asyncio
import os
import random
import re
import sys
from urllib.parse import urlparse

SESSION_PATH = os.environ.get(
    "HOTMART_SESSION_PATH",
    "/Users/guilhermerodrigues/teste/aula/.hotmart-session.json")
YAML_PATH = os.environ.get(
    "ATHENA_LOCAL_CURSOS",
    "/Users/guilhermerodrigues/.athena-local/cursos_hotmart.yaml")

# Pausa anti-ban entre cursos (segundos). Faixa 2-4s, aleatória para não desenhar um
# padrão robótico perfeitamente periódico por cima da captura ativa.
PAUSA_MIN_S = 2.0
PAUSA_MAX_S = 4.0

# --- PARSING / REWRITE do YAML (PUROS: sem I/O, sem Playwright — testáveis) ---
# Não usamos PyYAML para ESCREVER: o arquivo tem comentários carregados de sentido (nome
# do curso, tracker_lessons) e o PyYAML os apagaria. Reescrevemos por LINHA, tocando só o
# valor de `total_esperado:` dos cursos que conseguimos ler — todo o resto do texto
# (comentários, ordem, espaçamento) fica intacto.
_URL_RE = re.compile(r'^\s*-\s*url:\s*"(?P<url>[^"]+)"')
_PLAT_RE = re.compile(r'^\s*plataforma:\s*(?P<plat>\S+)')
_TOTAL_RE = re.compile(r'^(?P<pre>\s*total_esperado:\s*)(?P<val>\d+)(?P<post>.*)$')


def _plataforma_padrao(url):
    """ESPELHO de maestro/adaptadores/captura.py::plataforma_padrao (o default do
    carregar_cursos): sem `plataforma:`, é 'hotmart' SÓ num host hotmart.com (ou
    subdomínio); outro host => 'nao-declarada' — este script nunca injeta a sessão do
    Hotmart nem pede /v1/navigation num domínio que não é do Hotmart."""
    host = (urlparse(str(url)).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if host == "hotmart.com" or host.endswith(".hotmart.com"):
        return "hotmart"
    return "nao-declarada"


def parse_cursos(texto):
    """Extrai, na ordem do arquivo, [{'url','plataforma'}] de cada bloco de curso.
    Bloco = uma linha `- url: "..."` seguida (mais abaixo) por `plataforma:`. Sem a
    linha, a plataforma é a do carregar_cursos do athena_local (`_plataforma_padrao`:
    'hotmart' só em host hotmart.com)."""
    cursos = []
    atual = None
    for linha in texto.splitlines():
        m = _URL_RE.match(linha)
        if m:
            atual = {"url": m.group("url"),
                     "plataforma": _plataforma_padrao(m.group("url"))}
            cursos.append(atual)
            continue
        if atual is not None:
            mp = _PLAT_RE.match(linha)
            if mp:
                atual["plataforma"] = mp.group("plat")
    return cursos


def atualizar_yaml_texto(texto, totais):
    """Reescreve APENAS o número de `total_esperado:` das URLs presentes em
    `totais` (dict url->int), preservando indentação, comentário (nome do curso) e
    todo o resto do texto. URLs ausentes de `totais` (não lidas / após a parede) NÃO
    são tocadas — ficam no 0 fail-closed. Idempotente: rerodar com os mesmos totais
    produz o mesmo texto."""
    linhas = texto.splitlines(keepends=True)
    url_atual = None
    for i, linha in enumerate(linhas):
        mu = _URL_RE.match(linha)
        if mu:
            url_atual = mu.group("url")
            continue
        mt = _TOTAL_RE.match(linha)
        if mt and url_atual is not None and url_atual in totais:
            fim = "\n" if linha.endswith("\n") else ""
            corpo = linha[:-1] if fim else linha
            m2 = _TOTAL_RE.match(corpo)
            novo = m2.group("pre") + str(int(totais[url_atual])) + m2.group("post")
            linhas[i] = novo + fim
            url_atual = None  # um total por bloco
    return "".join(linhas)


# --- FETCH da navegação (I/O real: Playwright + motor). Uma parede -> LEVANTA. -----
class ParedeAntiBan(RuntimeError):
    """Sinal de PARE: a navegação não devolveu o índice do curso (sessão morta /
    redirect SSO / sem-resposta). Pode ser sessão expirada OU rate-limit — em ambos os
    casos o inviolável manda INTERROMPER a passada, não seguir martelando."""


async def _contar_aulas(context, url):
    """Conta as aulas (páginas CONTENT, dedup por hash) do curso via /v1/navigation,
    reusando a MESMA enumeração da captura. LEVANTA ParedeAntiBan se a navegação não
    provar sessão viva (a régra é a `classify_navigation` do motor)."""
    from motor.hotmart.enumerate import (
        capture_navigation, classify_navigation, parse_course_index,
        _parse_course_url)
    page = await context.new_page()
    try:
        status, body = await capture_navigation(page, url)
    finally:
        await page.close()
    if not classify_navigation(status, body):
        raise ParedeAntiBan(
            f"/v1/navigation não provou sessão viva p/ {url} (status={status}) — PARO")
    slug, pid = _parse_course_url(url)
    course_map = parse_course_index(body, course_id=pid, slug=slug)
    return len(course_map.lessons)


async def preencher(session_path, yaml_path, *, pausa=None):
    """Passada ÚNICA, sequencial e paçada. Lê o YAML, para cada curso HOTMART busca o
    total real, e ao final reescreve o YAML com o que conseguiu. PARA na 1ª parede
    (persiste o parcial). Devolve (totais_lidos, curso_parede | None)."""
    from playwright.async_api import async_playwright

    with open(yaml_path) as f:
        texto = f.read()
    cursos = [c for c in parse_cursos(texto) if c["plataforma"] == "hotmart"]

    totais = {}
    parede = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # storage_state read-only: injeta a sessão salva; NUNCA a persiste de volta.
        context = await browser.new_context(storage_state=session_path)
        try:
            for idx, curso in enumerate(cursos):
                url = curso["url"]
                try:
                    n = await _contar_aulas(context, url)
                except ParedeAntiBan as e:
                    parede = url
                    print(f"[PAREDE] {e} — interrompo (fail-closed nos restantes)",
                          file=sys.stderr)
                    break
                totais[url] = n
                print(f"[ok] {n:4d} aulas  {url}")
                # pausa anti-ban ENTRE cursos (não após o último).
                if idx < len(cursos) - 1:
                    espera = pausa if pausa is not None else random.uniform(
                        PAUSA_MIN_S, PAUSA_MAX_S)
                    await asyncio.sleep(espera)
        finally:
            await context.close()
            await browser.close()

    if totais:
        novo_texto = atualizar_yaml_texto(texto, totais)
        with open(yaml_path, "w") as f:
            f.write(novo_texto)
    return totais, parede


def main():  # pragma: no cover — I/O real
    import motor.config  # noqa: F401 — carrega o .env do motor (NOTION etc. p/ paridade)

    if not os.path.exists(SESSION_PATH):
        print(f"sessão não encontrada: {SESSION_PATH} — não rodo (fail-closed)",
              file=sys.stderr)
        sys.exit(2)
    totais, parede = asyncio.run(preencher(SESSION_PATH, YAML_PATH))
    print(f"\nresumo: {len(totais)} curso(s) preenchido(s) com total REAL"
          + (f"; PAREDE em {parede} (restantes ficam em 0, fail-closed)"
             if parede else "; sem parede"))
    if not totais:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
