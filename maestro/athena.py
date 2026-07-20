"""Camada 3 — INTERFACE de comandos da Athena no Telegram.

Duas responsabilidades, deliberadamente separadas:

  1. ACEITAR comandos do humano e despachá-los para a ação certa do Maestro:
       /status              -> estado dos serviços + captura
       /capturar <link>     -> enfileira um curso (INSERT na fila_captura)
       /restart <serviço>   -> reinicia um container conhecido
       /prioridade [<link>] -> mostra / reordena a fila de prioridade de captura
     A resposta a um comando é SOLICITADA: vai ao remetente (o humano acabou de
     pedir). Não é spam — é a resposta da pergunta dele.

  2. NOTIFICAR proativamente (broadcast ao operador) SÓ quando há uma DECISÃO
     humana a tomar. Evento de rotina (progresso, sucesso) é silencioso. Este é o
     único gate anti-spam da camada — e ele exige `requer_decisao is True`
     explícito (um valor truthy-mas-não-booleano NÃO vaza mensagem).

Reúso (não reimplementa):
  - `maestro.voz.Voz`     -> saída/escalação (broadcast ao operador) + o dataclass Comando.
  - `maestro.telegram_api.TelegramClient` -> seam de I/O (get_updates + send_message);
    injetável, dublado nos testes — NENHUMA rede real é aberta aqui.

INVIOLÁVEL 3 (anti-login / arquitetura do usuário): /capturar só ENFILEIRA (um
INSERT VPS-safe). NÃO abre Chrome, NÃO faz login. Quem executa o browser é o worker
residencial que reivindica a fila; a sessão morta é detectada por ele no claim.
"""
import time
from dataclasses import dataclass

from maestro.voz import Comando
from maestro.playbook import Acao
from maestro.sentinela import Problema

_VERBOS = {"status", "capturar", "restart", "prioridade"}


@dataclass(frozen=True)
class Evento:
    """Algo que aconteceu e PODE merecer notificação. Só vira mensagem se
    `requer_decisao is True` — o gate anti-spam vive em Athena.notificar."""
    tipo: str
    alvo: str
    detalhe: str
    requer_decisao: bool
    pedido: str = ""


def parse_comando(texto: str) -> Comando:
    """Parseia um comando-barra do Telegram. Reúsa o dataclass Comando da Voz.
    Aceita o sufixo de bot que o Telegram anexa em grupos (/status@MeuBot)."""
    t = (texto or "").strip()
    if not t.startswith("/"):
        return Comando("desconhecido", t)
    partes = t[1:].split(maxsplit=1)
    if not partes:
        return Comando("desconhecido", t)
    verbo = partes[0].lower().split("@", 1)[0]        # tira @botname de grupo
    arg = partes[1].strip() if len(partes) > 1 else ""
    if verbo in _VERBOS:
        return Comando(verbo, arg)
    return Comando("desconhecido", t)


