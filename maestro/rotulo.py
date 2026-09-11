"""RÓTULO SEGURO — texto vindo de FORA (URL/título de curso, aula, módulo) pronto para ir a
um alerta/log SEM casar as âncoras de MORTE do classificador.

POR QUÊ: as regex `_RE_SESSAO` / `_RE_TOKEN` de `maestro.causa` têm PRECEDÊNCIA sobre o
exit code (sessão morta = reseed IRREDUTÍVEL, login humano). Nenhum texto que não seja uma
morte PROVADA pode casá-las — nem um alerta do próprio daemon, nem um nome de curso que a
plataforma escolheu (um slug ".../re-login-avancado" ou um título "Unauthorized Access"
casaria). Irmão do `rotulo_seguro` do motor (motor/kiwify/enumerate.py), estendido: em vez
de OMITIR o rótulo inteiro (o dono precisa reconhecer QUAL curso — ex.: a URL a corrigir no
YAML), MASCARA só o trecho que casa e reconfere até não sobrar nada.

As regex são as do PRÓPRIO classificador (importadas, não copiadas): se a `causa.py` ganhar
uma âncora nova, o rótulo passa a mascará-la sem ninguém lembrar de atualizar aqui. A
conferência é na forma CRUA (sem remover negações como a `causa` faz) — mais estrita que o
classificador, de propósito.
"""
from maestro.causa import _RE_SESSAO, _RE_TOKEN

ROTULO_MAX = 200                     # URLs de curso são longas; o do motor corta em 70
MASCARA = "«…»"
OMITIDO = "«rótulo omitido (termo sensível ao classificador)»"
_MAX_MASCARAS = 16


def _ancora(texto):
    return _RE_SESSAO.search(texto) or _RE_TOKEN.search(texto)


def casa_ancora_de_morte(texto) -> bool:
    """O texto casaria a âncora de sessão morta ou de credencial ruim do classificador?"""
    return _ancora(str(texto or "")) is not None


def rotulo_seguro(texto, *, maximo: int = ROTULO_MAX) -> str:
    """Rótulo pronto para IMPRIMIR: espaços colapsados, truncado em `maximo`, e cada trecho
    que casa uma âncora de morte trocado por «…» (reconferido até limpar; se não limpar,
    OMITIDO). Função PURA; nunca levanta."""
    try:
        t = " ".join(str(texto or "").split())
        if len(t) > maximo:
            t = t[: maximo - 1] + "…"
        for _ in range(_MAX_MASCARAS):
            m = _ancora(t)
            if m is None:
                return t or "«sem nome»"
            t = t[: m.start()] + MASCARA + t[m.end():]
        return OMITIDO
    except Exception:
        return OMITIDO
