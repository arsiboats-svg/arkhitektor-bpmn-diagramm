"""
ИИ-генератор BPMN по тексту регламента (ПАО «Интер РАО», трек «Архитектор BPMN-диаграмм»).

Конвейер:
    регламент (RU) ──► LLM (Ollama / OpenAI-совместимый API) ──► Python-код для DIAGRAM
                  └──► [fail-safe] семантический эмулятор ────────┘
                                   │
                        безопасная песочница (AST-проверка)
                                   │
                    heal_graph → layout → BPMN XML + аудит

Публичный API:
    generate_bpmn_from_text(regulation_text) -> (bpmn_xml, audit_data, error)
    execute_generated_code(code_str)         -> (bpmn_xml, audit_data, error)
    PROMPT_TEMPLATE / build_prompt(text)

Модуль не падает при недоступности моделей: при любом сбое сети, таймауте или
некорректном коде включается встроенный эмулятор на основе семантического
сопоставления шагов регламента (роли, условия, параллельность, декомпозиция).
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from bpmn_framework import GATEWAY_KINDS, WORK_KINDS, BPMNDiagramBuilder
from validate_bpmn import xsd_errors_xml

ROOT_PROCESS_ID = "Process_Root"
ROOT_START_TASK_ID = "Event_RootStart"
ROOT_END_TASK_ID = "Event_RootEnd"

OLLAMA_PREFERRED_MODELS = ("qwen2.5-coder", "llama3")
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"

# --------------------------------------------------------------------------- #
# Системный промпт
# --------------------------------------------------------------------------- #
PROMPT_TEMPLATE = """Ты — Senior Enterprise Business Architect ПАО «Интер РАО» (Дирекция бизнес-архитектуры).
Твоя задача — превратить текст регламента в исполняемый Python-код построения BPMN 2.0 диаграммы
через готовый объект DIAGRAM. Объект DIAGRAM и константы ROOT_PROCESS_ID, ROOT_START_TASK_ID,
ROOT_END_TASK_ID уже определены — НЕ импортируй ничего и НЕ создавай их заново.

API объекта DIAGRAM (строго эти сигнатуры):
- pool_id, lane_ids = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Роль 1", "Роль 2", ...])   # список дорожек
- task_id = DIAGRAM.add_task(name, parent_id)              # обычная задача
- task_id = DIAGRAM.add_user_task(name, parent_id)         # действие человека
- task_id = DIAGRAM.add_script_task(name, parent_id)       # автоматическое действие системы
- sub_id = DIAGRAM.create_subprocess(name, parent_id)      # подпроцесс; внутрь добавляй задачи с parent_id=sub_id
- gw_id = DIAGRAM.add_exclusive_gateway(name, parent_id)   # XOR: name — вопрос («Допуск выдан?»)
- gw_id = DIAGRAM.add_parallel_gateway(name, parent_id)    # AND: разветвление/слияние параллельных веток
- gw_id = DIAGRAM.add_inclusive_gateway(name, parent_id)   # OR
- group_id = DIAGRAM.add_group(name, parent_id)
- DIAGRAM.add_link(source_id, target_id, condition_name="")  # поток; у веток шлюза ОБЯЗАТЕЛЬНА подпись условия

ЖЁСТКИЕ ТРЕБОВАНИЯ:
1. РОЛЕВАЯ МОДЕЛЬ. Выдели всех участников регламента и создай для них дорожки одним вызовом add_pool.
   parent_id для узлов — это id нужной дорожки (элемент lane_ids). Каждый шаг размещай в дорожке исполнителя.
2. ДЕКОМПОЗИЦИЯ (анти-«метро Токио»). Если последовательность шагов ОДНОГО подразделения превышает 3
   действия подряд — обязательно сгруппируй их в DIAGRAM.create_subprocess с понятным названием этапа.
   Задачи внутри подпроцесса соединяй между собой add_link; parent_id = id подпроцесса.
3. УСЛОВИЯ. Каждое «если/иначе» — exclusive-шлюз; обе исходящие ветки подписаны (например «Допуск выдан»,
   «Замечания»). Возвраты на доработку моделируй обратной связью на предыдущий шаг.
4. ПАРАЛЛЕЛЬНОСТЬ. Шаги «одновременно/параллельно» — через parallel-шлюзы (разветвление и слияние).
5. ЦЕЛОСТНОСТЬ. Все ветки обязаны начинаться от ROOT_START_TASK_ID и заканчиваться на ROOT_END_TASK_ID:
   первый шаг связывай DIAGRAM.add_link(ROOT_START_TASK_ID, ...), все финальные ветки —
   DIAGRAM.add_link(..., ROOT_END_TASK_ID). Тупиков и «висящих» узлов быть не должно.
