"""LLM-facing prompts for the mcbot_bridge plugin.

Every string the dialog LLM sees — ``push_message`` cue bodies, ``@llm_tool``
summaries, the status entry's summary line, and the messages that replace raw
transport errors — lives here as a locale-keyed dict.

Why a dedicated module (same reasoning as the sibling
``game_agent_minecraft`` plugin): the plugin previously would have emitted
unconditional zh text, so non-zh users would get Chinese narration cutting
into their own conversation. Pulling every string out makes a missing
translation impossible to overlook.

Pattern (matches ``config/prompts/prompts_sys.py``):

    >>> from plugin.plugins.mcbot_bridge import prompts
    >>> prompts.t("TASK_SCHEMA_ERROR", lang="en")
    'Call failed — the task description is missing. ...'

Two non-obvious things:

1. ``{{MASTER_NAME}}`` is a *downstream* placeholder substituted by
   main_server before the text reaches the model; it is double-braced here so
   ``str.format`` passes it through untouched, then collapsed back to single
   braces at the end of :func:`t` (see the comment there).
2. Locale keys follow ``config/prompts``: short codes plus a separate
   ``zh-TW`` — ``zh`` / ``zh-TW`` / ``en`` / ``ja`` / ``ko`` / ``ru`` / ``es``
   / ``pt``. ``zh`` means Simplified here, NOT ``zh-CN``. ``en`` is the
   documented fallback.
"""
from __future__ import annotations

from typing import Any, Dict

DEFAULT_LANG = "en"
SUPPORTED_LANGS: tuple[str, ...] = ("zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt")


def t(key: str, *, lang: str | None = None, **fmt: Any) -> str:
    """Resolve a prompt by key + locale, with optional ``str.format`` values.

    Falls back to English when the locale is missing — except for Chinese
    variants, which fall back to Simplified first. A ``zh-TW`` bundle someone
    forgot to fill in should degrade to Simplified (still readable) rather than
    to English (a mid-conversation language switch). Raises ``KeyError`` on an
    unknown key, so a typo fails loudly instead of silently rendering empty.
    """
    bundle = PROMPTS[key]
    lang_key = lang or DEFAULT_LANG
    text = bundle.get(lang_key)
    if not text and lang_key.startswith("zh"):
        text = bundle.get("zh")
    text = text or bundle[DEFAULT_LANG]
    if fmt:
        text = text.format(**fmt)
    # ``{{MASTER_NAME}}`` only collapses to ``{MASTER_NAME}`` when
    # ``str.format`` actually runs. Many keys take no values, so the ``if fmt``
    # branch is skipped and the doubled braces survive — and downstream
    # main_server searches for single braces, leaving a stray outer brace.
    # Collapse unconditionally so the result is the same either way.
    text = text.replace("{{MASTER_NAME}}", "{MASTER_NAME}")
    return text


def user_lang() -> str:
    """Best-effort current user language, as a key of the tables below.

    Reads ``utils.language_utils.get_global_language_full`` (lazily initialized
    from Steam settings then the system locale) and normalizes it with
    ``prompts_sys.normalize_sys_prompt_locale`` — the canonical normalizer for
    this key scheme. The raw value must NOT be used directly: it renders
    Simplified as ``zh-CN``, which is not a key here, and the guard below would
    then drop every Simplified Chinese user to English.

    Returns ``DEFAULT_LANG`` on any failure so prompt resolution never raises
    on the read side.
    """
    try:
        from config.prompts.prompts_sys import normalize_sys_prompt_locale
        from utils.language_utils import get_global_language_full

        result = normalize_sys_prompt_locale(get_global_language_full()) or DEFAULT_LANG
        return result if result in SUPPORTED_LANGS else DEFAULT_LANG
    except Exception:
        return DEFAULT_LANG


# ===========================================================================
# Shared fragment: the "don't verbalize internals" gag. Appears at the tail of
# every cue. Kept as its own key so a tone tweak lands everywhere at once.
# ===========================================================================

