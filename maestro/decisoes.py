"""Espinha de prestação de contas da Athena-Jarvis (nível C).

No nível C a Athena não pede aprovação por decisão: o usuário controla DEPOIS,
lendo o SITREP e corrigindo. Isso só é honesto se TODA decisão autônoma deixar
rastro estruturado — o quê, por quê, reversível, quanto custou. Decisão sem log
é autonomia sem prestação de contas, que o usuário não autorizou.

Este módulo é ADITIVO: não importa nem toca `causa.py`, `vigia.py`, `athena_local`
ou qualquer módulo existente. Só stdlib. A fiação (null-object + call-sites) é
outra peça; aqui está o store e os agregadores que o `/sitrep` consome.

Duas regras invioláveis codificadas aqui:

1. **Fail-safe (mesmo contrato de `alertas.py`).** `registrar_decisao` NUNCA
   levanta. O log existe para observar o loop; observabilidade jamais pode ser
   quem derruba o loop. No pior caso (disco cheio, permissão, valor não
   serializável) escreve uma linha de fallback no stderr e segue.

2. **Custo medido e presumido NUNCA num número só.** `medido=True` é `usage`
   real de API; `medido=False` é proxy × tabela de preço. Os agregados devolvem
   os dois totais SEPARADOS. Somar estimativa com fatura é mentir com autoridade
   — e é exatamente o que o teto de budget (fase 2) não pode consumir.

Store: append-only JSONL, um arquivo por dia LOCAL em
`~/.athena-local/decisoes/YYYY-MM-DD.jsonl`. Escritor único (o loop), uma linha
< 4 KB com flush — append é atômico o suficiente; NÃO usar tmp+replace (replace
perderia appends concorrentes). Leitura é tolerante: linha que não parseia é
pulada e contada em `linhas_invalidas`.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("athena.decisoes")

SCHEMA_V = 1
DIR_PADRAO = Path("~/.athena-local/decisoes").expanduser()

# domínios conhecidos (documentais; o store não rejeita valores fora — só stdlib,
# forward-compat, o leitor ignora o que não entende)
TIPOS = ("decisao", "custo", "escalada")
FONTES = ("deterministico", "llm", "fail-closed", "disjuntor", "guard")
TRAVAS = ("anti-ban", "budget", "irreversivel-externo", "plataforma-nova", "desconhecida")


def _coagir_str(x) -> str:
    """por_que/o_que podem chegar como objeto (ex.: Decisao.motivo exótico).
    Nunca deixamos isso quebrar a serialização: coage para str."""
    if isinstance(x, str):
        return x
    try:
        return str(x)
    except Exception:
        return "<irrepresentável>"


def _dia_local(agora: datetime) -> str:
    """Dia LOCAL do ts (não o dia UTC). Se o ts vier ingênuo (sem tzinfo),
    já é local por definição; se vier aware, `.astimezone()` traz ao fuso local
    antes de extrair a data — senão uma decisão às 23:30 local cairia no dia
    seguinte em UTC e apareceria no SITREP errado."""
    if agora.tzinfo is not None:
        agora = agora.astimezone()
    return agora.date().isoformat()


def _arquivo_do_dia(dia: str, dir_base: Path) -> Path:
    return dir_base / f"{dia}.jsonl"


def registrar_decisao(
    o_que,
    por_que,
    *,
    reversivel: bool,
    tipo: str = "decisao",
    curso=None,
    plataforma=None,
    fonte: str = "deterministico",
    escalada: bool = False,
    trava=None,
    modelo=None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    custo_usd: float = 0.0,
    medido: bool = True,
    origem=None,
    sistema=None,
    agora: datetime | None = None,
    dir_base: Path | None = None,
) -> None:
    """Grava UMA linha JSONL no arquivo do dia. NUNCA levanta (fail-safe).

    `agora` e `dir_base` são injetáveis para teste (nada de relógio/HOME reais).
    Custo: `medido=True` => veio de usage real; `medido=False` => estimativa.
    `sistema` (aditivo F4): slug do sistema gerado a que a decisão/custo pertence
    (None = decisão da captura/geral, agregada como 'athena-geral'). É o campo que
    o `gasto_do_dia_por_sistema` (F4-f) agrupa para o enforcement D5 e o SITREP.
    """
    try:
        agora = agora if agora is not None else datetime.now().astimezone()
        base = Path(dir_base) if dir_base is not None else DIR_PADRAO
        dia = _dia_local(agora)

        reg = {
            "v": SCHEMA_V,
            "ts": agora.isoformat(),
            "tipo": tipo,
            "origem": _coagir_str(origem) if origem is not None else None,
            "sistema": _coagir_str(sistema) if sistema is not None else None,
            "curso": _coagir_str(curso) if curso is not None else None,
            "plataforma": _coagir_str(plataforma) if plataforma is not None else None,
            "o_que": _coagir_str(o_que),
            "por_que": _coagir_str(por_que),
            "fonte": fonte,
            "reversivel": bool(reversivel),
            "escalada": bool(escalada),
            "trava": trava,
            "modelo": modelo,
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "custo_usd": float(custo_usd),
            "medido": bool(medido),
        }
        # serializa ANTES de tocar o disco: se algo for irrepresentável, cai no
        # except sem criar arquivo/linha parcial. ensure_ascii=False mantém
        # acentos legíveis no SITREP.
        linha = json.dumps(reg, ensure_ascii=False)

        base.mkdir(parents=True, exist_ok=True)
        with _arquivo_do_dia(dia, base).open("a", encoding="utf-8") as fh:
            fh.write(linha + "\n")
            fh.flush()
    except Exception:
        # observabilidade JAMAIS derruba o loop. Fallback best-effort no stderr.
        try:
            sys.stderr.write(
                f"[decisoes] FALHA ao registrar decisão "
                f"o_que={o_que!r} por_que={por_que!r}: registro perdido\n"
            )
        except Exception:
            pass  # nem o fallback pode levantar


def _totais_zerados() -> dict:
    return {
        "n_decisoes": 0,
        "n_escaladas": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "custo_medido_usd": 0.0,
        "custo_presumido_usd": 0.0,
        "por_fonte": {},
        "por_trava": {},
    }


def _ler_linhas(arq: Path):
    """Gera (registro | None) por linha; None = linha inválida (não branca).
    Nunca levanta — dir/arquivo ilegível vira 'sem linhas'."""
    try:
        texto = arq.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except Exception:
        # arquivo existe mas é ilegível (permissão etc.) — não derruba o resumo.
        log.warning("resumo: arquivo %s ilegível, tratando como vazio", arq)
        return
    for linha in texto.splitlines():
        if not linha.strip():
            continue  # linha em branco não conta como corrupção
        try:
            yield json.loads(linha)
        except Exception:
            yield None  # corrompida: pulada e contada pelo chamador


def resumo_do_dia(data: date | None = None, dir_base: Path | None = None) -> dict:
    """Agrega o dia: decisões cronológicas, escaladas, totais e linhas_invalidas.

    Dia sem arquivo → resumo vazio válido (zeros/listas vazias), nunca exceção.
    `custo_medido_usd` e `custo_presumido_usd` são somados SEPARADOS — jamais
    existe um campo que os une.
    """
    data = data if data is not None else datetime.now().astimezone().date()
    base = Path(dir_base) if dir_base is not None else DIR_PADRAO
    arq = _arquivo_do_dia(data.isoformat(), base)

    decisoes_lst: list[dict] = []
    escaladas_lst: list[dict] = []
    totais = _totais_zerados()
    invalidas = 0

    for reg in _ler_linhas(arq):
        if reg is None or not isinstance(reg, dict):
            invalidas += 1
            continue

        # Leitura tolerante NÃO para na sintaxe: um dict com campo de tipo
        # errado (int_in="abc", custo="x" — write truncado, produtor externo)
        # é JSON válido mas semanticamente corrompido. Sem esta cerca, um
        # único `int("abc")` derrubaria o resumo INTEIRO — e o /sitrep com ele,
        # violando "corrupção parcial nunca derruba o SITREP". Registro que não
        # agrega é contado como inválido, não propagado.
        # Parse dos campos que PODEM levantar (tipos errados de um write
        # truncado ou produtor externo) ANTES de tocar qualquer acumulador —
        # assim uma falha deixa o registro como inválido, sem efeito parcial
        # (nem listado, nem meio-somado). É o "leitura tolerante" estendido da
        # sintaxe (json) para a semântica (tipos): corrupção parcial nunca
        # derruba o resumo, e o /sitrep com ele.
        try:
            t_in = int(reg.get("tokens_in", 0) or 0)
            t_out = int(reg.get("tokens_out", 0) or 0)
            custo = float(reg.get("custo_usd", 0.0) or 0.0)
        except Exception:
            invalidas += 1
            continue

        tipo = reg.get("tipo", "decisao")
        eh_escalada = bool(reg.get("escalada")) or tipo == "escalada"

        if tipo == "decisao":
            decisoes_lst.append(reg)
            totais["n_decisoes"] += 1
        if eh_escalada:
            escaladas_lst.append(reg)
            totais["n_escaladas"] += 1

        # tokens/custo somam de QUALQUER registro que os carregue (custo, ou
        # decisão LLM com usage anexado no call-site).
        totais["tokens_in"] += t_in
        totais["tokens_out"] += t_out
        # Default HONESTO: custo sem rótulo `medido` é PRESUMIDO, nunca medido.
        # Contar um gasto não-rotulado como fatura real é exatamente o "mentir
        # com autoridade" que este módulo proíbe.
        if reg.get("medido", False):
            totais["custo_medido_usd"] += custo
        else:
            totais["custo_presumido_usd"] += custo

        fonte = reg.get("fonte")
        if fonte:
            totais["por_fonte"][fonte] = totais["por_fonte"].get(fonte, 0) + 1
        trava = reg.get("trava")
        if trava:
            totais["por_trava"][trava] = totais["por_trava"].get(trava, 0) + 1

    return {
        "data": data.isoformat(),
        "decisoes": decisoes_lst,
        "escaladas": escaladas_lst,
        "totais": totais,
        "linhas_invalidas": invalidas,
    }


def gasto_do_dia(data: date | None = None, dir_base: Path | None = None) -> dict:
    """Atalho de custo do dia — é o que o enforcement de teto (fase 2) consulta.

    Devolve medido e presumido SEPARADOS (nunca um total único), mais tokens.
    """
    r = resumo_do_dia(data, dir_base)
    t = r["totais"]
    return {
        "data": r["data"],
        "custo_medido_usd": t["custo_medido_usd"],
        "custo_presumido_usd": t["custo_presumido_usd"],
        "tokens_in": t["tokens_in"],
        "tokens_out": t["tokens_out"],
    }
