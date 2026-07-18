"""Cérebro: só entra quando a Sentinela acha algo SEM regra pronta. Recebe o log +
o problema, pede um diagnóstico ao Claude e devolve uma decisão. Guardrail duro: só
ações da WHITELIST são aceitas; qualquer outra (login, delete, acelerar) vira escala."""
import json
from dataclasses import dataclass

ACOES_SEGURAS = {"restart", "redeploy", "reenqueue", "nada"}


@dataclass(frozen=True)
class Decisao:
    diagnostico: str
    acao: str
    escalar: bool


_PROMPT = """Você é o Maestro-Ops. Um problema sem regra pronta apareceu.
Problema: {tipo} em {alvo}
Log (fim):
{log}
Responda SÓ um JSON: {{"diagnostico": "...", "acao": "restart|redeploy|reenqueue|nada",
"escalar": true|false}}. NUNCA sugira login, criar conta, deletar, ou acelerar cadência.
Se não tiver certeza, escalar=true."""


def diagnosticar(problema, log: str, llm) -> Decisao:
    bruto = llm(_PROMPT.format(tipo=problema.tipo, alvo=problema.alvo, log=(log or "")[-2000:]))
    try:
        d = json.loads(bruto)
    except Exception:
        return Decisao("resposta do LLM ilegível", "nada", True)
    if not isinstance(d, dict):
        return Decisao("resposta do LLM não é objeto", "nada", True)
    acao = str(d.get("acao", "nada"))
    segura = acao in ACOES_SEGURAS
    escalar = bool(d.get("escalar", True)) or not segura
    return Decisao(str(d.get("diagnostico", ""))[:500], acao if segura else "nada", escalar)