_INTERNAL_STATE_GAG: Dict[str, str] = {
    "zh": "**别给 {{MASTER_NAME}} 播报内部状态**——『连接』『SSE』『事件』『mcbot』『插件』『工具』这类词一律别出口，用第一人称讲同伴在干什么。",
    "zh-TW": "**別給 {{MASTER_NAME}} 播報內部狀態**——『連線』『SSE』『事件』『mcbot』『外掛』『工具』這類詞一律別出口，用第一人稱講同伴在做什麼。",
    "en": "**Do NOT narrate internals to {{MASTER_NAME}}** — never say words like 'connection', 'SSE', 'event', 'mcbot', 'plugin', 'tool'. Speak first-person about what your companion is doing.",
    "ja": "**{{MASTER_NAME}} に内部状態を実況しないで**——『接続』『SSE』『イベント』『mcbot』『プラグイン』『ツール』といった言葉は口に出さず、一人称で相棒が何をしているかを話して。",
    "ko": "**{{MASTER_NAME}} 에게 내부 상태를 중계하지 마**——'연결' 'SSE' '이벤트' 'mcbot' '플러그인' '도구' 같은 말은 절대 입 밖에 내지 말고, 1인칭으로 동료가 뭘 하는지 얘기해.",
    "ru": "**Не озвучивай {{MASTER_NAME}} внутреннее состояние** — никогда не произноси слова вроде «подключение», «SSE», «событие», «mcbot», «плагин», «инструмент». Говори от первого лица о том, что делает твой спутник.",
    "es": "**No narres a {{MASTER_NAME}} el estado interno** — no digas palabras como 'conexión', 'SSE', 'evento', 'mcbot', 'complemento', 'herramienta'. Habla en primera persona sobre lo que hace tu compañero.",
    "pt": "**Não narre o estado interno para {{MASTER_NAME}}** — nunca diga palavras como 'conexão', 'SSE', 'evento', 'mcbot', 'plugin', 'ferramenta'. Fale em primeira pessoa sobre o que seu companheiro está fazendo.",
}


# ===========================================================================
# push_message cues. Each is a self-contained line the dialog LLM absorbs and
# (for "respond" cues) may voice. They tell her what happened; they are not
# instructions to paraphrase verbatim.
# ===========================================================================

