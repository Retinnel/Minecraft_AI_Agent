"""Сквозной путь от реплики игрока до проверенного действия.

Порядок участия компонентов задаёт всю архитектуру, поэтому он вынесен в одно место:

    языковая модель  →  резолвер  →  валидатор  →  сборка команды
    (что хочет игрок)  (о чём речь)  (можно ли)   (что выполнить)

Модель участвует ровно на первом шаге и называет предмет словами игрока, а не
идентификатором. Идентификатор подставляет резолвер по онтологии, допустимость проверяет
валидатор, команду собирает сервер. Поэтому переформулировка запроса не меняет решения:
в самом решении модель не участвует.

Разбор реплики отделён от исполнения: сюда передаётся любая реализация IntentExtractor —
живая модель, локальная модель или заглушка в тестах.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Protocol, Sequence

from kb.graph import KnowledgeBase
from resolver.lexical import Candidate
from resolver.resolve import AMBIGUOUS, NOT_FOUND, Resolver
from solver.campaign import Campaign, CampaignBuilder, GoalCandidate
from solver.planner import Plan, Planner
from solver.progression import ProgressReport, Progression, render_progress
from solver.quest import COLLECT, KILL, QuestChecker
from solver.render import render_alternatives, render_summary, render_tree
from solver.validator import (ACT, CLARIFY, CREATIVE, EXECUTE_PLAN, MEMORY_ACTIONS, REJECT, SURVIVAL,
                              Validator)
from solver.worldfacts import (ABOUT, AVAILABLE, DROPS, SOURCES, USES, WORLD_ACTIONS,
                               WorldAnswer, WorldFacts, essentials as world_essentials,
                               render_answer)

NONE = "none"
PLAN_GOAL = "plan_goal"
CREATE_QUEST = "create_quest"
START_CAMPAIGN = "start_campaign"
PROPOSE_GOAL = "propose_goal"
SHOW_PROGRESS = "show_progress"
SEARCH_WEB = "search_web"
REMEMBER = "remember"
RECALL = "recall"
FORGET_FACT = "forget_fact"

# Игра, для которой есть онтология. Для любой другой игры знаний в базе нет, и модели
# предъявляется только поиск: выдавать, планировать и проверять там нечем.
DEFAULT_GAME = "Minecraft"

# Действия, которые может предложить модель. Остальные действия реестра (например, получение
# награды за задание) выполняются системой сами и модели не предъявляются.
PROPOSABLE = ("give_item", "remove_item", "summon_entity", PLAN_GOAL, CREATE_QUEST,
              START_CAMPAIGN, PROPOSE_GOAL, SHOW_PROGRESS, *WORLD_ACTIONS, SEARCH_WEB,
              *MEMORY_ACTIONS, EXECUTE_PLAN)

# «Сделай сам» в творческом режиме: модели такое действие не предъявляется, ответ даётся
# без неё — предмет проще выдать.
EXECUTE_CUES = re.compile(r"\b(сделай|скрафти|добудь|собери)\s+(мне\s+)?сам\b|\bсам\s+(сделай|скрафти|добудь)\b"
                          r"|\bза\s+меня\b", re.IGNORECASE)

# «Как сделать», «что нужно для» — просьба о плане, а не об исполнении.
# Обращение к помощнику — отдельному игроку под управлением сервера (companion/):
# «пусть помощник сделает…», «помощник, принеси…», «бот, добудь…».
COMPANION_CUES = re.compile(r"\bпомощник\w*|\bбот(а|у|ом)?\b|\bкомпаньон\w*", re.IGNORECASE)

HOWTO_CUES = re.compile(r"\bкак\b|\bчто\s+нужно\b|\bчем\b|\bплан\b|\bрецепт", re.IGNORECASE)

# Что доступно в игре без онтологии: память мира работает в любой игре — заметки игрока и
# факты с экрана, — а остальное ищется в сети.
OTHER_GAME_ACTIONS = (SEARCH_WEB, *MEMORY_ACTIONS)

# Слова просьбы о выдаче: по ним видно, что игрок ждал предмет, а не план.
GIVE_WORDS = re.compile(r"(?<![а-яё])(дай|дайте|выдай|выдайте|выдать|кинь|закинь|подкинь)(?![а-яё])",
                        re.IGNORECASE)

# Игра со своей базой знаний (kb/worlds): планы и вопросы о мире — по ней. Команд в такую
# игру ассистент не отдаёт: мода для неё нет, и выдача с призывом модели не предъявляются.
WORLD_GAME_ACTIONS = (PLAN_GOAL, *WORLD_ACTIONS, SEARCH_WEB, *MEMORY_ACTIONS)

# Сколько раз модели дают переделать отклонённое задание. Одна попытка — по описанию
# целевой системы: модель учитывает причину отказа, но перебором не занимается.
QUEST_RETRIES = 1

# Число, названное в реплике буквально, надёжнее числа из ответа модели: модель его
# пересказывает, а регулярное выражение читает. Знак значим — «дай -5 алмазов» должно
# отклоняться, а не молча превращаться в пятёрку.
LITERAL_COUNT = re.compile(r"(?<![\w.])(-?\d+)")

COMMANDS = {
    "give_item": "/give @s {id} {count}",
    "remove_item": "/clear @s {id} {count}",
}

# Согласие в ответ на вопрос с единственным вариантом.
AFFIRMATIVE = re.compile(r"^(да|ага|угу|давай|верно|точно|конечно|он самый|она самая|yes|yep)\b",
                         re.IGNORECASE)

# Признаки просьбы что-то убрать. Нужны только для удаления — единственного действия, ошибка
# в котором отнимает у игрока то, что у него было.
REMOVAL_CUES = re.compile(
    r"(забер|забир|убер|убир|удал|выброс|выкин|очист|сотр|clear|remove|delete|take\s)",
    re.IGNORECASE,
)

# Признаки просьбы забыть. Забытое из ответов и планов уходит насовсем, поэтому, как и с
# удалением предмета, одного слова модели мало.
FORGET_CUES = re.compile(r"(забуд|забыв|забыть|удали|сотри|стери|убери\s+(отметк|заметк)|forget|delete)",
                         re.IGNORECASE)

# «Тут», «здесь»: отметить место по позиции игрока, а не просто записать заметку.
HERE_CUES = re.compile(r"(\bтут\b|\bздесь\b|\bэто\s+место|\bна\s+этом\s+месте|\bhere\b)",
                       re.IGNORECASE)

# Вопрос о гибели: отвечает последняя запись о смерти, а не поиск по словам.
DEATH_CUES = re.compile(r"(умер|погиб|смерт|сдох|died|death)", re.IGNORECASE)

# Вопрос обо всей памяти сразу: «что ты помнишь», «покажи все отметки». Модель в таком
# вопросе называет целью «этот мир» или «все отметки», и поиск по этим словам ничего не
# находил — отвечать нужно обзором, а не поиском.
OVERVIEW_CUES = re.compile(
    r"(что\s+(ты\s+)?(помнишь|запомнил|записал|знаешь\s+об?\s+(этом\s+)?мире)|"
    r"(все|всю|весь|список)\s+\S*\s*(отметк|заметк|памят|запис)|"
    r"(покажи|открой|выведи)\s+память|что\s+(есть\s+)?в\s+памяти|what\s+do\s+you\s+remember)",
    re.IGNORECASE)

# Начало реплики «запомни, …» — сама заметка идёт после него.
REMEMBER_PREFIX = re.compile(r"^\s*(пожалуйста\s+)?(запомни|запиши|отметь|remember|note)\s*(что|это)?[\s,:—-]*",
                             re.IGNORECASE)

# Сколько фактов называть в ответе; остальные — во вкладке «Память».
RECALL_SPOKEN = 3
# Сколько кандидатов неоднозначного названия учитывается при поиске в памяти.
RECALL_CANDIDATES = 5

# Признаки просьбы о цели. Кампания начинается сразу, поэтому одного слова модели мало:
# малая модель, не разобрав «дай», выбирала предложение цели, и игрок получал кампанию,
# о которой не просил.
GOAL_CUES = re.compile(
    r"(цел|задач|заняться|занять себя|что\s+(мне\s+)?(делать|дальше)|чем\s+заня|предлож|"
    r"посовет|подскаж|куда\s+двигаться|goal|what\s+next|suggest)",
    re.IGNORECASE,
)


@dataclass
class Proposal:
    """Что языковая модель поняла из реплики."""

    action: str                      # give_item | remove_item | summon_entity | plan_goal | ...
    target: Optional[str] = None     # предмет или существо словами игрока, не идентификатор
    count: Any = 1
    # Только для create_quest: вид цели и награда, придуманные моделью.
    goal_kind: Optional[str] = None
    reward: Optional[str] = None
    reward_count: Any = 1
    # Только для search_web: поисковый запрос, который модель составила на английском —
    # на нём поисковики находят игровые руководства, на русском — новости и мусор.
    search_query: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class IntentExtractor(Protocol):
    """Источник намерения. Единственное место на входе конвейера, где участвует модель.

    actions — действия, доступные в текущем режиме: модели предъявляются только они.
    feedback — причина, по которой предыдущее предложение отклонено.
    context — предыдущий обмен репликами одной строкой.
    """

    def extract(self, utterance: str, *, actions: Sequence[str] = PROPOSABLE,
                feedback: Optional[str] = None, context: Optional[str] = None,
                game: Optional[str] = None, screen: Optional[str] = None) -> Proposal: ...


@dataclass
class Turn:
    """Итог обработки одной реплики со следом каждого компонента."""

    outcome: str                              # act | clarify | reject | none
    action: Optional[str] = None
    target: Optional[str] = None              # идентификатор из онтологии
    label: Optional[str] = None               # человеческое название цели
    count: Any = 1
    command: Optional[str] = None
    reason: Optional[str] = None
    # При каком условии отклонённая просьба станет выполнимой; выводит решатель.
    remedy: Optional[str] = None
    query: Optional[str] = None               # название словами игрока — для продолжения диалога
    options: Sequence[Candidate] = ()         # варианты для уточняющего вопроса
    explanation: List[str] = field(default_factory=list)
    stages: Dict[str, Any] = field(default_factory=dict)
    # Заполняется только для plan_goal: дерево достижения цели от текущего инвентаря.
    plan: Optional[Plan] = None
    plan_tree: Optional[str] = None
    plan_summary: Optional[str] = None
    # Чем ещё можно было получить цель и почему выбрано не оно.
    plan_alternatives: Optional[str] = None
    # Заполняется только для create_quest: принятое задание в виде, пригодном для сохранения.
    quest: Optional[Dict[str, Any]] = None
    # Заполняется только для start_campaign: доказанная цепочка этапов.
    campaign: Optional[Campaign] = None
    # Цель выбрана ассистентом из кандидатов решателя, а не названа игроком.
    proposed: bool = False
    candidates: Sequence[GoalCandidate] = ()
    # Заполняется только для show_progress: ступень развития и путь к следующей.
    progress: Optional[ProgressReport] = None
    progress_summary: Optional[str] = None
    # Заполняется только для вопросов о мире: ответ базы знаний и названия, которые
    # пересказ обязан сохранить.
    world: Optional[WorldAnswer] = None
    world_summary: Optional[str] = None
    world_essentials: Sequence[str] = ()
    # Заполняется только для search_web: найденные фрагменты и источники.
    web: Optional[Any] = None
    game: Optional[str] = None
    # Заполняется только для действий памяти: факты, ответ словами и числа (координаты),
    # которые пересказ обязан сохранить.
    memory: Sequence[Any] = ()
    memory_summary: Optional[str] = None
    memory_numbers: Sequence[int] = ()
    # Заполняется только для execute_plan: запуск, ждущий подтверждения, и вопрос игроку.
    autopilot: Optional[Dict[str, Any]] = None
    autopilot_summary: Optional[str] = None


class Pipeline:
    def __init__(
        self,
        extractor: IntentExtractor,
        kb: Optional[KnowledgeBase] = None,
        *,
        mode: str = CREATIVE,
        search: Optional[Any] = None,
        home: str = DEFAULT_GAME,
    ) -> None:
        # Игра, чью базу знаний держит этот конвейер. Для неё работают резолвер, валидатор
        # и планировщик; о любой другой игре — только поиск.
        self.home = home
        self._worlds: Dict[str, "Pipeline"] = {}
        self._preferences: Dict[str, Any] = {}
        self._utterance: str = ""
        self.starting_inventory: Dict[str, int] = {}
        self.kb = kb or KnowledgeBase.shared()
        self._search = search
        self.extractor = extractor
        self.resolver = Resolver(self.kb)
        self.validator = Validator(self.kb)
        self.mode = mode
        self._planner: Optional[Planner] = None
        self._campaigns: Optional[CampaignBuilder] = None
        self._world: Optional[WorldFacts] = None
        self._locator = None
        self._executor = None
        self._autopilot: Dict[str, Any] = {}
        self._executor_kind = "player"

    @property
    def planner(self) -> Planner:
        """Строится по требованию: предварительный расчёт стоимостей занимает секунду."""
        if self._planner is None:
            self._planner = Planner(self.kb)
        return self._planner

    @property
    def locator(self):
        if self._locator is None:
            from solver.locator import Locator
            self._locator = Locator(self.kb)
        return self._locator

    @property
    def campaigns(self) -> CampaignBuilder:
        if self._campaigns is None:
            self._campaigns = CampaignBuilder(self.kb, self.planner, self.validator)
        return self._campaigns

    @property
    def search(self):
        """Веб-поиск, общий на процесс: его кэш экономит кредиты и время."""
        if self._search is None:
            from search import shared
            self._search = shared()
        return self._search

    def actions_for(self, mode: str, game: Optional[str] = None,
                    autopilot: bool = False) -> Sequence[str]:
        """Действия, которые предъявляются модели в данном режиме и для данной игры.

        «Сделай сам» предъявляется только тогда, когда автопилот включён: выключенный
        режим не должен менять разбор остальных реплик.
        """
        if game and not self._at_home(game):
            return OTHER_GAME_ACTIONS
        if not self._minecraft:
            return WORLD_GAME_ACTIONS
        allowed = set(self.validator.available_actions(mode))
        if not autopilot:
            allowed.discard(EXECUTE_PLAN)
        return tuple(action for action in PROPOSABLE if action in allowed)

    @staticmethod
    def _named_in(target: Optional[str], text: Optional[str]) -> bool:
        """Звучит ли название в тексте (сверка по основам слов)."""
        from resolver.text import name_stems
        return bool(set(name_stems(target or "")) & set(name_stems(text or "")))

    def _own_target(self, proposal: Proposal, utterance: str, context: Optional[str],
                    actions: Sequence[str], game: str, extra: Dict[str, Any],
                    stages: Dict[str, Any]) -> Proposal:
        """Цель называет игрок, а не предыдущий обмен.

        Контекст нужен, чтобы продолжение («и ещё крипера») сохранило действие. Но модель
        иногда берёт из него и цель: после разговора о печи «пусть кирку сделает тоже ещё
        одну» разбиралось как печь. Признак — названной цели в реплике нет, а в контексте
        она есть. Тогда решает словарь онтологии: если в реплике есть слова из названий
        (кирка), целью становятся они, действие остаётся; если своих названий нет («ещё
        одну»), это настоящее продолжение, и цель из контекста остаётся. Модель повторно
        не спрашивается.
        """
        if not context or not proposal.target or not proposal.action or proposal.action == NONE:
            return proposal
        if self._named_in(proposal.target, utterance) or not self._named_in(proposal.target, context):
            return proposal
        from dataclasses import replace
        from resolver.text import NUMERALS, STOPWORDS, stem, tokenize
        vocabulary = self.resolver.index.vocabulary
        named = [word for word in tokenize(utterance)
                 if word not in STOPWORDS and word not in NUMERALS and stem(word) in vocabulary]
        stages["context_check"] = {"borrowed": proposal.target, "named": named}
        if not named:
            return proposal
        return replace(proposal, target=" ".join(named))

    @property
    def _minecraft(self) -> bool:
        return self.home.lower() == DEFAULT_GAME.lower()

    def _at_home(self, game: Optional[str]) -> bool:
        return (game or DEFAULT_GAME).lower() == self.home.lower()

    def _world_pipeline(self, game: str) -> Optional["Pipeline"]:
        """Конвейер для игры со своей базой знаний: те же компоненты на другой онтологии."""
        if not self._minecraft or self._at_home(game):
            return None
        from kb import games
        world = games.find(game)
        if world is None:
            return None
        if world.key not in self._worlds:
            pipeline = Pipeline(self.extractor, world.kb(), mode=SURVIVAL, search=self._search,
                                home=world.game)
            pipeline.starting_inventory = dict(world.starting_inventory)
            self._worlds[world.key] = pipeline
        return self._worlds[world.key]

    def handle(self, utterance: str, *, mode: Optional[str] = None,
               inventory: Optional[Dict[str, int]] = None,
               pending: Optional["Turn"] = None,
               context: Optional[str] = None,
               state: Optional[str] = None,
               game: Optional[str] = None,
               screen: Optional[str] = None,
               memory: Optional[Any] = None,
               position: Optional[Any] = None,
               autopilot: Optional[Dict[str, Any]] = None,
               preferences: Optional[Dict[str, Any]] = None) -> Turn:
        """Обрабатывает реплику.

        pending — предыдущий исход, если им был уточняющий вопрос: тогда реплика может
        оказаться ответом на него («алмазный» после «какой меч?»).
        context — предыдущий обмен репликами одной строкой, для продолжений вроде
        «и ещё крипера».
        state — снимок состояния игрока словами, для выбора цели кампании.
        game — игра, о которой спрашивает игрок; по умолчанию Minecraft.
        screen — что видно на экране игрока, факты модели зрения одной строкой. Они только
        поясняют реплику («что это за предмет»): действие выбирается по реплике и, как
        всегда, проходит валидатор.
        memory — память мира для игры разговора (`memory.store.MemoryStore`), position —
        последняя позиция игрока по сведениям мода. Координаты в ответах и отметках
        берутся только отсюда.
        """
        game = game or DEFAULT_GAME
        self._utterance = utterance
        world = self._world_pipeline(game)
        if world is not None:
            # Инвентарь Minecraft к другой игре не относится: мода для неё нет, и план
            # считается от того, с чем персонаж появляется в мире (описание мира).
            return world.handle(utterance, mode=SURVIVAL, inventory=world.starting_inventory,
                                pending=pending, context=context, game=world.home,
                                screen=screen, memory=memory, position=position)
        mode = mode or self.mode
        # Сведения для «Сделай сам»: включён ли режим, одиночная ли игра, на связи ли мод.
        self._autopilot = dict(autopilot or {})
        # Предпочтения игрока из его профиля (api/profile.py): влияют на порядок
        # предлагаемых целей, на допустимость — нет.
        self._preferences = dict(preferences or {})
        self._executor_kind = "player"
        companion = self._autopilot.get("companion") or {}
        # «Сделай сам» модели на выбор не даётся: она путала его с просьбой о плане и
        # тянула из прошлого обмена («каменная» → запуск автопилота). Действие
        # предъявляется, только когда игрок сказал это сам — «сделай сам», «за меня» или
        # обратился к помощнику, — либо включена настройка автовыбора (autopilot.auto_select).
        asked_to_execute = bool(EXECUTE_CUES.search(utterance or "")) or bool(
            companion.get("enabled") and COMPANION_CUES.search(utterance or ""))
        executable = bool(self._autopilot.get("enabled") or companion.get("enabled")) and (
            asked_to_execute or bool(self._autopilot.get("auto_select")))
        actions = self.actions_for(mode, game, executable)

        if (self._minecraft and self._at_home(game) and mode == CREATIVE
                and EXECUTE_CUES.search(utterance or "")):
            verdict = self.validator.validate(EXECUTE_PLAN, None, 1, mode)
            return Turn(outcome=REJECT, action=EXECUTE_PLAN, reason=verdict.reason,
                        explanation=verdict.explanation + ["«сделай сам» в творческом режиме"],
                        stages={"rule": "execute_in_creative"}, game=game)

        # Продолжение уточнения достраивает действие Minecraft («какой меч?» → «алмазный»)
        # и в разговоре о другой игре дало бы команду в обход проверки игры ниже.
        if self._at_home(game):
            follow_up = self._follow_up(utterance, pending, mode, inventory, memory, position)
            if follow_up is not None:
                return follow_up

        # Необязательные сведения передаются только когда они есть: извлекатель без них
        # получает тот же вызов, что и раньше.
        extra: Dict[str, Any] = {}
        if screen:
            extra["screen"] = screen
        if not self._minecraft and self._at_home(game):
            extra["ontology"] = True
        proposal = self.extractor.extract(utterance, actions=actions, context=context,
                                          game=game, **extra)
        stages: Dict[str, Any] = {
            "llm": {"action": proposal.action, "target": proposal.target, "count": proposal.count}
        }
        proposal = self._own_target(proposal, utterance, context, actions, game, extra, stages)
        if screen:
            stages["screen"] = screen
        if not proposal.action or proposal.action == NONE:
            return Turn(outcome=NONE, stages=stages, game=game)

        if proposal.action == SEARCH_WEB and SEARCH_WEB in actions:
            return self._search_web(utterance, proposal, game, stages)

        if proposal.action in MEMORY_ACTIONS and proposal.action in actions:
            return self._memory_turn(utterance, proposal, game, memory, position, actions, stages,
                                     inventory)

        # Второй рубеж для чужой игры: модели предъявлен только поиск, но если она всё же
        # предложит выдачу или план, валидатор об игре не знает и пропустил бы команду
        # Minecraft в разговор об Elden Ring. Онтология есть только для Minecraft.
        if not self._minecraft and self._at_home(game) and proposal.action not in actions:
            return Turn(outcome=REJECT, action=proposal.action, game=game,
                        reason=f"В {game} я строю планы и отвечаю на вопросы о мире: "
                               f"выдавать предметы умею только в Minecraft.",
                        explanation=[f"действие {proposal.action} недоступно для игры {game}: "
                                     f"команд в неё ассистент не отдаёт"],
                        stages=stages)
        if not self._at_home(game):
            return Turn(outcome=REJECT, action=proposal.action, game=game,
                        reason=f"В {game} я только ищу ответы на вопросы: выдавать предметы "
                               f"и строить планы умею в Minecraft.",
                        explanation=[f"действие {proposal.action} недоступно для игры {game}: "
                                     f"базы знаний по ней нет"],
                        stages=stages)

        if proposal.action == CREATE_QUEST and CREATE_QUEST in actions:
            return self._build_quest(utterance, proposal, actions, mode, inventory, stages)

        if proposal.action == PROPOSE_GOAL and PROPOSE_GOAL in actions:
            # Сомнение разрешается вопросом, а не действием — как и с удалением ниже.
            if not GOAL_CUES.search(utterance or ""):
                return Turn(
                    outcome=CLARIFY,
                    action=PROPOSE_GOAL,
                    reason="Предложить тебе цель? Если да, скажи прямо: «предложи цель».",
                    explanation=["цель предложена моделью, но в реплике нет просьбы о цели"],
                    stages=stages,
                )
            return self._propose_goal(mode, inventory, state, stages)

        if proposal.action == SHOW_PROGRESS and SHOW_PROGRESS in actions:
            return self._show_progress(inventory, stages)

        # «Что я могу сделать сейчас» — вопрос об инвентаре, цели у него нет.
        if proposal.action == AVAILABLE and AVAILABLE in actions:
            return self._answer_world(AVAILABLE, None, inventory, stages)

        count = self._reconcile_count(utterance, proposal.count)
        stages["count"] = count

        # «Сделай сам» против «как сделать» — главный риск разбора (B.11). Явные слова игрока
        # важнее догадки модели: «сделай сам» — исполнение, «как сделать» без «сам» — план.
        # Исполнение не предъявлялось, а модель его всё же назвала: игрок об исполнении не
        # просил, значит это просьба о плане.
        if (proposal.action == EXECUTE_PLAN and EXECUTE_PLAN not in actions
                and PLAN_GOAL in actions and not asked_to_execute):
            proposal = replace(proposal, action=PLAN_GOAL)
            stages["rule"] = "execute_not_asked"
        if EXECUTE_PLAN in actions and proposal.action in (PLAN_GOAL, EXECUTE_PLAN):
            to_companion = bool(companion.get("enabled") and COMPANION_CUES.search(utterance or ""))
            asked_how = bool(HOWTO_CUES.search(utterance or ""))
            asked_to_do = bool(EXECUTE_CUES.search(utterance or "")) or (to_companion and not asked_how)
            # Кто исполняет: помощник, если к нему обратились или автопилот мода выключен.
            if to_companion or (companion.get("enabled") and not self._autopilot.get("enabled")):
                self._executor_kind = "companion"
            if proposal.action == PLAN_GOAL and asked_to_do:
                proposal = replace(proposal, action=EXECUTE_PLAN)
                stages["rule"] = "execute_cue"
            elif proposal.action == EXECUTE_PLAN and asked_how and not asked_to_do:
                proposal = replace(proposal, action=PLAN_GOAL)
                stages["rule"] = "howto_cue"

        # Удаление необратимо, поэтому одного слова модели для него мало: в реплике должна
        # быть просьба что-то убрать. Без этого модель, не найдя в перечне выдачи, хватает
        # ближайший глагол — и «дай 10 факелов» в выживании превращается в «убираю 10
        # факелов». Сомнение здесь разрешается вопросом, а не действием.
        if proposal.action == "remove_item" and not REMOVAL_CUES.search(utterance or ""):
            return Turn(
                outcome=CLARIFY,
                action="remove_item",
                count=count,
                query=proposal.target,
                reason="Убрать это из инвентаря? Если да, скажи прямо: «убери …».",
                explanation=["удаление предложено моделью, но в реплике нет просьбы что-либо убрать"],
                stages=stages,
            )

        # «Что это?» без названия и без сведений об экране: гадать не о чем.
        if proposal.action == ABOUT and not str(proposal.target or "").strip():
            return Turn(
                outcome=CLARIFY, action=ABOUT, game=game,
                reason="Не вижу, на что ты смотришь. Назови предмет, блок или существо — "
                       "расскажу, что это.",
                explanation=["вопрос «что это» без названия: сведений об экране нет"],
                stages=stages,
            )

        # Цель не названа — уточняем, а не додумываем. Вопрос формулирует валидатор.
        if not str(proposal.target or "").strip():
            verdict = self.validator.validate(proposal.action, None, count, mode)
            return Turn(outcome=verdict.outcome, action=proposal.action, count=count,
                        reason=verdict.reason, explanation=verdict.explanation, stages=stages)

        resolution = self.resolver.resolve(str(proposal.target), action=proposal.action)
        stages["resolver"] = {
            "status": resolution.status,
            "thing": resolution.thing,
            "confidence": resolution.confidence,
            "candidates": [item.thing for item in resolution.candidates],
        }

        if resolution.status == AMBIGUOUS:
            return Turn(
                outcome=CLARIFY,
                action=proposal.action,
                count=count,
                query=proposal.target,
                reason=self._ask_which(resolution.candidates),
                options=resolution.candidates,
                explanation=[f"запрос «{proposal.target}» допускает несколько прочтений"],
                stages=stages,
            )

        if resolution.status == NOT_FOUND:
            suggestions = self._suggest(str(proposal.target), proposal.action, stages)
            if suggestions:
                names = ", ".join(f"«{option.label}»" for option in suggestions)
                return Turn(
                    outcome=CLARIFY,
                    action=proposal.action,
                    count=count,
                    query=proposal.target,
                    reason=f"Такого названия я не знаю. Возможно, ты имел в виду {names}?",
                    options=suggestions,
                    explanation=[f"«{proposal.target}» нет в базе знаний; модель предложила "
                                 f"другое название, оно найдено, но без подтверждения не выполняется"],
                    stages=stages,
                )
            return Turn(
                outcome=REJECT,
                action=proposal.action,
                count=count,
                reason="Такого в игре нет.",
                explanation=[f"«{proposal.target}» не сопоставлено ни с одной сущностью базы знаний"],
                stages=stages,
            )

        return self._decide(proposal.action, resolution.thing, count, mode, inventory, stages,
                            memory=memory, position=position)

    def _suggest(self, phrase: str, action: str, stages: Dict[str, Any]) -> List[Candidate]:
        """Одна попытка переформулировки, когда резолвер ничего не нашёл.

        Модель знает игру и переводит слова игрока в официальное название; резолвер
        проверяет, что такое название есть в базе знаний. Найденное предлагается вопросом,
        а не выполняется: переформулировка — догадка модели, и подтверждать её должен игрок.
        Векторное сопоставление для этой роли проверялось и отклонено: близость не отделяет
        парафразы от выдуманных названий (eval/reports/2026-10-19-embedding-probe.md).
        """
        rename = getattr(self.extractor, "rename", None)
        if rename is None:
            return []
        try:
            names = rename(phrase)
        except Exception:
            return []
        if isinstance(names, str):
            names = [names]
        stages["rename"] = {"from": phrase, "to": list(names or [])}

        for name in names or []:
            if name.strip().lower() == phrase.strip().lower():
                continue
            resolution = self.resolver.resolve(name, action=action)
            if resolution.ok:
                return [candidate for candidate in resolution.candidates
                        if candidate.thing == resolution.thing][:1]
            if resolution.status == AMBIGUOUS:
                return list(resolution.candidates[:3])
        return []

    def _follow_up(self, utterance: str, pending: Optional["Turn"], mode: str,
                   inventory: Optional[Dict[str, int]], memory: Optional[Any] = None,
                   position: Optional[Any] = None) -> Optional["Turn"]:
        """Ответ на уточняющий вопрос разбирается без модели.

        После «какой именно: алмазный, железный…?» реплика «алмазный» сама по себе ни о чём
        не говорит, но вместе с исходным запросом однозначна. Ответ ищется только среди
        предложенных вариантов: если реплика ни одному не соответствует, это уже новый
        запрос, и он идёт обычным путём.
        """
        if pending is None or pending.outcome != CLARIFY or not pending.options:
            return None
        offered = {option.thing for option in pending.options}

        # «Возможно, ты имел в виду X?» — «да». Согласие относится только к вопросу с одним
        # вариантом: при нескольких «да» ничего не выбирает.
        if len(pending.options) == 1 and AFFIRMATIVE.match((utterance or "").strip()):
            chosen = pending.options[0].thing
            stages = {"follow_up": {"question": pending.reason, "answer": utterance,
                                    "chosen": chosen}}
            return self._decide(pending.action, chosen, pending.count, mode, inventory, stages,
                                memory=memory, position=position)

        for query in (f"{utterance} {pending.query or ''}".strip(), utterance):
            resolution = self.resolver.resolve(query, action=pending.action)
            chosen = resolution.thing if resolution.ok else None
            if chosen in offered:
                stages = {"follow_up": {"question": pending.reason, "answer": utterance,
                                        "chosen": chosen}}
                return self._decide(pending.action, chosen, pending.count, mode, inventory, stages,
                                memory=memory, position=position)
        return None

    def _decide(self, action: str, thing: str, count: Any, mode: str,
                inventory: Optional[Dict[str, int]], stages: Dict[str, Any], *,
                memory: Optional[Any] = None, position: Optional[Any] = None) -> "Turn":
        """Вердикт валидатора и, если действие допустимо, команда или план."""
        # Вопрос о мире ничего в игре не меняет, поэтому проверять здесь нечего: спросить
        # можно и о запрещённом к призыву драконе. Отвечает база знаний.
        if action in WORLD_ACTIONS:
            return self._answer_world(action, thing, inventory, stages, memory=memory,
                                      position=position)

        verdict = self.validator.validate(action, thing, count, mode)
        stages["validator"] = {"outcome": verdict.outcome, "clamped": verdict.clamped}

        if verdict.outcome == ACT and verdict.action == PLAN_GOAL:
            turn = self._build_plan(verdict, inventory, stages, memory=memory, position=position)
            return self._plan_instead_of_give(turn, mode)
        if verdict.outcome == ACT and verdict.action == START_CAMPAIGN:
            return self._build_campaign(verdict, mode, inventory, stages)
        if verdict.outcome == ACT and verdict.action == EXECUTE_PLAN:
            return self._build_execution(verdict, inventory, stages, memory=memory,
                                         position=position)

        command = None
        if verdict.outcome == ACT:
            command = self._command(verdict.action, verdict.game_id or verdict.target, verdict.count)

        return Turn(
            outcome=verdict.outcome,
            action=verdict.action,
            target=verdict.target,
            label=self.kb.label(verdict.target) if verdict.target else None,
            count=verdict.count,
            command=command,
            reason=verdict.reason,
            remedy=self._remedy(verdict, inventory, stages),
            explanation=verdict.explanation,
            stages=stages,
        )

    def _plan_instead_of_give(self, turn: "Turn", mode: str) -> "Turn":
        """Игрок просил выдать, а получил план — об этом нужно сказать.

        В выживании выдача модели не предъявляется, и «дай кирку» она разбирает как
        просьбу о плане. Подмена не должна быть молчаливой: к ответу дописывается, что
        выдачи здесь нет и при каком условии она есть (условие — от валидатора).
        """
        if not GIVE_WORDS.search(self._utterance or ""):
            return turn
        refused = self.validator.validate("give_item", turn.target, 1, mode)
        if refused.cause != "mode":
            return turn
        turn.remedy = ("Выдачи в этом режиме нет, поэтому вместо неё план. "
                       + (refused.remedy or "")).strip()
        turn.explanation.append("просьба выдать в режиме без выдачи: вместо выдачи построен план")
        turn.stages["give_as_plan"] = True
        return turn

    def _remedy(self, verdict, inventory: Optional[Dict[str, int]],
                stages: Dict[str, Any]) -> Optional[str]:
        """Условие, при котором отклонённая просьба станет выполнимой.

        Условие по правилам называет валидатор. Здесь к нему добавляется вывод
        планировщика: выдача в выживании отклонена режимом, но тот же предмет достижим
        планом — и расстояние до него от инвентаря игрока известно, а не обещано.
        """
        if verdict.outcome != REJECT:
            return None
        remedy = verdict.remedy
        if verdict.cause == "mode" and verdict.action == "give_item" and verdict.target:
            checked = self.validator.validate(PLAN_GOAL, verdict.target, 1, SURVIVAL)
            if checked.allowed:
                plan = self.planner.plan(verdict.target, 1, inventory)
                steps = len(plan.actions())
                label = self.kb.label(verdict.target) or verdict.target
                stages["remedy"] = {"feasible": plan.feasible, "steps": steps}
                if plan.feasible and steps:
                    path = (f"В выживании это достижимо: до цели «{label}» от твоего инвентаря "
                            f"шагов — {steps}. Спроси, как это получить, — покажу план.")
                    remedy = path + (" " + remedy if remedy else "")
                    verdict.explanation.append(f"условие: план до «{label}» выполним, шагов {steps}")
        if remedy:
            stages.setdefault("remedy", {})["text"] = remedy
        return remedy

    @property
    def executor(self):
        if self._executor is None:
            from solver.executor import ExecutionPlanner
            self._executor = ExecutionPlanner(self.kb, self.planner)
        return self._executor

    def _build_execution(self, verdict, inventory: Optional[Dict[str, int]],
                         stages: Dict[str, Any], *, memory: Optional[Any] = None,
                         position: Optional[Any] = None) -> Turn:
        """«Сделай сам»: план от инвентаря, проверка, что автопилот его выполнит, и вопрос.

        Запуск только после «да» игрока; задачи моду выдаёт api/autopilot.py по одной,
        с пересчётом плана после каждой. Модель здесь уже не участвует.
        """
        from solver.executor import Context

        context_flags = self._autopilot
        companion = context_flags.get("companion") or {}
        by_companion = self._executor_kind == "companion"
        if by_companion:
            # План считается от инвентаря и места помощника, а не игрока.
            from memory.model import Position
            inventory = dict(companion.get("inventory") or {})
            position = Position.parse(companion.get("position") or {}) or position
        executor = self.executor
        limits = companion if by_companion else context_flags
        executor.max_effort = int(limits.get("max_effort") or executor.max_effort)
        storage, where, known = {}, {}, {}
        if memory is not None:
            storage, where = self.locator.storage(memory, position)
            known = self.locator.known_sources(memory, position)
        context = Context(inventory=dict(inventory or {}), storage=storage, where=where,
                          known=known)
        plan = executor.plan(verdict.target, verdict.count, context)
        label = self.kb.label(verdict.target) or verdict.target
        actions = plan.actions()
        taken = [step for step in plan.taken() if not step.children]
        stages["planner"] = {"feasible": plan.feasible, "effort": plan.effort,
                             "actions": len(actions), "taken": len(taken)}
        if plan.feasible and not actions and not taken:
            return Turn(outcome=REJECT, action=EXECUTE_PLAN, target=verdict.target, label=label,
                        count=verdict.count, reason=f"«{label}» уже есть в инвентаре.",
                        explanation=verdict.explanation + ["цель уже в инвентаре"],
                        stages=stages)
        problems = executor.check(plan)
        if by_companion:
            checked = self.validator.validate_companion(
                verdict, problems, enabled=bool(companion.get("enabled")),
                connected=bool(companion.get("connected")),
                name=str(companion.get("name") or "помощник"))
        else:
            checked = self.validator.validate_execution(
                verdict, problems, enabled=bool(context_flags.get("enabled")),
                singleplayer=context_flags.get("singleplayer"),
                available=bool(context_flags.get("available")))
        stages["autopilot"] = {"outcome": checked.outcome, "problems": problems,
                               "executor": self._executor_kind}
        if not checked.allowed:
            # В мире, открытом для сети, сам мод не исполняет, но помощник на связи —
            # отказ называет, как попросить его.
            remedy = None
            if (not by_companion and context_flags.get("singleplayer") is False
                    and companion.get("connected")):
                remedy = (f"В мире, открытом для сети, план исполняет помощник: скажи «пусть "
                          f"помощник сделает {label.lower()}».")
            return Turn(outcome=REJECT, action=EXECUTE_PLAN, target=verdict.target, label=label,
                        count=verdict.count, reason=checked.reason, remedy=remedy,
                        explanation=checked.explanation, stages=stages, plan=plan,
                        plan_tree=render_tree(plan, self.kb),
                        plan_summary=render_summary(plan, self.kb))
        total = len(actions) + len(taken)
        # Около 20 секунд на шаг: дойти, добыть или открыть станцию и забрать результат.
        minutes = max(1, round(total * 20 / 60))
        mining = any(step.kind == "mine" for step in actions)
        if by_companion:
            name = companion.get("name") or "помощник"
            handover = {"drop": " Готовое принесёт тебе.", "chest": " Готовое положит в сундук.",
                        "none": " Готовое оставит у себя."}.get(companion.get("delivery"), "")
            summary = (f"Сделает помощник {name}. План: {render_summary(plan, self.kb)} "
                       f"Шагов около {total}, примерно {minutes} мин.{handover} Начать? "
                       f"Остановить — «помощник, стоп».")
        else:
            note = ""
            if mining and not context_flags.get("baritone"):
                note = (" Baritone не установлен: добывать буду просить тебя, "
                        "остальное сделаю сам.")
            summary = (f"План: {render_summary(plan, self.kb)} Шагов около {total}, "
                       f"примерно {minutes} мин.{note} Начать? Остановить — клавиша K "
                       f"или любое движение.")
        return Turn(outcome=ACT, action=EXECUTE_PLAN, target=verdict.target, label=label,
                    count=verdict.count, explanation=checked.explanation, stages=stages,
                    plan=plan, plan_tree=render_tree(plan, self.kb),
                    plan_summary=render_summary(plan, self.kb),
                    autopilot={"status": "pending_confirm", "total": total,
                               "effort": plan.effort, "executor": self._executor_kind},
                    autopilot_summary=summary)

    def _build_quest(self, utterance: str, proposal: Proposal, actions: Sequence[str],
                     mode: str, inventory: Optional[Dict[str, int]],
                     stages: Dict[str, Any]) -> Turn:
        """Задание, придуманное моделью, принимается только после доказательства выполнимости.

        Модель предлагает цель и награду; решатель строит план достижения цели из текущего
        состояния, проверяет награду правилами выдачи и оценивает трудоёмкость. Отклонённое
        возвращается модели с причиной один раз. Если и второе предложение не проходит,
        игрок получает отказ, а не невыполнимое задание.
        """
        checker = QuestChecker(self.kb, self.planner, self.validator)
        attempts: List[Dict[str, Any]] = []
        current = proposal
        reason = "Не получилось составить посильное задание."

        for attempt in range(QUEST_RETRIES + 1):
            verdict, reason = self._check_quest(current, checker, inventory, mode)
            attempts.append({"target": current.target, "count": current.count,
                             "reward": current.reward, "accepted": bool(verdict and verdict.accepted),
                             "reason": None if verdict and verdict.accepted else reason})
            stages["quest"] = attempts

            if verdict is not None and verdict.accepted:
                return Turn(
                    outcome=ACT,
                    action=CREATE_QUEST,
                    target=verdict.target,
                    label=self.kb.label(verdict.target),
                    count=verdict.goal_count,
                    explanation=verdict.explanation,
                    stages=stages,
                    plan=verdict.plan,
                    plan_tree=render_tree(verdict.plan, self.kb) if verdict.plan else None,
                    plan_summary=render_summary(verdict.plan, self.kb) if verdict.plan else None,
                    quest={
                        "goal_kind": verdict.goal_kind,
                        "target": verdict.target,
                        "goal_count": verdict.goal_count,
                        "reward": verdict.reward,
                        "reward_label": self.kb.label(verdict.reward) if verdict.reward else None,
                        "reward_count": verdict.reward_count,
                        "reward_game_id": verdict.reward_game_id,
                        "effort": verdict.effort,
                        "attempts": len(attempts),
                    },
                )

            if attempt < QUEST_RETRIES:
                current = self.extractor.extract(utterance, actions=(CREATE_QUEST,), feedback=reason)
                if current.action != CREATE_QUEST:
                    break

        return Turn(outcome=REJECT, action=CREATE_QUEST, reason=reason,
                    explanation=[f"задание отклонено: {reason}"], stages=stages)

    def _check_quest(self, proposal: Proposal, checker: QuestChecker,
                     inventory: Optional[Dict[str, int]], mode: str):
        """Сопоставляет предложенное задание с онтологией и проверяет решателем."""
        goal_kind = (proposal.goal_kind or "").strip().lower()
        if goal_kind not in (COLLECT, KILL):
            goal_kind = COLLECT
        if not proposal.target or not proposal.reward:
            return None, "в задании не названы цель или награда"

        # Цель задания сопоставляется как существо для «победить» и как вещь для «собрать».
        goal_action = "summon_entity" if goal_kind == KILL else "give_item"
        target = self.resolver.resolve(proposal.target, action=goal_action)
        if not target.ok:
            return None, f"цель «{proposal.target}» не опознана однозначно, назови конкретный объект"
        reward = self.resolver.resolve(proposal.reward, action="give_item")
        if not reward.ok:
            return None, f"награда «{proposal.reward}» не опознана однозначно, назови конкретный предмет"

        try:
            goal_count = int(proposal.count)
            reward_count = int(proposal.reward_count or 1)
        except (TypeError, ValueError):
            return None, "количество в задании должно быть числом"

        verdict = checker.check(goal_kind, target.thing, goal_count, reward.thing, reward_count,
                                inventory=inventory, mode=mode)
        return verdict, verdict.reason or "задание не прошло проверку"

    def _build_plan(self, verdict, inventory: Optional[Dict[str, int]],
                    stages: Dict[str, Any], *, memory: Optional[Any] = None,
                    position: Optional[Any] = None) -> Turn:
        """План достижения цели от текущего инвентаря.

        Здесь символьная часть работает уже не арбитром, а исполнителем: ответ целиком
        выводится решателем, а модели остаётся его озвучить. Недостижимая цель даёт не
        отказ, а план с отметкой, на каком предмете вывод упёрся, — игроку важнее знать,
        чего не хватает, чем услышать «нельзя».
        """
        storage, where, known = {}, {}, {}
        # Наблюдения мода (сундуки, жилы) есть только у Minecraft.
        if memory is not None and self._minecraft:
            locator = self.locator
            storage, where = locator.storage(memory, position)
            known = locator.known_sources(memory, position)
        plan = self.planner.plan(verdict.target, verdict.count, inventory,
                                 storage=storage, where=where, known=known)
        stages["planner"] = {
            "feasible": plan.feasible,
            "steps": len(plan.actions()),
            "effort": plan.effort,
            "missing": plan.missing,
            "decisions": len(plan.decisions),
        }
        explanation = list(verdict.explanation)
        explanation.append(
            f"план построен: {len(plan.actions())} действий, трудоёмкость {plan.effort}"
            if plan.feasible else
            f"вывод упёрся на: {', '.join(plan.missing[:3])}"
        )
        taken = [step for step in plan.taken() if not step.children]
        if taken:
            stages["planner"]["taken"] = {step.item: step.count for step in taken}
            explanation.append("из хранилищ по памяти мира: " + ", ".join(
                f"{self.kb.label(step.item) or step.item} ×{step.count}"
                + (f" ({step.where})" if step.where else "") for step in taken))
        seen = [step for step in plan.actions() if step.where]
        if seen:
            explanation.append("источники, которые ты видел: " + ", ".join(
                dict.fromkeys(f"{self.kb.label(step.source) or step.source} — {step.where}"
                              for step in seen)))
        return Turn(
            outcome=ACT,
            action=PLAN_GOAL,
            target=verdict.target,
            label=self.kb.label(verdict.target),
            count=verdict.count,
            reason=None if plan.feasible else "Цель недостижима из текущего состояния.",
            explanation=explanation,
            stages=stages,
            plan=plan,
            plan_tree=render_tree(plan, self.kb),
            plan_summary=render_summary(plan, self.kb),
            plan_alternatives=render_alternatives(plan, self.kb),
        )

    def _build_campaign(self, verdict, mode: str, inventory: Optional[Dict[str, int]],
                        stages: Dict[str, Any], *, proposed: bool = False,
                        candidates: Sequence[GoalCandidate] = ()) -> Turn:
        """Кампания к цели: цепочка этапов, доказанная решателем.

        Модель здесь не участвует: что бы она ни сказала о цели, этапы и награды выводятся
        из дерева плана и инвентаря. Недостижимая цель — отказ с причиной решателя.
        """
        campaign = self.campaigns.build(verdict.target, verdict.count, inventory or {}, mode)
        stages["campaign"] = {
            "feasible": campaign.feasible,
            "stages": campaign.targets,
            "effort": campaign.effort,
        }
        label = self.kb.label(verdict.target)
        if not campaign.feasible:
            return Turn(
                outcome=REJECT, action=START_CAMPAIGN, target=verdict.target, label=label,
                count=verdict.count, reason=campaign.reason,
                explanation=list(verdict.explanation) + list(campaign.explanation),
                stages=stages, proposed=proposed, candidates=tuple(candidates),
            )
        first = campaign.stages[0]
        return Turn(
            outcome=ACT, action=START_CAMPAIGN, target=verdict.target, label=label,
            count=verdict.count,
            explanation=list(verdict.explanation) + [
                f"кампания: этапов {len(campaign.stages)}, трудоёмкость {campaign.effort}"],
            stages=stages,
            campaign=campaign,
            plan=first.plan,
            plan_tree=render_tree(first.plan, self.kb),
            plan_summary=render_summary(first.plan, self.kb),
            proposed=proposed,
            candidates=tuple(candidates),
        )

    def _answer_world(self, action: str, thing: Optional[str],
                      inventory: Optional[Dict[str, int]], stages: Dict[str, Any], *,
                      memory: Optional[Any] = None, position: Optional[Any] = None) -> Turn:
        """Вопрос о мире: что делается из предмета, откуда он, что падает, что доступно.

        Ответ — обратный запрос к онтологии. Модель определила только вид вопроса и назвала
        предмет словами игрока; в ответе нет ничего, чего нет в базе знаний.
        """
        if self._world is None:
            self._world = WorldFacts(self.kb, self.planner)
        if action == AVAILABLE:
            answer = self._world.available(inventory or {})
        elif action == USES:
            answer = self._world.uses(thing)
        elif action == SOURCES:
            answer = self._world.sources(thing)
        elif action == ABOUT:
            answer = self._world.about(thing)
        else:
            answer = self._world.drops(thing)
        stages["world"] = {"question": answer.question, "subject": thing,
                           "items": list(answer.items), "total": answer.total}
        summary = render_answer(answer, self.kb)
        explanation = list(answer.explanation)
        facts: List[Any] = []
        numbers: List[int] = []
        # «Откуда берётся» и «что это» дополняются тем, где игрок такое уже видел.
        if memory is not None and thing and action in (SOURCES, ABOUT):
            located = self.locator.locate(thing, memory, inventory=inventory, position=position)
            if located:
                nearest = located[0]
                facts = [entry.fact for entry in located]
                summary += " Ты видел: " + self._seen_text(nearest, position) + "."
                if nearest.fact.position is not None:
                    numbers = [nearest.fact.position.x, nearest.fact.position.y,
                               nearest.fact.position.z]
                explanation.append(f"память мира: увиденных источников {len(located)}, "
                                   f"ближайший — «{nearest.fact.label}»")
                stages["world"]["seen"] = [entry.fact.id for entry in located]
        return Turn(
            outcome=ACT, action=action, target=thing,
            label=self.kb.label(thing) if thing else None,
            explanation=explanation, stages=stages,
            world=answer, world_summary=summary,
            world_essentials=tuple(world_essentials(answer, self.kb)),
            memory=tuple(facts), memory_numbers=tuple(numbers),
        )

    def _seen_text(self, entry, position: Optional[Any]) -> str:
        from memory.recall import describe
        from solver.locator import tool_sentence
        text = describe(entry.fact, origin=position, label_of=self._label_of(DEFAULT_GAME),
                        show_source=False)
        sentence = tool_sentence(entry, self.kb)
        return f"{text}. {sentence[:-1]}" if sentence else text

    def _search_web(self, utterance: str, proposal: Proposal, game: str,
                    stages: Dict[str, Any]) -> Turn:
        """Вопрос, на который онтология не отвечает: ответ ищется в сети.

        Модель составила поисковый запрос, поиск нашёл фрагменты и отбросил нерелевантные;
        формулировать ответ модель будет только по ним. Найденный текст в выбор действия не
        попадает: на его основании ничего не выдаётся и не призывается.
        """
        query = proposal.search_query or proposal.target or utterance
        answer = self.search.search(query, game=game)
        stages["search"] = {"query": answer.query, "engine": answer.engine,
                            "sources": [hit.site for hit in answer.hits],
                            # Релевантность принятых страниц на общей шкале движков.
                            "scores": [round(hit.score, 2) for hit in answer.hits
                                       if hit.score is not None],
                            "rejected": answer.rejected, "errors": list(answer.errors),
                            "elapsed_ms": round(answer.elapsed_ms), "cached": answer.cached,
                            "game": game}
        if not answer.found:
            # Сбой всех движков и «ничего релевантного» — разные вещи, и игрок должен их
            # различать: в первом случае стоит спросить позже, во втором — иначе.
            unavailable = bool(answer.errors) and not answer.rejected
            reason = ("Поиск сейчас недоступен, попробуй позже." if unavailable
                      else "В сети не нашёл надёжного ответа на этот вопрос.")
            return Turn(outcome=REJECT, action=SEARCH_WEB, reason=reason, query=proposal.target,
                        explanation=[f"поиск «{answer.query}»: " + (
                            "все движки недоступны" if unavailable
                            else f"релевантных страниц нет, отброшено {answer.rejected}")],
                        stages=stages, web=answer, game=game)
        return Turn(
            outcome=ACT, action=SEARCH_WEB, query=proposal.target,
            explanation=[f"поиск «{answer.query}» через {answer.engine}: "
                         + ", ".join(hit.site for hit in answer.hits)],
            stages=stages, web=answer, game=game,
        )

    # --- память мира ---

    def _label_of(self, game: str):
        if game.lower() != DEFAULT_GAME.lower():
            return lambda thing: None
        return lambda thing: self.kb.label(thing) if thing and self.kb.exists(thing) else None

    def _memory_turn(self, utterance: str, proposal: Proposal, game: str, memory: Optional[Any],
                     position: Optional[Any], actions: Sequence[str],
                     stages: Dict[str, Any], inventory: Optional[Dict[str, int]] = None) -> Turn:
        """Запомнить, вспомнить, забыть.

        Модель определила только вид просьбы и назвала предмет словами игрока. Что
        записать, где и что ответить, решает система: позиция — из сведений мода, факты —
        из памяти, координаты в ответ ставит шаблон. Факт памяти ни на одном шаге не
        превращается в команду игре.
        """
        action = proposal.action
        if memory is None:
            return Turn(outcome=REJECT, action=action, game=game, stages=stages,
                        reason="Память мира сейчас недоступна.",
                        explanation=["память мира не подключена"])
        is_minecraft = game.lower() == DEFAULT_GAME.lower()
        if action == REMEMBER:
            return self._remember(utterance, proposal, game, memory,
                                  position if is_minecraft else None, stages)
        if action == FORGET_FACT:
            return self._forget(utterance, proposal, game, memory, position, stages)
        return self._recall(utterance, proposal, game, memory,
                            position if is_minecraft else None, actions, stages, inventory)

    def _remember(self, utterance: str, proposal: Proposal, game: str, memory: Any,
                  position: Optional[Any], stages: Dict[str, Any]) -> Turn:
        from memory.model import PLAYER

        # Заметка — слова игрока после «запомни», без «тут»: место ставит система.
        text = REMEMBER_PREFIX.sub("", utterance or "")
        text = " ".join(HERE_CUES.sub(" ", text).split()).strip(" .,!:—-") or None
        label = str(proposal.target or "").strip() or text
        if not label:
            return Turn(outcome=CLARIFY, action=REMEMBER, game=game, stages=stages,
                        reason="Что запомнить? Скажи, например: «запомни, тут база».",
                        explanation=["в просьбе запомнить не названо, что именно"])
        here = bool(HERE_CUES.search(utterance or ""))
        explanation = []
        place = position if here else None
        if here and position is None:
            explanation.append("игрок просил отметить место, но мод координат не присылал — "
                               "записана заметка без места")
        same = text is not None and text.lower().replace("ё", "е") == label.lower().replace("ё", "е")
        fact = memory.note(label, None if same else text, position=place,
                           source=PLAYER)
        memory.save()
        stages["memory"] = {"action": REMEMBER, "fact": fact.id, "kind": fact.kind}
        summary = f"Запомнил: {fact.label}"
        numbers: List[int] = []
        if fact.position is not None:
            summary += f", {fact.position.describe()}"
            numbers = [fact.position.x, fact.position.y, fact.position.z]
            explanation.append(f"отметка «{fact.label}» по позиции игрока: {fact.position.describe()}")
        else:
            explanation.append(f"заметка «{fact.label}» записана словами игрока")
        summary += "."
        if fact.kind != "place" and game.lower() != DEFAULT_GAME.lower():
            summary += f" Спроси «где {fact.label}» — напомню."
        return Turn(outcome=ACT, action=REMEMBER, label=fact.label, game=game, stages=stages,
                    explanation=explanation, memory=(fact,), memory_summary=summary,
                    memory_numbers=tuple(numbers))

    def _subjects(self, target: Optional[str]) -> List[str]:
        """Сущности онтологии, которые игрок назвал: сам предмет и блоки, из которых он
        добывается, — «где алмазы» ищет алмазную руду."""
        if not str(target or "").strip():
            return []
        resolution = self.resolver.resolve(str(target))
        if resolution.ok:
            things = [resolution.thing]
        elif resolution.status == "ambiguous":
            # Вспомнить — действие только для чтения: при неоднозначном названии («железо» —
            # слиток, кусочек или руда) ищется по всем кандидатам, а не переспрашивается.
            things = [candidate.thing for candidate in resolution.candidates[:RECALL_CANDIDATES]]
        else:
            return []
        subjects: List[str] = []
        for thing in things:
            subjects.append(thing)
            subjects.extend(source.id for source in self.kb.sources_of(thing)
                            if source.kind != "creature")
        return list(dict.fromkeys(subjects))

    def _recall(self, utterance: str, proposal: Proposal, game: str, memory: Any,
                position: Optional[Any], actions: Sequence[str], stages: Dict[str, Any],
                inventory: Optional[Dict[str, int]] = None) -> Turn:
        from memory.model import DEATH
        from memory.recall import describe, numbers as fact_numbers, recall

        label_of = self._label_of(game)
        target = str(proposal.target or "").strip() or None
        death = bool(DEATH_CUES.search(utterance or ""))
        overview = not death and bool(OVERVIEW_CUES.search(utterance or ""))
        subjects = ([] if death or overview or game.lower() != DEFAULT_GAME.lower()
                    else self._subjects(target))
        if death:
            last = memory.last_death()
            facts = [last] if last is not None else []
        elif overview:
            # Все факты мира, недавние и близкие — первыми; названо будет несколько, остальное —
            # во вкладке «Память».
            facts = [match.fact for match in recall(memory, None, position=position,
                                                    label_of=label_of, limit=50)]
        else:
            matches = recall(memory, target or utterance, subjects=subjects, position=position,
                             label_of=label_of)
            facts = [match.fact for match in matches]
        stages["memory"] = {"action": RECALL, "query": target, "subjects": subjects,
                            "death": death, "overview": overview,
                            "found": [fact.id for fact in facts]}

        if not facts and overview:
            return Turn(outcome=REJECT, action=RECALL, game=game, stages=stages,
                        reason="В памяти этого мира пока ничего нет. Скажи «запомни, …» — "
                               "запишу; увиденное в игре добавляется само.",
                        explanation=["память: обзор запрошен, фактов нет"])
        if not facts:
            # В другой игре память — первый источник, поиск — второй, как и раньше.
            if game.lower() != DEFAULT_GAME.lower() and SEARCH_WEB in actions and not death:
                stages["memory"]["fallback"] = SEARCH_WEB
                return self._search_web(utterance, proposal, game, stages)
            what = "о гибели" if death else (f"про «{target}»" if target else "об этом")
            return Turn(outcome=REJECT, action=RECALL, query=target, game=game, stages=stages,
                        reason=f"В памяти этого мира ничего нет {what}.",
                        explanation=[f"память: фактов {what} не найдено"])

        spoken = facts[:RECALL_SPOKEN]
        lines = [describe(fact, origin=position, label_of=label_of) for fact in spoken]
        summary = "; ".join(lines) + "."
        # Для руды — нужен ли инструмент и есть ли он: уровень из онтологии, инвентарь из игры.
        if subjects and spoken[0].kind == "resource":
            from solver.locator import tool_sentence
            located = [entry for entry in self.locator.locate(subjects[0], memory,
                                                                inventory=inventory,
                                                                position=position)
                       if entry.fact is spoken[0]]
            sentence = tool_sentence(located[0], self.kb) if located else None
            if sentence:
                summary += " " + sentence
        if len(facts) > len(spoken):
            summary += f" Ещё {len(facts) - len(spoken)} — во вкладке «Память»."
        numbers: List[int] = []
        for fact in spoken:
            numbers.extend(fact_numbers(fact))
        if overview:
            summary = f"В памяти записей: {len(facts)}. " + summary
        explanation = [f"память: {'обзор, ' if overview else ''}найдено фактов {len(facts)}, "
                       f"названо {len(spoken)}"]
        explanation.extend(f"факт {fact.kind} «{fact.label or fact.subject}», источник {fact.source}"
                           for fact in spoken)
        return Turn(outcome=ACT, action=RECALL, query=target, game=game, stages=stages,
                    label=spoken[0].label, explanation=explanation, memory=tuple(facts),
                    memory_summary=summary, memory_numbers=tuple(numbers))

    def _forget(self, utterance: str, proposal: Proposal, game: str, memory: Any,
                position: Optional[Any], stages: Dict[str, Any]) -> Turn:
        from memory.recall import recall

        target = str(proposal.target or "").strip() or None
        if not FORGET_CUES.search(utterance or ""):
            return Turn(outcome=CLARIFY, action=FORGET_FACT, query=target, game=game, stages=stages,
                        reason="Забыть это? Если да, скажи прямо: «забудь …».",
                        explanation=["забыть предложено моделью, но в реплике нет просьбы забыть"])
        if not target:
            return Turn(outcome=CLARIFY, action=FORGET_FACT, game=game, stages=stages,
                        reason="Что забыть? Назови отметку или заметку.",
                        explanation=["в просьбе забыть не названо, что именно"])
        subjects = self._subjects(target) if game.lower() == DEFAULT_GAME.lower() else []
        matches = recall(memory, target, subjects=subjects, position=position,
                         label_of=self._label_of(game))
        stages["memory"] = {"action": FORGET_FACT, "query": target,
                            "found": [match.fact.id for match in matches]}
        if not matches:
            return Turn(outcome=REJECT, action=FORGET_FACT, query=target, game=game, stages=stages,
                        reason=f"Про «{target}» в памяти ничего нет — забывать нечего.",
                        explanation=[f"память: фактов про «{target}» не найдено"])
        best = matches[0].score
        tied = [match.fact for match in matches if match.score == best]
        if len(tied) > 1 and len({(fact.label or "").lower() for fact in tied}) > 1:
            names = ", ".join(f"«{fact.label}»" for fact in tied[:RECALL_SPOKEN])
            return Turn(outcome=CLARIFY, action=FORGET_FACT, query=target, game=game,
                        stages=stages, reason=f"Что именно забыть: {names}?",
                        explanation=[f"под «{target}» подходят несколько записей — забывание "
                                     f"необратимо, поэтому нужен выбор"])
        for fact in tied:
            memory.forget(fact.id)
        memory.save()
        label = tied[0].label or target
        return Turn(outcome=ACT, action=FORGET_FACT, label=label, query=target, game=game,
                    stages=stages, memory=tuple(tied),
                    memory_summary=f"Забыл отметку «{label}».",
                    explanation=[f"забыто записей: {len(tied)}, «{label}»"])

    def _show_progress(self, inventory: Optional[Dict[str, int]], stages: Dict[str, Any]) -> Turn:
        """Ступень развития игрока — целиком вывод решателя, модель его только озвучит.

        Ступень определяется по лучшей кирке в инвентаре, открытое ею — по требованиям
        источников добычи из онтологии, путь к следующей — планировщиком.
        """
        report = Progression(self.kb, self.planner).assess(inventory or {})
        stages["progress"] = {"rank": report.rank, "tool": report.tool,
                              "next_tool": report.next_tool}
        plan = report.next_plan
        return Turn(
            outcome=ACT, action=SHOW_PROGRESS,
            target=report.next_tool, label=self.kb.label(report.next_tool) if report.next_tool else None,
            explanation=list(report.explanation), stages=stages,
            progress=report, progress_summary=render_progress(report, self.kb),
            plan=plan,
            plan_tree=render_tree(plan, self.kb) if plan else None,
            plan_summary=render_summary(plan, self.kb) if plan else None,
        )

    def _propose_goal(self, mode: str, inventory: Optional[Dict[str, int]],
                      state: Optional[str], stages: Dict[str, Any]) -> Turn:
        """Цель предлагает ассистент: решатель отбирает достижимые, модель выбирает уместную.

        Выбор модели сверяется со списком кандидатов; что-либо вне списка — или отсутствие
        модели — даёт самую короткую из доказанных целей. Так кампания к предложенной цели
        не может оказаться невыполнимой, кто бы её ни назвал.
        """
        interest = self._preferences.get("interest")
        candidates = self.campaigns.propose_goals(inventory or {}, mode, interest=interest)
        stages["candidates"] = [candidate.item for candidate in candidates]
        if interest:
            stages["interest"] = {"kind": interest,
                                  "basis": self._preferences.get("interest_basis")}
        if not candidates:
            return Turn(outcome=REJECT, action=PROPOSE_GOAL,
                        reason="Из того, что у тебя есть, посильной цели не вывести.",
                        explanation=["решатель не нашёл достижимых целей"], stages=stages)

        chosen = candidates[0]
        choice: Dict[str, Any] = {"goal": None, "why": None, "accepted": False}
        choose = getattr(self.extractor, "choose", None)
        if choose is not None:
            try:
                listed = [{"item": candidate.item, "label": self.kb.label(candidate.item) or candidate.item,
                           "stages": candidate.stages, "effort": candidate.effort}
                          for candidate in candidates]
                answer = choose(listed, state or "") or {}
                choice.update({"goal": answer.get("goal"), "why": answer.get("why")})
                wanted = str(answer.get("goal") or "").strip().lower()
                for candidate, entry in zip(candidates, listed):
                    if wanted and wanted in (candidate.item.lower(), entry["label"].lower()):
                        chosen = candidate
                        choice["accepted"] = True
                        break
            except Exception:
                pass
        stages["choice"] = choice

        verdict = self.validator.validate(START_CAMPAIGN, chosen.item, 1, mode)
        stages["validator"] = {"outcome": verdict.outcome, "clamped": verdict.clamped}
        if verdict.outcome != ACT:
            return Turn(outcome=verdict.outcome, action=START_CAMPAIGN, target=chosen.item,
                        reason=verdict.reason, explanation=verdict.explanation, stages=stages,
                        proposed=True, candidates=tuple(candidates))
        return self._build_campaign(verdict, mode, inventory, stages, proposed=True,
                                    candidates=candidates)

    # --- вспомогательное ---

    @staticmethod
    def _reconcile_count(utterance: str, proposed: Any) -> Any:
        """Число из реплики важнее числа из ответа модели.

        Модель предлагает, система проверяет: если игрок назвал число цифрами, берётся оно.
        Словесные количества остаются за моделью — цифрами их в тексте нет.
        """
        literal = LITERAL_COUNT.search(utterance or "")
        return int(literal.group(1)) if literal else proposed

    @staticmethod
    def _ask_which(candidates: Sequence[Candidate]) -> str:
        names = ", ".join(candidate.label for candidate in candidates)
        return f"Уточни, что именно нужно: {names}?"

    @staticmethod
    def _command(action: str, game_id: str, count: int) -> str:
        if action == "summon_entity":
            return "\n".join(f"/summon {game_id} ~ ~ ~" for _ in range(max(1, count)))
        template = COMMANDS.get(action)
        return template.format(id=game_id, count=count) if template else ""