6. Имена задач — короткие, в форме «глагол + объект» (до 70 символов), на русском языке.
7. ФОРМАТ ОТВЕТА: верни ТОЛЬКО исполняемый Python-код для объекта DIAGRAM. Никаких markdown-тегов
   (без ```), пояснений, импортов, циклов while и обращений к файлам/сети.

ПРИМЕР ОТВЕТА:
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Инициатор", "Согласующий"])
a = DIAGRAM.add_user_task("Подготовить заявку", lanes[0])
b = DIAGRAM.add_user_task("Проверить заявку", lanes[1])
g = DIAGRAM.add_exclusive_gateway("Заявка корректна?", lanes[1])
c = DIAGRAM.add_user_task("Исполнить заявку", lanes[1])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, b)
DIAGRAM.add_link(b, g)
DIAGRAM.add_link(g, c, "Заявка корректна")
DIAGRAM.add_link(g, a, "Замечания")
DIAGRAM.add_link(c, ROOT_END_TASK_ID)

ТЕКСТ РЕГЛАМЕНТА:
<<REGULATION>>
"""


def build_prompt(regulation_text: str) -> str:
    return PROMPT_TEMPLATE.replace("<<REGULATION>>", regulation_text.strip())


REPAIR_TEMPLATE = """

ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ:
<<CODE>>

В НЁМ НАЙДЕНЫ ОШИБКИ СТРУКТУРЫ ПРОЦЕССА:
<<ISSUES>>

Исправь их и верни ПОЛНЫЙ исправленный код целиком (только Python-код для DIAGRAM, без пояснений).
Помни: ветвление только через шлюз, у каждой задачи ровно один выход, каждый узел имеет вход и выход."""


def build_repair_prompt(regulation_text: str, code: str, issues: List[str]) -> str:
    """Промпт второй попытки: исходная задача + прошлый код + найденные ошибки."""
    return build_prompt(regulation_text) + (
        REPAIR_TEMPLATE.replace("<<CODE>>", code.strip()).replace("<<ISSUES>>", "\n".join(f"- {i}" for i in issues))
    )


# --------------------------------------------------------------------------- #
# Песочница исполнения сгенерированного кода
# --------------------------------------------------------------------------- #
_ALLOWED_BUILTINS = (
    "len range enumerate zip list dict tuple set str int float bool min max sum sorted reversed abs round "
    "any all map filter isinstance repr print Exception KeyError ValueError TypeError IndexError AttributeError"
).split()
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "exit", "quit", "help", "memoryview", "classmethod", "staticmethod",
}
_MAX_RANGE = 2000


class UnsafeCodeError(ValueError):
    """Сгенерированный код нарушает правила песочницы."""


def _strip_markdown(code: str) -> str:
    fenced = re.findall(r"```(?:python|py)?\s*\n(.*?)```", code, flags=re.S | re.I)
    if fenced:
        code = "\n".join(fenced)
    else:
        code = code.replace("```python", "").replace("```", "")
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if re.match(r"^(import\s+\S+|from\s+\S+\s+import\s+.+)$", stripped):
            continue  # модель любит добавлять импорты — они не нужны и запрещены
        if re.match(r"^DIAGRAM\s*=", stripped):
            continue  # DIAGRAM уже предоставлен песочницей
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def _validate_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise UnsafeCodeError("импорты запрещены")
        if isinstance(node, (ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal)):
            raise UnsafeCodeError(f"конструкция {type(node).__name__} запрещена")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise UnsafeCodeError(f"доступ к атрибуту {node.attr!r} запрещён")
        if isinstance(node, ast.Name) and (node.id in _FORBIDDEN_NAMES or node.id.startswith("__")):
            raise UnsafeCodeError(f"имя {node.id!r} запрещено")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int) and abs(arg.value) > _MAX_RANGE:
                    raise UnsafeCodeError("слишком большой диапазон range")


def _structure_issues(diagram: BPMNDiagramBuilder) -> List[str]:
    """Логические ошибки графа ДО самовосстановления: то, что heal_graph скрыл бы «метро»."""
    issues: List[str] = []
    outgoing: Dict[str, List[Any]] = {}
    incoming: Dict[str, int] = {}
    for link in diagram.links:
        outgoing.setdefault(link.source_id, []).append(link)
        incoming[link.target_id] = incoming.get(link.target_id, 0) + 1
    for node in diagram.nodes.values():
        outs = outgoing.get(node.id, [])
        targets = {l.target_id for l in outs}  # дубли в одну цель (после переадресации движком) — не ветвление
        if node.kind in WORK_KINDS | {"subProcess"} and len(targets) > 1:
            issues.append(f"У задачи «{node.name}» {len(targets)} выхода — ветвление без шлюза")
        if node.kind in GATEWAY_KINDS and len(outs) <= 1 and incoming.get(node.id, 0) <= 1:
            issues.append(f"Шлюз «{node.name}» ничего не разветвляет и не сливает")
        if node.kind in ("exclusiveGateway", "inclusiveGateway") and len(outs) > 1:
            if any(not l.condition_name.strip() for l in outs):
                issues.append(f"У шлюза «{node.name}» есть ветка без подписи условия")
    return issues


# Ошибки, при которых ответ модели отклоняется (остальные — предупреждения).
# «Шлюз ничего не разветвляет» — лишь предупреждение: так бывает, когда ветка из подпроцесса
# переадресована движком на сам подпроцесс; схема при этом корректна.
_CRITICAL_MARKERS = ("ветвление без шлюза", "без входа", "Несуществующие")


def _quality_report(structure_issues: List[str], audit: Dict[str, Any]) -> Dict[str, Any]:
    issues = list(structure_issues)
    issues += [h for h in audit.get("auto_healed", []) if "без входа" in h or "Тупик" in h]
    issues += [f"Связь пропущена: {s['reason']}" for s in audit.get("skipped_links", [])]
    critical = [i for i in issues if any(m in i for m in _CRITICAL_MARKERS)]
    return {"issues": issues, "critical": critical, "ok": not critical}


def execute_generated_code(
    code_str: str,
    process_name: str = "Бизнес-процесс ПАО «Интер РАО»",
    sla_target_hours: Optional[float] = None,
) -> Tuple[str, Dict[str, Any], str]:
    """Безопасно исполняет код для DIAGRAM и возвращает (bpmn_xml, audit_data, error).

    Любые исключения перехватываются: при ошибке возвращается ("", {}, "описание").
    """
    try:
        code = _strip_markdown(code_str or "")
        if len(code.strip()) < 10:
            return "", {}, "Пустой код: модель не вернула инструкций для DIAGRAM."
        try:
            tree = ast.parse(code, filename="<generated>")
        except SyntaxError as exc:
            return "", {}, f"Синтаксическая ошибка в сгенерированном коде (строка {exc.lineno}): {exc.msg}"
        try:
            _validate_ast(tree)
        except UnsafeCodeError as exc:
            return "", {}, f"Код отклонён песочницей: {exc}."

        diagram = BPMNDiagramBuilder(
            process_name=process_name,
            process_id=ROOT_PROCESS_ID,
            root_start_id=ROOT_START_TASK_ID,
            root_end_id=ROOT_END_TASK_ID,
            sla_target_hours=sla_target_hours,
        )
        safe_builtins = {name: getattr(builtins, name) for name in _ALLOWED_BUILTINS}
        namespace: Dict[str, Any] = {
            "__builtins__": safe_builtins,
            "DIAGRAM": diagram,
            "ROOT_PROCESS_ID": ROOT_PROCESS_ID,
            "ROOT_START_TASK_ID": ROOT_START_TASK_ID,
            "ROOT_END_TASK_ID": ROOT_END_TASK_ID,
        }
        exec(compile(tree, "<generated>", "exec"), namespace)  # noqa: S102 — песочница: AST-фильтр + whitelist builtins

        work_nodes = [n for n in diagram.nodes.values() if n.kind not in ("startEvent", "endEvent")]
        if len(work_nodes) < 2:
            return "", {}, "Диаграмма содержит меньше двух рабочих узлов — регламент не распознан."

        structure_issues = _structure_issues(diagram)
        diagram.heal_graph()
        xml = diagram.to_bpmn_xml(ROOT_PROCESS_ID, ROOT_START_TASK_ID, ROOT_END_TASK_ID)
        audit = diagram.analyze_bottlenecks()
        audit["quality"] = _quality_report(structure_issues, audit)
        errors = xsd_errors_xml(xml)  # официальная XSD BPMN 2.0: невалидный файл не отдаём
        if errors:
            return "", {}, "XML не прошёл проверку по XSD BPMN 2.0: " + "; ".join(errors[:3])
        audit["xsd_valid"] = errors is not None
        return xml, audit, ""
    except Exception as exc:  # noqa: BLE001 — песочница обязана не падать
        return "", {}, f"Ошибка исполнения кода: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# HTTP-клиенты (Ollama / OpenAI-совместимый API)
# --------------------------------------------------------------------------- #
def _http_json(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    try:
        import requests  # type: ignore

        if payload is None:
            resp = requests.get(url, headers=headers, timeout=timeout)
        else:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except ImportError:  # requests не установлен — стандартная библиотека
        import urllib.request

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))


def _ollama_base() -> str:
    return os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _ollama_models() -> List[str]:
    data = _http_json(f"{_ollama_base()}/api/tags", timeout=1.5)
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def _pick_ollama_model(models: List[str]) -> Optional[str]:
    prefs = [os.getenv("OLLAMA_MODEL", "")] + list(OLLAMA_PREFERRED_MODELS)
    for pref in filter(None, prefs):
        for name in models:
            if name.lower().startswith(pref.lower()):
                return name
    return None


def _call_ollama(prompt: str) -> Tuple[str, str]:
    model = _pick_ollama_model(_ollama_models())
    if not model:
        raise RuntimeError("в Ollama нет моделей qwen2.5-coder / llama3")
    # num_ctx: промпт ~1800 токенов + ответ до 3500 не помещаются в дефолтные 4096 Ollama.
    # Таймаут 300 с: 7B-модель на MacBook M2 генерирует ~5 ток/с, схема — 3–4 минуты.
    options = {"temperature": 0.1, "num_predict": 3500, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))}
    data = _http_json(
        f"{_ollama_base()}/api/generate",
        {"model": model, "prompt": prompt, "stream": False, "keep_alive": "30m", "options": options},
        timeout=float(os.getenv("OLLAMA_TIMEOUT", "300")),
    )
    return f"ollama:{model}", str(data.get("response", ""))


def _call_openai(prompt: str, prefix: str = "OPENAI") -> Tuple[str, str]:
    """OpenAI-совместимый chat/completions. prefix: OPENAI_* — основной провайдер, FALLBACK_* — запасной."""
    key = os.getenv(f"{prefix}_API_KEY")
    if not key:
        raise RuntimeError(f"{prefix}_API_KEY не задан")
    base = os.getenv(f"{prefix}_BASE_URL", OPENAI_DEFAULT_BASE).rstrip("/")
    model = os.getenv(f"{prefix}_MODEL", "gpt-4o-mini")
    system, _, regulation = prompt.partition("ТЕКСТ РЕГЛАМЕНТА:")
    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system.strip()},
            {"role": "user", "content": "ТЕКСТ РЕГЛАМЕНТА:\n" + regulation.strip()},
        ],
    }
    # Рассуждающие модели (gpt-oss на Groq): low — меньше «мыслей», быстрее и в пределах лимита токенов/мин.
    if os.getenv(f"{prefix}_REASONING_EFFORT"):
        payload["reasoning_effort"] = os.getenv(f"{prefix}_REASONING_EFFORT")
    data = _http_json(
        f"{base}/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {key}"},
        timeout=float(os.getenv("OPENAI_TIMEOUT", "90")),
    )
    label = "openai" if prefix == "OPENAI" else prefix.lower()
    return f"{label}:{model}", str(data["choices"][0]["message"]["content"])


# --------------------------------------------------------------------------- #
# Семантический эмулятор (fail-safe)
# --------------------------------------------------------------------------- #
ROLE_PATTERNS: List[Tuple[str, str]] = [
    (r"диспетчер\w*", "Диспетчер"),
    (r"начальник\w* смены", "Начальник смены"),
    (r"ремонтн\w+ бригад\w*|бригад[аыуеой]\b", "Ремонтная бригада"),
    (r"служб\w* безопасности|\bСБ\b", "Служба безопасности"),
    (r"тендерн\w+ комитет\w*|закупочн\w+ комисси\w*", "Тендерный комитет"),
    (r"закупочн\w+ служб\w*|департамент\w* закупок|отдел\w* закупок", "Закупочная служба"),
    (r"технический департамент\w*|технический заказчик\w*|инициатор\w* закупки", "Технический департамент"),
    (r"юридическ\w+ служб\w*|юридическ\w+ департамент\w*|юрист\w*", "Юридическая служба"),
    (r"финансов\w+ департамент\w*|казначейств\w*", "Финансовый департамент"),
    (r"заявител\w+|потребител\w+", "Заявитель"),
    (r"центр\w* обслуживания клиентов|клиентск\w+ служб\w*", "Центр обслуживания клиентов"),
    (r"отдел\w* технологического присоединения", "Отдел технологического присоединения"),
    (r"эксплуатационн\w+ служб\w*|служб\w* эксплуатации", "Эксплуатационная служба"),
    (r"руководител\w+|директор\w*", "Руководитель"),
]
_ROLE_RE = [(re.compile(p, re.I), name) for p, name in ROLE_PATTERNS]

VERB_MAP = {
    "принимает": "принять", "подаёт": "подать", "подает": "подать", "выдаёт": "выдать", "выдает": "выдать",
    "вносит": "внести", "проводит": "провести", "готовит": "подготовить", "производит": "произвести",
    "оформляет": "оформить", "направляет": "направить", "определяет": "определить", "проверяет": "проверить",
    "выполняет": "выполнить", "составляет": "составить", "подписывает": "подписать", "заключает": "заключить",
    "передаёт": "передать", "передает": "передать", "допускает": "допустить", "выводит": "вывести",
    "восстанавливает": "восстановить", "измеряет": "измерить", "рассчитывает": "рассчитать",
    "регистрирует": "зарегистрировать", "формирует": "сформировать", "фиксирует": "зафиксировать",
    "утверждает": "утвердить", "согласует": "согласовать", "публикует": "опубликовать",
    "уведомляет": "уведомить", "закрывает": "закрыть", "обосновывает": "обосновать",
    "уточняет": "уточнить", "разрабатывает": "разработать", "оценивает": "оценить", "подключает": "подключить",
    "организует": "организовать", "устраняет": "устранить", "выбирает": "выбрать", "готовят": "подготовить",
    "создаёт": "создать", "создает": "создать", "заносит": "занести", "платит": "оплатить", "берёт": "взять",
    "отвечает": "ответить", "решает": "решить", "принимают": "принять", "отправит": "отправить",
    "вводит": "ввести", "выносит": "вынести", "запросит": "запросить", "получит": "получить",
}
_VERB_SUFFIXES = (("ирует", "ировать"), ("ует", "овать"), ("ает", "ать"), ("яет", "ять"))
_SYSTEM_RE = re.compile(
    r"автоматическ|в (?:оперативном )?журнал|в (?:информационной |автоматизированной |электронной )?систем|"
    r"рассчитывает|электронн\w+ площадк|в реестр",
    re.I,
)
_DUR_RE = re.compile(
    r"\(?\s*(?:(?:в течение|не более|до|срок[:\s]*)\s*)?(\d+(?:[.,]\d+)?)\s*"
    r"(рабоч\w*\s+дн\w*|календарн\w*\s+дн\w*|сут\w*|дн\w*|час\w*|ч\b|мин\w*)\s*\)?",
    re.I,
)
_REF_RE = re.compile(r"(?:п(?:ункт\w*|\.|п\.)?|шаг\w*)\s*(\d+)", re.I)
_END_KW = re.compile(r"завершить|отказать|завершается|завершение процесса|прекрат|закрыть\s+(?:заявку|процесс|закупку)", re.I)
_BACK_KW = re.compile(r"верну|возврат|доработ|повтор|заново", re.I)


_PAGE_MARK_RE = re.compile(r"(?:стр\.?|страница)\s*\d+\s*(?:из\s*\d+)?", re.I)
_MULTI_NUM_RE = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+", re.M)


def normalize_regulation(text: str) -> str:
    """Чистит текст, скопированный из PDF/Word, до вида «шаг на строке».

    - убирает колонтитулы («Стр. 5 из 17») и строки из одних номеров страниц;
    - склеивает переносы слов («регистри-\\nрует») и ссылки, разорванные строкой («п.\\n3.2.5»);
    - многоуровневую нумерацию «3.2.1.» переводит в сквозную «1.», «2.» … вместе со ссылками «п. 3.2.5».
    """
    text = text.replace("\r\n", "\n").replace("­", "")
    text = re.sub(r"(\w)-\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"(\bп(?:\.|ункт\w*)|\bшаг\w*)\s*\n\s*(?=\d)", r"\1 ", text, flags=re.I)
    lines = []
    for line in text.split("\n"):
        line = _PAGE_MARK_RE.sub("", line) if _PAGE_MARK_RE.search(line) and len(line.strip()) < 120 else line
        if re.search(r"\S\s{8,}\S", line):  # колонтитул PDF: «ПАО …        СТО 123-2023»
            continue
        if not re.fullmatch(r"\s*[-–—]?\s*\d{1,3}\s*[-–—]?\s*", line):  # номер страницы
            lines.append(line.rstrip())
    text = "\n".join(lines)

    numbers = _MULTI_NUM_RE.findall(text)
    depth = max((n.count(".") for n in numbers), default=0)
    steps = [n for n in numbers if n.count(".") == depth]  # «3.2 Раздел» — заголовок, «3.2.1.» — шаг
    if len(steps) >= 2:
        mapping = {num: str(i) for i, num in enumerate(dict.fromkeys(steps), start=1)}
        text = _MULTI_NUM_RE.sub(lambda m: mapping[m.group(1)] + ". " if m.group(1) in mapping else m.group(0), text)
        for num in sorted(mapping, key=len, reverse=True):
            text = re.sub(r"(?<![\d.])" + re.escape(num) + r"(?![\d])", mapping[num], text)
    return text


def _to_hours(value: str, unit: str) -> float:
    num = float(value.replace(",", "."))
    unit = unit.lower()
    if unit.startswith("мин"):
        return num / 60.0
    if unit.startswith("час") or unit == "ч":
        return num
    if unit.startswith("сут") or unit.startswith("календарн"):
        return num * 24.0
    return num * 8.0  # рабочие дни


def _infinitive(word: str) -> str:
    low = word.lower()
    if low in VERB_MAP:
        return VERB_MAP[low]
    if len(low) >= 6:
        for suffix, repl in _VERB_SUFFIXES:
            if low.endswith(suffix):
                return low[: -len(suffix)] + repl
    return word


def _task_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .;,:—–-")
    words = [_infinitive(w) if re.fullmatch(r"[А-Яа-яЁё]+", w) else w for w in text.split(" ")]
    title = " ".join(words)
    title = title[:1].upper() + title[1:] if title else "Выполнить действие"
    if len(title) > 70:
        cut = title[:70].rsplit(" ", 1)[0]
        title = cut.rstrip(",;:—–- ") + "…"
    return title


_CONNECTORS_RE = re.compile(r"^(?:затем|далее|потом|после этого|также|при этом)[,\s]+", re.I)
_ADVERBS = {"автоматически", "затем", "далее", "также", "самостоятельно", "обязательно", "незамедлительно", "оперативно"}


def _is_verb(word: str) -> bool:
    low = word.lower().strip(",.;")
    return low in VERB_MAP or (len(low) >= 6 and low.endswith(("ает", "яет", "ует")))


def _subject_before_verb(text: str) -> Optional[Tuple[str, int]]:
    """Подлежащее — слова перед первым глаголом («Начальник смены» оценивает …)."""
    m = _CONNECTORS_RE.match(text)
    offset = m.end() if m else 0
    tokens = list(re.finditer(r"\S+", text[offset:]))
    for i, tok in enumerate(tokens[:6]):
        if i >= 1 and _is_verb(tok.group()):
            words = [t.group() for t in tokens[:i]]
            while words and words[-1].lower().strip(",") in _ADVERBS:
                words.pop()
            if not words or len(words) > 4 or any(re.search(r"[\d:;()«»,]", w) for w in words):
                return None
            return " ".join(words), offset + tok.start()
        if re.search(r"[.;:]$", tok.group()):
            break
    return None


def _canonical_role(subject: str) -> str:
    for regex, name in _ROLE_RE:
        if regex.search(subject):
            return name
    return subject[:1].upper() + subject[1:]


def _find_role(text: str) -> Tuple[Optional[str], int, int]:
    """Роль-исполнитель шага: (название, начало, конец вырезаемого префикса)."""
    m = re.match(r"^([А-ЯЁ][А-Яа-яЁё\- ]{2,45}?)\s*[:—–]\s+", text)
    if m and len(m.group(1).split()) <= 5 and not re.match(r"(?i)^(если|параллельно|одновременно)", m.group(1)):
        return _canonical_role(m.group(1).strip()), 0, m.end()
    subject = _subject_before_verb(text[:120])
    if subject:
        return _canonical_role(subject[0]), 0, subject[1]
    best: Optional[Tuple[int, int, str]] = None
    for regex, name in _ROLE_RE:
        mm = regex.search(text[:90])
        if mm and (best is None or mm.start() < best[0]):
            best = (mm.start(), mm.end(), name)
    if best:
        return best[2], best[0], best[1]
    return None, -1, -1


@dataclass
class Decision:
    yes_label: str
    no_label: str
    yes_ref: Optional[int] = None
    no_ref: Optional[int] = None
    yes_end: bool = False
    no_end: bool = False
    no_back: bool = False
    has_else: bool = False


@dataclass
class Step:
    idx: int
    num: int
    role: str = ""
    title: str = ""
    parallel: bool = False
    stage: Optional[str] = None
    hours: Optional[float] = None
    system: bool = False
    decision: Optional[Decision] = None
    action: bool = True  # есть ли собственное действие перед шлюзом


@dataclass
class Block:
    kind: str  # step | decision | parallel | subprocess
    steps: List[Step]
    name: str = ""

    @property
    def role(self) -> str:
        return self.steps[0].role


@dataclass
class ParsedRegulation:
    title: str
    sla_hours: Optional[float]
    steps: List[Step] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)


def _derive_no_label(clause: str) -> str:
    low = clause.lower()
    if "замечан" in low:
        return "Замечания"
    if "отказ" in low or "отклон" in low:
        return "Отказ"
    if _BACK_KW.search(low):
        return "На доработку"
    return "Иначе"


def _parse_decision(text: str) -> Tuple[str, Optional[Decision]]:
    m = re.search(r"\bесли\b", text, re.I)
    if not m:
        return text, None
    action = text[: m.start()].strip(" .;,—–-")
    rest = text[m.end():]
    else_m = re.search(r"[,;.]?\s*\b(?:иначе|в противном случае)\b[,:]?", rest, re.I)
    yes_part = rest[: else_m.start()] if else_m else rest
    no_part = rest[else_m.end():] if else_m else ""
    pieces = re.split(r"\s+[—–-]\s+|,\s+|:\s+", yes_part.strip(), maxsplit=1)
    cond = pieces[0].strip(" ,.;")
    yes_clause = pieces[1] if len(pieces) > 1 else ""
    quoted_yes = re.search(r"«([^»]+)»", cond)
    quoted_no = re.search(r"«([^»]+)»", no_part)
    yes_label = (quoted_yes.group(1) if quoted_yes else cond).strip()
    yes_label = yes_label[:1].upper() + yes_label[1:]
    no_label = quoted_no.group(1).strip() if quoted_no else _derive_no_label(no_part)
    no_label = no_label[:1].upper() + no_label[1:]
    yes_ref = _REF_RE.search(yes_clause)
    no_ref = _REF_RE.search(no_part)
    decision = Decision(
        yes_label=yes_label[:40],
        no_label=no_label[:40],
        yes_ref=int(yes_ref.group(1)) if yes_ref else None,
        no_ref=int(no_ref.group(1)) if no_ref else None,
        yes_end=bool(_END_KW.search(yes_clause)),
        no_end=bool(_END_KW.search(no_part)) and not no_ref,
        no_back=bool(_BACK_KW.search(no_part)) and not no_ref,
        has_else=bool(else_m),
    )
    return action, decision


def parse_regulation(text: str) -> ParsedRegulation:
    title: Optional[str] = None
    sla: Optional[float] = None
    raw_steps: List[Tuple[Optional[int], str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(?:регламент|название|процесс)\s*[:—–-]\s*(.+)$", s, re.I)
        if m and title is None:
            title = m.group(1).strip()
            continue
        m = re.match(r"^(?:целев\w+\s+(?:срок|sla)[^:—–]*|sla)\s*[:—–-]\s*(.+)$", s, re.I)
        if m:
            d = _DUR_RE.search(m.group(1))
            if d:
                sla = _to_hours(d.group(1), d.group(2))
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)$", s)
        if m:
            raw_steps.append((int(m.group(1)), m.group(2)))
            continue
        m = re.match(r"^[-•*]\s+(.*)$", s)
        if m:
            raw_steps.append((None, m.group(1)))
            continue
        if raw_steps:
            num, prev = raw_steps[-1]
            raw_steps[-1] = (num, prev + " " + s)
        elif title is None:
            title = s
        else:
            raw_steps.append((None, s))

    if len(raw_steps) < 2:  # нет нумерации — режем на предложения
        sentences = re.split(r"(?<=[.;!?])\s+(?=[А-ЯЁA-Z«])", " ".join(t for _, t in raw_steps) or text)
        raw_steps = [(None, s.strip()) for s in sentences if len(s.strip()) > 8]

    parsed = ParsedRegulation(title=title or "Бизнес-процесс по регламенту", sla_hours=sla)
    last_role = ""
    for idx, (num, body) in enumerate(raw_steps):
        step = Step(idx=idx, num=num if num is not None else idx + 1)
        t = body.strip()
        pm = re.match(r"^(параллельно|одновременно)\s*[:,—–-]?\s*", t, re.I)
        if pm:
            step.parallel = True
            t = t[pm.end():]
        sm = re.search(r"\(\s*этап\s*«([^»]+)»\s*\)", t, re.I)
        if sm:
            step.stage = sm.group(1).strip()
            t = (t[: sm.start()] + t[sm.end():]).strip()
        dm = _DUR_RE.search(t)
        if dm:
            step.hours = _to_hours(dm.group(1), dm.group(2))
            t = (t[: dm.start()] + " " + t[dm.end():]).strip()
        action, decision = _parse_decision(t)
        step.decision = decision
        step.action = bool(action)
        source = action if action else t
        role, rs, re_ = _find_role(source)
        if role is None and decision is not None:
            role, rs, re_ = _find_role(t)
        step.role = role or last_role or "Исполнитель"
        last_role = step.role
        if role and rs == 0:
            source = source[re_:]
        step.title = _task_title(source) if step.action else ""
        step.system = bool(_SYSTEM_RE.search(source))
        parsed.steps.append(step)
        if step.role not in parsed.roles:
            parsed.roles.append(step.role)
    return parsed


def _build_blocks(parsed: ParsedRegulation) -> Tuple[List[Block], Dict[int, int]]:
    steps = parsed.steps
    targets = {s.decision.yes_ref for s in steps if s.decision and s.decision.yes_ref}
    targets |= {s.decision.no_ref for s in steps if s.decision and s.decision.no_ref}

    blocks: List[Block] = []
    for step in steps:
        if step.decision is not None:
            blocks.append(Block("decision", [step]))
        elif step.parallel and blocks and blocks[-1].kind in ("step", "parallel"):
            prev = blocks[-1]
            if prev.kind == "step":
                blocks[-1] = Block("parallel", prev.steps + [step])
            else:
                prev.steps.append(step)
        else:
            blocks.append(Block("step", [step]))

    merged: List[Block] = []
    i = 0
    while i < len(blocks):
        if blocks[i].kind != "step":
            merged.append(blocks[i])
            i += 1
            continue
        j = i + 1
        while (
            j < len(blocks)
            and blocks[j].kind == "step"
            and blocks[j].role == blocks[i].role
            and blocks[j].steps[0].num not in targets
        ):
            j += 1
        run = blocks[i:j]
        if len(run) > 3:  # строго больше 3 действий подразделения → подпроцесс
            pieces = 1 if len(run) <= 7 else -(-len(run) // 5)
            size = -(-len(run) // pieces)
            for k in range(0, len(run), size):
                chunk = run[k:k + size]
                chunk_steps = [b.steps[0] for b in chunk]
                stage = next((s.stage for s in chunk_steps if s.stage), None)
                name = stage or f"{chunk[0].role}: {chunk_steps[0].title[:38].rstrip('…')}"
                merged.append(Block("subprocess", chunk_steps, name=name))
        else:
            merged.extend(run)
        i = j

    number_to_block: Dict[int, int] = {}
    for b_idx, block in enumerate(merged):
        for s in block.steps:
            number_to_block[s.num] = b_idx
    return merged, number_to_block


def _q(text: str) -> str:
    return repr(text)


def emulate_generation(regulation_text: str) -> Tuple[str, Dict[str, Any]]:
    """Строит код для DIAGRAM по тексту регламента без внешних моделей."""
    parsed = parse_regulation(regulation_text)
    blocks, num_to_block = _build_blocks(parsed)
    lane_var = {role: f"lane_{i}" for i, role in enumerate(parsed.roles)}

    code: List[str] = [
        f"# Сгенерировано семантическим эмулятором: {parsed.title}",
        f"pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, {json.dumps(parsed.roles, ensure_ascii=False)})",
    ]
    code += [f"lane_{i} = lanes[{i}]" for i in range(len(parsed.roles))]

    def emit_task(var: str, step: Step, parent: str) -> None:
        func = "add_script_task" if step.system else "add_user_task"
        code.append(f"{var} = DIAGRAM.{func}({_q(step.title)}, {parent})")
        if step.hours is not None:
            code.append(f"DIAGRAM.set_sla({var}, {round(step.hours, 3)})")

    entry: List[str] = []
    exits: List[str] = []
    gateways: Dict[int, str] = {}
    for b_idx, block in enumerate(blocks):
        lane = lane_var[block.role]
        if block.kind == "step":
            var = f"n{b_idx}"
            emit_task(var, block.steps[0], lane)
            entry.append(var)
            exits.append(var)
        elif block.kind == "decision":
            step = block.steps[0]
            gw = f"g{b_idx}"
            question = step.decision.yes_label.rstrip("?") + "?"  # type: ignore[union-attr]
            if step.action:
                var = f"n{b_idx}"
                emit_task(var, step, lane)
                code.append(f"{gw} = DIAGRAM.add_exclusive_gateway({_q(question)}, {lane})")
                code.append(f"DIAGRAM.add_link({var}, {gw})")
                entry.append(var)
            else:
                code.append(f"{gw} = DIAGRAM.add_exclusive_gateway({_q(question)}, {lane})")
                entry.append(gw)
            gateways[b_idx] = gw
            exits.append(gw)
        elif block.kind == "parallel":
            split, join = f"ps{b_idx}", f"pj{b_idx}"
            code.append(f"{split} = DIAGRAM.add_parallel_gateway('Параллельные работы', {lane})")
            code.append(f"{join} = DIAGRAM.add_parallel_gateway('Работы завершены', {lane})")
            for k, step in enumerate(block.steps):
                var = f"n{b_idx}_{k}"
                emit_task(var, step, lane_var[step.role])
                code.append(f"DIAGRAM.add_link({split}, {var})")
                code.append(f"DIAGRAM.add_link({var}, {join})")
            entry.append(split)
            exits.append(join)
        else:  # subprocess
            sub = f"sp{b_idx}"
            code.append(f"{sub} = DIAGRAM.create_subprocess({_q(block.name)}, {lane})")
            prev = None
            for k, step in enumerate(block.steps):
                var = f"n{b_idx}_{k}"
                emit_task(var, step, sub)
                if prev:
                    code.append(f"DIAGRAM.add_link({prev}, {var})")
                prev = var
            entry.append(sub)
            exits.append(sub)

    end_expr = "ROOT_END_TASK_ID"
    code.append(f"DIAGRAM.add_link(ROOT_START_TASK_ID, {entry[0]})")

    def resolve(ref: Optional[int], own: int) -> Optional[str]:
        if ref is None or ref not in num_to_block:
            return None
        target = num_to_block[ref]
        return None if target == own else entry[target]

    for b_idx, block in enumerate(blocks):
        nxt = entry[b_idx + 1] if b_idx + 1 < len(blocks) else end_expr
        if block.kind != "decision":
            code.append(f"DIAGRAM.add_link({exits[b_idx]}, {nxt})")
            continue
        d = block.steps[0].decision
        assert d is not None
        gw = gateways[b_idx]
        yes_t = "ROOT_END_TASK_ID" if d.yes_end else (resolve(d.yes_ref, b_idx) or nxt)
        if d.no_end:
            no_t = end_expr
        elif d.no_ref is not None:
            no_t = resolve(d.no_ref, b_idx) or nxt
        elif d.no_back:
            no_t = entry[b_idx - 1] if b_idx > 0 else end_expr
        elif d.has_else:
            no_t = end_expr
        else:
            no_t = nxt
        code.append(f"DIAGRAM.add_link({gw}, {yes_t}, {_q(d.yes_label)})")
        if no_t != yes_t:
            code.append(f"DIAGRAM.add_link({gw}, {no_t}, {_q(d.no_label)})")

    info = {
        "title": parsed.title,
        "steps": len(parsed.steps),
        "roles": parsed.roles,
        "blocks": len(blocks),
        "subprocesses": sum(1 for b in blocks if b.kind == "subprocess"),
        "decisions": sum(1 for b in blocks if b.kind == "decision"),
        "parallel_groups": sum(1 for b in blocks if b.kind == "parallel"),
        "sla_hours": parsed.sla_hours,
    }
    return "\n".join(code) + "\n", info


# --------------------------------------------------------------------------- #
# Главная точка входа
# --------------------------------------------------------------------------- #
def _error_reason(exc: Exception) -> str:
    """Короткая причина сбоя LLM: для HTTP-ошибок — код и сообщение провайдера («401: Invalid API Key»)."""
    if "Timeout" in type(exc).__name__:
        return "таймаут"
    response = getattr(exc, "response", None)  # requests.Response ложен при 4xx — сравниваем с None
    if response is None:
        response = getattr(exc, "fp", None)
    status = getattr(response, "status_code", None) or getattr(exc, "code", None)
    if status is None:
        return type(exc).__name__
    message = ""
    try:
        body = response.json() if hasattr(response, "json") else json.loads(response.read().decode("utf-8"))
        error = body.get("error", body) if isinstance(body, dict) else body
        message = error.get("message", "") if isinstance(error, dict) else str(error)
    except Exception:  # noqa: BLE001
        message = str(getattr(response, "text", ""))
    message = re.sub(r"\s+", " ", message).strip()[:160]
    return f"HTTP {status}: {message}" if message else f"HTTP {status}"


def cloud_engine_status() -> str:
    """Строка для интерфейса: подключена ли облачная модель (без раскрытия ключа)."""
    parts = []
    for prefix in ("OPENAI", "FALLBACK"):
        if os.getenv(f"{prefix}_API_KEY"):
            host = re.sub(r"^https?://([^/]+).*$", r"\1", os.getenv(f"{prefix}_BASE_URL", OPENAI_DEFAULT_BASE))
            parts.append(f"{os.getenv(f'{prefix}_MODEL', 'gpt-4o-mini')} · {host}")
    if not parts:
        return "не подключена (OPENAI_API_KEY не задан)"
    return parts[0] + (f" · запасная: {parts[1]}" if len(parts) > 1 else "")


def _engines() -> List[Tuple[str, Callable[[str], Tuple[str, str]]]]:
    mode = os.getenv("BPMN_AI_MODE", "auto").lower()
    if mode == "emulator":
        return []
    engines: List[Tuple[str, Callable[[str], Tuple[str, str]]]] = [("ollama", _call_ollama)]
    # Облачные модели отвечают за секунды — они первые; Ollama — офлайн-резерв.
    # FALLBACK_* — запасной провайдер: Groq бывает недоступен с отдельных IP (HTTP 403).
    if os.getenv("FALLBACK_API_KEY"):
        engines.insert(0, ("fallback", lambda prompt: _call_openai(prompt, "FALLBACK")))
    if os.getenv("OPENAI_API_KEY"):
        engines.insert(0, ("openai", _call_openai))
    return engines


def generate_bpmn_from_text(regulation_text: str, use_llm: bool = True) -> Tuple[str, Dict[str, Any], str]:
    """Регламент (RU) → (bpmn_xml, audit_data, error).

    Порядок: облачный API (если задан OPENAI_API_KEY) → локальная Ollama (qwen2.5-coder / llama3) →
    встроенный семантический эмулятор. Исключения сети наружу не выходят.
    Ответ модели с ошибками структуры (висящие узлы, ветвление без шлюза) отклоняется;
    модель получает список ошибок и одну попытку исправиться (LLM_MAX_ATTEMPTS).
    """
    started = time.time()
    text = normalize_regulation(regulation_text or "").strip()
    if len(text) < 20:
        return "", {}, "Регламент пуст или слишком короткий: введите не менее одного-двух шагов процесса."

    try:
        header = parse_regulation(text)
        process_name, sla = header.title, header.sla_hours
    except Exception:  # noqa: BLE001
        process_name, sla = "Бизнес-процесс по регламенту", None

    trace: List[str] = []
    rejected: List[Dict[str, Any]] = []  # отклонённые ответы LLM — для разбора в «Технических деталях»
    if use_llm:
        max_attempts = max(1, int(os.getenv("LLM_MAX_ATTEMPTS", "2")))
        retry_limit_s = float(os.getenv("LLM_RETRY_MAX_CALL_S", "90"))
        if not os.getenv("OPENAI_API_KEY"):
            trace.append("облачный API: OPENAI_API_KEY не задан")
        for name, call in _engines():
            prompt = build_prompt(text)
            for attempt in range(1, max_attempts + 1):
                call_started = time.time()
                try:
                    engine_label, raw = call(prompt)
                except Exception as exc:  # noqa: BLE001 — недоступность модели не должна ронять приложение
                    trace.append(f"{name}: недоступен ({_error_reason(exc)})")
                    break
                call_s = time.time() - call_started
                xml, audit, err = execute_generated_code(raw, process_name, sla)
                quality = audit.get("quality", {}) if not err else {}
                if not err and quality.get("ok"):
                    audit["generation"] = {
                        "engine": engine_label,
                        "fallback": False,
                        "attempts": attempt,
                        "trace": trace,
                        "rejected": rejected,
                        "code": _strip_markdown(raw),
                        "elapsed_s": round(time.time() - started, 2),
                    }
                    return xml, audit, ""
                problems = [err] if err else quality["critical"]
                rejected.append({"engine": engine_label, "attempt": attempt, "problems": problems, "code": _strip_markdown(raw)})
                trace.append(
                    f"{engine_label}, попытка {attempt} ({call_s:.0f} с): результат отклонён — "
                    + "; ".join(problems[:5])
                )
                if attempt < max_attempts and call_s > retry_limit_s:
                    trace.append(f"{engine_label}: повтор пропущен — модель отвечала дольше {retry_limit_s:.0f} с")
                    break
                prompt = build_repair_prompt(text, _strip_markdown(raw), problems)

    try:
        code, info = emulate_generation(text)
    except Exception as exc:  # noqa: BLE001
        return "", {}, f"Не удалось разобрать регламент: {type(exc).__name__}: {exc}"
    xml, audit, err = execute_generated_code(code, process_name, sla)
    if err:
        return "", {}, f"Не удалось построить диаграмму: {err}"
    audit["generation"] = {
        "engine": "semantic-emulator",
        "fallback": use_llm,
        "trace": trace or (["LLM отключён"] if not use_llm else ["LLM недоступна — включён встроенный эмулятор"]),
        "rejected": rejected,
        "code": code,
        "elapsed_s": round(time.time() - started, 2),
        "parsed": info,
    }
    return xml, audit, ""
