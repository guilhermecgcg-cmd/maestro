"""Alertas de supervisão da Athena via Telegram. A Athena chama estas funções em
eventos de supervisão — captura morreu, sessão expirou, curso concluído — e o dono
recebe no Telegram. Reusa o TelegramClient da ponte (retry/429 já embutido). Só
SAÍDA: nenhuma porta de entrada nova (INVIOLÁVEL 3).

Fail-safe é a regra central deste módulo: sem token/cliente, o alerta vira log e
NUNCA levanta. O canal que existe para avisar que o supervisor caiu jamais pode ser
ele próprio quem derruba o supervisor — por isso todo envio é best-effort e toda
exceção do Telegram é engolida (a ponte já tem retry para o hiccup transitório).

GATE DE ESSENCIALIDADE (fix do ruído 22/07): o Telegram só recebe o que exige AÇÃO
HUMANA ou é incidente real; todo o resto (flap que re-tenta sozinho, morte transitória
auto-tratada) vira SÓ-LOG — o arquivo de log continua registrando
TUDO, só o CANAL fica limpo. O call-site declara `essencial=True` no que pede humano;
`ATHENA_ALERTA_NIVEL=tudo` restaura o comportamento antigo (debug). Regra fail-safe
de classificação: pede ação humana => essencial; auto-tratado => só-log.

O BENCH exit-5 SAIU do só-log (achado r6): ele PARA o curso até alguém corrigir a URL /
o adaptador — num adaptador novo, um defeito permanente ficava calado. O loop o marca
essencial com dedup por curso (`chave=("bench", curso)`).

INVARIANTE (anti-ban): `sessao_expirada` (reseed) NUNCA passa pelo gate nem pelo
dedup — é o único jeito de o dono saber que precisa refazer o login.

DEDUP: um alerta essencial com `chave` (ex.: ("humano", curso)) não repete dentro da
janela `ATHENA_ALERTA_DEDUP_S` (default 1h) — 2 mortes iguais do mesmo curso viram
1 ping + log. Estado em memória: reinício do daemon zera (na dúvida, re-alerta)."""
import logging
import os
import time

from maestro.telegram_api import TelegramClient

log = logging.getLogger("athena.alertas")

# NÍVEIS — decisão do dono (14/09): **`mudo` é o padrão**. A Athena não procura ninguém.
#
# Ele foi explícito duas vezes: "a Athena no telegram fica apenas para interação... não
# uma lista de afazeres e pendência" e depois "apagar a possibilidade de envio de avisos
# aleatórios — eu quero saber da captura quando eu pedir". Então o silêncio não é uma
# configuração que alguém pode esquecer ligada: é o comportamento de fábrica, e o envio
# passou a exigir um ato deliberado (`ATHENA_ALERTA_NIVEL`).
#
# NADA se perde: todo alerta continua indo para o LOG, e é de lá que os relatórios
# (`/sitrep`, `/sitrepcaptura`) tiram o que responder quando ELE pergunta.
#
# O preço, dito com todas as letras: sessão morta e login pendente também param de
# procurá-lo. A captura pode ficar dias parada esperando um login sem nada avisar — é
# ele quem descobre, perguntando. `ATHENA_ALERTA_NIVEL=essencial` devolve o
# comportamento anterior (só o que pede ação humana) e `tudo` é o modo de depuração.
_NIVEIS = ("mudo", "essencial", "tudo")
_DEDUP_JANELA_PADRAO_S = 3600.0


