"""Capacidade B — Orquestrador da criação AUTÔNOMA de adaptadores de plataforma.

Dada a URL de uma plataforma NOVA (Kiwify, Nutror, Alpaclass, Kajabi, Hubla…), a
Athena não constrói o adaptador na mão: ela roda ESTE pipeline, que encadeia os
estágios na ordem certa e tranca cada porta. Este módulo é só o ORQUESTRADOR — não
sabe nada de nenhuma plataforma específica; cada estágio é uma COSTURA injetável
(callable), pra rodar contra dublês nos testes e contra os agentes/sistemas reais
em produção.

O fluxo (o que hoje o humano faz na mão):

    recon → [Portão POP] → spec → build (subagente) → [review = Portão POP]
          → deploy → validação

As COSTURAS (todas injetadas em `rodar_pipeline`):
  * `recon(url)            -> tem .ok`  — recon autenticado (sessão residencial):
    identifica plataforma, player, listagem de aulas, esquema de sessão/anti-ban.
  * `portao(artefato)      -> tem .pronto` — dispara o Portão de Qualidade POP
    (Capacidade A). Roda DUAS vezes: no recon (antes do spec) e no build (o review
    depois do build). `.pronto` == review LIMPO; qualquer coisa < limpo REPROVA.
  * `escrever_spec(recon)  -> tem .ok`  — spec do adaptador (só depois do Portão).
  * `build(spec)           -> tem .ok`  — build TDD via SUBAGENTE.
  * `deploy(build)         -> tem .ok`  — publica o adaptador na boca de entrada.
  * `validar(deploy)       -> tem .ok`  — validação AO VIVO (I-1: prova no Notion).

As TRAVAS invioláveis (o que os testes trancam):
  1. Não avança sem a etapa anterior — `pode_avancar` exige a flag do passo prévio.
  2. Portão POP é OBRIGATÓRIO antes do spec E depois do build. Portão sujo antes do
     spec: o spec não roda. Portão sujo no review: o deploy é BARRADO (nem chamado)
     — `deploy` exige `review_ok`, não basta `build_ok`.
  3. Fail-closed: qualquer estágio que não devolve `.ok` (ou portão não-`pronto`)
     PARA o pipeline e ESCALA — nunca declara pronto no escuro.
  4. I-1: só é `pronto` com a validação ao vivo confirmada (`validado`); deploy sem
     validação NÃO é pronto (mata o falso-pronto)."""
from dataclasses import dataclass, replace


# A ordem canônica dos estágios cujo AVANÇO depende da etapa anterior. "recon" é o
# começo (não tem pré-requisito); "review" é o Portão POP pós-build, que produz
# `review_ok` — e é ele, não `build_ok`, que o deploy exige.
ORDEM = ("recon", "spec", "build", "review", "deploy", "validar")

# Pré-condição de cada estágio: a flag do EstadoPipeline que PRECISA estar setada
# pra ele poder rodar. "recon" não aparece -> sempre pode começar.
_PRECOND = {
    "spec": "recon_ok",
    "build": "spec_ok",
    "review": "build_ok",
    "deploy": "review_ok",   # <- a trava central: review, não build
    "validar": "deploy_ok",
}


@dataclass(frozen=True)
class EstadoPipeline:
    """Estado do pipeline em flags booleanas. Fonte única da verdade sobre 'até onde
    chegou'. Imutável: cada estágio bem-sucedido produz um NOVO estado."""
    recon_ok: bool = False
    spec_ok: bool = False
    build_ok: bool = False
    review_ok: bool = False
    deploy_ok: bool = False
    validado: bool = False


@dataclass(frozen=True)
class EtapaTrilha:
    """Registro auditável de UM estágio executado."""
    etapa: str
    ok: bool
    motivo: str = ""


@dataclass(frozen=True)
class ResultadoPipeline:
    """`pronto` só é True com `validado` (I-1). `escalar` = precisa do humano
    (estágio falhou ou Portão reprovou). `parou_em` = onde travou (None se completou
    até validar). `trilha` = a sequência auditável do que rodou."""
    estado: EstadoPipeline
    pronto: bool
    escalar: bool
    motivo: str
    parou_em: str | None = None
    trilha: tuple = ()


def pode_avancar(estado: EstadoPipeline, etapa: str) -> bool:
    """A trava 'não pula etapa': True só se a flag da etapa ANTERIOR está setada.
    `recon` não tem pré-requisito. Estágio desconhecido -> False (fail-closed)."""
    if etapa == "recon":
        return True
    flag = _PRECOND.get(etapa)
    if flag is None:
        return False
    return bool(getattr(estado, flag))