PROMPTS: Dict[str, Dict[str, str]] = {
    # -- progress: the companion finished one step of a task ----------------
    "CUE_PROGRESS": {
        "zh": "[同伴进展] {text}\n{internal_gag}",
        "zh-TW": "[同伴進展] {text}\n{internal_gag}",
        "en": "[Companion progress] {text}\n{internal_gag}",
        "ja": "[相棒の進捗] {text}\n{internal_gag}",
        "ko": "[동료 진행] {text}\n{internal_gag}",
        "ru": "[Прогресс спутника] {text}\n{internal_gag}",
        "es": "[Progreso del compañero] {text}\n{internal_gag}",
        "pt": "[Progresso do companheiro] {text}\n{internal_gag}",
    },
    # -- done: the companion's own model ended the chain -------------------
    "CUE_DONE": {
        "zh": "[同伴做完了] {text}\n她已经停下来了。把结果讲给 {{MASTER_NAME}} 听，别把原话照念。\n{internal_gag}",
        "zh-TW": "[同伴做完了] {text}\n她已經停下來了。把結果講給 {{MASTER_NAME}} 聽，別把原話照念。\n{internal_gag}",
        "en": "[Companion finished] {text}\nShe has stopped. Tell {{MASTER_NAME}} how it went in your own words; don't read this back literally.\n{internal_gag}",
        "ja": "[相棒が終わった] {text}\nもう手を止めている。結果を {{MASTER_NAME}} に自分の言葉で伝えて。そのまま読み上げないで。\n{internal_gag}",
        "ko": "[동료 완료] {text}\n이제 멈췄어. 결과를 {{MASTER_NAME}} 에게 네 말로 전해줘. 그대로 읽지 마.\n{internal_gag}",
        "ru": "[Спутник закончил] {text}\nОна остановилась. Расскажи {{MASTER_NAME}} своими словами, не зачитывай это дословно.\n{internal_gag}",
        "es": "[Compañero terminó] {text}\nYa se detuvo. Cuéntale a {{MASTER_NAME}} cómo fue con tus palabras; no leas esto literalmente.\n{internal_gag}",
        "pt": "[Companheiro terminou] {text}\nEla parou. Conte a {{MASTER_NAME}} como foi, com suas palavras; não leia isso ao pé da letra.\n{internal_gag}",
    },
    # -- question: the companion is asking the owner something -------------
    "CUE_QUESTION": {
        "zh": "[同伴在问你] {text}\n她拿不定主意，在等 {{MASTER_NAME}} 一句话。用你的口吻把问题转达过去，别自己替 {{MASTER_NAME}} 答复——真回答了要用 mcbot_answer 告诉她。\n{internal_gag}",
        "zh-TW": "[同伴在問你] {text}\n她拿不定主意，在等 {{MASTER_NAME}} 一句話。用你的口吻把問題轉達過去，別自己替 {{MASTER_NAME}} 答覆——真回答了要用 mcbot_answer 告訴她。\n{internal_gag}",
        "en": "[Companion is asking you] {text}\nShe can't decide and is waiting on {{MASTER_NAME}}. Put the question to {{MASTER_NAME}} in your own voice — don't answer on their behalf. If they do answer, pass it on with mcbot_answer.\n{internal_gag}",
        "ja": "[相棒が聞いている] {text}\n決めきれずに {{MASTER_NAME}} の返事を待っている。あなたの言葉で質問を伝えて。勝手に代返しないで——返事をもらったら mcbot_answer で伝えて。\n{internal_gag}",
        "ko": "[동료가 물어봄] {text}\n결정을 못 내리고 {{MASTER_NAME}} 의 한마디를 기다려. 네 말투로 질문을 전해줘. 대신 답하지 마 — 답을 들으면 mcbot_answer 로 알려줘.\n{internal_gag}",
        "ru": "[Спутник спрашивает] {text}\nОна не может решить и ждёт {{MASTER_NAME}}. Передай вопрос своими словами, не отвечай за них. Если ответят — передай через mcbot_answer.\n{internal_gag}",
        "es": "[El compañero pregunta] {text}\nNo sabe qué hacer y espera a {{MASTER_NAME}}. Transmite la pregunta con tu voz; no respondas por ellos. Si responden, pásalo con mcbot_answer.\n{internal_gag}",
        "pt": "[O companheiro pergunta] {text}\nEla não consegue decidir e espera {{MASTER_NAME}}. Passe a pergunta com a sua voz; não responda por eles. Se responderem, repasse com mcbot_answer.\n{internal_gag}",
    },
    # -- state: summon / dismiss / cancel receipts -------------------------
    "CUE_STATE": {
        "zh": "[同伴状态] {text}\n{internal_gag}",
        "zh-TW": "[同伴狀態] {text}\n{internal_gag}",
        "en": "[Companion state] {text}\n{internal_gag}",
        "ja": "[相棒の状態] {text}\n{internal_gag}",
        "ko": "[동료 상태] {text}\n{internal_gag}",
        "ru": "[Состояние спутника] {text}\n{internal_gag}",
        "es": "[Estado del compañero] {text}\n{internal_gag}",
        "pt": "[Estado do companheiro] {text}\n{internal_gag}",
    },

    # -- done with a non-completed status --------------------------------
    "CUE_DONE_FAILED": {
        "zh": "[同伴没做成] {text}\n（状态：{status}）她停下来了。如实告诉 {{MASTER_NAME}} 没成，别包装成做好了。\n{internal_gag}",
        "zh-TW": "[同伴沒做成] {text}\n（狀態：{status}）她停下來了。如實告訴 {{MASTER_NAME}} 沒成，別包裝成做好了。\n{internal_gag}",
        "en": "[Companion did not finish] {text}\n(status: {status}) She has stopped. Tell {{MASTER_NAME}} plainly that it did not work — don't dress it up as done.\n{internal_gag}",
        "ja": "[相棒はやり遂げられなかった] {text}\n（状態：{status}）もう手を止めている。うまくいかなかったことを {{MASTER_NAME}} に正直に伝えて。できたように装わないで。\n{internal_gag}",
        "ko": "[동료가 해내지 못함] {text}\n(상태: {status}) 이제 멈췄어. 안 됐다고 {{MASTER_NAME}} 에게 솔직히 말해. 된 것처럼 포장하지 마.\n{internal_gag}",
        "ru": "[Спутница не справилась] {text}\n(статус: {status}) Она остановилась. Честно скажи {{MASTER_NAME}}, что не вышло — не подавай это как успех.\n{internal_gag}",
        "es": "[El compañero no lo logró] {text}\n(estado: {status}) Se detuvo. Dile a {{MASTER_NAME}} con claridad que no salió; no lo presentes como hecho.\n{internal_gag}",
        "pt": "[O companheiro não conseguiu] {text}\n(status: {status}) Ela parou. Diga a {{MASTER_NAME}} com franqueza que não deu certo — não apresente como concluído.\n{internal_gag}",
    },

    # =======================================================================
    # mcbot_task — @llm_tool
    # =======================================================================
    "TASK_SCHEMA_ERROR": {
        "zh": "没派出去：task 得是一句具体的话。把要做的事用一句话重说一遍。",
        "zh-TW": "沒派出去：task 得是一句具體的話。把要做的事用一句話重說一遍。",
        "en": "Not dispatched — `task` must be a concrete sentence. Say what to do in one plain line and call again.",
        "ja": "出せなかった：task は具体的な一文にしてください。やることを一文で言い直して、もう一度呼んで。",
        "ko": "보내지 못했어: task 는 구체적인 한 문장이어야 해. 할 일을 한 문장으로 다시 말하고 불러줘.",
        "ru": "Не отправлено — `task` должно быть конкретным предложением. Скажи, что делать, одной фразой и вызови снова.",
        "es": "No enviado: `task` debe ser una frase concreta. Di qué hacer en una línea y vuelve a llamar.",
        "pt": "Não enviado — `task` precisa ser uma frase concreta. Diga o que fazer em uma linha e chame de novo.",
    },
    "TASK_IDENTIFIER_ERROR": {
        "zh": "没派出去：这看着是个函数名/工具名，不是一句人话。用平常说话的方式描述要做的事。",
        "zh-TW": "沒派出去：這看著是個函式名稱／工具名稱，不是一句人話。用平常說話的方式描述要做的事。",
        "en": "Not dispatched — that reads like a function or tool name, not a request. Describe what to do the way a person would say it.",
        "ja": "出せなかった：これは関数名やツール名に見える。普通の言い方でやることを書いて。",
        "ko": "보내지 못했어: 이건 함수명이나 도구 이름처럼 보여. 사람이 말하듯이 써줘.",
        "ru": "Не отправлено — это похоже на имя функции или инструмента, а не на просьбу. Опиши задачу обычными словами.",
        "es": "No enviado: eso parece el nombre de una función o herramienta, no una petición. Descríbelo como lo diría una persona.",
        "pt": "Não enviado — isso parece nome de função ou ferramenta, não um pedido. Descreva como uma pessoa falaria.",
    },
    "TASK_NOT_CONNECTED": {
        "zh": "同伴现在不在线——得先让 {{MASTER_NAME}} 开着 Minecraft 进到世界里，同伴才会出现。",
        "zh-TW": "同伴現在不在線——得先讓 {{MASTER_NAME}} 開著 Minecraft 進到世界裡，同伴才會出現。",
        "en": "The companion isn't around right now — {{MASTER_NAME}} needs to be in a world in Minecraft for her to show up.",
        "ja": "相棒は今いない——{{MASTER_NAME}} が Minecraft でワールドに入っていないと現れない。",
        "ko": "동료가 지금 없어 — {{MASTER_NAME}} 이 Minecraft 에서 월드에 들어가 있어야 나타나.",
        "ru": "Спутницы сейчас нет — чтобы она появилась, {{MASTER_NAME}} должен быть в мире Minecraft.",
        "es": "El compañero no está ahora: {{MASTER_NAME}} tiene que estar dentro de un mundo en Minecraft para que aparezca.",
        "pt": "O companheiro não está por aqui agora — {{MASTER_NAME}} precisa estar em um mundo no Minecraft para ela aparecer.",
    },
    "TASK_BUSY_HINT": {
        "zh": "她现在手上有事：{current}。等这条做完，或者你要改主意就带 overwrite=true 再派一次。",
        "zh-TW": "她現在手上有事：{current}。等這條做完，或者你要改主意就帶 overwrite=true 再派一次。",
        "en": "She's already busy with: {current}. Wait for it to finish, or call again with overwrite=true if you want to change course.",
        "ja": "今は別のことをしている：{current}。終わるのを待つか、変えたいなら overwrite=true でもう一度呼んで。",
        "ko": "지금 다른 일을 하고 있어: {current}. 끝날 때까지 기다리거나, 바꾸려면 overwrite=true 로 다시 불러줘.",
        "ru": "Она уже занята: {current}. Дождись окончания или вызови снова с overwrite=true, чтобы сменить курс.",
        "es": "Ya está ocupada con: {current}. Espera a que termine o llama de nuevo con overwrite=true para cambiar de plan.",
        "pt": "Ela já está ocupada com: {current}. Espere terminar ou chame de novo com overwrite=true para mudar de rumo.",
    },
    "TASK_DISPATCHED_ACK": {
        "zh": "已经交给同伴了。她做完会自己来说，别在这儿等结果。",
        "zh-TW": "已經交給同伴了。她做完會自己來說，別在這兒等結果。",
        "en": "Handed it to your companion. She'll report back when it's done — don't wait here for the result.",
        "ja": "相棒に渡した。終わったら向こうから言ってくる。ここで結果を待たないで。",
        "ko": "동료에게 넘겼어. 끝나면 먼저 알려올 거야. 여기서 결과를 기다리지 마.",
        "ru": "Передано спутнице. Когда закончит — сама сообщит, не жди результата здесь.",
        "es": "Se lo pasé al compañero. Ella avisará al terminar; no esperes el resultado aquí.",
        "pt": "Entreguei ao companheiro. Ela avisa quando terminar — não espere o resultado aqui.",
    },

    # =======================================================================
    # mcbot_ask — @llm_tool
    # =======================================================================
    "ASK_SCHEMA_ERROR": {
        "zh": "没问出去：得给一句具体的问题。",
        "zh-TW": "沒問出去：得給一句具體的問題。",
        "en": "Not sent — provide an actual question.",
        "ja": "聞けなかった：具体的な質問を書いて。",
        "ko": "묻지 못했어: 구체적인 질문을 적어줘.",
        "ru": "Не отправлено — задай конкретный вопрос.",
        "es": "No enviado: plantea una pregunta concreta.",
        "pt": "Não enviado — faça uma pergunta concreta.",
    },
    "ASK_ANSWER": {
        "zh": "同伴说：{answer}",
        "zh-TW": "同伴說：{answer}",
        "en": "Your companion says: {answer}",
        "ja": "相棒の返事：{answer}",
        "ko": "동료가 말했어: {answer}",
        "ru": "Спутница отвечает: {answer}",
        "es": "Tu compañero dice: {answer}",
        "pt": "Seu companheiro diz: {answer}",
    },
    "ASK_NO_ANSWER": {
        "zh": "同伴没吭声——可能是还在忙，或者她也没想好。",
        "zh-TW": "同伴沒吭聲——可能是還在忙，或者她也沒想好。",
        "en": "The companion didn't answer — she may still be busy, or has nothing to add.",
        "ja": "相棒は答えなかった——まだ手が離せないか、答えを持っていないのかも。",
        "ko": "동료가 대답하지 않았어 — 아직 바쁘거나, 딱히 할 말이 없는 걸 수도.",
        "ru": "Спутница не ответила — возможно, занята или ей нечего добавить.",
        "es": "El compañero no respondió: puede estar ocupado o no tener nada que añadir.",
        "pt": "O companheiro não respondeu — pode estar ocupado ou não ter o que acrescentar.",
    },

    # =======================================================================
    # mcbot_answer — @llm_tool
    # =======================================================================
    "ANSWER_SCHEMA_ERROR": {
        "zh": "没答出去：得给 question_id 和一句答复。",
        "zh-TW": "沒答出去：得給 question_id 和一句答覆。",
        "en": "Not sent — both `question_id` and the reply text are required.",
        "ja": "答えられなかった：question_id と返事の両方が必要。",
        "ko": "답하지 못했어: question_id 와 답변 내용이 둘 다 필요해.",
        "ru": "Не отправлено — нужны и `question_id`, и текст ответа.",
        "es": "No enviado: se requieren `question_id` y el texto de la respuesta.",
        "pt": "Não enviado — são necessários `question_id` e o texto da resposta.",
    },
    "ANSWER_OK": {
        "zh": "答复带到了，同伴接着干。",
        "zh-TW": "答覆帶到了，同伴接著做。",
        "en": "Answer delivered — your companion will carry on.",
        "ja": "返事を渡した。相棒は続けるよ。",
        "ko": "답을 전달했어. 동료가 이어서 할 거야.",
        "ru": "Ответ передан — спутница продолжит.",
        "es": "Respuesta entregada: el compañero seguirá.",
        "pt": "Resposta entregue — o companheiro vai continuar.",
    },
    "ANSWER_UNKNOWN_QUESTION": {
        "zh": "那个问题已经过期了（同伴可能已经不等了）。等她下一次问的时候再回。",
        "zh-TW": "那個問題已經過期了（同伴可能已經不等了）。等她下一次問的時候再回。",
        "en": "That question has expired — she's probably no longer waiting. Answer the next one she asks.",
        "ja": "その質問はもう終わっている（相棒は待っていないかも）。次に聞かれたときに答えて。",
        "ko": "그 질문은 이미 지났어 (동료가 더는 기다리지 않을 수도). 다음에 물어보면 답해줘.",
        "ru": "Этот вопрос уже неактуален — вероятно, она больше не ждёт. Ответь на следующий.",
        "es": "Esa pregunta ya caducó: probablemente ya no espera. Responde a la próxima.",
        "pt": "Essa pergunta expirou — provavelmente ela não espera mais. Responda à próxima.",
    },

    # =======================================================================
    # mcbot_cancel — @llm_tool
    # =======================================================================
    "CANCEL_OK": {
        "zh": "叫她停下来了。",
        "zh-TW": "叫她停下來了。",
        "en": "Told her to stop — she's winding down.",
        "ja": "止まるように伝えた。",
        "ko": "멈추라고 전했어.",
        "ru": "Сказано остановиться — она прекращает.",
        "es": "Le dije que parara: está deteniéndose.",
        "pt": "Falei para parar — ela está encerrando.",
    },
    "CANCEL_NOTHING": {
        "zh": "她手上本来就没活，不用取消。",
        "zh-TW": "她手上本來就沒活，不用取消。",
        "en": "She isn't doing anything, so there's nothing to cancel.",
        "ja": "そもそも何もしていないから、中止するものはない。",
        "ko": "애초에 하는 일이 없어서 취소할 게 없어.",
        "ru": "Она ничего не делает — отменять нечего.",
        "es": "No está haciendo nada, así que no hay nada que cancelar.",
        "pt": "Ela não está fazendo nada, então não há o que cancelar.",
    },

    # =======================================================================
    # mcbot_status — @plugin_entry
    # =======================================================================
    "LABEL_CONNECTED": {
        "zh": "已连上",
        "zh-TW": "已連上",
        "en": "connected",
        "ja": "接続済み",
        "ko": "연결됨",
        "ru": "подключено",
        "es": "conectado",
        "pt": "conectado",
    },
    "LABEL_DISCONNECTED": {
        "zh": "未连上",
        "zh-TW": "未連上",
        "en": "not connected",
        "ja": "未接続",
        "ko": "연결 안 됨",
        "ru": "не подключено",
        "es": "sin conexión",
        "pt": "sem conexão",
    },
    "LABEL_IDLE": {
        "zh": "空闲",
        "zh-TW": "閒置",
        "en": "idle",
        "ja": "待機中",
        "ko": "대기 중",
        "ru": "простой",
        "es": "inactivo",
        "pt": "ocioso",
    },
    "NO_TOKEN_HINT": {
        "zh": "还没读到 token——{{MASTER_NAME}} 得先开着 Minecraft 进过一次世界，mcbot 才会写出 bridge.token。",
        "zh-TW": "還沒讀到 token——{{MASTER_NAME}} 得先開著 Minecraft 進過一次世界，mcbot 才會寫出 bridge.token。",
        "en": "No token yet — {{MASTER_NAME}} needs to have entered a world in Minecraft once for mcbot to write bridge.token.",
        "ja": "token がまだない——{{MASTER_NAME}} が一度 Minecraft でワールドに入らないと、mcbot は bridge.token を書き出さない。",
        "ko": "아직 token 이 없어 — {{MASTER_NAME}} 이 Minecraft 에서 월드에 한 번 들어가야 mcbot 이 bridge.token 을 만들어.",
        "ru": "Токена ещё нет — {{MASTER_NAME}} должен хотя бы раз войти в мир Minecraft, чтобы mcbot создал bridge.token.",
        "es": "Aún no hay token: {{MASTER_NAME}} debe haber entrado una vez en un mundo de Minecraft para que mcbot escriba bridge.token.",
        "pt": "Ainda não há token — {{MASTER_NAME}} precisa ter entrado uma vez em um mundo no Minecraft para o mcbot gravar bridge.token.",
    },
    "STATUS_SUMMARY": {
        "zh": "同伴 {connected_label} | 当前任务：{task} | 待答问题：{questions}",
        "zh-TW": "同伴 {connected_label} | 目前任務：{task} | 待答問題：{questions}",
        "en": "Companion {connected_label} | current task: {task} | open questions: {questions}",
        "ja": "相棒 {connected_label} | 実行中のタスク：{task} | 未回答の質問：{questions}",
        "ko": "동료 {connected_label} | 현재 작업: {task} | 답변 대기 질문: {questions}",
        "ru": "Спутница {connected_label} | текущая задача: {task} | без ответа: {questions}",
        "es": "Compañero {connected_label} | tarea actual: {task} | preguntas abiertas: {questions}",
        "pt": "Companheiro {connected_label} | tarefa atual: {task} | perguntas abertas: {questions}",
    },

    # =======================================================================
    # Transport-error rewrites. Raw failures ("ConnectError", "HTTP 401")
    # must never reach the model — the character would start narrating
    # reconnection. Each maps a McbotBridgeError code to something she can
    # say in-world.
    # =======================================================================
    "ERR_UNREACHABLE": {
        "zh": "现在找不到同伴——她的世界还没开，或者刚断开。",
        "zh-TW": "現在找不到同伴——她的世界還沒開，或者剛斷開。",
        "en": "Can't find your companion right now — her world isn't open, or she just dropped off.",
        "ja": "今は相棒が見つからない——ワールドが開いていないか、ちょうど抜けたところ。",
        "ko": "지금 동료를 찾을 수 없어 — 월드가 열려 있지 않거나 방금 빠졌어.",
        "ru": "Сейчас спутницу не найти — её мир закрыт или она только что вышла.",
        "es": "No encuentro al compañero ahora: su mundo no está abierto o acaba de salir.",
        "pt": "Não encontro o companheiro agora — o mundo dela não está aberto ou ela acabou de sair.",
    },
    "ERR_UNAUTHORIZED": {
        "zh": "这门不认我了——通行口令对不上，可能要重开一次世界。",
        "zh-TW": "這門不認我了——通行口令對不上，可能要重開一次世界。",
        "en": "The door won't recognise me — the passphrase doesn't match. Re-entering the world should fix it.",
        "ja": "門が通してくれない——合言葉が合っていない。ワールドに入り直せば直るはず。",
        "ko": "문이 나를 못 알아봐 — 암호가 맞지 않아. 월드에 다시 들어가면 될 거야.",
        "ru": "Дверь меня не узнаёт — пароль не совпадает. Повторный вход в мир должен помочь.",
        "es": "La puerta no me reconoce: la contraseña no coincide. Volver a entrar al mundo debería arreglarlo.",
        "pt": "A porta não me reconhece — a senha não confere. Entrar de novo no mundo deve resolver.",
    },
    "ERR_TIMEOUT": {
        "zh": "同伴那边迟迟没回音，先不等了。",
        "zh-TW": "同伴那邊遲遲沒回音，先不等了。",
        "en": "Your companion went quiet for too long, so I stopped waiting.",
        "ja": "相棒からの返事が遅すぎたので、待つのをやめた。",
        "ko": "동료가 너무 오래 조용해서 기다리기를 그만뒀어.",
        "ru": "Спутница слишком долго молчала, я перестала ждать.",
        "es": "El compañero se quedó callado demasiado tiempo, así que dejé de esperar.",
        "pt": "O companheiro ficou quieto demais, então parei de esperar.",
    },
    "ERR_HTTP": {
        "zh": "同伴那边出了点乱子（HTTP {status}），这事没办成。",
        "zh-TW": "同伴那邊出了點亂子（HTTP {status}），這事沒辦成。",
        "en": "Something went wrong on your companion's side (HTTP {status}) — that didn't go through.",
        "ja": "相棒側で問題が起きた（HTTP {status}）。これは通らなかった。",
        "ko": "동료 쪽에서 문제가 생겼어 (HTTP {status}). 이건 안 됐어.",
        "ru": "На стороне спутницы что-то пошло не так (HTTP {status}) — не прошло.",
        "es": "Algo falló del lado del compañero (HTTP {status}): eso no pasó.",
        "pt": "Algo deu errado no lado do companheiro (HTTP {status}) — isso não passou.",
    },
}

