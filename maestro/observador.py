"""Camada 1 — Observabilidade (os OLHOS da Athena). Processo 24/7 na VPS.

Resolve P1: **o Claude é cego na VPS**. Ele roda um comando, lê o output DAQUELE
instante e reporta; se o processo cai 5s depois, ele nunca sabe. Uma sonda de um
instante não distingue "está up" de "estava up quando eu olhei". A correção é
parar de confiar no instante: a cada ciclo o observador monta um snapshot com
timestamp e o PERSISTE numa tabela durável (`observacoes`). O cérebro (Camada 2)
então não pergunta "está up AGORA?" (sonda) — pergunta "esse serviço ficou up ao
longo dos últimos N min, ou subiu-e-caiu?" (série temporal). Isso é `flapping` /
`estado_estavel`: a diferença entre enxergar o estado REAL e enxergar um instante.

Costuras REUSADAS (NÃO abre conexão nova): recebe `acesso` (docker/HTTP/recursos
via socket — os mesmos seams de acesso.py) e uma FÁBRICA `db` (estilo psycopg3:
`with db() as conn`). Tudo injetável e testável com dublês que modelam o mecanismo
(store durável ordenado por ts) — nunca docker/HTTP/Postgres real nos testes.

O que se OBSERVA aqui; de ONDE a verdade vem fica plugado por fora: o progresso da
captura é uma COSTURA (`progresso_captura`) — Camada 1 não fala com o Notion, o
chamador injeta a contagem-verdade depois. Assim a Camada 1 fica só sobre OLHAR."""
import json
from dataclasses import dataclass, field


# --- Dataclasses: cada campo carrega o timestamp `agora` do ciclo -----------
@dataclass(frozen=True)
class ServicoObservado:
    """Estado de UM serviço num instante carimbado. `up` (container Up, via
    docker ps) e `health` (GET /health 2xx) são independentes: Up-mas-doente é
    real. `ts` é o que torna a série temporal ordenável — sem ele, dois ciclos
    são indistinguíveis e o flapping desaparece."""
    nome: str
    up: bool
    health: bool
    restarting: bool
    ts: float


@dataclass(frozen=True)
class Recursos:
    disco_pct: float
    ram_pct: float
    ts: float


@dataclass(frozen=True)
class Fila:
    """Estado da fila de captura no instante. `itens` é opaco pro observador
    (o adaptador que sabe o schema o produz); aqui só se carimba e persiste."""
    itens: object
    ts: float


@dataclass(frozen=True)
class ProgressoCurso:
    """Progresso REAL por curso, medido na FONTE DE VERDADE (Notion), nunca em
    flag. `completo` = done>=total com total>0 — mata o falso-pronto (curso que
    a fila diz 'pronto' mas que capturou 0)."""
    curso: str
    total: int
    done: int
    ts: float

    @property
    def completo(self) -> bool:
        return self.total > 0 and self.done >= self.total


@dataclass(frozen=True)
class EstadoObservado:
    ts: float
    servicos: tuple = ()
    recursos: Recursos = None
    fila: Fila = None
    progresso: tuple = ()


# --- Coleta: monta o snapshot a partir das costuras injetadas ---------------
def coletar_estado(acesso, alvos_saude, agora, *, fila_fn=None, progresso_fn=None,
                   servicos_raw=None, saude=None, recursos_raw=None
                   ) -> EstadoObservado:
    """Monta o snapshot carimbado do estado REAL, lendo SÓ as costuras de `acesso`
    (não abre docker/HTTP/DB novo). `agora` vai em TODO campo — é o eixo do tempo
    da série. `fila_fn`/`progresso_fn` são seams: fila = estado da fila de captura
    (default: `acesso.fila()` se existir); progresso = contagem-verdade por curso,
    plugada pelo chamador (nunca o Notion aqui).

    `servicos_raw`/`saude`/`recursos_raw` são valores JÁ SONDADOS pelo chamador NESTE
    ciclo (docker ps, /health, df/free) — quando passados, o observador os REUSA em
    vez de re-sondar (evita a sondagem dupla quando o mesmo ciclo já os coletou para o
    laço por-projeto). Não altera a série: os valores continuam carimbados com `agora`.
    Ausentes (None) -> o observador sonda por conta própria (retrocompatível)."""
    servs_raw = acesso.servicos() if servicos_raw is None else servicos_raw
    if saude is None:
        saude = acesso.saude_http(alvos_saude) if alvos_saude else {}
    servicos = tuple(
        ServicoObservado(nome=s.nome, up=s.up, health=bool(saude.get(nome, False)),
                         restarting=s.restarting, ts=agora)
        for nome, s in servs_raw.items())

    r = acesso.recursos() if recursos_raw is None else recursos_raw
    recursos = Recursos(disco_pct=r.get("disco_pct", 0.0),
                        ram_pct=r.get("ram_pct", 0.0), ts=agora)

    if fila_fn is None:
        fila_fn = getattr(acesso, "fila", None)
    fila = Fila(itens=(fila_fn() if fila_fn is not None else None), ts=agora)

    progresso = tuple(progresso_fn()) if progresso_fn is not None else ()

    return EstadoObservado(ts=agora, servicos=servicos, recursos=recursos,
                           fila=fila, progresso=progresso)