def _motivo_portao(res) -> str:
    return str(getattr(res, "motivo", "") or "Portão POP reprovou")


def _motivo(res, padrao: str) -> str:
    return str(getattr(res, "motivo", "") or padrao)


def rodar_pipeline(
    url,
    *,
    recon,
    portao,
    escrever_spec,
    build,
    deploy,
    validar,
):
    """Roda o pipeline de criação de adaptador ponta-a-ponta, trancando cada porta.
    Devolve um ResultadoPipeline. NUNCA avança pulando etapa (asserção defensiva via
    `pode_avancar`); NUNCA declara pronto sem a validação ao vivo (fail-closed)."""
    estado = EstadoPipeline()
    trilha = []

    def _para(parou_em, motivo):
        return ResultadoPipeline(
            estado=estado, pronto=False, escalar=True, motivo=motivo,
            parou_em=parou_em, trilha=tuple(trilha))

    # 1) RECON autenticado. É o começo — sempre pode rodar.
    r_recon = recon(url)
    if not getattr(r_recon, "ok", False):
        motivo = _motivo(r_recon, "recon falhou")
        trilha.append(EtapaTrilha("recon", False, motivo))
        return _para("recon", motivo)
    estado = replace(estado, recon_ok=True)
    trilha.append(EtapaTrilha("recon", True))

    # 2) PORTÃO POP #1 — obrigatório ANTES do spec (review do recon + código-base).
    p1 = portao(r_recon)
    if not getattr(p1, "pronto", False):
        motivo = _motivo_portao(p1)
        trilha.append(EtapaTrilha("portao_pre_spec", False, motivo))
        return _para("portao_pre_spec", motivo)
    trilha.append(EtapaTrilha("portao_pre_spec", True))

    # 3) SPEC do adaptador (só depois do Portão limpo).
    assert pode_avancar(estado, "spec")  # trava: exige recon_ok
    r_spec = escrever_spec(r_recon)
    if not getattr(r_spec, "ok", False):
        motivo = _motivo(r_spec, "spec falhou")
        trilha.append(EtapaTrilha("spec", False, motivo))
        return _para("spec", motivo)
    estado = replace(estado, spec_ok=True)
    trilha.append(EtapaTrilha("spec", True))

    # 4) BUILD via SUBAGENTE (TDD).
    assert pode_avancar(estado, "build")  # trava: exige spec_ok
    r_build = build(r_spec)
    if not getattr(r_build, "ok", False):
        motivo = _motivo(r_build, "build falhou")
        trilha.append(EtapaTrilha("build", False, motivo))
        return _para("build", motivo)
    estado = replace(estado, build_ok=True)
    trilha.append(EtapaTrilha("build", True))

    # 5) REVIEW = PORTÃO POP #2 — obrigatório DEPOIS do build. Review não-clean
    #    BLOQUEIA o deploy: review_ok só nasce de um Portão limpo.
    assert pode_avancar(estado, "review")  # trava: exige build_ok
    p2 = portao(r_build)
    if not getattr(p2, "pronto", False):
        motivo = _motivo_portao(p2)
        trilha.append(EtapaTrilha("review", False, motivo))
        return _para("review", motivo)
    estado = replace(estado, review_ok=True)
    trilha.append(EtapaTrilha("review", True))

    # 6) DEPLOY — só com review_ok (não basta build_ok).
    assert pode_avancar(estado, "deploy")  # trava: exige review_ok
    r_deploy = deploy(r_build)
    if not getattr(r_deploy, "ok", False):
        motivo = _motivo(r_deploy, "deploy falhou")
        trilha.append(EtapaTrilha("deploy", False, motivo))
        return _para("deploy", motivo)
    estado = replace(estado, deploy_ok=True)
    trilha.append(EtapaTrilha("deploy", True))

    # 7) VALIDAÇÃO ao vivo (I-1): sem prova, NÃO é pronto.
    assert pode_avancar(estado, "validar")  # trava: exige deploy_ok
    r_val = validar(r_deploy)
    if not getattr(r_val, "ok", False):
        motivo = _motivo(r_val, "validação ao vivo não confirmou")
        trilha.append(EtapaTrilha("validar", False, motivo))
        return _para("validar", motivo)
    estado = replace(estado, validado=True)
    trilha.append(EtapaTrilha("validar", True))

    return ResultadoPipeline(
        estado=estado, pronto=True, escalar=False,
        motivo="adaptador validado ao vivo — pronto (provado, não presumido)",
        parou_em=None, trilha=tuple(trilha))