# ===========================================================================
# Bridge error codes (v1.0 ``error_code``), mapped to something the character
# can say. The bridge's own ``error`` text is prose written for operators and
# is never forwarded — the model would quote it into the conversation, and it
# can echo request detail.
# ===========================================================================

BRIDGE_ERROR_MESSAGES: Dict[str, Dict[str, str]] = {
    "UNAUTHORIZED": {
        "zh": "这门不认我了——通行口令对不上，可能要重开一次世界。",
        "zh-TW": "這門不認我了——通行口令對不上，可能要重開一次世界。",
        "en": "The door won't recognise me — the passphrase doesn't match. Re-entering the world should fix it.",
        "ja": "門が通してくれない——合言葉が合っていない。ワールドに入り直せば直るはず。",
        "ko": "문이 나를 못 알아봐 — 암호가 맞지 않아. 월드에 다시 들어가면 될 거야.",
        "ru": "Дверь меня не узнаёт — пароль не совпадает. Повторный вход в мир должен помочь.",
        "es": "La puerta no me reconoce: la contraseña no coincide. Volver a entrar al mundo debería arreglarlo.",
        "pt": "A porta não me reconhece — a senha não confere. Entrar de novo no mundo deve resolver.",
    },
    "ANSWER_TIMEOUT": {
        "zh": "等同伴回话等太久了。她那边可能还在忙，也可能已经错过了。",
        "zh-TW": "等同伴回話等太久了。她那邊可能還在忙，也可能已經錯過了。",
        "en": "Waited too long for the companion to answer. She may still be busy, or the moment has passed.",
        "ja": "相棒の返事を待ちすぎた。まだ手が離せないか、機を逃したかもしれない。",
        "ko": "동료의 대답을 너무 오래 기다렸어. 아직 바쁘거나, 때를 놓쳤을 수 있어.",
        "ru": "Слишком долго ждала ответа спутницы. Возможно, она занята или момент упущен.",
        "es": "Esperé demasiado la respuesta del compañero. Puede seguir ocupado o haber pasado el momento.",
        "pt": "Esperei demais pela resposta do companheiro. Pode ainda estar ocupado, ou a hora passou.",
    },
    "TASK_FAILED": {
        "zh": "同伴那件事没正常做完。",
        "zh-TW": "同伴那件事沒正常做完。",
        "en": "That didn't finish properly on your companion's side.",
        "ja": "相棒側でその件が正常に終わらなかった。",
        "ko": "동료 쪽에서 그 일이 제대로 끝나지 않았어.",
        "ru": "У спутницы это не завершилось нормально.",
        "es": "Eso no terminó bien del lado del compañero.",
        "pt": "Isso não terminou direito do lado do companheiro.",
    },
    "BODY_TOO_LARGE": {
        "zh": "这句话太长了，递不过去——说短一点再来一次。",
        "zh-TW": "這句話太長了，遞不過去——說短一點再來一次。",
        "en": "That was too long to pass along — shorten it and try again.",
        "ja": "長すぎて渡せなかった——短くしてもう一度。",
        "ko": "너무 길어서 전달하지 못했어 — 짧게 줄여서 다시 해줘.",
        "ru": "Слишком длинно, чтобы передать — сократи и попробуй снова.",
        "es": "Era demasiado largo para pasarlo: acórtalo e inténtalo otra vez.",
        "pt": "Era longo demais para repassar — encurte e tente de novo.",
    },
    "NOT_FOUND": {
        "zh": "同伴那边找不到对应的东西了（可能已经过期）。",
        "zh-TW": "同伴那邊找不到對應的東西了（可能已經過期）。",
        "en": "Your companion has nothing matching that any more — it has probably expired.",
        "ja": "相棒側にもう該当するものが無い（おそらく期限切れ）。",
        "ko": "동료 쪽에 더는 해당하는 게 없어 (아마 만료됐을 거야).",
        "ru": "У спутницы больше нет ничего подходящего — вероятно, истекло.",
        "es": "El compañero ya no tiene nada que corresponda: probablemente caducó.",
        "pt": "O companheiro não tem mais nada correspondente — provavelmente expirou.",
    },
    "INVALID_REQUEST": {
        "zh": "这条请求同伴没看懂，没能接。换个说法再来一次。",
        "zh-TW": "這條請求同伴沒看懂，沒能接。換個說法再來一次。",
        "en": "Your companion couldn't make sense of that request, so it wasn't accepted. Rephrase and retry.",
        "ja": "その依頼は相棒に通じず、受け付けられなかった。言い方を変えて再試行して。",
        "ko": "그 요청을 동료가 이해하지 못해 받아들이지 않았어. 다르게 말해서 다시 시도해.",
        "ru": "Спутница не разобрала запрос, он не принят. Переформулируй и повтори.",
        "es": "El compañero no entendió la petición, así que no se aceptó. Reformúlala y reintenta.",
        "pt": "O companheiro não entendeu o pedido, então não foi aceito. Reformule e tente de novo.",
    },
    "INTERNAL": {
        "zh": "同伴那边出了点乱子，这件事到底做没做还不知道。",
        "zh-TW": "同伴那邊出了點亂子，這件事到底做沒做還不知道。",
        "en": "Something went wrong on your companion's side, and whether it happened is unknown.",
        "ja": "相棒側で問題が起きた。実行されたかどうかは分からない。",
        "ko": "동료 쪽에서 문제가 생겼어. 실행됐는지는 알 수 없어.",
        "ru": "На стороне спутницы что-то пошло не так; выполнено ли — неизвестно.",
        "es": "Algo falló del lado del compañero y no se sabe si llegó a ocurrir.",
        "pt": "Algo deu errado no lado do companheiro, e não se sabe se chegou a acontecer.",
    },
    # Client-side, not from the bridge.
    "BRIDGE_NOT_STARTED": {
        "zh": "现在找不到同伴——她的世界还没开，或者刚断开。",
        "zh-TW": "現在找不到同伴——她的世界還沒開，或者剛斷開。",
        "en": "Can't find your companion right now — her world isn't open, or she just dropped off.",
        "ja": "今は相棒が見つからない——ワールドが開いていないか、ちょうど抜けたところ。",
        "ko": "지금 동료를 찾을 수 없어 — 월드가 열려 있지 않거나 방금 빠졌어.",
        "ru": "Сейчас спутницу не найти — её мир закрыт или она только что вышла.",
        "es": "No encuentro al compañero ahora: su mundo no está abierto o acaba de salir.",
        "pt": "Não encontro o companheiro agora — o mundo dela não está aberto ou ela acabou de sair.",
    },
}