class Alertas:
    """Emite alertas de supervisão. `tg=None` => modo só-log (fail-safe), para a
    Athena poder chamar alertas mesmo sem canal Telegram configurado.

    `nivel`: 'essencial' (default — só o que exige ação humana pinga o Telegram),
    'so_humano' (o mesmo, menos notícia boa e o que o sistema trata sozinho) ou
    'tudo' (comportamento antigo: tudo pinga). `dedup_janela_s` e `relogio`
    são injetáveis para teste; defaults vêm do ambiente/`time.time`."""

    def __init__(self, tg, chat_ids, nivel=None, dedup_janela_s=None,
                 relogio=time.time):
        self._tg = tg
        self._chats = list(chat_ids)
        nivel = (nivel or os.getenv("ATHENA_ALERTA_NIVEL", "mudo"))
        nivel = str(nivel).strip().lower()
        # fail-safe: valor desconhecido cai no PADRÃO (mudo). Antes ele caía em
        # 'essencial' — com o silêncio virando a regra, errar para o lado de FALAR
        # passaria a contrariar a decisão do dono a cada typo de env.
        self._nivel = nivel if nivel in _NIVEIS else "mudo"
        if dedup_janela_s is None:
            try:
                dedup_janela_s = float(
                    os.getenv("ATHENA_ALERTA_DEDUP_S", str(_DEDUP_JANELA_PADRAO_S)))
            except (TypeError, ValueError):
                dedup_janela_s = _DEDUP_JANELA_PADRAO_S
        self._dedup_janela_s = float(dedup_janela_s)
        self._relogio = relogio
        self._ultimo_envio = {}          # chave -> ts do último envio Telegram

    def _enviar(self, texto: str) -> bool:
        """True = o texto CHEGOU a pelo menos um chat (só isso conta pro dedup).

        Este é o ÚNICO ponto por onde qualquer alerta sai — por isso o `mudo` mora
        aqui: não existe método que o contorne, nem um futuro que esqueça de checar."""
        if self._nivel == "mudo":
            log.warning("alerta MUDO (nível mudo — só-log, sem Telegram): %s", texto)
            return False
        if self._tg is None or not self._chats:
            log.warning("alerta sem canal Telegram (só-log): %s", texto)
            return False
        chegou = False
        for chat in self._chats:
            try:
                self._tg.send_message(chat, texto)
                chegou = True
            except Exception:
                # best-effort: o alerta nunca pode derrubar quem o chamou.
                log.exception("falha ao enviar alerta para chat %s", chat)
        return chegou

    def _suprimido_nao_essencial(self, essencial: bool, texto: str) -> bool:
        """True = fica SÓ no log (auto-tratado; nenhuma ação do dono é necessária).

        Continua valendo para quem LIGAR o canal (`ATHENA_ALERTA_NIVEL=essencial`): é o
        corte que separa "o sistema se resolve" de "a captura parou esperando você".
        No padrão `mudo` ele nem chega a ser consultado — o silêncio é decidido antes,
        no `_enviar`."""
        if essencial or self._nivel == "tudo":
            return False
        log.warning("alerta NÃO-essencial (só-log, sem Telegram): %s", texto)
        return True

    def _dedup_repetido(self, chave, texto: str) -> bool:
        """True = mesma chave já ENVIOU (com sucesso) dentro da janela — vira só-log.
        Fail-safe total: sem chave, em nivel='tudo' (comportamento antigo integral),
        com relógio quebrado ou chave não-hashable => ENVIA (o dedup jamais engole
        por bug, e jamais levanta pro chamador)."""
        if chave is None or self._dedup_janela_s <= 0 or self._nivel == "tudo":
            return False
        try:
            agora = float(self._relogio())
            ts = self._ultimo_envio.get(chave)
        except Exception:
            return False
        if ts is not None and (agora - ts) < self._dedup_janela_s:
            log.warning("alerta dedupado (mesma chave na janela, só-log): %s", texto)
            return True
        return False

    def _marcar_enviado(self, chave) -> None:
        """Registra o envio BEM-SUCEDIDO da chave (review IMPORTANTE-1: um envio
        que FALHOU não pode armar o dedup — a próxima morte re-tenta o Telegram).
        Nunca levanta (chave não-hashable/relógio quebrado => só não marca)."""
        if chave is None:
            return
        try:
            self._ultimo_envio[chave] = float(self._relogio())
        except Exception:
            pass

    def captura_morreu(self, plataforma: str, motivo: str, *,
                       essencial: bool = False, chave=None) -> None:
        """Morte de captura/sistema. `essencial=True` SÓ quando o evento exige ação
        humana (token/desconhecida/escalada/bench exit-5) — flap/transitório
        auto-tratados ficam no default só-log. `chave` (hashable, ex.: ("humano", curso)) dedupa
        o MESMO alerta do MESMO curso dentro da janela."""
        texto = (f"🔴 Captura {plataforma} MORREU — {motivo}. "
                 f"Fila sem supervisão; retomar exige olhar humano.")
        if self._suprimido_nao_essencial(essencial, texto):
            return
        chave_dedup = ("captura_morreu", chave) if chave is not None else None
        if self._dedup_repetido(chave_dedup, texto):
            return
        if self._enviar(texto):
            self._marcar_enviado(chave_dedup)

    def maquina_sobrecarregada(self, motivo: str, *, essencial: bool = False,
                               chave=None) -> None:
        """Aviso AGREGADO do portão de carga (maestro.carga): disparos de captura ADIADOS
        porque o Mac está sufocado. Adiamento é auto-tratado (retoma sozinho) => só-log
        por default; o loop marca `essencial=True` só quando a sobrecarga PERSISTE
        (ATHENA_CARGA_ALERTA_S) — aí o dono precisa saber que nada está sendo capturado.
        `chave` dedupa dentro da janela (1 ping por janela, nunca 1 por curso)."""
        texto = (f"🟠 Máquina sobrecarregada — {motivo}. Disparos de captura ADIADOS "
                 f"(não é falha; retomo sozinho quando a carga baixar).")
        if self._suprimido_nao_essencial(essencial, texto):
            return
        chave_dedup = ("maquina_sobrecarregada", chave) if chave is not None else None
        if self._dedup_repetido(chave_dedup, texto):
            return
        if self._enviar(texto):
            self._marcar_enviado(chave_dedup)

    def _enviar_ou_so_log(self, texto: str) -> bool:
        """Envio SEM dedup local (o dedup destes é do zelador, PERSISTIDO no status dele).
        True = entregue OU não há canal (só-log: nada a re-tentar); False = havia canal e
        falhou (o zelador re-tenta no próximo ciclo em vez de armar o dedup)."""
        if self._nivel == "mudo" or self._tg is None or not self._chats:
            # True = "nada a re-tentar". No `mudo` isso importa: devolver False faria
            # o zelador re-tentar o mesmo alerta a cada ciclo, para sempre.
            log.warning("alerta só-log (mudo ou sem canal): %s", texto)
            return True
        return self._enviar(texto)

    def logins_pendentes(self, alvos, comando: str) -> bool:
        """P7 — UM alerta com TODAS as contas cuja sessão o ZELADOR provou morta (`alvos` =
        `plataforma:conta`, a sintaxe do reseed) e o COMANDO EXATO que as resolve numa
        sentada. Essencial por natureza (só o humano loga) e fora do gate: o zelador já
        dedupa pelo CONJUNTO, e o dedup dele sobrevive a restart."""
        alvos = list(alvos)
        n = len(alvos)
        texto = (f"🔑 login necessário: {', '.join(alvos)} — sessão morta (provado pela "
                 f"sonda da plataforma). No Mac, no checkout do motor, rode `{comando}` "
                 f"e resolva {'as ' + str(n) + ' contas' if n > 1 else 'a conta'} numa "
                 f"sentada. Até lá o zelador não toca {'nelas' if n > 1 else 'nela'}.")
        return self._enviar_ou_so_log(texto)

    def sessao_vence(self, alvo: str, quando: str, dias: float, comando: str) -> bool:
        """P7 — alerta PREVENTIVO: o relógio da sessão é DURO (não avançou depois de uma
        prova de vida) e vence em menos de N dias. Dedup pelo VALOR do relógio, no zelador."""
        if dias > 0:
            prazo = f"vence em ~{f'{dias:.1f}'.replace('.', ',')} dia(s) ({quando})"
        else:
            prazo = f"venceu em {quando}"
        texto = (f"⏳ Sessão {alvo} {prazo} e não se renova sozinha — faça o login antes "
                 f"de ela cair: `{comando}` (no Mac, no checkout do motor).")
        return self._enviar_ou_so_log(texto)

    def sessao_expirada(self, plataforma: str) -> None:
        # INVARIANTE: reseed é sempre essencial e NUNCA dedupado — só o humano
        # destrava, e engolir este alerta deixaria a conta parada em silêncio.
        self._enviar(f"⚠️ Sessão {plataforma} expirou — refaça o login "
                     f"para a captura voltar a andar.")

    def curso_concluido(self, plataforma: str, curso: str, n: int) -> None:
        # Positivo, raro (1× por curso) => essencial por natureza; dedup por curso
        # protege contra um chamador futuro que repita o evento a cada ciclo.
        # No nível `so_humano` ele NÃO passa: é notícia boa, não pedido de ação — e
        # notícia boa empurrada é justamente o feed que o dono mandou desligar. Ele
        # vê o mesmo número quando pergunta (`/sitrepcaptura`).
        texto = (f"✅ {plataforma}: curso '{curso}' concluído — "
                 f"{n} aulas capturadas.")
        if self._suprimido_nao_essencial(True, texto):
            return
        chave_dedup = ("curso_concluido", plataforma, curso)
        if self._dedup_repetido(chave_dedup, texto):
            return
        if self._enviar(texto):
            self._marcar_enviado(chave_dedup)


def de_ambiente() -> "Alertas":
    """Constrói do ambiente (.env), no padrão de ponte/config.py. Sem
    TELEGRAM_BOT_TOKEN → modo só-log (fail-safe): a Athena chama alertas mesmo
    sem canal, e o build nunca explode por variável ausente. `ATHENA_ALERTA_NIVEL`
    (essencial|tudo) e `ATHENA_ALERTA_DEDUP_S` calibram o gate/dedup."""
    from dotenv import load_dotenv

    load_dotenv()
    ids = [int(x) for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x]
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN ausente — Alertas em modo só-log")
        return Alertas(None, ids)
    return Alertas(TelegramClient(token), ids)