# --- Progresso-por-verdade: a COSTURA (source of truth plugado por fora) -----
def progresso_captura(fn_notion_count, cursos, agora) -> tuple:
    """Costura injetável: `fn_notion_count(curso) -> (done, total)` lê a contagem
    REAL na fonte de verdade (o chamador liga no Notion depois). Camada 1 NÃO
    fala com o Notion — mantém-se sobre OBSERVAR; a fonte é plugada. Retorna
    ProgressoCurso por curso, todos carimbados com `agora`."""
    out = []
    for curso in cursos:
        done, total = fn_notion_count(curso)
        out.append(ProgressoCurso(curso=curso, total=int(total), done=int(done),
                                  ts=agora))
    return tuple(out)


# --- Persistência: store durável (a correção da cegueira) -------------------
_CREATE = ("CREATE TABLE IF NOT EXISTS observacoes("
           "ts TIMESTAMPTZ, servico TEXT, up BOOL, health BOOL, detalhe JSONB)")
_INSERT = ("INSERT INTO observacoes(ts, servico, up, health, detalhe) "
           "VALUES (to_timestamp(%s), %s, %s, %s, %s::jsonb)")
_SELECT = ("SELECT extract(epoch from ts), up, health FROM observacoes "
           "WHERE servico = %s ORDER BY ts ASC")


def registrar(db, estado) -> None:
    """Grava o snapshot na tabela durável `observacoes` — UMA linha por serviço,
    com o `ts` do campo (o eixo do tempo). Cria a tabela idempotentemente (IF NOT
    EXISTS) a CADA ciclo — seguro rodar sempre. É a persistência que faz o estado
    sobreviver ao instante: sem ela, 'estava up' vira 'está up' pra sempre e o
    cérebro fica cego (P1). O ambiente (recursos/fila/progresso) vai no JSONB de
    cada linha pra auditoria posterior."""
    ambiente = {
        "recursos": _recursos_dict(estado.recursos),
        "fila": estado.fila.itens if estado.fila is not None else None,
        "progresso": [_prog_dict(p) for p in estado.progresso],
    }
    with db() as conn:
        conn.execute(_CREATE)
        for s in estado.servicos:
            detalhe = dict(ambiente, restarting=s.restarting)
            conn.execute(_INSERT, (s.ts, s.nome, s.up, s.health, json.dumps(detalhe)))
        conn.commit()


# --- Leitura da série: "estável ou subiu-e-caiu?" (o que a sonda não vê) ----
def _amostras(db, servico):
    """Toda a série (ts, up, health) do serviço, ordenada por ts ASC — lida do
    store durável, NÃO de uma sonda do instante."""
    with db() as conn:
        cur = conn.execute(_SELECT, (servico,))
        return [(float(ts), bool(up), bool(health)) for ts, up, health in cur.fetchall()]


def _na_janela(db, servico, janela_s, agora):
    amostras = _amostras(db, servico)
    if janela_s is None:
        return amostras
    if agora is None:
        agora = amostras[-1][0] if amostras else 0.0
    corte = agora - janela_s
    return [a for a in amostras if a[0] >= corte]


def flapping(db, servico, janela_s, agora=None) -> bool:
    """True se, DENTRO da janela, o serviço teve estados `up` diferentes — i.e.,
    subiu-e-caiu (ou caiu-e-subiu). É exatamente o caso que uma sonda de um
    instante perde: em t0 estava up, 5s depois caiu; a série sabe, a sonda não."""
    amostras = _na_janela(db, servico, janela_s, agora)
    return len({up for (_, up, _) in amostras}) > 1


def estado_estavel(db, servico, janela_s=None, agora=None):
    """"up" | "down" | None. O estado REAL só quando a série é CONSISTENTE na
    janela; se flapou, retorna None (instável) — NUNCA afirma o "up" obsoleto do
    primeiro instante. Considera a série inteira, não a última nem a primeira
    amostra sozinha. Sem amostras -> None."""
    amostras = _na_janela(db, servico, janela_s, agora)
    if not amostras:
        return None
    estados = {up for (_, up, _) in amostras}
    if len(estados) > 1:
        return None  # instável (flapou) — não afirma estabilidade
    return "up" if amostras[-1][1] else "down"


def _recursos_dict(r):
    if r is None:
        return None
    return {"disco_pct": r.disco_pct, "ram_pct": r.ram_pct}


def _prog_dict(p):
    return {"curso": p.curso, "total": p.total, "done": p.done,
            "completo": p.completo}