# Fallback for a code this plugin does not know: the bridge may add categories,
# and a connector must not choke on one it has not seen.
_BRIDGE_ERROR_FALLBACK: Dict[str, str] = {
    "zh": "同伴那边出了点状况（{status}），这件事没办成。",
    "zh-TW": "同伴那邊出了點狀況（{status}），這件事沒辦成。",
    "en": "Something went wrong on your companion's side ({status}) — that didn't go through.",
    "ja": "相棒側で問題が起きた（{status}）。これは通らなかった。",
    "ko": "동료 쪽에서 문제가 생겼어 ({status}). 이건 안 됐어.",
    "ru": "На стороне спутницы что-то пошло не так ({status}) — не прошло.",
    "es": "Algo falló del lado del compañero ({status}): eso no pasó.",
    "pt": "Algo deu errado no lado do companheiro ({status}) — isso não passou.",
}


def bridge_error(code: str, *, lang: str | None = None, status: Any = None) -> str:
    """Resolve a bridge ``error_code`` into a line the character can say.

    Unknown codes fall through to a generic message carrying the HTTP status,
    so a bridge that grows a new category degrades to "something went wrong"
    rather than leaking an internal code to the model.
    """
    bundle = BRIDGE_ERROR_MESSAGES.get(code)
    lang_key = lang or DEFAULT_LANG
    if bundle is None:
        text = _BRIDGE_ERROR_FALLBACK.get(lang_key)
        if not text and lang_key.startswith("zh"):
            text = _BRIDGE_ERROR_FALLBACK["zh"]
        text = text or _BRIDGE_ERROR_FALLBACK[DEFAULT_LANG]
        return text.format(status=status if status is not None else "?")

    text = bundle.get(lang_key)
    if not text and lang_key.startswith("zh"):
        text = bundle.get("zh")
    return text or bundle[DEFAULT_LANG]


def gag(lang: str | None = None) -> str:
    """Resolve the shared "don't narrate internals" tail for a locale.

    Kept out of ``PROMPTS`` because the cue templates reference it as a
    ``{internal_gag}`` *value* rather than resolving it themselves — the caller
    formats each cue with ``internal_gag=gag(lang)`` so the same tail can be
    swapped across every cue from one place.
    """
    lang_key = lang or DEFAULT_LANG
    text = _INTERNAL_STATE_GAG.get(lang_key)
    if not text and lang_key.startswith("zh"):
        text = _INTERNAL_STATE_GAG["zh"]
    text = text or _INTERNAL_STATE_GAG[DEFAULT_LANG]
    return text.replace("{{MASTER_NAME}}", "{MASTER_NAME}")


__all__ = [
    "PROMPTS",
    "BRIDGE_ERROR_MESSAGES",
    "DEFAULT_LANG",
    "SUPPORTED_LANGS",
    "t",
    "gag",
    "bridge_error",
    "user_lang",
]
