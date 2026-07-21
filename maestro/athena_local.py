"""ENTRYPOINT DOMÉSTICO da Athena — o loop que roda NO MAC (`python -m maestro.athena_local`).

Decisão de arquitetura (INVIOLÁVEL): a Athena roda DOMÉSTICA no Mac, NÃO na VPS. O
executor de captura, aqui, é o `captura.LocalExecutor` (chama o MOTOR DIRETO como
subprocesso), NÃO o `FilaExecutor` (que enfileira pro worker da VPS). Este módulo é o
`main.py` do modelo doméstico: monta o executor local + a contagem-verdade do Notion
LOCAL e roda o mesmo LOOP-DONO já testado.

REUSO (não reimplementação): a COORDENAÇÃO é a MESMA `orquestrador.orquestrar_captura`
do modelo VPS — completude-por-Notion, vigília de STALL, uma-passada-por-ciclo,
fail-closed quando uma passada estoura. O que muda é só a `passada_fn` (o EXECUTOR):
de `coordenar(executor=FilaExecutor)` para uma passada LOCAL enxuta sobre o
`LocalExecutor`.

Por que a passada local é ENXUTA (e não o `captura.coordenar` verbatim): o `coordenar`
é amarrado ao mundo VPS — `estado_sessao`/`resolver_course_id`/`progresso` leem a
`fila_captura`/`estado_aulas` via `docker exec`, e o auto-ingest chama `reconciliar`
via `docker exec` no container do app. NADA disso existe no Mac. A passada local
PRESERVA os mesmos INVIOLÁVEIS que valem no doméstico — anti-dup por COMPLETUDE
(Notion), anti-ban (delegado ao executor), disjuntor (teto de tentativas), e completude
PROVADA no Notion (via o owner) — e delega ao MOTOR o que é do motor: a sessão e o
reseed HEADED interativo (`ensure_session`), que é como 'nunca logar sozinho' se cumpre
no Mac.

PENDENTE / o que ficou por LIGAR (honesto):
  - DE ONDE vem a lista de cursos: hoje `carregar_cursos(path)` lê um YAML
    (ATHENA_LOCAL_CURSOS) com {url, conta, plataforma, total_esperado}. A ponte para a
    lista do Painel/Notion (o cadastro real de cursos desejados) não está ligada.
  - `total_esperado` (o DENOMINADOR da completude): vem do YAML. Sem ele (0), o owner
    NUNCA declara concluído (fail-closed) — não há, no Mac, um oráculo local de
    quantas aulas o curso tem (o tracker do motor é por-processo). Um valor errado só
    atrasa/adianta a declaração de 'pronto'; nunca inventa done.
  - AUTO-INGEST (Notion->pgvector) e SINTETIZADOR (Capacidade C) pós-captura: no VPS
    rodam via `docker exec` no container do conhecimento. LOCALMENTE não estão ligados
    aqui — a captura escreve no Notion; a esteira downstream (ingest/síntese) é um passo
    POSTERIOR que precisa de acesso ao pgvector/Sintetizador e fica como pendência
    explícita (não finjo que roda).
  - A contagem-verdade do Notion LOCAL (`progresso_local_fn`) roda o MESMO script de
    contagem por prefixo de 'Origem' do adaptador, só que localmente (motor_python -c),
    reusando `motor.config` para carregar o NOTION_TOKEN do .env. É I/O real
    (pragma: no cover); testada pelo seam `run` injetável.
"""
import asyncio
import os
import time

from maestro import adaptador_pipeline, orquestrador
from maestro.adaptadores import captura
from maestro.playbook import Acao
from maestro.sentinela import Problema

# Fase por-curso (reusa as do adaptador de captura — mesmo vocabulário de máquina de
# estados; só as fases NOVO/CAPTURANDO/CONCLUIDO importam no doméstico).
FASE_NOVO = captura.FASE_NOVO
FASE_CAPTURANDO = captura.FASE_CAPTURANDO
FASE_CONCLUIDO = captura.FASE_CONCLUIDO