class Athena:
    def __init__(self, tg, voz, *, acesso, executor=None, captura_fn=None,
                 prioridades=None, autorizados=None):
        self._tg = tg                    # seam de I/O (entrada + resposta ao remetente)
        self._voz = voz                  # saída/escalação (broadcast ao operador)
        self._acesso = acesso            # servicos() + restart()
        self._executor = executor        # .disparar(url) -> confirmação (contrato captura)
        self._captura_fn = captura_fn    # () -> str p/ a seção de captura do /status
        self._prioridades = prioridades if prioridades is not None else []
        # SEGURANÇA: chats que PODEM comandar a infra. None = sem restrição (uso
        # confiável/legado); produção DEVE passar o(s) chat(s) do operador —
        # senão qualquer um que ache o bot dispara /restart, /capturar.
        self._autorizados = set(autorizados) if autorizados is not None else None

    # -- resposta SOLICITADA: vai ao remetente do comando ---------------------
    def _responder(self, chat_id, texto: str) -> None:
        try:
            self._tg.send_message(chat_id, texto)
        except Exception:
            pass                         # best-effort; a ponte já tem retry

    # -- despacho de UM comando ----------------------------------------------
    def atender(self, chat_id, texto: str):
        # SEGURANÇA: só o operador comanda a infra. Comando de um chat estranho é
        # IGNORADO — sem agir e SEM responder (não confirmamos sequer que o bot
        # existe: nada de virar reflector/alvo de flood). Gate opt-in: se
        # `autorizados` é None (uso confiável), atende todos. rodar() passa todo
        # update por aqui, então este é o ÚNICO ponto de entrada a proteger.
        if self._autorizados is not None and chat_id not in self._autorizados:
            return None
        cmd = parse_comando(texto)
        try:
            if cmd.tipo == "status":
                return self._cmd_status(chat_id)
            if cmd.tipo == "capturar":
                return self._cmd_capturar(chat_id, cmd.arg)
            if cmd.tipo == "restart":
                return self._cmd_restart(chat_id, cmd.arg)
            if cmd.tipo == "prioridade":
                return self._cmd_prioridade(chat_id, cmd.arg)
        except Exception as e:
            # HONESTIDADE (I-1): um handler que quebra NÃO pode virar silêncio —
            # o operador precisa distinguir "nada a dizer" de "engasguei", senão
            # confia numa falha invisível. Responde o erro ao REMETENTE e segue;
            # o offset avança no rodar (sem retry cego que poderia, ex., reiniciar
            # duas vezes um serviço numa falha transiente).
            self._responder(chat_id, f"❌ falhei ao processar /{cmd.tipo}: {str(e)[:160]}")
            return Acao("", False, True, f"/{cmd.tipo} falhou")
        self._responder(chat_id, "🤔 não entendi. comandos: /status, "
                                 "/capturar <link>, /restart <serviço>, /prioridade")
        return None

    def _cmd_status(self, chat_id):
        servs = self._acesso.servicos()
        linhas = ["📊 Serviços:"]
        if not servs:
            linhas.append("  (nenhum serviço visível)")
        for nome, s in sorted(servs.items()):
            if s.up:
                estado = "🟢 up"
            elif s.restarting:
                estado = "🔁 restart-loop"
            else:
                estado = "🔴 caído"
            linhas.append(f"  • {nome}: {estado}")
        cap = self._captura_fn() if self._captura_fn else "(sem info)"
        linhas.append(f"🎥 Captura: {cap}")
        self._responder(chat_id, "\n".join(linhas))
        return None

    def _cmd_capturar(self, chat_id, link: str):
        link = link.strip()
        if not link:
            self._responder(chat_id, "uso: /capturar <link do curso>")
            return None
        if self._executor is None:
            self._responder(chat_id, "captura indisponível (sem executor configurado)")
            return None
        # INVIOLÁVEL 3: apenas ENFILEIRA (INSERT VPS-safe). Sem Chrome, sem login.
        try:
            conf = self._executor.disparar(link)
        except Exception as e:
            self._responder(chat_id, f"❌ falhei ao enfileirar {link}: {str(e)[:160]}")
            return Acao("", False, True, f"falha ao enfileirar {link}")
        if not conf:
            # honesto (I-1): sem confirmação do executor, NÃO assumo sucesso.
            self._responder(chat_id, f"❓ enfileiramento de {link} sem confirmação "
                                     f"— não assumo sucesso")
            return Acao("", False, True, f"enfileiramento de {link} sem confirmação")
        self._responder(chat_id, f"✅ curso enfileirado: {conf}")
        return Acao(f"enfileirei {link}", True, False)

    def _cmd_restart(self, chat_id, servico: str):
        servico = servico.strip()
        if not servico:
            self._responder(chat_id, "uso: /restart <serviço>")
            return None
        conhecidos = self._acesso.servicos()
        if servico not in conhecidos:
            nomes = ", ".join(sorted(conhecidos)) or "(nenhum)"
            self._responder(chat_id, f"serviço '{servico}' não encontrado. "
                                     f"conheço: {nomes}")
            return None
        self._acesso.restart(servico)
        self._responder(chat_id, f"🔧 reiniciei o serviço '{servico}'")
        return Acao(f"reiniciei {servico}", True, False)

    def _cmd_prioridade(self, chat_id, arg: str):
        arg = arg.strip()
        if not arg:
            if self._prioridades:
                corpo = "\n".join(f"  {i + 1}. {u}"
                                  for i, u in enumerate(self._prioridades))
            else:
                corpo = "  (fila de prioridade vazia)"
            self._responder(chat_id, f"⭐ Prioridade de captura:\n{corpo}")
            return None
        # DECISÃO: move o curso para o topo (muta a lista compartilhada in-place,
        # p/ o loop de captura ler a nova ordem no próximo ciclo). Sem duplicar.
        if arg in self._prioridades:
            self._prioridades.remove(arg)
        self._prioridades.insert(0, arg)
        self._responder(chat_id, f"⭐ prioridade atualizada: '{arg}' no topo")
        return Acao(f"prioridade '{arg}' no topo", True, False)

    # -- NOTIFICAÇÃO proativa: só quando há DECISÃO real (anti-spam) ----------
    def notificar(self, evento: Evento) -> bool:
        """Emite (broadcast ao operador via Voz) SÓ quando `evento.requer_decisao
        is True`. Retorna se emitiu. `is True` (não `if truthy`) é proposital:
        um valor truthy-mas-não-booleano não pode vazar spam pelo gate."""
        if evento.requer_decisao is not True:
            return False
        pedido = evento.pedido or evento.detalhe
        self._voz.escalar(
            Problema(evento.tipo, evento.alvo, evento.detalhe, "aviso"), pedido)
        return True

    # -- laço de recepção: long-poll, despacha, AVANÇA o offset --------------
    def rodar(self, *, intervalo=25, max_iters=None, sleep=time.sleep):
        """Long-poll dos updates via o seam TelegramClient. Avança o offset para
        cada update tratado — senão o mesmo comando seria reprocessado sempre.
        Tolerante a falha por-update e por-ciclo (um erro não derruba o laço)."""
        offset = 0
        i = 0
        while max_iters is None or i < max_iters:
            i += 1
            try:
                updates = self._tg.get_updates(offset, timeout=intervalo)
            except Exception:
                sleep(intervalo)
                continue
            for u in updates:
                try:
                    self.atender(u.chat_id, u.texto)
                except Exception:
                    pass                 # um comando ruim não pode matar o laço
                offset = max(offset, u.update_id + 1)
            if not updates:
                sleep(min(intervalo, 1))
        return i