def _escalar_plataforma_nova(projeto_nome, voz, curso_url, st):
    """Gate da Capacidade B no doméstico: escala UMA vez (latch por-curso) que a
    plataforma é NOVA (sem adaptador) e PULA o curso. Não dispara criação de adaptador
    (decisão humana). Espelha `loop._escalar_plataforma_nova`, sem depender de um objeto
    Projeto (aqui só temos o nome)."""
    if st.get("plataforma_nova_avisada"):
        return None
    plat = adaptador_pipeline.plataforma_de_url(curso_url)
    pedido = (f"[{projeto_nome}] {curso_url} está numa PLATAFORMA NOVA "
              f"('{plat or 'desconhecida'}', sem adaptador). NÃO capturo sem adaptador; "
              f"criar o adaptador precisa da SUA aprovação.")
    voz.escalar(Problema("plataforma_nova", curso_url, pedido, "aviso"), pedido)
    st["plataforma_nova_avisada"] = True
    return Acao("", False, True, pedido)


def _passada_local_fn(executor, progresso_fn, voz, estado, *, projeto_nome,
                      max_tentativas):
    """Fábrica da `passada_fn` LOCAL que o owner (`orquestrar_captura`) invoca por curso.

    A máquina de estados por-curso (persiste em `estado[curso]` entre ciclos):
      1. já CONCLUIDO -> None (idempotente).
      2. lê a contagem-verdade do Notion. Ilegível -> escala honesto, NÃO dispara às
         cegas (o próximo ciclo re-tenta).
      3. COMPLETO no Notion (no_notion >= total, total>0) -> anti-dup por COMPLETUDE:
         marca CONCLUIDO e NÃO dispara (não re-captura um curso pronto).
      4. processo AINDA ativo (executor) -> quieto (None): captura em andamento, não
         martela (disjuntor).
      5. teto de tentativas atingido e nada ativo -> DISJUNTOR: escala UMA vez e para
         de disparar (não martela um curso que morre repetidamente).
      6. senão -> DISPARA via LocalExecutor. `ContaOcupada` (anti-ban) => aguarda a vez
         (None, não é falha). Falha/silêncio do disparo -> escala honesto.
    """
    def passada(curso):
        st = estado.setdefault(curso, {})
        if st.get("fase") == FASE_CONCLUIDO:
            return None
        try:
            no_notion, total = progresso_fn(curso)
        except Exception as e:
            pedido = (f"[{projeto_nome}] NÃO consegui ler o Notion p/ {curso} "
                      f"(anti-dup/completude): {str(e)[:140]} — NÃO disparo às cegas")
            voz.escalar(Problema("progresso_notion_inacessivel", curso, pedido, "aviso"),
                        pedido)
            return Acao("", False, True, pedido)
        if no_notion is not None and int(total) > 0 and int(no_notion) >= int(total):
            st["fase"] = FASE_CONCLUIDO
            acao = Acao(f"[{projeto_nome}] {curso} COMPLETO no Notion "
                        f"({no_notion}/{total}) — não disparo (anti-dup por completude)",
                        True, False)
            voz.avisar_acao(acao)
            return acao
        if executor.curso_ativo(curso):
            return None                                    # capturando: quieto
        if st.get("tentativas", 0) >= max_tentativas:
            pedido = (f"[{projeto_nome}] {curso} NÃO concluiu após {max_tentativas} "
                      f"passada(s) locais e o processo não está mais ativo — DISJUNTOR: "
                      f"paro de disparar e escalo (não martelo). Precisa de olho humano.")
            if not st.get("esgotado_avisado"):
                voz.escalar(Problema("captura_local_esgotada", curso, pedido, "critico"),
                            pedido)
                st["esgotado_avisado"] = True
            return Acao("", False, True, pedido)
        try:
            conf = executor.disparar(curso)
        except captura.ContaOcupada:
            # anti-ban FUNCIONANDO: a conta está ocupada por outro curso. Aguarda a vez —
            # NÃO é falha, NÃO escala, NÃO consome tentativa.
            return None
        except Exception as e:
            pedido = (f"[{projeto_nome}] FALHEI ao disparar a captura LOCAL de {curso}: "
                      f"{str(e)[:160]}")
            voz.escalar(Problema("captura_local_disparo_falhou", curso, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        if not conf:
            pedido = (f"[{projeto_nome}] disparo LOCAL de {curso} SEM confirmação — "
                      f"não assumo sucesso")
            voz.escalar(Problema("captura_local_sem_confirmacao", curso, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        st["tentativas"] = st.get("tentativas", 0) + 1
        st["fase"] = FASE_CAPTURANDO
        acao = Acao(f"[{projeto_nome}] captura LOCAL de {curso} iniciada "
                    f"(tentativa {st['tentativas']}): {conf}", True, False)
        voz.avisar_acao(acao)
        return acao
    return passada


def ciclo_local(cursos, executor, progresso_fn, voz, voo, estado, *, agora=None,
                plataformas_suportadas=None, max_tentativas=3,
                projeto_nome="athena-local"):
    """UM ciclo doméstico. Aplica o gate de PLATAFORMA-NOVA (pula cursos sem adaptador),
    e delega os demais ao OWNER `orquestrar_captura` com a passada LOCAL.

    A serialização anti-ban 1-por-conta é EMERGENTE: o owner chama a passada de cada
    curso na ordem; a 1ª de uma conta dispara (o executor registra o processo), e as
    seguintes da MESMA conta batem em `ContaOcupada` e AGUARDAM — sem precisar pré-
    ordenar aqui. Contas diferentes disparam no mesmo ciclo (paralelo).

    A contagem-verdade do Notion é lida UMA vez por curso por ciclo (cache) e servida
    tanto à passada (anti-dup) quanto ao `notion_fn` do owner (completude/stall) —
    evita ler o Notion duas vezes por curso por ciclo."""
    if agora is None:
        agora = time.time()

    cache = {}

    def progresso_cached(curso):
        if curso not in cache:
            try:
                cache[curso] = ("ok", progresso_fn(curso))
            except Exception as e:                         # erro cacheado: 1 leitura/ciclo
                cache[curso] = ("err", e)
        kind, val = cache[curso]
        if kind == "err":
            raise val
        return val

    cursos_ok = []
    for c in cursos:
        st = estado.setdefault(c.url, {})
        if (plataformas_suportadas is not None and
                not adaptador_pipeline.plataforma_suportada(c.url, plataformas_suportadas)):
            _escalar_plataforma_nova(projeto_nome, voz, c.url, st)
            continue
        cursos_ok.append(c.url)

    passada = _passada_local_fn(executor, progresso_cached, voz, estado,
                                projeto_nome=projeto_nome, max_tentativas=max_tentativas)

    def notion_fn(curso):
        # contrato do owner: (no_notion|None, total). Ilegível => (None, 0): o owner não
        # julga o curso às cegas (nem conclui, nem escala stall).
        try:
            no_notion, total = progresso_cached(curso)
        except Exception:
            return (None, 0)
        return (no_notion, total)

    return orquestrador.orquestrar_captura(cursos_ok, passada, notion_fn, voz, voo,
                                           agora=agora)


async def rodar(cursos, executor, progresso_fn, voz, *, sleep=asyncio.sleep,
                intervalo_s=120.0, max_iters=None, plataformas_suportadas=None,
                max_tentativas=3, projeto_nome="athena-local"):
    """O LOOP doméstico. Cria o `voo` (store cross-ciclo do owner) e o `estado`
    (máquina por-curso) UMA vez e os REINJETA a cada ciclo — sem isso a confirmação/
    stall (que são cross-ciclo) nunca fechariam. Um ciclo que estoura não derruba o
    loop (o owner já é fail-closed por-curso; isto é o cinto extra) — MAS a falha NÃO é
    engolida em silêncio: ela é ESCALADA via voz (I-1 escala honesta). Sem isso, uma
    falha SISTEMÁTICA (ex.: dependência quebrada) viraria um no-op silencioso — o loop
    'rodando' sem capturar nada e ninguém sabendo. Latch por assinatura de erro: escala
    UMA vez por episódio e re-arma quando um ciclo volta a passar (não inunda o Telegram
    a cada `intervalo_s`, mas nunca mascara uma falha nova ou persistente sem avisar)."""
    estado = {}
    voo = {}
    ultimo_erro = None
    i = 0
    while max_iters is None or i < max_iters:
        i += 1
        try:
            ciclo_local(cursos, executor, progresso_fn, voz, voo, estado,
                        agora=time.time(), plataformas_suportadas=plataformas_suportadas,
                        max_tentativas=max_tentativas, projeto_nome=projeto_nome)
            ultimo_erro = None                             # ciclo passou: re-arma o latch
        except Exception as e:
            assinatura = f"{type(e).__name__}:{str(e)[:120]}"
            if assinatura != ultimo_erro:                  # episódio novo -> escala honesto
                pedido = (f"[{projeto_nome}] o CICLO doméstico ESTOUROU (não derrubo o "
                          f"loop, mas NÃO capturo nada até resolver): {assinatura}")
                try:
                    voz.escalar(Problema("ciclo_local_estourou", projeto_nome, pedido,
                                         "critico"), pedido)
                except Exception:
                    pass                                   # a voz falhar não pode matar o loop
                ultimo_erro = assinatura
        await sleep(intervalo_s)
    return i


# ---------------------------------------------------------------------------
# Contagem-verdade do Notion LOCAL (I/O real; testada pelo seam `run`).
# ---------------------------------------------------------------------------
def _run_local(cmd, *, cwd):  # pragma: no cover — subprocesso REAL
    import subprocess
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=120).stdout


def contar_no_notion_local(motor_python, motor_dir, curso_url, *, run=None) -> int:
    """Conta as aulas deste curso já no Notion (por prefixo de 'Origem'), rodando o
    MESMO script do adaptador (`captura._PROGRESSO_SCRIPT`) LOCALMENTE (motor_python -c),
    com cwd=motor_dir para que `motor.config` (importado no topo do script) carregue o
    NOTION_TOKEN/NOTION_DB_LESSONS_ID do .env do motor. LEVANTA se a sentinela não vier
    (silêncio NÃO vira 0 — subestimar o Notion re-capturaria; o chamador escala)."""
    run = run or _run_local
    prefixo = captura._prefixo_de_curso(curso_url)
    # prepende o import de motor.config (que roda load_dotenv no import) para popular o
    # ambiente com o token do Notion — o script do adaptador assume os env já presentes.
    script = "import motor.config  # carrega .env (NOTION_TOKEN etc.)\n" + captura._PROGRESSO_SCRIPT
    cmd = [motor_python, "-c", script, prefixo]
    saida = run(cmd, cwd=motor_dir) or ""
    if captura.PROGRESSO_SENTINELA not in saida:
        raise RuntimeError(
            f"contagem LOCAL no Notion SEM confirmação ({captura.PROGRESSO_SENTINELA} "
            f"ausente) p/ {curso_url}: {saida[-160:]!r}")
    linha = next(l for l in saida.splitlines() if captura.PROGRESSO_SENTINELA in l)
    return int(linha.split(captura.PROGRESSO_SENTINELA, 1)[1].strip().split()[0])


def progresso_local_fn(motor_python, motor_dir, total_por_curso, *, run=None):
    """Fábrica da `progresso_fn` doméstica -> (no_notion, total). O numerador é a
    contagem-verdade LOCAL do Notion; o denominador vem de `total_por_curso`
    (o `total_esperado` do YAML). Curso sem total => 0 (o owner não conclui: fail-closed)."""
    def _fn(curso_url):
        no_notion = contar_no_notion_local(motor_python, motor_dir, curso_url, run=run)
        return (no_notion, int(total_por_curso.get(curso_url, 0)))
    return _fn


def carregar_cursos(path) -> list:
    """Lê a lista de cursos desejados do YAML doméstico. Cada entrada:
    {url, conta, plataforma?, total_esperado?}. `conta` é OBRIGATÓRIA (chave anti-ban:
    sem ela não dá para garantir 1-por-conta) — a ausência LEVANTA (fail-closed)."""
    import yaml
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        out.append(captura.CursoLocal(
            url=d["url"], conta=d["conta"],
            plataforma=d.get("plataforma", "hotmart"),
            total_esperado=int(d.get("total_esperado", 0))))
    return out


def _motor_dirs_por_plataforma() -> dict:
    """Overrides de diretório do motor POR PLATAFORMA, lidos de env ATHENA_MOTOR_DIR_<PLAT>
    (ex.: ATHENA_MOTOR_DIR_STOA aponta pro worktree `adaptador-stoa`, onde vivem o código
    CORRIGIDO da Stoa E a sessão/tracker VIVOS). Sem override => o LocalExecutor usa o
    ATHENA_MOTOR_DIR global. Genérico: qualquer plataforma pode ter árvore própria sem
    tocar no código."""
    prefixo = "ATHENA_MOTOR_DIR_"
    return {k[len(prefixo):].lower(): v for k, v in os.environ.items()
            if k.startswith(prefixo) and v}


def _ler_groq_key(motor_dir) -> str:  # pragma: no cover — I/O real (lê o chave-groq.txt)
    """GROQ_API_KEY p/ INJETAR no subprocesso do motor. Necessária para as plataformas
    cujo cwd NÃO é o /aula (ex.: Stoa no worktree): lá o `motor.config` não acha o
    `chave-groq.txt` e o Whisper/Groq (único backend viável p/ captura sem legenda)
    ficaria sem chave. Lê de env GROQ_API_KEY ou do chave-groq.txt do `motor_dir` (o
    /aula). '' se não achar — o Hotmart (cwd=/aula) ainda pega via motor.config; as
    outras escalam honesto sem chave em vez de fingir transcrição."""
    import re
    v = os.getenv("GROQ_API_KEY")
    if v:
        return v
    try:
        with open(os.path.join(motor_dir, "chave-groq.txt")) as f:
            m = re.search(r"gsk_[A-Za-z0-9]{20,}", f.read())
            return m.group(0) if m else ""
    except OSError:
        return ""


def main():  # pragma: no cover — I/O real (monta os seams concretos e roda o loop)
    from maestro.config import carregar
    from maestro.telegram_api import TelegramClient
    from maestro.voz import Voz

    cfg = carregar()
    voz = Voz(TelegramClient(cfg.bot_token), cfg.chat_ids)

    motor_python = os.getenv(
        "ATHENA_MOTOR_PYTHON", "/Users/guilhermerodrigues/teste/aula/.venv/bin/python")
    motor_dir = os.getenv("ATHENA_MOTOR_DIR", "/Users/guilhermerodrigues/teste/aula")
    cursos_path = os.environ["ATHENA_LOCAL_CURSOS"]        # YAML dos cursos desejados
    cursos = carregar_cursos(cursos_path)

    # lock_dir DURÁVEL e ESTÁVEL entre restarts (o guard anti-ban depende disso — ver
    # LocalExecutor). Env sobrepõe; o default do executor já é um caminho estável do SO.
    lock_dir = os.getenv("ATHENA_LOCK_DIR") or None
    # GROQ p/ injetar (Stoa roda com cwd=worktree, sem chave-groq.txt lá) e overrides de
    # diretório do motor por plataforma (Stoa => worktree adaptador-stoa).
    groq_key = _ler_groq_key(motor_dir) or None
    executor = captura.LocalExecutor(
        cursos, motor_python=motor_python, motor_dir=motor_dir, lock_dir=lock_dir,
        groq_key=groq_key, motor_dir_por_plataforma=_motor_dirs_por_plataforma())
    total_por_curso = {c.url: c.total_esperado for c in cursos}
    # A contagem-verdade do Notion (numerador) só precisa de motor.config+notion_client
    # (o NOTION_TOKEN do /aula/.env) — é independente de plataforma; usa o motor_dir
    # global mesmo para Stoa/Kajabi (o prefixo de 'Origem' basta).
    progresso_fn = progresso_local_fn(motor_python, motor_dir, total_por_curso)

    plataformas = frozenset(
        p for p in os.getenv(
            "PLATAFORMAS_SUPORTADAS",
            "hotmart.com,memberkit.com.br,stoa.com.br,mykajabi.com")
        .replace(" ", "").split(",") if p)
    plataformas_suportadas = plataformas or None

    # Teto de re-disparos por curso (disjuntor anti-martelo). Env-overridável: Stoa/Kajabi
    # (multi-curso, sem denominador de completude confiável) podem precisar de mais fôlego
    # de retomada que o default do Hotmart.
    max_tentativas = int(os.getenv("ATHENA_MAX_TENTATIVAS", "3"))

    asyncio.run(rodar(cursos, executor, progresso_fn, voz,
                      intervalo_s=cfg.intervalo_s, max_tentativas=max_tentativas,
                      plataformas_suportadas=plataformas_suportadas))


if __name__ == "__main__":  # pragma: no cover
    main()
