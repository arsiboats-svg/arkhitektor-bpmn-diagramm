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
    assistant_chat(...)                      -> диалог (аналитика / правка / реверс)
    generate_process_passport(xml, audit, text) -> Markdown «Паспорт процесса»
    inspect_task_details(task_name, task_role, process_context) -> операционная карточка
    build_diagram_catalog(xml, audit, text) -> метаданные узлов для клика по холсту
    build_canvas_copilot(xml, audit, text) -> ответы плавающего ассистента на холсте
    optimize_process_to_be(text, audit) -> целевой регламент To-Be и дельта SLA
    generate_raci_matrix(steps, roles) -> матрица ответственности R/A/C/I
    export_docx_passport(xml, audit, text) -> официальный регламент Microsoft Word
    PROMPT_TEMPLATE / build_prompt(text)

Модуль не падает при недоступности моделей: при любом сбое сети, таймауте или
некорректном коде включается встроенный эмулятор на основе семантического
сопоставления шагов регламента (роли, условия, параллельность, декомпозиция).
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import re
import time
import xml.etree.ElementTree as _ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from bpmn_framework import DEFAULT_HOURS, GATEWAY_KINDS, WORK_KINDS, BPMNDiagramBuilder
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
- DIAGRAM.set_sla(task_id, hours)                           # трудозатраты шага, часы (1 раб. день = 8 ч). ОБЯЗАТЕЛЬНО, если срок есть в регламенте.

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


def _sla_stems(text: str) -> set:
    return {w[:6] for w in re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", (text or "").lower())}


def _sla_title_score(left: str, right: str) -> int:
    a, b = _sla_stems(left), _sla_stems(right)
    if not a or not b:
        return 0
    return len(a & b)


def _sla_looks_default(node: Any) -> bool:
    """True, если трудозатраты не заданы или совпадают с дефолтом вида узла (2 ч для userTask)."""
    if getattr(node, "sla_hours", None) is None:
        return True
    expected = DEFAULT_HOURS.get(getattr(node, "kind", ""), None)
    if expected is None:
        return False
    try:
        return abs(float(node.sla_hours) - float(expected)) < 1e-6
    except (TypeError, ValueError):
        return True


def _enrich_sla_from_regulation(diagram: BPMNDiagramBuilder, regulation_text: str) -> int:
    """Подставляет часы из текста регламента узлам без явного set_sla (или с дефолтом 2 ч)."""
    if not regulation_text or not str(regulation_text).strip():
        return 0
    try:
        parsed = parse_regulation(normalize_regulation(regulation_text))
    except Exception:  # noqa: BLE001
        return 0
    steps = [s for s in parsed.steps if s.hours]
    work = [n for n in diagram.nodes.values() if n.kind in WORK_KINDS]
    if not steps or not work:
        return 0
    applied = 0
    used_nodes: set = set()
    used_steps: set = set()

    def _free(node: Any) -> bool:
        return node.id not in used_nodes and _sla_looks_default(node)

    for idx, step in enumerate(steps):
        title = step.title or ""
        best, score = None, 0
        for node in work:
            if not _free(node):
                continue
            sc = _sla_title_score(title, node.name or "")
            if sc > score:
                best, score = node, sc
        if best is not None and score >= 1:
            best.sla_hours = float(step.hours)
            used_nodes.add(best.id)
            used_steps.add(idx)
            applied += 1

    leftover_nodes = [n for n in work if _free(n)]
    leftover_steps = [s for i, s in enumerate(steps) if i not in used_steps]
    for step, node in zip(leftover_steps, leftover_nodes):
        node.sla_hours = float(step.hours)
        applied += 1
    return applied


def execute_generated_code(
    code_str: str,
    process_name: str = "Бизнес-процесс ПАО «Интер РАО»",
    sla_target_hours: Optional[float] = None,
    regulation_text: str = "",
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

        sla_enriched = _enrich_sla_from_regulation(diagram, regulation_text)
        structure_issues = _structure_issues(diagram)
        diagram.heal_graph()
        xml = diagram.to_bpmn_xml(ROOT_PROCESS_ID, ROOT_START_TASK_ID, ROOT_END_TASK_ID)
        audit = diagram.analyze_bottlenecks()
        audit["quality"] = _quality_report(structure_issues, audit)
        if sla_enriched:
            audit["sla_enriched_nodes"] = sla_enriched
        errors = xsd_errors_xml(xml)  # официальная XSD BPMN 2.0: невалидный файл не отдаём
        if errors:
            return "", {}, "XML не прошёл проверку по XSD BPMN 2.0: " + "; ".join(errors[:3])
        audit["xsd_valid"] = errors is not None
        audit.setdefault("artifacts", [])
        audit.setdefault("it_systems", [])
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
    effort = os.getenv(f"{prefix}_REASONING_EFFORT", "")
    if effort == "none" and "openrouter.ai" in base:
        payload["reasoning"] = {"enabled": False}  # OpenRouter: без «размышлений» qwen3 отвечает в ~2 раза быстрее
    elif effort:
        payload["reasoning_effort"] = effort
    for attempt in range(2):
        try:
            data = _http_json(
                f"{base}/chat/completions",
                payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=float(os.getenv("OPENAI_TIMEOUT", "90")),
            )
            break
        except Exception as exc:  # noqa: BLE001
            wait = _rate_limit_wait(exc)
            if attempt or wait is None:
                raise
            time.sleep(wait)  # бесплатный тариф Groq: 8K токенов/мин — ждём, сколько просит провайдер
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
    (r"эколог\w*|служб\w* экологии|отдел\w* экологии", "Служба экологии"),
    (r"охран\w* труда|специалист\w* по охране труда", "Служба охраны труда"),
    (r"главн\w+ инженер\w*", "Главный инженер"),
    (r"бухгалтер\w*", "Бухгалтерия"),
    (r"служб\w* (?:информационных технологий|ИТ)\b|ИТ-служб\w*|\bИТ-отдел\w*", "Служба ИТ"),
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
    r"(рабоч\w*\s+(?:дн\w*|день)|календарн\w*\s+(?:дн\w*|день)|сут\w*|дн\w*|день|час\w*|ч\b|мин\w*)\s*\)?",
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
    if len(title) > TITLE_MAX:
        cut = title[:TITLE_MAX].rsplit(" ", 1)[0]
        title = cut.rstrip(",;:—–- ") + "…"
    return title


TITLE_MAX = 110  # длиннее — обрезаем по слову; блок задачи растёт по высоте под текст
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


# --------------------------------------------------------------------------- #
# ИТ-ландшафт и документооборот: какие документы и системы фигурируют в регламенте
# --------------------------------------------------------------------------- #
_ENDINGS = r"(?:а|у|ом|е|ы|и|ой|ов|ам|ами|ах)?"
# (regex, каноническое название); более конкретные шаблоны стоят раньше общих и «съедают» свой фрагмент.
ARTIFACT_PATTERNS: List[Tuple[str, str]] = [
    (r"наряд[\s-]*допуск\w*", "Наряд-допуск"),
    (r"дефектн\w+\s+ведомост\w+", "Дефектная ведомость"),
    (r"техническ\w+\s+услови\w+|(?-i:\bТУ\b)", "Технические условия (ТУ)"),
    (r"техническ\w+\s+задани\w+|(?-i:\bТЗ\b)", "Техническое задание (ТЗ)"),
    (r"закупочн\w+\s+документаци\w+", "Закупочная документация"),
    (r"\bакт" + _ENDINGS + r"\s+о(?:б)?\s+[а-яё]+(?:ом|ем|ой|ей|ых)\s+[а-яё]{4,}", "Акт {tail}"),
    (r"\bакт" + _ENDINGS + r"\s+о(?:б)?\s+[а-яё]{4,}", "Акт {tail}"),
    (r"\bакт" + _ENDINGS + r"\s+(?:выполненных\s+работ|при[её]мки(?:-передачи)?|сдачи-при[её]мки|осмотра|допуска)", "Акт {tail}"),
    (r"\bакт" + _ENDINGS + r"\b", "Акт"),
    (r"\bдоговор" + _ENDINGS + r"\b", "Договор"),
    (r"\bзаяв(?:к(?:а|и|е|у|ой|ам|ами|ах)|ок|лени\w+)\b", "Заявка"),
    (r"\bсмет" + _ENDINGS + r"\b", "Смета"),
    (r"\bсч[её]т" + _ENDINGS + r"\b", "Счёт"),
    (r"\bпротокол" + _ENDINGS + r"\b", "Протокол"),
    (r"\bприказ" + _ENDINGS + r"\b|\bраспоряжени\w+", "Приказ / распоряжение"),
    (r"экспертн\w+\s+заключени\w+|заключени\w+\s+экспертизы", "Экспертное заключение"),
    (r"\bизвещени\w+", "Извещение о закупке"),
    (r"график\w*\s+ремонт\w*", "График ремонтов"),
    (r"\bуведомлени\w+", "Уведомление"),
    (r"пакет\w*\s+документ\w*", "Пакет документов"),
]
SYSTEM_PATTERNS: List[Tuple[str, str]] = [
    (r"(?-i:\bАСУ\s*ТП\b)|(?-i:\bSCADA\b)|\bскад[аеу]\b|(?-i:\bАСДУ\b)|(?-i:\bОИК\b)|телемеханик\w*", "АСУ ТП / SCADA"),
    (r"оперативн\w+\s+журнал\w*", "Оперативный журнал"),
    (r"\bв\s+журнал\w*", "Журнал"),
    (r"(?-i:\bCRM\b)", "CRM"),
    (r"электронн\w+\s+площадк\w+|(?-i:\bЭТП\b)", "Электронная площадка"),
    (r"биллинг\w*", "Биллинг"),
    (r"(?-i:\b1[СC]\b)|(?-i:\bSAP\b)|(?-i:\bERP\b)", "1С / SAP / ERP"),
    (r"(?-i:\bСЭД\b)|электронн\w+\s+документооборот\w*|систем\w+\s+электронного\s+документооборота", "СЭД (электронный документооборот)"),
    (r"личн\w+\s+кабинет\w*", "Личный кабинет"),
    (r"информационн\w+\s+систем\w+|автоматизированн\w+\s+систем\w+|(?-i:\bА?ИС\b)", "Информационная система (ИС / АС)"),
]
_ARTIFACT_RE = [(re.compile(p, re.I), n) for p, n in ARTIFACT_PATTERNS]
_SYSTEM_RE_LIST = [(re.compile(p, re.I), n) for p, n in SYSTEM_PATTERNS]


def _find_named(text: str, patterns: List[Tuple["re.Pattern[str]", str]]) -> List[str]:
    """Канонические названия всех найденных объектов (в порядке появления, без повторов)."""
    taken: List[Tuple[int, int]] = []
    found: List[Tuple[int, str]] = []
    for regex, name in patterns:
        for m in regex.finditer(text):
            span = m.span()
            if any(span[0] < e and s < span[1] for s, e in taken):  # уже учтено более конкретным шаблоном
                continue
            taken.append(span)
            if "{tail}" in name:
                head = re.match(r"\bакт\w*\s+", m.group(0), re.I)
                tail = m.group(0)[head.end():] if head else ""
                label = name.replace("{tail}", tail.strip().lower())
            else:
                label = name
            found.append((span[0], label))
    found.sort()
    result: List[str] = []
    for _, label in found:
        if label not in result:
            result.append(label)
    return result


def extract_artifacts(text: str) -> List[str]:
    """Документы и артефакты процесса: наряд-допуск, дефектная ведомость, ТУ, договор, заявка, акт…"""
    return _find_named(text, _ARTIFACT_RE)


def extract_it_systems(text: str) -> List[str]:
    """ИТ-системы: АСУ ТП / SCADA, оперативный журнал, CRM, электронная площадка, биллинг, 1С / SAP…"""
    return _find_named(text, _SYSTEM_RE_LIST)


def aggregate_landscape(steps: List["Step"], attr: str) -> List[Dict[str, Any]]:
    """Сводит найденное по шагам в список {name, mentions, steps, roles}, самые упоминаемые — первыми."""
    index: Dict[str, Dict[str, Any]] = {}
    for step in steps:
        for name in getattr(step, attr):
            item = index.setdefault(name, {"name": name, "mentions": 0, "steps": [], "roles": []})
            item["mentions"] += 1
            if step.num not in item["steps"]:
                item["steps"].append(step.num)
            if step.role and step.role not in item["roles"]:
                item["roles"].append(step.role)
    return sorted(index.values(), key=lambda it: (-it["mentions"], it["steps"][0] if it["steps"] else 0))


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
    artifacts: List[str] = field(default_factory=list)  # документы шага: наряд-допуск, акт, договор…
    systems: List[str] = field(default_factory=list)  # ИТ-системы шага: АСУ ТП, CRM, 1С…


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
    artifacts: List[Dict[str, Any]] = field(default_factory=list)  # сводка по документам процесса
    it_systems: List[Dict[str, Any]] = field(default_factory=list)  # сводка по ИТ-системам процесса


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
        step.artifacts = extract_artifacts(body)
        step.systems = extract_it_systems(body)
        parsed.steps.append(step)
        if step.role not in parsed.roles:
            parsed.roles.append(step.role)
    parsed.artifacts = aggregate_landscape(parsed.steps, "artifacts")
    parsed.it_systems = aggregate_landscape(parsed.steps, "systems")
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
def _rate_limit_wait(exc: Exception) -> Optional[float]:
    """HTTP 429 → сколько секунд подождать (Retry-After или «try again in 7.5s»), не больше 20; иначе None."""
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 429:
        return None
    header = (getattr(response, "headers", None) or {}).get("retry-after")
    try:
        seconds = float(header) if header else None
    except ValueError:
        seconds = None
    if seconds is None:
        m = re.search(r"try again in ([\d.]+)s", getattr(response, "text", "") or "")
        seconds = float(m.group(1)) if m else 6.0
    return min(20.0, seconds + 0.5)


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

    artifacts: List[Dict[str, Any]] = []
    it_systems: List[Dict[str, Any]] = []
    try:
        header = parse_regulation(text)
        process_name, sla = header.title, header.sla_hours
        artifacts, it_systems = header.artifacts, header.it_systems
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
                xml, audit, err = execute_generated_code(raw, process_name, sla, regulation_text=text)
                quality = audit.get("quality", {}) if not err else {}
                if not err and quality.get("ok"):
                    audit["artifacts"], audit["it_systems"] = artifacts, it_systems
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
                if err and "не распознан" in err:
                    # Модель сама не нашла процесс в тексте: «исправлять» — значит заставить её выдумывать.
                    trace.append(f"{engine_label}: регламент не распознан — повтор не делаем")
                    return "", {}, "Регламент не распознан: в тексте не найдено шагов процесса и исполнителей."
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
    xml, audit, err = execute_generated_code(code, process_name, sla, regulation_text=text)
    if err:
        return "", {}, f"Не удалось построить диаграмму: {err}"
    audit["artifacts"], audit["it_systems"] = artifacts, it_systems
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


# =========================================================================== #
# AI-АССИСТЕНТ БИЗНЕС-АРХИТЕКТОРА: диалог над активным процессом
# =========================================================================== #
#   1) аналитика и объяснение      — «В чём причина срыва SLA?», «Как разгрузить диспетчера?»
#   2) правка процесса на лету     — «Добавь согласование с экологами после шага 3»
#                                     → правка текста регламента → перестроение через execute_generated_code
#   3) реверс-генерация            — «Составь инструкцию для роли Диспетчер» (по схеме BPMN)
# Цепочка: облачная модель (OPENAI_* / FALLBACK_*) → локальная Ollama → локальные эвристики (без сети).
# --------------------------------------------------------------------------- #
_NS_BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_NS_DI = "http://www.omg.org/spec/BPMN/20100524/DI"
_NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
_TASK_TAGS = {"task", "userTask", "scriptTask", "serviceTask", "manualTask", "sendTask", "receiveTask", "businessRuleTask"}
_GATE_TAGS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway", "complexGateway"}

CHAT_SYSTEM = """Ты — «AI-Ассистент Бизнес-Архитектора» ПАО «Интер РАО». Собеседник — эксперт Дирекции бизнес-архитектуры.
Правила:
- Отвечай по-русски, по делу, не более 12 строк, Markdown. Опирайся ТОЛЬКО на данные процесса ниже; числа и названия не выдумывай.
- Причины и рекомендации подкрепляй конкретными шагами, ролями и цифрами из данных (срок, доля нагрузки, циклы возврата).
- Если в данных есть блок «СРАВНЕНИЕ AS-IS / TO-BE», на вопросы «сравни», «as-is / to-be», «до и после»
  отвечай цифрами: дельта SLA (часы и %), петли возврата до/после, замена журналов на scriptTask, Quality Score.
- На «как сократили / за счёт чего / объясни подробнее» дай декомпозицию экономии To-Be:
  1) петли доработки (часы циклов до/после), 2) параллелизация с номерами шагов, 3) автоматизация journal→scriptTask.
- На «почему Quality Score / упал балл нотации»: если 100% — стандарты соблюдены полностью; если был перепад —
  объясни, что добавлены AND-шлюзы и входной контроль, а правило «глагол + объект» сохранено. Это НЕ ответ про «метро Токио».
- Не начинай каждый ответ одной и той же заглушкой «N шагов / M узлов». Отвечай на заданный вопрос.
- Если предлагаешь изменить процесс, заверши ответ ГОТОВОЙ командой в кавычках «…», которую пользователь может отправить
  в чат, например: «Сделай шаги 4 и 5 параллельными» или «Добавь согласование с экологами после шага 3».
- Если данных для ответа нет — так и скажи.

ДАННЫЕ АКТИВНОГО ПРОЦЕССА:
"""

EDIT_SYSTEM = """Ты — редактор регламентов ПАО «Интер РАО». Тебе дан текущий регламент и команда пользователя.
Формат регламента: заголовок «Регламент: …», строка «Целевой срок …: …», затем нумерованные шаги:
«N. Роль глагол … (срок). Если условие — перейти к п.K, иначе «метка» — вернуть на п.M.»; «Параллельно: …» — шаг,
выполняемый одновременно с предыдущим.
Верни ТОЛЬКО полный обновлённый регламент в том же формате: перенумеруй шаги подряд и поправь ссылки «п.N»;
не меняй то, что пользователь не просил; каждый шаг начинается с названия роли (подразделения); без пояснений и markdown."""

POLISH_SYSTEM = """Оформи приведённые факты как официальную должностную инструкцию на русском языке.
Используй ТОЛЬКО факты из входных данных: ничего не добавляй и не выдумывай (сроки, системы, роли, документы).
Структура: 1. Общие положения, 2. Входные данные и триггеры, 3. Порядок действий (нумерованный список, сохрани все шаги
и условия ветвления), 4. Передача результата, 5. Используемые системы и документы, 6. Контроль сроков и риски. Markdown."""


# --------------------------- вызовы LLM в диалоговом режиме --------------------------- #
def _chat_openai(messages: List[Dict[str, str]], prefix: str) -> Tuple[str, str]:
    key = os.getenv(f"{prefix}_API_KEY")
    if not key:
        raise RuntimeError(f"{prefix}_API_KEY не задан")
    base = os.getenv(f"{prefix}_BASE_URL", OPENAI_DEFAULT_BASE).rstrip("/")
    model = os.getenv(f"{prefix}_MODEL", "gpt-4o-mini")
    payload: Dict[str, Any] = {"model": model, "temperature": 0.2, "messages": messages}
    if os.getenv(f"{prefix}_REASONING_EFFORT"):
        payload["reasoning_effort"] = os.getenv(f"{prefix}_REASONING_EFFORT")
    data = _http_json(
        f"{base}/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {key}"},
        timeout=float(os.getenv("CHAT_TIMEOUT", "60")),
    )
    content = str(data["choices"][0]["message"].get("content") or "").strip()
    if not content:
        raise RuntimeError("пустой ответ модели")
    return ("openai" if prefix == "OPENAI" else prefix.lower()) + f":{model}", content


def _chat_ollama(messages: List[Dict[str, str]]) -> Tuple[str, str]:
    model = _pick_ollama_model(_ollama_models())
    if not model:
        raise RuntimeError("в Ollama нет моделей qwen2.5-coder / llama3")
    data = _http_json(
        f"{_ollama_base()}/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.2, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))},
        },
        timeout=float(os.getenv("OLLAMA_CHAT_TIMEOUT", "120")),
    )
    content = str((data.get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("пустой ответ модели")
    return f"ollama:{model}", content


def _chat_llm(messages: List[Dict[str, str]], trace: List[str]) -> Optional[Tuple[str, str]]:
    """Первая доступная модель отвечает; сбои пишутся в trace, наружу исключения не выходят."""
    if os.getenv("BPMN_AI_MODE", "auto").lower() == "emulator":
        return None
    attempts: List[Tuple[str, Callable[[], Tuple[str, str]]]] = []
    if os.getenv("OPENAI_API_KEY"):
        attempts.append(("openai", lambda: _chat_openai(messages, "OPENAI")))
    if os.getenv("FALLBACK_API_KEY"):
        attempts.append(("fallback", lambda: _chat_openai(messages, "FALLBACK")))
    attempts.append(("ollama", lambda: _chat_ollama(messages)))
    for name, call in attempts:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — недоступность модели не должна ронять диалог
            trace.append(f"{name}: {_error_reason(exc)}")
    return None


# --------------------------- контекст активного процесса --------------------------- #
def _fh(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} мин"
    if hours < 48:
        return f"{hours:.1f} ч".replace(".0 ", " ")
    return f"{hours:.0f} ч (≈ {hours / 8:.0f} раб. дн.)"


def build_process_context(
    current_text: str,
    current_xml: str,
    current_audit: Dict[str, Any],
    tobe_delta: Optional[dict] = None,
) -> Dict[str, Any]:
    """Сводка активного процесса: шаги, роли, SLA, критический путь, bus-factor, циклы, ИТ-ландшафт, To-Be."""
    audit = current_audit or {}
    title, sla_target, steps = "Бизнес-процесс", None, []
    try:
        parsed = parse_regulation(normalize_regulation(current_text or ""))
        title, sla_target = parsed.title, parsed.sla_hours
        for st in parsed.steps:
            decision = None
            if st.decision:
                decision = {"yes": st.decision.yes_label, "no": st.decision.no_label, "back": st.decision.no_back}
            steps.append(
                {
                    "num": st.num, "role": st.role, "title": st.title or "(решение)", "hours": st.hours,
                    "parallel": st.parallel, "stage": st.stage, "decision": decision,
                    "systems": st.systems, "artifacts": st.artifacts,
                }
            )
    except Exception:  # noqa: BLE001
        pass
    sla = audit.get("sla") or {}
    if sla_target is None:
        sla_target = sla.get("target_hours")
    ctx: Dict[str, Any] = {
        "title": title,
        "sla_target_hours": sla_target,
        "steps": steps,
        "lane_load": audit.get("lane_load") or [],
        "bus_factor": audit.get("bus_factor") or {},
        "sla": sla,
        "critical_path": audit.get("critical_path") or [],
        "rework_loops": audit.get("rework_loops") or [],
        "sla_risks": audit.get("sla_risks") or [],
        "it_systems": audit.get("it_systems") or [],
        "artifacts": audit.get("artifacts") or [],
        "recommendations": audit.get("recommendations") or [],
        "stats": audit.get("stats") or {},
        "methodology": audit.get("methodology") or {},
        "xml_available": bool(current_xml),
        "tobe_ready": False,
        "sla_hours_as_is": None,
        "sla_hours_to_be": None,
        "delta_sla_percent": None,
        "rework_loops_as_is": None,
        "rework_loops_to_be": None,
        "quality_score_as_is": None,
        "quality_score_to_be": None,
        "tobe_actions": [],
    }
    _attach_tobe_compare(ctx, audit, tobe_delta)
    return ctx


def _attach_tobe_compare(ctx: Dict[str, Any], audit: Dict[str, Any], tobe_delta: Optional[dict]) -> None:
    """Пишет в контекст sla_hours_as_is / sla_hours_to_be и соседние поля, если To-Be уже посчитан."""
    d = tobe_delta or (audit or {}).get("tobe_compare") or {}
    if not isinstance(d, dict) or not d:
        return
    if d.get("sla_after_hours") is None and d.get("sla_hours_to_be") is None:
        return
    sla = (audit or {}).get("sla") or {}
    meth = (audit or {}).get("methodology") or {}
    as_is = d.get("sla_hours_as_is")
    if as_is is None:
        as_is = d.get("sla_before_hours")
    if as_is is None:
        as_is = sla.get("with_rework_hours") or sla.get("critical_path_hours") or 0
    to_be = d.get("sla_hours_to_be")
    if to_be is None:
        to_be = d.get("sla_after_hours") or 0
    pct = d.get("delta_sla_percent")
    if pct is None:
        pct = d.get("sla_saved_pct") or 0
    q_as = d.get("quality_score_as_is")
    if q_as is None:
        q_as = d.get("quality_before")
    if q_as is None:
        q_as = int(meth.get("score") or 0)
    q_to = d.get("quality_score_to_be")
    if q_to is None:
        q_to = d.get("quality_after")
    if q_to is None:
        q_to = q_as
    rw_as = d.get("rework_loops_as_is")
    if rw_as is None:
        rw_as = d.get("rework_before")
    if rw_as is None:
        rw_as = len((audit or {}).get("rework_loops") or [])
    rw_to = d.get("rework_loops_to_be")
    if rw_to is None:
        rw_to = d.get("rework_after")
    if rw_to is None:
        rw_to = rw_as
    ctx["tobe_ready"] = True
    ctx["sla_hours_as_is"] = round(float(as_is or 0), 3)
    ctx["sla_hours_to_be"] = round(float(to_be or 0), 3)
    ctx["delta_sla_percent"] = round(float(pct or 0), 1)
    ctx["rework_loops_as_is"] = int(rw_as or 0)
    ctx["rework_loops_to_be"] = int(rw_to or 0)
    ctx["quality_score_as_is"] = int(q_as or 0)
    ctx["quality_score_to_be"] = int(q_to or 0)
    ctx["tobe_actions"] = [a for a in (d.get("actions") or []) if isinstance(a, dict)]
    ctx["sla_saved_hours"] = round(float(d.get("sla_saved_hours") or max(0.0, float(as_is or 0) - float(to_be or 0))), 3)
    ctx["rework_hours_as_is"] = round(float(d.get("rework_hours_before") or 0), 3)
    ctx["rework_hours_to_be"] = round(float(d.get("rework_hours_after") or 0), 3)


def format_context_for_prompt(ctx: Dict[str, Any], max_steps: int = 60) -> str:
    lines = [f"Процесс: {ctx['title']}"]
    if ctx.get("sla_target_hours"):
        lines.append(f"Целевой срок (SLA): {_fh(float(ctx['sla_target_hours']))}")
    sla = ctx.get("sla") or {}
    if sla:
        lines.append(
            f"Критический путь: {_fh(float(sla.get('critical_path_hours', 0)))}; "
            f"с худшим возвратом: {_fh(float(sla.get('with_rework_hours', 0)))}; "
            f"срыв SLA: {'да' if sla.get('breach') else 'нет'}"
        )
    lines.append("Шаги:")
    for s in ctx["steps"][:max_steps]:
        extra = []
        if s["hours"]:
            extra.append(_fh(float(s["hours"])))
        if s["parallel"]:
            extra.append("параллельно с предыдущим")
        if s["decision"]:
            extra.append(f"ветвление: «{s['decision']['yes']}» / «{s['decision']['no']}»")
        lines.append(f"  {s['num']}. [{s['role']}] {s['title']}" + (f" ({'; '.join(extra)})" if extra else ""))
    if len(ctx["steps"]) > max_steps:
        lines.append(f"  … ещё {len(ctx['steps']) - max_steps} шагов")
    if ctx["lane_load"]:
        lines.append("Нагрузка по ролям: " + "; ".join(
            f"{r['role']} {float(r['share']):.0%} ({int(r['tasks'])} шаг.)" for r in ctx["lane_load"]))
    bus = ctx.get("bus_factor") or {}
    if bus:
        lines.append(f"Bus-factor: макс. доля {float(bus.get('max_share', 0)):.0%} у роли «{bus.get('top_role')}» "
                     f"(порог {float(bus.get('threshold', 0.45)):.0%}, статус {bus.get('status')})")
    if ctx["critical_path"]:
        lines.append("Критический путь: " + " → ".join(
            f"{c['name']} [{c['role']}, {_fh(float(c['hours']))}]" for c in ctx["critical_path"][:12]))
    for loop in ctx["rework_loops"]:
        lines.append(f"Цикл возврата «{loop['label']}»: {loop['from']} → {loop['to']} ({loop['lane']}), "
                     f"стоимость цикла {_fh(float(loop['cycle_hours']))}")
    if ctx["it_systems"]:
        lines.append("ИТ-системы: " + ", ".join(f"{i['name']} (шаги {i['steps']})" for i in ctx["it_systems"]))
    if ctx["artifacts"]:
        lines.append("Документы: " + ", ".join(f"{i['name']} (шаги {i['steps']})" for i in ctx["artifacts"]))
    for risk in ctx["sla_risks"][:6]:
        lines.append(f"Риск [{risk.get('severity')}]: {risk.get('message')}")
    meth = ctx.get("methodology") or {}
    if meth.get("score") is not None:
        lines.append(f"Quality Score (линтер нотации): {int(meth.get('score') or 0)}%")
        for chk in (meth.get("checks") or [])[:4]:
            mark = "ok" if chk.get("passed") else "fail"
            lines.append(f"  [{mark}] {chk.get('title')}: {chk.get('detail')}")
    if ctx.get("tobe_ready"):
        lines += [
            "СРАВНЕНИЕ AS-IS / TO-BE:",
            f"  sla_hours_as_is: {ctx.get('sla_hours_as_is')}",
            f"  sla_hours_to_be: {ctx.get('sla_hours_to_be')}",
            f"  delta_sla_percent: {ctx.get('delta_sla_percent')}",
            f"  rework_loops_as_is: {ctx.get('rework_loops_as_is')}",
            f"  rework_loops_to_be: {ctx.get('rework_loops_to_be')}",
            f"  quality_score_as_is: {ctx.get('quality_score_as_is')}",
            f"  quality_score_to_be: {ctx.get('quality_score_to_be')}",
        ]
        for act in (ctx.get("tobe_actions") or [])[:6]:
            lines.append(f"  действие To-Be [{act.get('kind')}]: {act.get('detail')}")
        lines.append(f"  rework_hours_as_is: {ctx.get('rework_hours_as_is')}")
        lines.append(f"  rework_hours_to_be: {ctx.get('rework_hours_to_be')}")
        lines.append(f"  sla_saved_hours: {ctx.get('sla_saved_hours')}")
    return "\n".join(lines)


# --------------------------- классификация намерения --------------------------- #
_EDIT_VERB_RE = re.compile(
    r"\b(добав\w+|вставь\w*|вставить|включи\w*|дополни\w*|удал\w+|убер\w+|убрать|исключ\w+|сдела\w+|измени\w*|изменить|"
    r"замени\w*|заменить|перенес\w+|перемест\w+|поменя\w+|сократ\w+|увелич\w+|постав\w+|установи\w*|передай\w*|назначь\w*)\b",
    re.I,
)
_QUESTION_START_RE = re.compile(r"^\s*(?:как|почему|что|зачем|можно ли|стоит ли|какие|какой|какая|сколько|где|когда|кто|в чем|в чём|есть ли)\b", re.I)
_INSTRUCTION_RE = re.compile(
    r"инструкци|памятк|регламент для исполнител|для исполнител|должностн|чем занимается|что делает|опиши работу|обязанност", re.I)
_NEXT_STEP_RE = re.compile(r"следующ\w+ шаг|что дальше|чего не хватает|предложи\w* шаг|что добавить|каких шагов", re.I)


def classify_intent(message: str) -> str:
    """'instruction' | 'edit' | 'next_step' | 'analysis'."""
    msg = (message or "").strip()
    if _INSTRUCTION_RE.search(msg) and not re.match(r"^\s*(?:удал|убер)", msg, re.I):
        return "instruction"
    if _NEXT_STEP_RE.search(msg):
        return "next_step"
    if _EDIT_VERB_RE.search(msg) and not msg.endswith("?") and not _QUESTION_START_RE.match(msg):
        return "edit"
    return "analysis"


def _roles_of(ctx: Dict[str, Any]) -> List[str]:
    roles: List[str] = [r["role"] for r in ctx["lane_load"]]
    for s in ctx["steps"]:
        if s["role"] not in roles:
            roles.append(s["role"])
    return roles


def _roles_in_message(message: str, roles: List[str]) -> List[str]:
    """Роли процесса, упомянутые в сообщении (в порядке упоминания)."""
    low = message.lower()
    hits: List[Tuple[int, str]] = []
    canonical = _find_role(message)[0]
    for role in roles:
        pos = -1
        if canonical == role:
            pos = 0
        else:
            stems = [w[:6].lower() for w in re.findall(r"[А-Яа-яЁё]{5,}", role)]
            found = [low.find(st) for st in stems if st in low]
            if found:
                pos = min(found)
        if pos >= 0:
            hits.append((pos, role))
    return [r for _, r in sorted(hits)]


# --------------------------- режим 1: аналитика и объяснение --------------------------- #
def _analysis_sla(ctx: Dict[str, Any]) -> str:
    sla, target = ctx["sla"], ctx.get("sla_target_hours")
    if not sla:
        return "Данных аудита пока нет: сгенерируйте диаграмму."
    base, total = float(sla["critical_path_hours"]), float(sla["with_rework_hours"])
    out = [f"**Критический путь — {_fh(base)}**" + (f" при цели {_fh(float(target))}" if target else "") + "."]
    if sla.get("breach"):
        if target and base > float(target):
            out.append(f"🔴 Срыв SLA заложен в самом процессе: даже без возвратов путь длиннее цели на {_fh(base - float(target))}.")
        else:
            out.append(f"🔴 Без возвратов процесс укладывается в срок, но **с худшим циклом возврата** — {_fh(total)}: срыв из-за доработок.")
    elif float(sla.get("rework_share", 0)) > 0.25:
        out.append(f"🟠 В срок укладываемся, но худший возврат добавляет {float(sla['rework_share']):.0%} ({_fh(total)} с возвратом) — запас съедается.")
    else:
        out.append("🟢 Процесс укладывается в срок, риски умеренные.")
    heavy = sorted(ctx["critical_path"], key=lambda c: -float(c["hours"]))[:3]
    if heavy and base > 0:
        out.append("Что занимает больше всего времени на критическом пути:")
        out += [f"- «{c['name']}» ({c['role']}) — {_fh(float(c['hours']))}, {float(c['hours']) / base:.0%} пути" for c in heavy]
    loops = sorted(ctx["rework_loops"], key=lambda l: -float(l["cycle_hours"]))
    if loops:
        l0 = loops[0]
        out.append(f"Самый дорогой возврат — «{l0['label']}» ({l0['from']} → {l0['to']}): {_fh(float(l0['cycle_hours']))}.")
    out.append("Что делать: сократить самые долгие шаги пути, вынести независимые шаги в параллель, ужесточить входной контроль, "
               "чтобы реже возвращать на доработку.")
    if len(ctx["steps"]) >= 5:
        out.append("Например: «Сделай шаги 4 и 5 параллельными» (если они независимы).")
    return "\n".join(out)


def _analysis_load(ctx: Dict[str, Any], roles: List[str]) -> str:
    load = {r["role"]: r for r in ctx["lane_load"]}
    if not load:
        return "Данных о нагрузке пока нет."
    role = roles[0] if roles and roles[0] in load else max(load, key=lambda r: float(load[r]["share"]))
    info, bus = load[role], ctx["bus_factor"]
    thr = float(bus.get("threshold", 0.45))
    mine = [s for s in ctx["steps"] if s["role"] == role]
    out = [f"**{role}: {float(info['share']):.0%} шагов процесса ({int(info['tasks'])} задач)**, порог bus-factor — {thr:.0%}."]
    if float(info["share"]) > thr:
        out.append("🔴 Процесс держится на этой роли: отсутствие одного сотрудника остановит значительную часть шагов.")
    else:
        out.append("🟢 Нагрузка в пределах нормы.")
    if mine:
        out.append("Шаги роли:")
        out += [f"- {s['num']}. {s['title']}" + (f" ({_fh(float(s['hours']))})" if s["hours"] else "") for s in mine[:8]]
    routine = [s for s in mine if re.search(r"регистр|фиксир|вносит|вести|журнал|уведом|направ|передач|заказ", s["title"], re.I)]
    out.append("Как разгрузить:")
    if routine:
        out.append(f"- автоматизировать или передать рутину: «{routine[0]['title']}» (шаг {routine[0]['num']});")
    out.append("- назначить заместителя/резерв на ключевые шаги и описать их в инструкции (реверс-генерация: «Составь инструкцию для роли "
               f"{role}»);")
    others = sorted((r for r in load if r != role), key=lambda r: float(load[r]["share"]))
    if others:
        out.append(f"- перераспределить часть проверок на роль с минимальной загрузкой — «{others[0]}» ({float(load[others[0]]['share']):.0%}).")
    return "\n".join(out)


def _analysis_speedup(ctx: Dict[str, Any]) -> str:
    out = ["**Как ускорить процесс — по убыванию эффекта:**"]
    sla = ctx["sla"]
    heavy = sorted(ctx["critical_path"], key=lambda c: -float(c["hours"]))[:2]
    for i, c in enumerate(heavy, 1):
        out.append(f"{i}. Сократить/автоматизировать «{c['name']}» ({c['role']}, {_fh(float(c['hours']))}) — это на критическом пути.")
    loops = sorted(ctx["rework_loops"], key=lambda l: -float(l["cycle_hours"]))
    if loops:
        out.append(f"{len(heavy) + 1}. Снизить долю возвратов «{loops[0]['label']}»: чек-лист на входе шага «{loops[0]['from']}» "
                   f"(цикл стоит {_fh(float(loops[0]['cycle_hours']))}).")
    pairs = []
    steps = ctx["steps"]
    for a, b in zip(steps, steps[1:]):
        if a["role"] != b["role"] and not a["decision"] and not b["parallel"] and not b["decision"]:
            pairs.append((a, b))
    if pairs:
        a, b = max(pairs, key=lambda p: (p[1]["hours"] or 0))
        out.append(f"{len(heavy) + (1 if loops else 0) + 1}. Проверить независимость шагов {a['num']} и {b['num']} "
                   f"(«{a['role']}» и «{b['role']}») — возможна параллель: «Сделай шаги {a['num']} и {b['num']} параллельными».")
    if sla:
        out.append(f"\nСейчас критический путь — {_fh(float(sla['critical_path_hours']))}.")
    return "\n".join(out)


def _analysis_loops(ctx: Dict[str, Any]) -> str:
    loops = ctx["rework_loops"]
    if not loops:
        return "Циклов возврата на доработку в модели нет — процесс линейный."
    out = [f"**Циклов возврата: {len(loops)}**"]
    out += [f"- «{l['label']}»: {l['from']} → {l['to']} ({l['lane']}), стоимость цикла {_fh(float(l['cycle_hours']))}" for l in loops]
    out.append("Риск: каждый возврат повторяет участок пути. Рекомендация — входной контроль и лимит возвратов с эскалацией руководителю.")
    return "\n".join(out)


def _analysis_landscape(ctx: Dict[str, Any]) -> str:
    sys_, docs = ctx["it_systems"], ctx["artifacts"]
    out = ["**ИТ-ландшафт процесса**"]
    out.append("💻 Системы: " + (", ".join(f"{i['name']} (шаги {', '.join(map(str, i['steps']))})" for i in sys_) or "не упомянуты"))
    out.append("📄 Документы: " + (", ".join(f"{i['name']} (шаги {', '.join(map(str, i['steps']))})" for i in docs) or "не обнаружены"))
    if not sys_:
        out.append("⚠️ Ни один шаг не привязан к информационной системе — риск ручной обработки и потери данных.")
    elif len(sys_) < 2 and len(ctx["steps"]) > 8:
        out.append("Для процесса такого размера систем мало: проверьте, нет ли шагов, которые ведутся «на бумаге».")
    return "\n".join(out)


def _analysis_summary(ctx: Dict[str, Any]) -> str:
    st_ = ctx["stats"]
    sla = ctx["sla"]
    out = [f"**{ctx['title']}**: {len(ctx['steps'])} шагов, {len(_roles_of(ctx))} ролей"
           + (f", подпроцессов: {st_.get('subprocesses')}" if st_ else "") + "."]
    if sla:
        out.append(f"Критический путь {_fh(float(sla['critical_path_hours']))}, срыв SLA: {'да' if sla.get('breach') else 'нет'}.")
    if ctx["bus_factor"]:
        b = ctx["bus_factor"]
        out.append(f"Самая загруженная роль — «{b.get('top_role')}» ({float(b.get('max_share', 0)):.0%}).")
    out.append("Спросите: «В чём причина срыва SLA?», «Как разгрузить диспетчера?», «Как ускорить процесс?»; "
               "попросите изменить процесс («Добавь согласование с экологами после шага 3») "
               "или составить инструкцию для роли.")
    return "\n".join(out)


def _analysis_tobe(ctx: Dict[str, Any]) -> str:
    if not ctx.get("tobe_ready"):
        sla = ctx.get("sla") or {}
        loops = ctx.get("rework_loops") or []
        meth = ctx.get("methodology") or {}
        return (
            "**As-Is пока без посчитанного To-Be.** Откройте вкладку «Оптимизация As-Is → To-Be» — "
            "оптимизатор параллелит независимые роли, снимает петли возврата (Zero-Rework) и переводит журналы в scriptTask.\n"
            f"Сейчас: путь {_fh(float(sla.get('with_rework_hours') or sla.get('critical_path_hours') or 0))}, "
            f"петель возврата {len(loops)}, Quality Score {int(meth.get('score') or 0)}%."
        )
    as_h = float(ctx.get("sla_hours_as_is") or 0)
    to_h = float(ctx.get("sla_hours_to_be") or 0)
    pct = float(ctx.get("delta_sla_percent") or 0)
    saved = float(ctx.get("sla_saved_hours") or max(0.0, as_h - to_h))
    rb, ra = int(ctx.get("rework_loops_as_is") or 0), int(ctx.get("rework_loops_to_be") or 0)
    qb, qa = int(ctx.get("quality_score_as_is") or 0), int(ctx.get("quality_score_to_be") or 0)
    stats = ctx.get("stats") or {}
    out = [
        f"**As-Is → To-Be для «{ctx.get('title')}».**",
        f"SLA (с учётом возвратов): **{_fh(as_h)} → {_fh(to_h)}** (экономия {_fh(saved)}, **−{pct:g}%**).",
        f"Петли возврата: **{rb} → {ra}** (ликвидировано {max(0, rb - ra)}).",
        f"Quality Score нотации: **{qb}% → {qa}%**.",
    ]
    kinds = {str(a.get("kind")) for a in (ctx.get("tobe_actions") or [])}
    if "parallel" in kinds:
        out.append("Параллелизация: независимые шаги разных ролей идут одновременно, а не цепочкой.")
    if "zero_rework" in kinds:
        out.append("Zero-Rework: перед согласованиями входной контроль, возвраты заменены эскалацией.")
    if "automation" in kinds:
        out.append("Автоматизация: фиксация в журналах/реестрах переведена в scriptTask, а не ручной userTask.")
    for act in (ctx.get("tobe_actions") or [])[:4]:
        if act.get("detail"):
            out.append(f"- {act['detail']}")
    subs = int(stats.get("subprocesses") or 0)
    out.append(
        f"Читаемость: {'подпроцессы уже режут «метро Токио»' if subs else 'длинные цепочки лучше упаковать в subprocess'}; "
        "снятие возвратных стрелок убирает пересечения как на карте метро."
    )
    return "\n".join(out)


def _analysis_readability(ctx: Dict[str, Any]) -> str:
    meth = ctx.get("methodology") or {}
    stats = ctx.get("stats") or {}
    score = int(meth.get("score") or 0)
    subs = int(stats.get("subprocesses") or 0)
    layers = int(stats.get("critical_path_layers") or 0)
    out = [f"**Читаемость BPMN «{ctx.get('title')}».** Quality Score: **{score}%**."]
    if subs:
        out.append(f"Декомпозиция: {subs} подпроцесс(а) — это как раз против «метро Токио».")
    else:
        out.append("Подпроцессов нет: монотонные цепочки одной роли лучше свернуть в subprocess.")
    if layers:
        out.append(f"Слоёв на критическом пути: {layers}.")
    for chk in meth.get("checks") or []:
        mark = "✓" if chk.get("passed") else "✗"
        out.append(f"- {mark} {chk.get('title')}: {chk.get('detail')}")
    if ctx.get("tobe_ready"):
        out.append(
            f"To-Be: Quality {ctx.get('quality_score_as_is')}% → {ctx.get('quality_score_to_be')}%, "
            f"петли {ctx.get('rework_loops_as_is')} → {ctx.get('rework_loops_to_be')} — меньше возвратных стрелок, схема читается линейнее."
        )
    return "\n".join(out)


def _analysis_open(message: str, ctx: Dict[str, Any]) -> str:
    """Развёрнутый ответ по архитектуре процесса — не статичная заглушка с числом шагов."""
    q = (message or "").strip()
    sla = ctx.get("sla") or {}
    bus = ctx.get("bus_factor") or {}
    meth = ctx.get("methodology") or {}
    recs: List[str] = []
    for item in ctx.get("recommendations") or []:
        recs.append(item if isinstance(item, str) else str((item or {}).get("message") or ""))
    for item in ctx.get("sla_risks") or []:
        if isinstance(item, dict) and item.get("message"):
            recs.append(str(item["message"]))
    recs = [r for r in recs if r]
    out: List[str] = []
    if q:
        out.append(f"По запросу «{q[:140]}» — факты аудита, не общая заглушка.")
    else:
        out.append(f"Архитектурный разбор «{ctx.get('title')}».")
    if sla:
        out.append(
            f"SLA: критический путь {_fh(float(sla.get('critical_path_hours') or 0))}, "
            f"с худшим возвратом {_fh(float(sla.get('with_rework_hours') or 0))}, "
            f"срыв: {'да' if sla.get('breach') else 'нет'}."
        )
    if bus:
        out.append(f"Ключевая роль: «{bus.get('top_role')}» ({float(bus.get('max_share') or 0):.0%} шагов).")
    if meth.get("score") is not None:
        out.append(f"Линтер нотации: Quality Score {int(meth.get('score') or 0)}%.")
    loops = ctx.get("rework_loops") or []
    if loops:
        l0 = max(loops, key=lambda x: float((x or {}).get("cycle_hours") or 0))
        out.append(f"Дороже всего цикл «{l0.get('label')}»: {_fh(float(l0.get('cycle_hours') or 0))}.")
    if ctx.get("tobe_ready"):
        out.append(
            f"To-Be уже есть: SLA {_fh(float(ctx.get('sla_hours_as_is') or 0))} → "
            f"{_fh(float(ctx.get('sla_hours_to_be') or 0))} (−{ctx.get('delta_sla_percent')}%)."
        )
    if recs:
        out.append("Что делать архитектору:")
        out += [f"- {r}" for r in recs[:3]]
    out.append("Уточните: сравнение As-Is/To-Be, читаемость (анти-метро), роли или ускорение SLA.")
    return "\n".join(out)


_QUALITY_Q_RE = re.compile(
    r"quality|качеств\w+\s+(нотац|score)|почему.{0,50}(score|балл|нотац|линтер)|"
    r"(упал|изменил|просел)\w*.{0,30}(quality|балл|нотац)|линтер\w*\s+нотац",
    re.I,
)
_WHY_SAVED_RE = re.compile(
    r"как\s+(мы\s+)?сократ|за сч[её]т чего|за счет чего|объясни подробн|"
    r"почему.{0,40}(сократ|ускор|экономи|to-be|tobe)|"
    r"как.{0,40}(ускор\w+\s+процесс|экономи)|декомпозиц\w+\s+экономи|"
    r"в ч[её]м причина.{0,30}(сократ|экономи|to-be|tobe)",
    re.I,
)


def _analysis_why_saved(ctx: Dict[str, Any]) -> str:
    """Декомпозиция экономии SLA To-Be: петли, параллель, scriptTask — не заглушка читаемости."""
    if not ctx.get("tobe_ready"):
        return (
            "**To-Be ещё не посчитан.** Откройте вкладку «Оптимизация As-Is → To-Be» — "
            "разложу экономию по петлям доработки, параллелизации шагов и автоматизации (scriptTask)."
        )
    as_h = float(ctx.get("sla_hours_as_is") or 0)
    to_h = float(ctx.get("sla_hours_to_be") or 0)
    saved = float(ctx.get("sla_saved_hours") or max(0.0, as_h - to_h))
    pct = float(ctx.get("delta_sla_percent") or 0)
    rw_h_as = float(ctx.get("rework_hours_as_is") or 0)
    rw_h_to = float(ctx.get("rework_hours_to_be") or 0)
    loop_saved = max(0.0, rw_h_as - rw_h_to)
    rb, ra = int(ctx.get("rework_loops_as_is") or 0), int(ctx.get("rework_loops_to_be") or 0)
    actions = [a for a in (ctx.get("tobe_actions") or []) if isinstance(a, dict)]
    par = [a for a in actions if a.get("kind") == "parallel"]
    auto = [a for a in actions if a.get("kind") == "automation"]
    ctrl = [a for a in actions if a.get("kind") == "zero_rework"]
    rest = max(0.0, saved - loop_saved)
    out = [
        f"**Как сократили SLA «{ctx.get('title')}»: {_fh(as_h)} → {_fh(to_h)} "
        f"(экономия {_fh(saved)}, {pct:g}%).**",
        "Декомпозиция выигрыша:",
        f"1) **Устранение петель доработки:** {rb} → {ra} "
        f"(снято {max(0, rb - ra)} цикл.). Экономия циклов: **{_fh(loop_saved)}** "
        f"(худший возврат {_fh(rw_h_as)} → {_fh(rw_h_to)}).",
    ]
    loops = sorted(ctx.get("rework_loops") or [], key=lambda x: -float((x or {}).get("cycle_hours") or 0))
    for item in loops[:3]:
        out.append(f"   - цикл As-Is «{item.get('label')}»: {_fh(float(item.get('cycle_hours') or 0))}")
    par_txt = "; ".join(str(a.get("detail") or "").rstrip(".") for a in par if a.get("detail")) or (
        "независимые шаги разных ролей вынесены в параллельный AND-шлюз"
    )
    out.append(f"2) **Параллелизация конкретных шагов:** {par_txt}. "
               f"Выигрыш на критическом пути (без петель): **{_fh(rest)}**.")
    if auto:
        auto_txt = "; ".join(str(a.get("detail") or "") for a in auto if a.get("detail"))
        out.append(
            f"3) **Автоматизация рутины (scriptTask):** {auto_txt}. "
            "Фиксация в журналах выполняется информационной системой (обычно 5 мин вместо ручного шага)."
        )
    else:
        out.append("3) **Автоматизация рутины (scriptTask):** отдельных журнальных шагов к автоматизации не было.")
    for act in ctrl[:2]:
        if act.get("detail"):
            out.append(f"- Zero-Rework: {act['detail']}")
    return "\n".join(out)


def _analysis_quality_score(ctx: Dict[str, Any]) -> str:
    """Почему изменился Quality Score — не анти-метро и не заглушка читаемости."""
    meth = ctx.get("methodology") or {}
    now = int(meth.get("score") or 0)
    qb = int(ctx.get("quality_score_as_is") or now)
    qa = int(ctx.get("quality_score_to_be") or now)
    naming = next((c for c in (meth.get("checks") or []) if c.get("key") == "naming"), None)
    if ctx.get("tobe_ready"):
        if qa >= 100 and qb >= 100:
            return (
                f"**Quality Score нотации: {qb}% → {qa}%.** Стандарты соблюдены на 100%: "
                "все задачи To-Be в форме «глагол в инфинитиве + объект», шлюзы подписаны. "
                "Параллельные шлюзы и входной контроль добавлены без поломки Naming Compliance."
            )
        extra = f"\nNaming: {naming.get('detail')}" if naming else ""
        if qa < qb:
            return (
                f"**Quality Score: {qb}% → {qa}%.** При реинжиниринге добавлены шлюзы параллелизации "
                "и шаги входного контроля. Правило «глагол + объект» для задач сохранено; "
                "расхождение даёт линтер на новых элементах, а не отказ от стандарта."
                + extra
            )
        return (
            f"**Quality Score: {qb}% → {qa}%.** Нотация не просела: глагол + объект сохранены, "
            "добавлены только параллельные шлюзы и контроли."
        )
    if now >= 100:
        return (
            f"**Quality Score текущей схемы: {now}%.** Стандарты нотации соблюдены на 100% "
            "(глагол в инфинитиве + объект)."
        )
    return _analysis_readability(ctx)


def heuristic_analysis(message: str, ctx: Dict[str, Any]) -> str:
    low = message.lower()
    roles = _roles_in_message(message, _roles_of(ctx))
    if _QUALITY_Q_RE.search(low):
        return _analysis_quality_score(ctx)
    if _WHY_SAVED_RE.search(low):
        return _analysis_why_saved(ctx)
    if re.search(r"сравни|as-is|as is|to-be|tobe|до и после|до/после|целев\w+\s+схем|реинжинир", low):
        return _analysis_tobe(ctx)
    if re.search(r"читаем|метро|нотаци|подпроцесс|анти-метро|методолог", low):
        return _analysis_readability(ctx)
    if re.search(r"sla|срок|срыв|задерж|критическ|длительн|долго|почему.*(долго|медлен)", low):
        return _analysis_sla(ctx)
    if re.search(r"цикл|возврат|доработ|rework|замечани", low):
        return _analysis_loops(ctx)
    if re.search(r"ускор|оптимиз|быстрее|повысить эффективн|улучш", low):
        return _analysis_speedup(ctx)
    if re.search(r"систем|it\b|ит-|scada|документ|артефакт|ландшафт|crm|1с|sap", low):
        return _analysis_landscape(ctx)
    if re.search(r"нагрузк|bus|загружен|разгруз|перегруз|исполнител|кто.*(ключев|зависит)", low) or roles:
        return _analysis_load(ctx, roles)
    if re.search(r"риск|узк|проблем|слаб|разбор", low):
        return _analysis_sla(ctx) + "\n\n" + _analysis_load(ctx, [])
    return _analysis_open(message, ctx)


# --------------------------- «Предложить следующий шаг» --------------------------- #
def suggest_next_steps(ctx: Dict[str, Any]) -> str:
    steps = ctx["steps"]
    if not steps:
        return "Процесс пока пуст — сгенерируйте диаграмму."
    last = steps[-1]
    text_all = " ".join(f"{s['title']} {s['role']}" for s in steps).lower()
    ideas: List[Tuple[str, str]] = []
    if not re.search(r"уведом|информир|сообщ", text_all):
        ideas.append((f"уведомить заинтересованные стороны о результате",
                      f"Добавь уведомление заявителя после шага {last['num']}"))
    if not ctx["it_systems"]:
        ideas.append(("зафиксировать результат в информационной системе (сейчас процесс не привязан к системам)",
                      f"Добавь фиксацию результата в информационной системе после шага {last['num']}"))
    if ctx["rework_loops"] and not re.search(r"эскалац|руководител", text_all):
        ideas.append(("добавить эскалацию руководителю после повторного возврата (ограничить циклы доработки)",
                      f"Добавь эскалацию руководителю после шага {max(1, last['num'] - 1)}"))
    if not re.search(r"контрол|проверк|аудит|приёмк|приемк", " ".join(s["title"] for s in steps[-3:]).lower()):
        ideas.append(("финальный контроль результата перед закрытием процесса",
                      f"Добавь контроль результата руководителем после шага {last['num']}"))
    if not re.search(r"архив|хранени", text_all):
        ideas.append(("архивировать документы процесса", f"Добавь архивирование документов после шага {last['num']}"))
    ideas = ideas[:3] or [("процесс выглядит завершённым; можно добавить шаг оценки удовлетворённости заявителя",
                           f"Добавь уведомление заявителя после шага {last['num']}")]
    out = [f"**Последний шаг сейчас — {last['num']}. «{last['title']}» ({last['role']}).** Что логично добавить дальше:"]
    for i, (why, cmd) in enumerate(ideas, 1):
        out.append(f"{i}. {why[:1].upper() + why[1:]}.  \n   Команда: «{cmd}»")
    return "\n".join(out)


# --------------------------- режим 2: правка регламента на лету --------------------------- #
_STEP_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_REF_STRICT = re.compile(r"(?<![A-Za-zА-Яа-яЁё])(п(?:ункт\w*|\.|п\.)?\s*|шаг\w*\s*)(\d+)", re.I)
_HEADER_RE = re.compile(
    r"^(?:регламент|название|процесс)\s*[:—–-]|^(?:целев\w+[^:\n]{0,40}|sla)\s*[:—–-]",
    re.I,
)
_NOUN_VERBS: List[Tuple[str, str, str]] = [  # (основа существительного, 3 л. ед.ч., вин. падеж)
    ("согласован", "согласовывает", "согласование"), ("проверк", "проверяет", "проверку"),
    ("утвержден", "утверждает", "утверждение"), ("уведомлен", "уведомляет", "уведомление"),
    ("подписан", "подписывает", "подписание"), ("рассмотрен", "рассматривает", "рассмотрение"),
    ("оценк", "оценивает", "оценку"), ("экспертиз", "проводит экспертизу", "экспертизу"),
    ("контрол", "контролирует", "контроль"), ("регистрац", "регистрирует", "регистрацию"),
    ("информирован", "информирует", "информирование"), ("анализ", "анализирует", "анализ"),
    ("приёмк", "принимает", "приёмку"), ("приемк", "принимает", "приёмку"), ("оплат", "оплачивает", "оплату"),
    ("инструктаж", "проводит инструктаж", "инструктаж"), ("аудит", "проводит аудит", "аудит"),
    ("архивирован", "архивирует", "архивирование"), ("эскалац", "эскалирует", "эскалацию"),
    ("фиксац", "фиксирует", "фиксацию"),
]


def _step_title(body: str) -> str:
    """Короткое название шага для отчёта о правках (без «Параллельно:», роли и срока)."""
    b = re.sub(r"^(?:параллельно|одновременно)\s*[:,—–-]?\s*", "", body.strip(), flags=re.I)
    b = _parse_decision(_DUR_RE.sub(" ", b))[0] or b
    role, rs, re_ = _find_role(b)
    if role and rs == 0:
        b = b[re_:]
    return _task_title(b)[:80]


def _split_steps(text: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Текст регламента → (строки заголовка, [{num, body}]). Проза без нумерации режется на предложения."""
    header: List[str] = []
    steps: List[Dict[str, Any]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        m = _STEP_LINE_RE.match(line)
        if m:
            steps.append({"num": int(m.group(1)), "body": m.group(2).strip()})
        elif steps:
            steps[-1]["body"] += " " + line.strip()
        elif _HEADER_RE.match(line.strip()) or not header:
            header.append(line.strip())
        else:
            steps.append({"num": len(steps) + 1, "body": line.strip()})
    if len(steps) < 2:
        body = " ".join(l.strip() for l in text.split("\n") if l.strip() and not _HEADER_RE.match(l.strip()))
        sentences = [s.strip() for s in re.split(r"(?<=[.;!?])\s+(?=[А-ЯЁA-Z«])", body) if len(s.strip()) > 8]
        steps = [{"num": i, "body": s} for i, s in enumerate(sentences, 1)]
        header = [l.strip() for l in text.split("\n") if _HEADER_RE.match(l.strip())]
    return header, steps


def _join_steps(header: List[str], steps: List[Dict[str, Any]]) -> str:
    return "\n".join([*header, "", *[f"{i}. {s['body']}" for i, s in enumerate(steps, 1)]]).strip() + "\n"


def _remap(body: str, fn: Callable[[int], int]) -> str:
    return _REF_STRICT.sub(lambda m: f"{m.group(1)}{fn(int(m.group(2)))}", body)


def _nums_in(fragment: str) -> List[int]:
    nums: List[int] = []
    for m in re.finditer(r"(\d+)\s*(?:[-–—]|по)\s*(\d+)|(\d+)", fragment):
        if m.group(1):
            nums += list(range(int(m.group(1)), int(m.group(2)) + 1))
        else:
            nums.append(int(m.group(3)))
    return list(dict.fromkeys(nums))


def _step_refs(message: str) -> List[int]:
    m = re.search(r"(?:шаг\w*|этап\w*|пункт\w*|п\.)\s*((?:\d+\s*(?:[-–—]|по|,|и)?\s*)+)", message, re.I)
    return _nums_in(m.group(1)) if m else []


def _default_duration(steps: List[Dict[str, Any]]) -> str:
    hours = []
    for s in steps:
        d = _DUR_RE.search(s["body"])
        if d:
            hours.append(_to_hours(d.group(1), d.group(2)))
    hours.sort()
    median = hours[len(hours) // 2] if hours else 1.0
    return "(1 рабочий день)" if median >= 8 else ("(2 часа)" if median >= 1 else "(30 минут)")


def _role_instrumental(role: str) -> str:
    """«Служба экологии» → «службой экологии» (творительный для «с …»)."""
    words = role.split()
    first = words[0]
    low = first.lower()
    if low.endswith("а"):
        first = first[:-1] + "ой"
    elif low.endswith("я"):
        first = first[:-1] + "ей"
    elif low.endswith("ь"):
        first = first[:-1] + "ью"
    elif not low.endswith(("ом", "ем", "ой", "ей", "ью", "ами", "ями")):
        first = first + "ом"
    words[0] = first[:1].lower() + first[1:]
    return " ".join([words[0]] + [w[:1].lower() + w[1:] for w in words[1:]])


def _build_new_step(content: str, anchor_role: str, duration: str) -> str:
    content = re.sub(r"^\s*(?:нов\w+\s+)?(?:шаг|этап|пункт)\s*[:—–-]?\s*", "", content.strip(" .:;«»\"'"), flags=re.I).strip()
    role, rs, re_ = _find_role(content)
    has_verb = any(_is_verb(w) for w in re.findall(r"[А-Яа-яЁё]+", content))
    if role and has_verb and rs == 0:
        body = content
    else:
        noun = next(((stem, v3, acc) for stem, v3, acc in _NOUN_VERBS if re.search(r"\b" + stem, content, re.I)), None)
        rest = content
        with_whom = ""
        extra_role = role
        if extra_role:
            # «с экологами» / «для СБ» — объект действия, не исполнитель; предлог перед ролью тоже вырезаем.
            lead = content[:rs]
            m_with = re.search(r"(с|со|у|от|для|силами|через)\s*$", lead, re.I)
            if m_with:
                prep = m_with.group(1).lower()
                whom = _role_instrumental(extra_role) if prep in ("с", "со") else extra_role[:1].lower() + extra_role[1:]
                with_whom = f"{prep} {whom}"
                rest = (lead[: m_with.start()] + " " + content[re_:]).strip()
                extra_role = None
            elif extra_role and extra_role != anchor_role:
                # «проверку охраны труда» — тема шага, исполнитель остаётся якорем
                rest = content
                extra_role = None
            else:
                rest = (lead + " " + content[re_:]).strip()
            rest = re.sub(r"^\s*(?:с|со|у|от|для|силами|через)\s+", "", rest, flags=re.I).strip()
        who = extra_role or anchor_role
        if noun:
            stem, v3, acc = noun
            rest = re.sub(r"\b" + stem + r"\w*", "", rest, flags=re.I).strip(" ,.;")
            if with_whom:
                body = f"{who} {v3} {with_whom}" + (f" {rest}" if rest else "")
            elif not rest:
                body = f"{who} {v3} результаты предыдущего шага"
            else:
                body = f"{who} проводит {acc} {rest}"
        elif has_verb and not role:
            body = f"{who} {content[:1].lower() + content[1:]}"
        else:
            body = f"{who} выполняет: {content[:1].lower() + content[1:]}"
    body = body[:1].upper() + body[1:]
    return f"{body} {duration}."


@dataclass
class EditResult:
    text: str
    changes: List[str]


def heuristic_edit(message: str, text: str) -> Tuple[Optional[EditResult], str]:
    """Детерминированная правка регламента по команде. Возвращает (результат | None, пояснение для пользователя)."""
    header, steps = _split_steps(normalize_regulation(text))
    if len(steps) < 2:
        return None, "В регламенте не удалось выделить шаги — пронумеруйте их («1. Диспетчер принимает …»)."
    low = message.lower()
    n = len(steps)
    changes: List[str] = []
    refs = [r for r in _step_refs(message) if 1 <= r <= n]

    def by_phrase(phrase: str) -> Optional[int]:
        stems = {w[:5].lower() for w in re.findall(r"[А-Яа-яЁё]{4,}", phrase)} - {"шаг", "этап", "пункт", "шага", "этапа"}
        scored = []
        for i, s in enumerate(steps, 1):
            b = s["body"].lower()
            score = sum(1 for st in stems if st in b)
            if score:
                scored.append((score, i))
        scored.sort(reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        return None

    # --- поменять местами ---
    if re.search(r"местами|поменя\w+\s+(?:шаг|этап)\w*\s+\d+\s*(?:и|с)\s*\d+", low) and len(refs) == 2:
        a, b = refs
        swap = lambda k: b if k == a else (a if k == b else k)  # noqa: E731
        bodies = [s["body"] for s in steps]
        bodies[a - 1], bodies[b - 1] = bodies[b - 1], bodies[a - 1]
        steps = [{"num": i + 1, "body": _remap(body, swap)} for i, body in enumerate(bodies)]
        changes.append(f"шаги {a} и {b} поменяны местами")
        return EditResult(_join_steps(header, steps), changes), ""

    # --- параллельность ---
    if re.search(r"параллельн|одновременн", low) and len(refs) >= 2:
        make_seq = re.search(r"(?:не\s+параллельн|последовательн)", low) is not None
        first = min(refs)
        for k in sorted(refs):
            body = steps[k - 1]["body"]
            has = bool(re.match(r"^(?:параллельно|одновременно)\b", body, re.I))
            if make_seq and has:
                steps[k - 1]["body"] = re.sub(r"^(?:параллельно|одновременно)\s*[:,—–-]?\s*", "", body, flags=re.I)
                changes.append(f"шаг {k} теперь выполняется последовательно")
            elif not make_seq and k != first and not has:
                steps[k - 1]["body"] = "Параллельно: " + body
                changes.append(f"шаг {k} выполняется параллельно шагу {k - 1}")
        if not changes:
            return None, "Эти шаги уже в нужном режиме — изменений не потребовалось."
        return EditResult(_join_steps(header, steps), changes), ""

    # --- целевой срок процесса ---
    if re.search(r"целев\w+\s+срок|\bsla\b", low) and not refs:
        d = _DUR_RE.search(message)
        if d:
            line = f"Целевой срок процесса: {d.group(0).strip(' ()')}"
            header = [h for h in header if not re.match(r"^(?:целев\w+\s+(?:срок|sla)|sla)", h, re.I)] + [line]
            changes.append(f"целевой срок: {d.group(0).strip(' ()')}")
            return EditResult(_join_steps(header, steps), changes), ""

    # --- удаление ---
    if re.search(r"\b(?:удал\w+|убер\w+|убрать|исключ\w+)\b", low):
        targets = refs or ([by_phrase(message)] if by_phrase(message) else [])
        targets = [t for t in targets if t]
        if not targets:
            return None, "Не понял, какой шаг удалить: укажите номер («Удали шаг 5») или название шага."
        if len(steps) - len(targets) < 2:
            return None, "Нельзя удалить так много шагов — в процессе должно остаться минимум два."
        removed = set(targets)
        new_index: Dict[int, int] = {}
        counter = 0
        for old in range(1, n + 1):
            if old not in removed:
                counter += 1
                new_index[old] = counter

        def fn(k: int) -> int:
            if k in new_index:
                return new_index[k]
            # ссылка на удалённый шаг: ближайший сохранившийся по направлению «вперёд»
            nxt = next((new_index[j] for j in range(k + 1, n + 1) if j in new_index), None)
            prv = next((new_index[j] for j in range(k - 1, 0, -1) if j in new_index), None)
            return nxt if nxt is not None else (prv if prv is not None else 1)

        kept = [{"num": 0, "body": _remap(s["body"], fn)} for i, s in enumerate(steps, 1) if i not in removed]
        for t in sorted(removed):
            changes.append(f"удалён шаг {t}: «{_step_title(steps[t - 1]['body'])}»")
        return EditResult(_join_steps(header, kept), changes), ""

    # --- срок шага ---
    if refs and _DUR_RE.search(re.sub(r"(?:шаг\w*|этап\w*|пункт\w*)\s*\d+", "", message, flags=re.I)) and re.search(
            r"срок|сократ|увелич|измени|постав|установи|до\s+\d|на\s+\d", low):
        k = refs[0]
        d = _DUR_RE.search(re.sub(r"(?:шаг\w*|этап\w*|пункт\w*)\s*\d+", "", message, flags=re.I))
        new_dur = d.group(0).strip(" ()")
        body = steps[k - 1]["body"]
        existing = _DUR_RE.search(body)
        if existing:
            body = body[: existing.start()] + f"({new_dur})" + body[existing.end():]
        else:
            m = re.search(r"\.\s*(?:Если\b|$)", body)
            cut = m.start() if m else len(body)
            body = f"{body[:cut].rstrip()} ({new_dur}){body[cut:]}"
        steps[k - 1]["body"] = re.sub(r"\s{2,}", " ", body)
        changes.append(f"срок шага {k}: {new_dur}")
        return EditResult(_join_steps(header, steps), changes), ""

    # --- передать шаг другой роли ---
    if refs and re.search(r"передай|назначь|поручи|перенес\w+\s+(?:на|к)", low):
        role = next((r for r in [_find_role(message)[0]] if r), None)
        if role:
            k = refs[0]
            body = steps[k - 1]["body"]
            old_role, rs, re_ = _find_role(body)
            if old_role and rs == 0:
                rest = body[re_:].lstrip()
                body = f"{role} {rest}"
            else:
                body = f"{role} {body[:1].lower() + body[1:]}"
            steps[k - 1]["body"] = body
            changes.append(f"шаг {k} передан роли «{role}»")
            return EditResult(_join_steps(header, steps), changes), ""

    # --- добавление ---
    if re.search(r"\b(?:добав\w+|вставь\w*|вставить|включи\w*|дополни\w*)\b", low):
        m = re.search(r"\b(?:добав\w+|вставь\w*|вставить|включи\w*|дополни\w*)\b\s*(.*)$", message, re.I | re.S)
        content = m.group(1) if m else ""
        pos_after = re.search(r"\bпосле\s+(?:(?:шага|этапа|пункта|п\.)\s*)?(\d+)(?:\s*-?\s*(?:го|ого)\s*(?:шага|этапа)?)?", content, re.I)
        pos_before = re.search(r"\bперед\s+(?:(?:шагом|этапом|пунктом|п\.)\s*)?(\d+)", content, re.I)
        in_start = re.search(r"\bв\s+начал\w+", content, re.I)
        in_end = re.search(r"\bв\s+конец\b|\bв\s+конце\b", content, re.I)
        anchor = n
        if pos_after:
            anchor = int(pos_after.group(1))
        elif pos_before:
            anchor = int(pos_before.group(1)) - 1
        elif in_start:
            anchor = 0
        for rx in (pos_after, pos_before, in_start, in_end):
            if rx:
                content = content.replace(rx.group(0), " ")
        content = content.strip(" ,.;:")
        if not content:
            return None, "Что именно добавить? Например: «Добавь согласование с экологами после шага 3»."
        if not 0 <= anchor <= n:
            return None, f"В процессе {n} шагов — шага {anchor} нет."
        anchor_role = steps[max(anchor, 1) - 1]["body"]
        anchor_role = _find_role(anchor_role)[0] or "Исполнитель"
        new_body = _build_new_step(content, anchor_role, _default_duration(steps))
        pos = anchor + 1  # номер нового шага

        def fn_shift(k: int) -> int:
            return k + 1 if k >= pos else k

        shifted = []
        for i, s in enumerate(steps, 1):
            if i == anchor:  # ветка «да» якорного шага теперь ведёт на новый шаг, а не мимо него
                body = _REF_STRICT.sub(
                    lambda mm: f"{mm.group(1)}{pos if int(mm.group(2)) == pos else fn_shift(int(mm.group(2)))}", s["body"])
            else:
                body = _remap(s["body"], fn_shift)
            shifted.append({"num": 0, "body": body})
        shifted.insert(pos - 1, {"num": pos, "body": new_body})
        where = "в начало" if anchor == 0 else (f"после шага {anchor}" if anchor < n else "в конец")
        changes.append(f"добавлен шаг {pos} ({where}): «{_step_title(new_body)}»")
        return EditResult(_join_steps(header, shifted), changes), ""

    return None, ""


_EDIT_HELP = (
    "Не смог однозначно понять правку. Поддерживаются команды, например:\n"
    "- «Добавь согласование с экологами после шага 3»\n"
    "- «Удали шаг 5»\n"
    "- «Сделай шаги 4 и 5 параллельными»\n"
    "- «Измени срок шага 3 на 2 часа»\n"
    "- «Передай шаг 4 службе безопасности»\n"
    "- «Поменяй шаги 6 и 7 местами»\n"
    "- «Установи целевой срок 10 часов»"
)


def _llm_edit(message: str, text: str, trace: List[str]) -> Optional[Tuple[str, str]]:
    messages = [
        {"role": "system", "content": EDIT_SYSTEM},
        {"role": "user", "content": f"ТЕКУЩИЙ РЕГЛАМЕНТ:\n{text.strip()}\n\nКОМАНДА ПОЛЬЗОВАТЕЛЯ: {message.strip()}"},
    ]
    got = _chat_llm(messages, trace)
    if not got:
        return None
    label, raw = got
    new_text = _strip_markdown(raw).strip()
    _, old_steps = _split_steps(normalize_regulation(text))
    _, new_steps = _split_steps(normalize_regulation(new_text))
    if len(new_steps) < 2 or abs(len(new_steps) - len(old_steps)) > 6 or new_text.strip() == text.strip():
        trace.append(f"{label}: правка отклонена (некорректный регламент в ответе)")
        return None
    return label, new_text + ("\n" if not new_text.endswith("\n") else "")


def _rebuild_process(text: str, engine: str, trace: List[str]) -> Tuple[str, Dict[str, Any], str]:
    """Перестроение диаграммы по обновлённому регламенту: эмулятор → код DIAGRAM → execute_generated_code."""
    started = time.time()
    norm = normalize_regulation(text)
    header = parse_regulation(norm)
    code, info = emulate_generation(norm)
    xml, audit, err = execute_generated_code(code, header.title, header.sla_hours, regulation_text=norm)
    if err:
        return "", {}, err
    audit["artifacts"], audit["it_systems"] = header.artifacts, header.it_systems
    audit["generation"] = {
        "engine": engine, "fallback": False, "attempts": 1, "trace": trace, "rejected": [],
        "code": code, "elapsed_s": round(time.time() - started, 2), "parsed": info,
    }
    return xml, audit, ""


def _audit_delta(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    try:
        o, n = old.get("sla") or {}, new.get("sla") or {}
        lines = []
        if o and n:
            lines.append(f"- критический путь: {_fh(float(o['critical_path_hours']))} → **{_fh(float(n['critical_path_hours']))}**")
            if bool(o.get("breach")) != bool(n.get("breach")):
                lines.append(f"- срыв SLA: {'да' if o.get('breach') else 'нет'} → **{'да' if n.get('breach') else 'нет'}**")
        ob, nb = old.get("bus_factor") or {}, new.get("bus_factor") or {}
        if ob and nb:
            lines.append(f"- bus-factor: {float(ob['max_share']):.0%} → **{float(nb['max_share']):.0%}** ({nb['top_role']})")
        os_, ns_ = (old.get("stats") or {}), (new.get("stats") or {})
        if os_ and ns_:
            lines.append(f"- узлов в диаграмме: {os_.get('nodes')} → **{ns_.get('nodes')}**")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


# --------------------------- режим 3: реверс-генерация (инструкция по схеме) --------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_bpmn_structure(xml: str) -> Dict[str, Any]:
    """BPMN XML → узлы, потоки, дорожки и порядок (по координатам DI): основа реверс-генерации."""
    root = _ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    nodes: Dict[str, Dict[str, Any]] = {}
    flows: List[Dict[str, str]] = []

    def walk(el: "_ET.Element", parent_sub: Optional[str]) -> None:
        for child in el:
            tag = _local(child.tag)
            cid = child.get("id", "")
            if tag in _TASK_TAGS or tag in _GATE_TAGS or tag in ("startEvent", "endEvent", "subProcess"):
                nodes[cid] = {"id": cid, "type": tag, "name": child.get("name", ""), "sub": parent_sub}
                if tag == "subProcess":
                    walk(child, cid)
            elif tag == "sequenceFlow":
                flows.append({"id": cid, "src": child.get("sourceRef", ""), "dst": child.get("targetRef", ""), "name": child.get("name", "")})

    for proc in root.iter(f"{{{_NS_BPMN}}}process"):
        walk(proc, None)
    lane_of: Dict[str, str] = {}
    for lane in root.iter(f"{{{_NS_BPMN}}}lane"):
        for ref in lane.findall(f"{{{_NS_BPMN}}}flowNodeRef"):
            if ref.text:
                lane_of[ref.text.strip()] = lane.get("name", "")
    pos: Dict[str, Tuple[float, float]] = {}
    for shape in root.iter(f"{{{_NS_DI}}}BPMNShape"):
        b = shape.find(f"{{{_NS_DC}}}Bounds")
        if b is not None:
            pos[shape.get("bpmnElement", "")] = (float(b.get("x", 0)), float(b.get("y", 0)))
    for nid, node in nodes.items():
        owner = nid
        while owner in nodes and owner not in lane_of and nodes[owner]["sub"]:
            owner = nodes[owner]["sub"]  # внутренние узлы подпроцесса принадлежат дорожке подпроцесса
        node["lane"] = lane_of.get(owner, "")
        node["x"], node["y"] = pos.get(nid, (0.0, 0.0))
    return {"nodes": nodes, "flows": flows, "lanes": list(dict.fromkeys(lane_of.values()))}


def _work_neighbors(struct: Dict[str, Any], node_id: str, forward: bool) -> List[Tuple[Dict[str, Any], List[str]]]:
    """Ближайшие рабочие узлы выше/ниже по потоку (сквозь шлюзы и события) + условия на пути."""
    nodes, flows = struct["nodes"], struct["flows"]
    result: List[Tuple[Dict[str, Any], List[str]]] = []
    seen = set()
    stack: List[Tuple[str, List[str]]] = [(node_id, [])]
    while stack:
        cur, labels = stack.pop()
        for f in flows:
            a, b = (f["src"], f["dst"]) if forward else (f["dst"], f["src"])
            if a != cur:
                continue
            nxt = nodes.get(b)
            if not nxt or (b, tuple(labels)) in seen:
                continue
            seen.add((b, tuple(labels)))
            new_labels = labels + ([f["name"]] if f["name"] else [])
            if nxt["type"] in _TASK_TAGS or nxt["type"] == "subProcess":
                result.append((nxt, new_labels))
            elif nxt["type"] == "endEvent":
                if not nxt["sub"]:  # «Завершено» внутри подпроцесса — служебное событие
                    result.append((nxt, new_labels))
            elif nxt["type"] in _GATE_TAGS:
                stack.append((b, new_labels))
    return result


def build_role_instruction(role: str, current_xml: str, current_text: str, audit: Dict[str, Any]) -> str:
    """Пошаговая должностная инструкция для роли по схеме BPMN (детерминированный скелет из диаграммы)."""
    struct = parse_bpmn_structure(current_xml)
    nodes = struct["nodes"]
    ctx = build_process_context(current_text, current_xml, audit)
    text_steps = {s["title"]: s for s in ctx["steps"]}
    mine = sorted(
        (n for n in nodes.values() if n["lane"] == role and (n["type"] in _TASK_TAGS or n["type"] == "subProcess")),
        key=lambda n: (n["x"], n["y"]),
    )
    inner = {n["id"]: sorted((c for c in nodes.values() if c["sub"] == n["id"] and c["type"] in _TASK_TAGS), key=lambda c: (c["x"], c["y"]))
             for n in mine if n["type"] == "subProcess"}
    if not mine:
        return f"В схеме нет задач для роли «{role}»."
    load = {r["role"]: r for r in ctx["lane_load"]}
    crit_names = {c["name"]: c for c in ctx["critical_path"] if c["role"] == role}
    lines = [f"# Должностная инструкция по процессу: {role}", f"**Процесс:** {ctx['title']}", ""]
    lines.append("## 1. Общие положения")
    share = load.get(role)
    tasks_total = sum(len(inner[n["id"]]) if n["type"] == "subProcess" else 1 for n in mine)
    lines.append(f"- Роль «{role}» выполняет {tasks_total} задач процесса" + (f" ({float(share['share']):.0%} от всех шагов)." if share else "."))
    first = mine[0]
    preds = [p for p, lab in _work_neighbors(struct, first["id"], forward=False)]
    lines.append("")
    lines.append("## 2. Входные данные и триггеры")
    if preds:
        lines += [f"- Результат шага «{p['name']}» (роль «{p['lane']}»)" for p in preds if p["type"] != "endEvent"]
    else:
        lines.append("- Процесс начинается с действий этой роли: поступление заявки / сообщения / запроса.")
    lines.append("")
    lines.append("## 3. Порядок действий")
    n = 0

    def step_lines(task: Dict[str, Any], indent: str = "") -> None:
        nonlocal n
        n += 1
        meta = text_steps.get(task["name"])
        extra = []
        if meta and meta["hours"]:
            extra.append(f"срок: {_fh(float(meta['hours']))}")
        elif task["name"] in crit_names:
            extra.append(f"срок: {_fh(float(crit_names[task['name']]['hours']))}")
        if meta and meta["systems"]:
            extra.append("системы: " + ", ".join(meta["systems"]))
        if meta and meta["artifacts"]:
            extra.append("документы: " + ", ".join(meta["artifacts"]))
        lines.append(f"{indent}{n}. **{task['name']}**" + (f" — {'; '.join(extra)}" if extra else "") + ".")
        for nxt, labels in _work_neighbors(struct, task["id"], forward=True):
            if labels:
                target = "завершение процесса" if nxt["type"] == "endEvent" else f"«{nxt['name']}» ({nxt['lane']})"
                lines.append(f"{indent}   - Если «{labels[-1]}» — перейти к {target}.")

    for task in mine:
        if task["type"] == "subProcess":
            lines.append(f"**Этап «{task['name']}»** (выполняется по порядку):")
            for child in inner.get(task["id"], []):
                step_lines(child, "   ")
        else:
            step_lines(task)
    lines.append("")
    lines.append("## 4. Передача результата")
    outs: List[str] = []
    for task in mine:
        ids = [task["id"]] + ([c["id"] for c in inner.get(task["id"], [])] if task["type"] == "subProcess" else [])
        for tid in ids:
            for nxt, labels in _work_neighbors(struct, tid, forward=True):
                if nxt["lane"] != role:
                    outs.append(f"- «{nxt['name']}»" if nxt["type"] == "endEvent" else f"- Роли «{nxt['lane']}»: «{nxt['name']}»")
    lines += list(dict.fromkeys(outs)) or ["- Результат остаётся внутри роли; процесс завершается её действиями."]
    lines.append("")
    lines.append("## 5. Контроль сроков и риски")
    if crit_names:
        lines.append("- Шаги на критическом пути SLA: " + "; ".join(f"«{k}» ({_fh(float(v['hours']))})" for k, v in crit_names.items()))
    loops = [l for l in ctx["rework_loops"] if l.get("lane") == role]
    for l in loops:
        lines.append(f"- Возврат «{l['label']}»: {l['from']} → {l['to']}, стоимость цикла {_fh(float(l['cycle_hours']))} — снижать входным контролем.")
    bus = ctx["bus_factor"]
    if bus and bus.get("top_role") == role and bus.get("status") == "risk":
        lines.append(f"- ⚠️ Bus-factor: на роли {float(bus['max_share']):.0%} шагов — необходим назначенный заместитель.")
    if len(lines) and lines[-1].startswith("## 5"):
        lines.append("- Существенных рисков для роли не выявлено.")
    return "\n".join(lines)


# --------------------------- единая точка входа --------------------------- #
def _normalize_history(history: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in history or []:
        if isinstance(item, dict) and item.get("content"):
            role = "assistant" if item.get("role") == "assistant" else "user"
            out.append({"role": role, "content": str(item["content"])})
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            out.append({"role": "assistant" if item[0] in ("assistant", "ai") else "user", "content": str(item[1])})
    return out


def _source_note(label: Optional[str], trace: List[str]) -> str:
    if label:
        return f"\n\n<sub>Ответ: {label}</sub>"
    reason = f" ({'; '.join(trace[:2])})" if trace else ""
    return f"\n\n<sub>Ответ: локальный анализ процесса — облачная модель недоступна{reason}</sub>"


def assistant_chat(
    message: str,
    history: List[Dict[str, str]],
    current_xml: str,
    current_audit: Dict[str, Any],
    current_text: str,
    use_llm: bool = True,
    tobe_delta: Optional[dict] = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Диалог с AI-ассистентом над активным процессом.

    Возвращает (reply_text, updated_text, updated_xml, updated_audit). Поля updated_* заполнены, только если
    ассистент изменил процесс: новый текст регламента, новая BPMN-диаграмма и её аудит.
    Исключения наружу не выходят: при любом сбое возвращается понятный ответ, процесс остаётся без изменений.
    """
    try:
        message = (message or "").strip()
        if not message:
            return "Напишите вопрос о процессе или команду на изменение — например, «Добавь согласование с экологами после шага 3».", None, None, None
        audit = current_audit or {}
        ctx = build_process_context(current_text, current_xml, audit, tobe_delta=tobe_delta)
        if not ctx["steps"] and not current_xml:
            return "Активного процесса пока нет: выберите регламент и нажмите «Сгенерировать BPMN 2.0».", None, None, None
        intent = classify_intent(message)
        trace: List[str] = []
        hist = _normalize_history(history)[-8:]

        # ---- режим 2: правка на лету ----
        if intent == "edit":
            new_text, label, changes = None, None, []
            result, hint = heuristic_edit(message, current_text)
            if result:
                new_text, changes, label = result.text, result.changes, "детерминированная правка регламента"
            elif use_llm:
                llm = _llm_edit(message, current_text, trace)
                if llm:
                    label, new_text = llm
                    changes = ["правка сформулирована моделью по вашей команде"]
            if not new_text:
                return (hint or _EDIT_HELP) + _source_note(None, trace), None, None, None
            xml, new_audit, err = _rebuild_process(new_text, f"assistant · {label}", trace)
            if err:
                return f"Правка сформирована, но диаграмму построить не удалось: {err}. Процесс оставлен без изменений.", None, None, None
            delta = _audit_delta(audit, new_audit)
            reply = "✨ **Диаграмма обновлена ассистентом в диалоге.**\n\n" + "\n".join(f"- {c}" for c in changes)
            if delta:
                reply += "\n\n**Влияние на метрики:**\n" + delta
            reply += f"\n\n<sub>Правка: {label}; диаграмма перестроена через execute_generated_code</sub>"
            return reply, new_text, xml, new_audit

        # ---- режим 3: реверс-генерация инструкции ----
        if intent == "instruction":
            roles = _roles_of(ctx)
            named = _roles_in_message(message, roles)
            role = named[0] if named else (ctx["bus_factor"].get("top_role") if ctx["bus_factor"] else None) or (roles[0] if roles else "")
            if not role:
                return "Не нашёл ролей в схеме: сначала сгенерируйте диаграмму.", None, None, None
            if not current_xml:
                return "Для реверс-генерации нужна диаграмма — сгенерируйте BPMN.", None, None, None
            skeleton = build_role_instruction(role, current_xml, current_text, audit)
            label: Optional[str] = None
            final = skeleton
            if use_llm:
                got = _chat_llm([{"role": "system", "content": POLISH_SYSTEM}, {"role": "user", "content": skeleton}], trace)
                if got and len(got[1]) > 200 and role.lower()[:5] in got[1].lower():
                    label, final = got[0], got[1]
            note = "" if named else f"\n\n_Роль не указана — взята самая загруженная. Другие роли: {', '.join(r for r in roles if r != role)}._"
            return final + note + _source_note(label, trace), None, None, None

        # ---- режим 1: аналитика ----
        if intent == "next_step" and not use_llm:
            return suggest_next_steps(ctx) + _source_note(None, trace), None, None, None
        force_local = bool(_QUALITY_Q_RE.search(message.lower()) or _WHY_SAVED_RE.search(message.lower()))
        if use_llm and not force_local:
            system = CHAT_SYSTEM + format_context_for_prompt(ctx)
            got = _chat_llm([{"role": "system", "content": system}, *hist, {"role": "user", "content": message}], trace)
            if got:
                return got[1].strip() + _source_note(got[0], trace), None, None, None
        body = suggest_next_steps(ctx) if intent == "next_step" else heuristic_analysis(message, ctx)
        return body + _source_note(None, trace), None, None, None
    except Exception as exc:  # noqa: BLE001 — диалог не должен ронять приложение
        return f"Не удалось обработать запрос: {type(exc).__name__}: {exc}. Процесс оставлен без изменений.", None, None, None


# --------------------------- Паспорт процесса (обратная выгрузка) --------------------------- #
def _md_cell(value: Any) -> str:
    text = "—" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip() or "—"


def _md_join(items: Any) -> str:
    if not items:
        return "—"
    if isinstance(items, str):
        return _md_cell(items)
    names: List[str] = []
    for it in items:
        if isinstance(it, dict):
            name = str(it.get("name") or "").strip()
            if name:
                names.append(name)
        else:
            s = str(it).strip()
            if s:
                names.append(s)
    return _md_cell(", ".join(dict.fromkeys(names))) if names else "—"


def _step_transitions(step: Step) -> str:
    parts: List[str] = []
    if step.parallel:
        parts.append("параллельно с предыдущим шагом")
    d = step.decision
    if d is None:
        return "; ".join(parts) if parts else "—"
    if d.yes_end:
        parts.append(f"«{d.yes_label}» → завершение")
    elif d.yes_ref:
        parts.append(f"«{d.yes_label}» → п.{d.yes_ref}")
    else:
        parts.append(f"«{d.yes_label}» → далее")
    if d.has_else or d.no_ref or d.no_end or d.no_back:
        if d.no_end:
            parts.append(f"«{d.no_label}» → завершение")
        elif d.no_ref:
            dest = f"п.{d.no_ref}"
            if d.no_back:
                dest += " (возврат)"
            parts.append(f"«{d.no_label}» → {dest}")
        else:
            parts.append(f"«{d.no_label}»")
    return "; ".join(parts) if parts else "—"


def _process_owner(regulation_text: str, parsed: ParsedRegulation, audit: Dict[str, Any]) -> str:
    m = re.search(r"владелец(?:\s+процесса)?\s*[:—–-]\s*(.+)$", regulation_text or "", re.I | re.M)
    if m:
        return m.group(1).strip().rstrip(".") or "—"
    top = str((audit.get("bus_factor") or {}).get("top_role") or "").strip()
    if top:
        return top
    if parsed.roles:
        return parsed.roles[0]
    lanes = audit.get("lane_load") or []
    if lanes:
        return str(lanes[0].get("role") or "—")
    return "—"


def generate_process_passport(xml_str: str, audit_data: dict, regulation_text: str) -> str:
    """Официальный Markdown: паспорт процесса, RACI, операционный регламент, ИТ-ландшафт, риски.

    Все поля аудита читаются через .get() с значениями по умолчанию — KeyError исключён.
    """
    audit = audit_data if isinstance(audit_data, dict) else {}
    text = regulation_text or ""
    try:
        parsed = parse_regulation(normalize_regulation(text)) if text.strip() else ParsedRegulation(
            title="Бизнес-процесс", sla_hours=None
        )
    except Exception:  # noqa: BLE001
        parsed = ParsedRegulation(title="Бизнес-процесс", sla_hours=None)

    sla = audit.get("sla") or {}
    stats = audit.get("stats") or {}
    bus = audit.get("bus_factor") or {}
    load = audit.get("lane_load") or []
    path = audit.get("critical_path") or []
    loops = audit.get("rework_loops") or []
    recs = audit.get("recommendations") or []
    systems = audit.get("it_systems") or parsed.it_systems
    artifacts = audit.get("artifacts") or parsed.artifacts
    threshold = float(bus.get("threshold") or 0.45)
    target = sla.get("target_hours")
    if target is None:
        target = parsed.sla_hours
    owner = _process_owner(text, parsed, audit)
    title = parsed.title or "Бизнес-процесс по регламенту"
    nodes = stats.get("nodes") if stats.get("nodes") is not None else "—"
    subs = stats.get("subprocesses") if stats.get("subprocesses") is not None else "—"
    roles_n = stats.get("lanes") if stats.get("lanes") is not None else (len(parsed.roles) or "—")

    lines: List[str] = [
        "# Паспорт процесса и операционный регламент ПАО «Интер РАО»",
        "",
        "*Документ сформирован автоматически модулем «Архитектор BPMN-диаграмм». "
        "Подлежит согласованию Дирекцией бизнес-архитектуры.*",
        "",
        "## 1. Паспорт процесса",
        "",
        f"| Параметр | Значение |",
        f"| --- | --- |",
        f"| Наименование | {_md_cell(title)} |",
        f"| Целевой SLA | {_md_cell(_fh(float(target)) if target else 'не задан')} |",
        f"| Владелец процесса | {_md_cell(owner)} |",
        f"| Число узлов диаграммы | {_md_cell(nodes)} |",
        f"| Подпроцессов | {_md_cell(subs)} |",
        f"| Ролей (дорожек) | {_md_cell(roles_n)} |",
        f"| Рабочих шагов | {_md_cell(stats.get('work_items') if stats.get('work_items') is not None else len(parsed.steps))} |",
        "",
        "## 2. Матрица ролей и ответственности (RACI)",
        "",
        "| Роль | Количество задач | Доля нагрузки, % | Статус bus-factor |",
        "| --- | ---: | ---: | --- |",
    ]
    if load:
        for item in sorted(load, key=lambda it: -float((it or {}).get("share") or 0)):
            role = (item or {}).get("role") or "—"
            tasks = (item or {}).get("tasks") or 0
            share = float((item or {}).get("share") or 0)
            status = "риск bus-factor" if share > threshold else "норма"
            if role == (bus.get("top_role") or "") and str(bus.get("status") or "") == "risk":
                status = "риск bus-factor (ключевой исполнитель)"
            lines.append(f"| {_md_cell(role)} | {int(tasks)} | {share * 100:.0f} | {status} |")
    elif parsed.roles:
        n = len(parsed.steps) or 1
        counts: Dict[str, int] = {}
        for st in parsed.steps:
            counts[st.role] = counts.get(st.role, 0) + 1
        for role, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
            share = cnt / n
            status = "риск bus-factor" if share > threshold else "норма"
            lines.append(f"| {_md_cell(role)} | {cnt} | {share * 100:.0f} | {status} |")
    else:
        lines.append("| — | 0 | 0 | данных нет |")
    lines += [
        "",
        f"_Порог bus-factor: {threshold:.0%}. Роль выше порога — процесс зависит от одного подразделения._",
        "",
        "## 3. Пошаговый операционный регламент",
        "",
        "| № | Роль-исполнитель | Наименование действия | Нормативный срок | Входные документы / артефакты | Используемые ИТ-системы | Условия переходов |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    if parsed.steps:
        for st in parsed.steps:
            dur = _fh(float(st.hours)) if st.hours else "—"
            name = st.title or "(шлюз решения)"
            if st.stage:
                name = f"{name} (этап «{st.stage}»)"
            lines.append(
                f"| {st.num} | {_md_cell(st.role)} | {_md_cell(name)} | {_md_cell(dur)} | "
                f"{_md_join(st.artifacts)} | {_md_join(st.systems)} | {_md_cell(_step_transitions(st))} |"
            )
    elif xml_str:
        try:
            struct = parse_bpmn_structure(xml_str)
            work = sorted(
                (n for n in (struct.get("nodes") or {}).values()
                 if n.get("type") in _TASK_TAGS or n.get("type") == "subProcess"),
                key=lambda n: (float(n.get("x") or 0), float(n.get("y") or 0)),
            )
            if work:
                for i, n in enumerate(work, 1):
                    lines.append(
                        f"| {i} | {_md_cell(n.get('lane'))} | {_md_cell(n.get('name'))} | — | — | — | — |"
                    )
            else:
                lines.append("| — | — | Шаги не распознаны | — | — | — | — |")
        except Exception:  # noqa: BLE001
            lines.append("| — | — | Шаги не распознаны | — | — | — | — |")
    else:
        lines.append("| — | — | Шаги не распознаны | — | — | — | — |")

    lines += [
        "",
        "## 4. ИТ-ландшафт и документооборот",
        "",
        "### 4.1. Реестр ИТ-систем",
        "",
        "| Система | Упоминаний | Шаги | Роли |",
        "| --- | ---: | --- | --- |",
    ]
    if systems:
        for it in systems:
            item = it if isinstance(it, dict) else {"name": it}
            steps = item.get("steps") or []
            roles = item.get("roles") or []
            lines.append(
                f"| {_md_cell(item.get('name'))} | {int(item.get('mentions') or 0)} | "
                f"{_md_cell(', '.join(str(s) for s in steps) if steps else '—')} | {_md_join(roles)} |"
            )
    else:
        lines.append("| — | 0 | — | системы в регламенте не обнаружены |")

    lines += [
        "",
        "### 4.2. Реестр документов и артефактов",
        "",
        "| Документ | Упоминаний | Шаги | Роли |",
        "| --- | ---: | --- | --- |",
    ]
    if artifacts:
        for it in artifacts:
            item = it if isinstance(it, dict) else {"name": it}
            steps = item.get("steps") or []
            roles = item.get("roles") or []
            lines.append(
                f"| {_md_cell(item.get('name'))} | {int(item.get('mentions') or 0)} | "
                f"{_md_cell(', '.join(str(s) for s in steps) if steps else '—')} | {_md_join(roles)} |"
            )
    else:
        lines.append("| — | 0 | — | документы в регламенте не обнаружены |")

    crit_h = sla.get("critical_path_hours")
    rework_h = sla.get("rework_hours")
    with_rw = sla.get("with_rework_hours")
    breach = sla.get("breach")
    path_txt = " → ".join(
        f"«{(p or {}).get('name') or '—'}» ({_fh(float((p or {}).get('hours') or 0))})"
        for p in path
        if (p or {}).get("name")
    ) or "не рассчитан"
    lines += [
        "",
        "## 5. Карта рисков и рекомендации",
        "",
        "### 5.1. Критический путь (алгоритм Беллмана — Форда)",
        "",
        f"- Длительность критического пути: **{_md_cell(_fh(float(crit_h)) if crit_h is not None else '—')}**",
        f"- Целевой SLA: **{_md_cell(_fh(float(target)) if target else 'не задан')}**",
        f"- Срыв SLA: **{'да' if breach else 'нет'}**",
        f"- Маршрут: {path_txt}",
        "",
        "### 5.2. Стоимость циклов возврата",
        "",
        f"- Худший одиночный возврат: **{_md_cell(_fh(float(rework_h)) if rework_h is not None else '0 мин')}**",
        f"- Срок с учётом худшего возврата: **{_md_cell(_fh(float(with_rw)) if with_rw is not None else '—')}**",
        f"- Сумма всех циклов (по одному разу): **{_md_cell(_fh(float(sla.get('rework_total_hours') or 0)))}**",
        "",
    ]
    if loops:
        lines += [
            "| Цикл | Откуда | Куда | Стоимость, ч |",
            "| --- | --- | --- | ---: |",
        ]
        for loop in loops:
            item = loop or {}
            hours = item.get("cycle_hours")
            cost = f"{float(hours):.1f}" if hours is not None else "—"
            lines.append(
                f"| {_md_cell(item.get('label'))} | {_md_cell(item.get('from'))} | "
                f"{_md_cell(item.get('to'))} | {cost} |"
            )
        lines.append("")
    else:
        lines.append("_Циклы возврата не обнаружены._")
        lines.append("")

    lines += ["### 5.3. Меры по оптимизации", ""]
    if recs:
        for rec in recs:
            lines.append(f"- {_md_cell(rec)}")
    else:
        lines.append("- Существенных узких мест не выявлено: процесс сбалансирован по ролям.")
    if xml_str:
        lines += ["", f"_Исходная BPMN-модель: {len(xml_str)} символов XML._"]
    lines.append("")
    return "\n".join(lines)


# --------------------------- инспектор задачи и каталог кликов по холсту --------------------------- #
_KIND_RU = {
    "userTask": "Пользовательская задача",
    "scriptTask": "Скриптовая / сервисная задача",
    "task": "Задача",
    "exclusiveGateway": "Исключающий шлюз (XOR)",
    "parallelGateway": "Параллельный шлюз (AND)",
    "inclusiveGateway": "Включающий шлюз (OR)",
    "startEvent": "Стартовое событие",
    "endEvent": "Завершающее событие",
    "subProcess": "Подпроцесс",
}

_ROLE_QUAL = (
    ("диспетчер", "IV–V группа по электробезопасности, право ведения оперативных переговоров и переключений."),
    ("начальник смены", "V группа по электробезопасности, ответственный руководитель работ в электроустановке."),
    ("служб безопасн", "V группа, контроль нарядов-допусков, проверка состава бригады и удостоверений."),
    ("ремонтн", "III–IV группа по электробезопасности, допуск к работам в электроустановках до и выше 1000 В."),
    ("эколог", "Профильная подготовка по охране окружающей среды; допуск к площадке — по наряду."),
    ("охран труд", "Специалист по ОТ, группа не ниже III, контроль СИЗ и инструктажа."),
    ("закуп", "Квалификация закупщика / договорной работы; ЭЦП для электронной площадки."),
    ("бухгалт", "Квалификация бухгалтера энергосбыта / генерирующей компании, доступ к 1С / ERP."),
)

INSPECT_SYSTEM = """Ты — старший мастер оперативного персонала ПАО «Интер РАО».
По задаче BPMN составь операционную карточку исполнителя. Ответь ТОЛЬКО JSON без markdown:
{
  "role_requirements": "квалификация, разряд, группа допуска по электробезопасности (II–V)",
  "procedure_steps": ["шаг 1", "шаг 2", "шаг 3", "шаг 4"],
  "safety_and_tools": "приборы, СИЗ, заземления, ИТ-системы",
  "input_trigger": "основание начать работы и входные документы",
  "output_artifact": "результат: акт, журнал, статус"
}
Только факты из контекста процесса; 3–4 конкретных технических действия; по-русски; ничего не выдумывай сверх роли и названия шага."""


def _role_requirements(role: str) -> str:
    low = (role or "").lower()
    for stem, text in _ROLE_QUAL:
        if stem in low:
            return text
    return (
        f"Исполнитель роли «{role or 'специалист'}»: профильная подготовка, "
        "группа по электробезопасности не ниже III, допуск к работам по действующему наряду."
    )


def _match_step(task_name: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    name = (task_name or "").lower()
    best: Dict[str, Any] = {}
    score = 0
    for st in ctx.get("steps") or []:
        title = str(st.get("title") or "").lower()
        s = 0
        if title and (title in name or name in title):
            s = 3
        else:
            stems = {w[:6] for w in re.findall(r"[А-Яа-яЁё]{5,}", name)}
            s = sum(1 for w in stems if w in title)
        if s > score:
            score, best = s, st
    return best


def _heuristic_inspect(task_name: str, task_role: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    name = (task_name or "Выполнить действие").strip()
    role = (task_role or "Исполнитель").strip()
    low = name.lower()
    step = _match_step(name, ctx)
    systems = list(step.get("systems") or [])
    artifacts = list(step.get("artifacts") or [])
    if not systems:
        systems = [i.get("name") for i in (ctx.get("it_systems") or []) if isinstance(i, dict) and i.get("name")]
    if not artifacts:
        artifacts = [i.get("name") for i in (ctx.get("artifacts") or []) if isinstance(i, dict) and i.get("name")]
    sys_txt = ", ".join(str(s) for s in systems[:4]) or "оперативный журнал, ИС/АС предприятия"
    doc_txt = ", ".join(str(a) for a in artifacts[:4]) or "задание смены / наряд-допуск"

    steps = [f"Получить задание и подтвердить полномочия роли «{role}»."]
    action = name[:1].lower() + name[1:] if name else "выполнить операцию по регламенту"
    steps.append(f"Выполнить: {action}.")
    if any(k in low for k in ("наряд", "допуск", "безопасн", "инструктаж")):
        steps.append("Проверить наряд-допуск, состав бригады, удостоверения и наличие СИЗ.")
        steps.append("Допустить персонал к работе или вернуть пакет документов на устранение замечаний.")
    elif any(k in low for k in ("журнал", "фиксир", "регистр", "заявк")):
        steps.append(f"Внести запись в {sys_txt}.")
        steps.append("Сверить статус заявки со смежной ролью и закрыть шаг в системе.")
    elif any(k in low for k in ("ремонт", "переключ", "вывест", "восстанов", "схем")):
        steps.append("Выполнить оперативные переключения / работы по бланку, установить переносные заземления.")
        steps.append("Подтвердить восстановление схемы и снять наряд после осмотра.")
    elif any(k in low for k in ("осмотр", "диагност", "измер", "тепловиз", "изоляц")):
        steps.append("Провести измерения приборами (тепловизор, мегаомметр) и зафиксировать дефекты.")
        steps.append("Передать дефектную ведомость следующей роли по схеме.")
    else:
        steps.append("Сверить результат с нормами ПТЭ и локальными инструкциями предприятия.")
        steps.append("Передать результат следующей роли и зафиксировать статус в учётной системе.")

    if "script" in low or "автомат" in low:
        safety = f"Автоматизированная операция. Системы: {sys_txt}. Контроль журнала событий, без работ в электроустановке."
    else:
        safety = (
            f"СИЗ: каска, термостойкий комбинезон, диэлектрические перчатки и боты (по наряду). "
            f"При работах в электроустановке — переносные заземления и запирание коммутационных аппаратов. "
            f"Системы: {sys_txt}."
        )
    trigger = (
        f"Основание: поступление шага «{name}» роли «{role}» по схеме процесса"
        + (f"; входные документы: {doc_txt}." if artifacts or doc_txt else ".")
    )
    output = (
        f"Результат шага «{name}»: статус выполнен; "
        + (f"оформлены {doc_txt}; " if artifacts else "запись в оперативном журнале; ")
        + "передача следующей роли по BPMN."
    )
    return {
        "role_requirements": _role_requirements(role),
        "procedure_steps": steps[:4],
        "safety_and_tools": safety,
        "input_trigger": trigger,
        "output_artifact": output,
        "source": "heuristic",
    }


def inspect_task_details(task_name: str, task_role: str, process_context: dict) -> dict:
    """Развёрнутая операционная инструкция исполнителя по шагу BPMN.

    Облачная модель (Groq/OpenAI) → эвристики процесса. Всегда возвращает словарь с ключами
    role_requirements, procedure_steps, safety_and_tools, input_trigger, output_artifact.
    """
    ctx = process_context if isinstance(process_context, dict) else {}
    fallback = _heuristic_inspect(task_name or "", task_role or "", ctx)
    if os.getenv("BPMN_AI_MODE", "auto").lower() == "emulator":
        return fallback
    payload = {
        "task": task_name,
        "role": task_role,
        "process": ctx.get("title"),
        "sla": ctx.get("sla"),
        "step_match": _match_step(task_name or "", ctx),
        "it_systems": ctx.get("it_systems"),
        "artifacts": ctx.get("artifacts"),
        "critical_path": [c.get("name") for c in (ctx.get("critical_path") or [])][:12],
    }
    trace: List[str] = []
    got = _chat_llm(
        [
            {"role": "system", "content": INSPECT_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:6000]},
        ],
        trace,
    )
    if not got:
        return fallback
    raw = got[1]
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0) if m else raw)
    except Exception:  # noqa: BLE001
        return fallback
    steps = data.get("procedure_steps") if isinstance(data, dict) else None
    if isinstance(steps, str):
        steps = [s.strip(" -•") for s in re.split(r"[\n;]+", steps) if s.strip()]
    if not isinstance(steps, list) or len(steps) < 2:
        steps = fallback["procedure_steps"]
    return {
        "role_requirements": str(data.get("role_requirements") or fallback["role_requirements"]),
        "procedure_steps": [str(s).strip() for s in steps[:6] if str(s).strip()],
        "safety_and_tools": str(data.get("safety_and_tools") or fallback["safety_and_tools"]),
        "input_trigger": str(data.get("input_trigger") or fallback["input_trigger"]),
        "output_artifact": str(data.get("output_artifact") or fallback["output_artifact"]),
        "source": got[0],
    }


def live_node_comment(name: str, role: str, kind: str, ctx: Dict[str, Any]) -> str:
    """Короткий комментарий ИИ для карточки клика: зачем шаг в энергосистеме и где риск задержки."""
    low = (name or "").lower()
    crit = {str(c.get("name") or "") for c in (ctx.get("critical_path") or [])}
    on_crit = name in crit or any(name and name[:18] in (c or "") for c in crit)
    bus = ctx.get("bus_factor") or {}
    top = str(bus.get("top_role") or "")
    loops = ctx.get("rework_loops") or []
    loop_hit = any(name and name[:12] in str(l.get("from", "")) + str(l.get("to", "")) for l in loops)

    if kind in _GATE_TAGS:
        why = "Развилка фиксирует решение, от которого зависит допуск, схема или возврат на доработку."
        risk = "Неподписанная или спорная ветка здесь даёт ложный маршрут и простой бригады."
    elif kind == "startEvent":
        why, risk = "Точка входа аварии или заявки в оперативный контур.", "Поздняя фиксация старта съедает весь запас SLA."
    elif kind == "endEvent":
        why, risk = "Подтверждение, что схема и документы закрыты.", "Незакрытый конец оставляет «висящий» наряд и открытый SLA."
    elif kind == "subProcess":
        why = "Декомпозиция однотипных шагов одной роли — защита от «карты метро»."
        risk = "Если внутри длинная цепочка без контроля, узкое место прячется в подпроцессе."
    elif any(k in low for k in ("наряд", "допуск")):
        why = "Наряд-допуск — юридический и технический барьер перед работой в электроустановке."
        risk = "Замечания СБ и возврат наряда — типичный цикл, который раздувает SLA."
    elif any(k in low for k in ("ремонт", "восстанов", "переключ")):
        why = "Оперативные переключения и ремонт возвращают оборудование в нормальную схему."
        risk = "Шаг на критическом пути: каждый час простоя — недоотпуск и риск каскада."
    elif any(k in low for k in ("журнал", "фиксир")):
        why = "Оперативный журнал — единый источник правды для диспетчера и смены."
        risk = "Ошибка или задержка записи ломает трассировку аварии и расследование."
    else:
        why = f"Шаг роли «{role or 'исполнитель'}» продвигает процесс по регламенту энергосистемы."
        risk = "Задержка на исполнителе с высокой долей нагрузки повышает bus-factor."
    if on_crit:
        risk = "Узел на критическом пути SLA (Беллман — Форд): задержка сдвигает весь срок восстановления."
    elif loop_hit:
        risk = "Узел связан с циклом возврата — повторный проход умножает трудозатраты."
    elif role and role == top and float(bus.get("max_share") or 0) > 0.4:
        risk = f"Роль «{role}» держит {float(bus.get('max_share') or 0):.0%} шагов — простой ключевого исполнителя останавливает процесс."
    return f"{why} {risk}"


def build_diagram_catalog(xml_str: str, audit_data: dict, regulation_text: str) -> Dict[str, Dict[str, Any]]:
    """id BPMN-элемента → карточка для клика на холсте (роль, критический путь, комментарий ИИ)."""
    ctx = build_process_context(regulation_text or "", xml_str or "", audit_data or {})
    catalog: Dict[str, Dict[str, Any]] = {}
    if not xml_str:
        return catalog
    try:
        struct = parse_bpmn_structure(xml_str)
    except Exception:  # noqa: BLE001
        return catalog
    clickable = _TASK_TAGS | _GATE_TAGS | {"startEvent", "endEvent", "subProcess"}
    for nid, node in (struct.get("nodes") or {}).items():
        kind = str(node.get("type") or "")
        if kind not in clickable:
            continue
        name = str(node.get("name") or "").strip() or kind
        role = str(node.get("lane") or "").strip()
        crit = any(
            name == str(c.get("name") or "") or nid == str(c.get("id") or "")
            for c in (ctx.get("critical_path") or [])
        )
        catalog[nid] = {
            "id": nid,
            "name": name,
            "type": _KIND_RU.get(kind, kind),
            "kind": kind,
            "role": role or "—",
            "critical": bool(crit),
            "comment": live_node_comment(name, role, kind, ctx),
        }
    return catalog


def build_canvas_copilot(
    xml_str: str,
    audit_data: dict,
    regulation_text: str,
    tobe_delta: Optional[dict] = None,
) -> Dict[str, Any]:
    """Пакет для плавающего ассистента на холсте: чипы и ответы по аудиту + сравнение As-Is/To-Be."""
    ctx = build_process_context(regulation_text or "", xml_str or "", audit_data or {}, tobe_delta=tobe_delta)
    title = str(ctx.get("title") or "Бизнес-процесс")
    try:
        sla_a = heuristic_analysis("В чём причина срыва SLA?", ctx)
        speed_a = heuristic_analysis("Как ускорить процесс?", ctx)
        roles_a = _analysis_load(ctx, [])
        compare_a = heuristic_analysis("Сравни As-Is и To-Be, до и после", ctx)
        read_a = heuristic_analysis("Оцени читаемость схемы и анти-метро", ctx)
        why_a = heuristic_analysis("Как мы сократили время? Объясни подробнее", ctx)
        qual_a = heuristic_analysis("Почему изменился Quality Score?", ctx)
        loops_a = heuristic_analysis("Циклы возврата на доработку", ctx)
        land_a = heuristic_analysis("ИТ-ландшафт и документы процесса", ctx)
        fallback = _analysis_open("краткий архитектурный разбор", ctx)
    except Exception:  # noqa: BLE001 — холст не должен падать
        sla_a = speed_a = roles_a = compare_a = read_a = why_a = qual_a = loops_a = land_a = fallback = (
            "Сгенерируйте диаграмму, чтобы ассистент опирался на аудит процесса."
        )
    chips = [
        {"id": "speed", "label": "⚡ Как ускорить?", "q": "Как ускорить процесс?", "a": speed_a},
        {"id": "sla", "label": "🔍 Анализ SLA", "q": "В чём причина срыва SLA?", "a": sla_a},
        {"id": "roles", "label": "👤 Роли и риски", "q": "Как оптимизировать нагрузку ролей?", "a": roles_a},
        {"id": "tobe", "label": "🔀 As-Is → To-Be", "q": "Сравни As-Is и To-Be", "a": compare_a},
    ]
    return {
        "title": title,
        "greeting": (
            f"Я ассистент процесса «{title}». Спросите про SLA, сравнение As-Is/To-Be, "
            "читаемость или роли — отвечаю по цифрам аудита, в том числе в панораме."
        ),
        "fallback": fallback,
        "compare": compare_a,
        "readability": read_a,
        "why": why_a,
        "quality": qual_a,
        "loops": loops_a,
        "landscape": land_a,
        "chips": chips,
    }


# --------------------------- RACI, To-Be, официальный DOCX --------------------------- #
_ACCOUNTABLE_RE = re.compile(
    r"начальник|руководител|директор|владелец|главн\w+\s+инженер|начальник смены|зам[\.\s]",
    re.I,
)
_CONSULTED_RE = re.compile(
    r"безопасност|юрист|правов|эколог|эксперт|согласующ|охрана труда|служба охраны|нормативн",
    re.I,
)
_INFORMED_RE = re.compile(r"диспетчер|заявител|заказчик|потребител|инициатор", re.I)
_JOURNAL_RE = re.compile(
    r"фикс\w+|журнал|реестр|регистр\w+|вносит\s+запис|учётн\w+\s+систем|оперативн\w+\s+журнал",
    re.I,
)
_CONTROL_HINT_RE = re.compile(r"входн\w+\s+контрол|комплектност\w+\s+документ", re.I)
_RACI_STOP = {"выполн", "провод", "оформ", "провер", "принят", "переда", "состав", "оценк"}
TOBE_SYSTEM = """Ты — ведущий бизнес-архитектор ПАО «Интер РАО».
Перепиши регламент, сохранив заголовок и целевой SLA. Правила:
1) Параллелизация: независимые шаги РАЗНЫХ ролей начинай с «Параллельно:».
2) Zero-Rework: перед шлюзами согласования добавь шаг «Роль проводит предварительный входной контроль … перед шагом N»;
   формулировки «вернуть на п.N / на доработку» замени эскалацией руководителю без повторного цикла.
   Не начинай шаг с существительного («Входной контроль…») — только роль + глагол.
3) Автоматизация: фиксацию в журналах пиши БЕЗ двоеточия после системы:
   «Информационная система автоматически регистрирует … (5 минут)».
Верни ТОЛЬКО текст регламента с нумерованными шагами, без комментариев и markdown."""


def _accountable_role(roles: List[str]) -> str:
    for role in roles or []:
        if _ACCOUNTABLE_RE.search(role or ""):
            return role
    return (roles[0] if roles else "Руководитель процесса") or "Руководитель процесса"


def _consulted_roles(roles: List[str]) -> List[str]:
    return [r for r in (roles or []) if _CONSULTED_RE.search(r or "")]


def _informed_roles(roles: List[str]) -> List[str]:
    return [r for r in (roles or []) if _INFORMED_RE.search(r or "")]


def _step_as_row(item: Any, idx: int) -> Dict[str, Any]:
    if isinstance(item, Step):
        return {
            "num": item.num,
            "title": item.title or "(шлюз решения)",
            "role": item.role or "",
            "decision": bool(item.decision),
            "hours": item.hours,
            "artifacts": list(item.artifacts or []),
            "systems": list(item.systems or []),
        }
    if isinstance(item, dict):
        title = item.get("title") or item.get("name") or item.get("action") or ""
        decision = item.get("decision")
        return {
            "num": item.get("num") if item.get("num") is not None else idx,
            "title": title or "(шаг)",
            "role": item.get("role") or item.get("lane") or item.get("executor") or "",
            "decision": bool(decision) if not isinstance(decision, dict) else True,
            "hours": item.get("hours"),
            "artifacts": list(item.get("artifacts") or []),
            "systems": list(item.get("systems") or []),
        }
    return {"num": idx, "title": str(item), "role": "", "decision": False, "hours": None, "artifacts": [], "systems": []}


def generate_raci_matrix(steps: list, roles: list) -> List[Dict[str, Any]]:
    """Матрица RACI: строки — шаги, столбцы — роли, на пересечении буквы R/A/C/I."""
    role_list = [str(r).strip() for r in (roles or []) if str(r).strip()]
    rows_in = [_step_as_row(s, i + 1) for i, s in enumerate(steps or [])]
    for row in rows_in:
        if row["role"] and row["role"] not in role_list:
            role_list.append(row["role"])
    if not role_list:
        role_list = ["Исполнитель"]
    owner = _accountable_role(role_list)
    consulted = _consulted_roles(role_list)
    informed = _informed_roles(role_list)
    matrix: List[Dict[str, Any]] = []
    last_idx = len(rows_in) - 1
    for i, row in enumerate(rows_in):
        executor = row["role"] or role_list[0]
        title = str(row["title"] or "")
        decision = bool(row["decision"]) or bool(re.search(r"соглас|допуск|утверд|экспертиз", title, re.I))
        milestone = decision or i >= last_idx - 1
        accountable = executor if _ACCOUNTABLE_RE.search(executor) else owner
        if decision:
            accountable = executor
        cells: Dict[str, List[str]] = {role: [] for role in role_list}

        def _add(role: str, letter: str) -> None:
            if role not in cells:
                cells[role] = []
            if letter not in cells[role]:
                cells[role].append(letter)

        _add(executor, "R")
        _add(accountable, "A")
        if decision or bool(re.search(r"проверк|допуск|наряд|экспертиз|правов", title, re.I)):
            for role in consulted:
                if role not in (executor, accountable):
                    _add(role, "C")
        if milestone:
            for role in informed:
                if role not in (executor, accountable):
                    _add(role, "I")
        matrix.append(
            {
                "num": row["num"],
                "title": title,
                "executor": executor,
                "assignments": cells,
            }
        )
    return matrix


def _independent_steps(a: Step, b: Step) -> bool:
    if not a.role or not b.role or a.role == b.role:
        return False
    if a.decision or b.decision or b.parallel:
        return False
    stems_a = {w[:6].lower() for w in re.findall(r"[А-Яа-яЁё]{5,}", a.title or "")}
    stems_b = {w[:6].lower() for w in re.findall(r"[А-Яа-яЁё]{5,}", b.title or "")}
    return not ((stems_a & stems_b) - _RACI_STOP)


def _automate_journal_body(body: str) -> str:
    """scriptTask: система сама выполняет действие. Заголовок должен начинаться с глагола, не с роли."""
    text = body.strip()
    if _DUR_RE.search(text):
        text = _DUR_RE.sub("(5 минут)", text, count=1)
    else:
        text = text.rstrip(" .") + " (5 минут)."

    def _action_after_role(src: str) -> str:
        role, start, end = _find_role(src)
        if role and start == 0 and end > 0:
            src = src[end:].lstrip(" :;—–-")
        return src

    action = _action_after_role(text)
    probe = (action[:1].upper() + action[1:]) if action else ""
    action = _action_after_role(probe) or action
    if not action:
        action = "фиксирует запись в журнале (5 минут)."
    action = action[:1].lower() + action[1:]
    return f"Информационная система автоматически {action}"


def _cut_rework_loop(body: str) -> str:
    """Убирает петлю «вернуть на п.N / на доработку» без слов верну/возврат/доработ/повтор."""
    text = re.sub(
        r"(?:иначе\s+)?(?:«[^»]+»\s*[—–-]\s*)?(?:вернуть|возврат(?:ить)?)\s+на\s+(?:п(?:ункт)?\.?\s*)?\d+",
        "иначе эскалация руководителю процесса",
        body,
        flags=re.I,
    )
    return re.sub(
        r"«[^»]*(?:доработ|замечан|повторн)[^»]*»\s*[—–-]\s*(?:вернуть|возврат)\s+на\s+(?:п(?:ункт)?\.?\s*)?\d+",
        "иначе эскалация руководителю процесса",
        text,
        flags=re.I,
    )


def _heuristic_optimize_to_be(regulation_text: str) -> Tuple[str, List[Dict[str, str]]]:
    raw_text = normalize_regulation(regulation_text or "")
    header, raw_steps = _split_steps(raw_text)
    try:
        parsed = parse_regulation(raw_text)
    except Exception:  # noqa: BLE001
        return raw_text, []
    n = min(len(parsed.steps), len(raw_steps))
    if n < 2:
        return raw_text, []
    actions: List[Dict[str, str]] = []
    bodies = [raw_steps[i]["body"] for i in range(n)]
    nums = [parsed.steps[i].num for i in range(n)]

    for i in range(n):
        body = bodies[i]
        if _JOURNAL_RE.search(body) and not re.search(r"систем\w+\s+автоматическ", body, re.I):
            bodies[i] = _automate_journal_body(body)
            actions.append(
                {
                    "kind": "automation",
                    "detail": f"Шаг {nums[i]} «{parsed.steps[i].title or 'фиксация'}»: scriptTask, фиксация в журнале выполняется системой.",
                }
            )

    i = 0
    while i < n - 1:
        a, b = parsed.steps[i], parsed.steps[i + 1]
        already = bool(re.match(r"^(?:параллельно|одновременно)\s*[:,—–-]?", bodies[i + 1], re.I))
        if _independent_steps(a, b) and not already:
            bodies[i + 1] = "Параллельно: " + bodies[i + 1]
            b.parallel = True
            actions.append(
                {
                    "kind": "parallel",
                    "detail": f"Шаги {a.num} ({a.role}) и {b.num} ({b.role}) выполняются параллельно.",
                }
            )
            i += 2
            continue
        i += 1

    control_at: Dict[int, str] = {}
    for i in range(n):
        st = parsed.steps[i]
        d = st.decision
        loops = bool(d and (d.no_back or (d.no_ref is not None and d.no_ref < st.num)))
        if not loops:
            continue
        bodies[i] = _cut_rework_loop(bodies[i])
        prev = bodies[i - 1] if i else ""
        if _CONTROL_HINT_RE.search(prev) or _CONTROL_HINT_RE.search(bodies[i]):
            actions.append(
                {
                    "kind": "zero_rework",
                    "detail": f"Шаг {st.num}: петля возврата снята, входной контроль уже есть.",
                }
            )
            continue
        prev_role = parsed.steps[i - 1].role if i else ""
        ctrl_role = prev_role if prev_role and prev_role != st.role else next(
            (s.role for s in parsed.steps if s.role and s.role != st.role), st.role
        )
        control_at[i] = (
            f"{ctrl_role} проводит предварительный входной контроль комплектности документов "
            f"и исходных данных перед шагом {st.num} (15 минут)."
        )
        actions.append(
            {
                "kind": "zero_rework",
                "detail": f"Перед шагом {st.num} «{st.title or 'согласование'}» введён входной контроль, цикл доработки заменён эскалацией.",
            }
        )

    assembled: List[Tuple[Optional[int], str]] = []
    for i in range(n):
        if i in control_at:
            assembled.append((None, control_at[i]))
        assembled.append((nums[i], bodies[i]))
    old_to_new: Dict[int, int] = {}
    new_bodies: List[str] = []
    for old_num, body in assembled:
        new_bodies.append(body)
        if old_num is not None:
            old_to_new[old_num] = len(new_bodies)

    def _fn(ref: int) -> int:
        return old_to_new.get(ref, ref)

    remapped = [_remap(b, _fn) for b in new_bodies]
    return _join_steps(header, [{"body": b} for b in remapped]), actions


def _collect_tobe_actions(old_text: str, new_text: str) -> List[Dict[str, str]]:
    actions: List[Dict[str, str]] = []
    _, old_s = _split_steps(normalize_regulation(old_text))
    _, new_s = _split_steps(normalize_regulation(new_text))
    old_par = sum(1 for s in old_s if re.match(r"^(?:параллельно|одновременно)", s["body"], re.I))
    new_par = sum(1 for s in new_s if re.match(r"^(?:параллельно|одновременно)", s["body"], re.I))
    if new_par > old_par:
        actions.append({"kind": "parallel", "detail": f"Добавлено параллельных веток: {new_par - old_par}."})
    old_ctrl = sum(1 for s in old_s if _CONTROL_HINT_RE.search(s["body"]))
    new_ctrl = sum(1 for s in new_s if _CONTROL_HINT_RE.search(s["body"]))
    if new_ctrl > old_ctrl:
        actions.append({"kind": "zero_rework", "detail": f"Введён входной контроль ({new_ctrl - old_ctrl} шаг.). Петли возврата сокращены."})
    old_auto = sum(1 for s in old_s if re.search(r"автоматическ", s["body"], re.I))
    new_auto = sum(1 for s in new_s if re.search(r"автоматическ", s["body"], re.I))
    if new_auto > old_auto:
        actions.append({"kind": "automation", "detail": f"Автоматизировано шагов фиксации: {new_auto - old_auto}."})
    if not actions:
        actions.append({"kind": "reengine", "detail": "Целевой регламент переписан с сохранением ролей и SLA."})
    return actions


def _try_llm_optimize_to_be(regulation_text: str, audit_data: dict) -> Optional[str]:
    """Облачная перепись To-Be только при BPMN_TOBE_LLM=1 — иначе 25 с лишней генерации."""
    if os.getenv("BPMN_TOBE_LLM", "").strip().lower() not in ("1", "true", "yes"):
        return None
    ctx = build_process_context(regulation_text, "", audit_data or {})
    messages = [
        {"role": "system", "content": TOBE_SYSTEM},
        {
            "role": "user",
            "content": "АУДИТ AS-IS:\n" + format_context_for_prompt(ctx, max_steps=40)
            + "\n\nРЕГЛАМЕНТ AS-IS:\n" + (regulation_text or "").strip(),
        },
    ]
    trace: List[str] = []
    got = _chat_llm(messages, trace)
    if not got:
        return None
    _, raw = got
    new_text = _strip_markdown(raw).strip()
    _, old_steps = _split_steps(normalize_regulation(regulation_text))
    _, new_steps = _split_steps(normalize_regulation(new_text))
    if len(new_steps) < 2 or abs(len(new_steps) - len(old_steps)) > 8:
        return None
    return new_text + ("\n" if not new_text.endswith("\n") else "")


def _tobe_delta(old_audit: dict, new_audit: dict, actions: List[Dict[str, str]], engine: str) -> Dict[str, Any]:
    old_a, new_a = old_audit or {}, new_audit or {}
    old_sla = old_a.get("sla") or {}
    new_sla = new_a.get("sla") or {}
    before = float(old_sla.get("with_rework_hours") or old_sla.get("critical_path_hours") or 0)
    after = float(new_sla.get("with_rework_hours") or new_sla.get("critical_path_hours") or 0)
    saved = max(0.0, before - after)
    pct = round(100.0 * saved / before, 1) if before and saved > 0 else 0.0
    loops_b = len(old_a.get("rework_loops") or [])
    loops_a = len(new_a.get("rework_loops") or [])
    q_b = int((old_a.get("methodology") or {}).get("score") or 0)
    q_a = int((new_a.get("methodology") or {}).get("score") or 0)
    rw_h_b = float(old_sla.get("rework_hours") or 0)
    rw_h_a = float(new_sla.get("rework_hours") or 0)
    return {
        "sla_before_hours": round(before, 3),
        "sla_after_hours": round(after, 3),
        "sla_saved_hours": round(saved, 3),
        "sla_saved_pct": pct,
        "rework_before": loops_b,
        "rework_after": loops_a,
        "rework_removed": max(0, loops_b - loops_a),
        "rework_hours_before": round(rw_h_b, 3),
        "rework_hours_after": round(rw_h_a, 3),
        "quality_before": q_b,
        "quality_after": q_a,
        "quality_gain": max(0, q_a - q_b),
        "breach_before": bool(old_sla.get("breach")),
        "breach_after": bool(new_sla.get("breach")),
        "actions": actions,
        "engine": engine,
    }


def optimize_process_to_be(regulation_text: str, audit_data: dict) -> Tuple[str, dict]:
    """Реинжиниринг As-Is → To-Be: параллелизация, Zero-Rework, автоматизация журналов.

    По умолчанию — детерминированный семантический оптимизатор (~0.05 с).
    Облачная LLM включается только переменной BPMN_TOBE_LLM=1.
    """
    text = (regulation_text or "").strip()
    empty = {
        "sla_before_hours": 0.0, "sla_after_hours": 0.0, "sla_saved_hours": 0.0, "sla_saved_pct": 0.0,
        "rework_before": 0, "rework_after": 0, "rework_removed": 0,
        "rework_hours_before": 0.0, "rework_hours_after": 0.0,
        "quality_before": 0, "quality_after": 0, "quality_gain": 0,
        "breach_before": False, "breach_after": False, "actions": [], "engine": "semantic-optimizer",
        "tobe_xml": "", "tobe_audit": {}, "tobe_error": "",
    }
    if len(text) < 20:
        return text, empty
    engine = "semantic-optimizer"
    optimized = None
    llm_text = None
    try:
        llm_text = _try_llm_optimize_to_be(text, audit_data or {})
    except Exception:  # noqa: BLE001
        llm_text = None
    if llm_text:
        optimized, engine = llm_text, "llm"
        actions = _collect_tobe_actions(text, optimized)
    else:
        optimized, actions = _heuristic_optimize_to_be(text)
        if not actions:
            actions = [{"kind": "stable", "detail": "Существенных узких мест для автоматического реинжиниринга не найдено."}]
    xml, new_audit, err = "", {}, ""
    try:
        xml, new_audit, err = _rebuild_process(optimized, f"to-be · {engine}", [])
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    delta = _tobe_delta(audit_data or {}, new_audit or {}, actions, engine)
    delta["tobe_xml"] = xml or ""
    delta["tobe_audit"] = new_audit or {}
    delta["tobe_error"] = err or ""
    return optimized, delta


def _docx_shade(cell: Any, fill: str) -> None:
    from docx.oxml.ns import nsdecls
    from docx.oxml import parse_xml

    tc_pr = cell._tc.get_or_add_tcPr()
    tc_pr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill}" w:val="clear"/>'))


def _docx_set_cell(cell: Any, text: str, *, header: bool = False, center: bool = False) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    cell.text = ""
    p = cell.paragraphs[0]
    if center or header:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(str(text if text not in (None, "") else "—"))
    run.font.size = Pt(9 if not header else 9)
    run.font.name = "Calibri"
    run.bold = header
    if header:
        run.font.color.rgb = RGBColor(255, 255, 255)
        _docx_shade(cell, "003366")


def export_docx_passport(xml_str: str, audit_data: dict, regulation_text: str) -> bytes:
    """Официальный регламент Microsoft Word: паспорт, RACI, пошаговый порядок, карта рисков."""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt, RGBColor
    except ImportError as exc:
        raise RuntimeError("Для экспорта .docx установите пакет python-docx>=1.0.0") from exc

    audit = audit_data if isinstance(audit_data, dict) else {}
    text = regulation_text or ""
    try:
        parsed = parse_regulation(normalize_regulation(text)) if text.strip() else ParsedRegulation(
            title="Бизнес-процесс", sla_hours=None
        )
    except Exception:  # noqa: BLE001
        parsed = ParsedRegulation(title="Бизнес-процесс", sla_hours=None)

    sla = audit.get("sla") or {}
    stats = audit.get("stats") or {}
    bus = audit.get("bus_factor") or {}
    loops = audit.get("rework_loops") or []
    recs = audit.get("recommendations") or []
    target = sla.get("target_hours") if sla.get("target_hours") is not None else parsed.sla_hours
    owner = _process_owner(text, parsed, audit)
    title = parsed.title or "Бизнес-процесс по регламенту"
    roles = list(parsed.roles or [x.get("role") for x in (audit.get("lane_load") or []) if x.get("role")])
    raci = generate_raci_matrix(list(parsed.steps), roles)

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(1.6)
    hp = section.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.LEFT
    hr = hp.add_run("ПАО «Интер РАО»  ·  Дирекция бизнес-архитектуры  ·  официальный регламент процесса")
    hr.bold = True
    hr.font.size = Pt(9)
    hr.font.color.rgb = RGBColor(0x00, 0x33, 0x66)
    hr.font.name = "Calibri"
    fp = section.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    fr = fp.add_run("Архитектор BPMN-диаграмм  ·  документ сформирован автоматически  ·  конфиденциально")
    fr.font.size = Pt(8)
    fr.font.color.rgb = RGBColor(0x45, 0x5A, 0x64)
    fr.font.name = "Calibri"

    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r = h.add_run("Регламент бизнес-процесса")
    r.bold = True
    r.font.size = Pt(18)
    r.font.color.rgb = RGBColor(0x00, 0x33, 0x66)
    r.font.name = "Calibri"
    sub = doc.add_paragraph()
    sr = sub.add_run(title)
    sr.bold = True
    sr.font.size = Pt(13)
    sr.font.color.rgb = RGBColor(0x15, 0x65, 0xC0)
    sr.font.name = "Calibri"

    def heading(label: str) -> None:
        p = doc.add_paragraph()
        run = p.add_run(label)
        run.bold = True
        run.font.size = Pt(13)
        run.font.color.rgb = RGBColor(0x00, 0x33, 0x66)
        run.font.name = "Calibri"

    heading("1. Паспорт процесса")
    pass_rows = [
        ("Наименование", title),
        ("Владелец процесса", owner),
        ("Целевой SLA", _fh(float(target)) if target else "не задан"),
        ("Критический путь", _fh(float(sla["critical_path_hours"])) if sla.get("critical_path_hours") is not None else "—"),
        ("Срок с учётом возврата", _fh(float(sla["with_rework_hours"])) if sla.get("with_rework_hours") is not None else "—"),
        ("Срыв SLA", "да" if sla.get("breach") else "нет"),
        ("Bus-factor", f"{float(bus.get('max_share') or 0):.0%} · {bus.get('top_role') or '—'}"),
        ("Узлов / ролей / подпроцессов", f"{stats.get('nodes', '—')} / {stats.get('lanes', len(roles))} / {stats.get('subprocesses', '—')}"),
        ("Quality Score", f"{int((audit.get('methodology') or {}).get('score') or 0)}%"),
    ]
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    _docx_set_cell(table.rows[0].cells[0], "Параметр", header=True)
    _docx_set_cell(table.rows[0].cells[1], "Значение", header=True)
    for k, v in pass_rows:
        row = table.add_row().cells
        _docx_set_cell(row[0], k)
        _docx_set_cell(row[1], v)

    heading("2. Матрица ответственности (RACI)")
    legend = doc.add_paragraph()
    lg = legend.add_run("R — Responsible (исполнитель) · A — Accountable (итог) · C — Consulted (эксперт) · I — Informed (уведомление)")
    lg.font.size = Pt(8)
    lg.font.color.rgb = RGBColor(0x45, 0x5A, 0x64)
    cols = ["№", "Шаг", *roles] if roles else ["№", "Шаг"]
    rtable = doc.add_table(rows=1, cols=len(cols))
    rtable.style = "Table Grid"
    for i, name in enumerate(cols):
        _docx_set_cell(rtable.rows[0].cells[i], name, header=True, center=True)
    for item in raci:
        cells = rtable.add_row().cells
        _docx_set_cell(cells[0], str(item.get("num") or ""), center=True)
        _docx_set_cell(cells[1], str(item.get("title") or "")[:80])
        assigns = item.get("assignments") or {}
        for j, role in enumerate(roles):
            letters = "".join(assigns.get(role) or [])
            _docx_set_cell(cells[j + 2], letters or "—", center=True)

    heading("3. Пошаговый операционный регламент")
    ot = doc.add_table(rows=1, cols=6)
    ot.style = "Table Grid"
    for i, name in enumerate(["№", "Роль", "Действие", "Срок", "Системы", "Документы"]):
        _docx_set_cell(ot.rows[0].cells[i], name, header=True)
    if parsed.steps:
        for st in parsed.steps:
            row = ot.add_row().cells
            _docx_set_cell(row[0], str(st.num), center=True)
            _docx_set_cell(row[1], st.role)
            name = st.title or "(шлюз решения)"
            if st.stage:
                name = f"{name} (этап «{st.stage}»)"
            _docx_set_cell(row[2], name)
            _docx_set_cell(row[3], _fh(float(st.hours)) if st.hours else "—", center=True)
            _docx_set_cell(row[4], ", ".join(st.systems) or "—")
            _docx_set_cell(row[5], ", ".join(st.artifacts) or "—")
    else:
        row = ot.add_row().cells
        _docx_set_cell(row[0], "—")
        _docx_set_cell(row[1], "—")
        _docx_set_cell(row[2], "Шаги не распознаны")
        _docx_set_cell(row[3], "—")
        _docx_set_cell(row[4], "—")
        _docx_set_cell(row[5], "—")

    heading("4. Карта рисков и план мероприятий по оптимизации")
    risk_p = doc.add_paragraph()
    rp = risk_p.add_run(
        f"Циклов возврата: {len(loops)}. "
        f"Худший возврат: {_fh(float(sla.get('rework_hours') or 0))}. "
        f"Рекомендации движка аудита:"
    )
    rp.font.size = Pt(10)
    rp.font.name = "Calibri"
    if loops:
        lt = doc.add_table(rows=1, cols=4)
        lt.style = "Table Grid"
        for i, name in enumerate(["Цикл", "Откуда", "Куда", "Стоимость"]):
            _docx_set_cell(lt.rows[0].cells[i], name, header=True)
        for loop in loops:
            row = lt.add_row().cells
            _docx_set_cell(row[0], str(loop.get("label") or ""))
            _docx_set_cell(row[1], str(loop.get("from") or ""))
            _docx_set_cell(row[2], str(loop.get("to") or ""))
            _docx_set_cell(row[3], _fh(float(loop.get("cycle_hours") or 0)))
    if recs:
        for rec in recs:
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(str(rec))
            run.font.size = Pt(10)
            run.font.name = "Calibri"
    else:
        p = doc.add_paragraph()
        run = p.add_run("Существенных узких мест не выявлено: процесс сбалансирован по ролям.")
        run.font.size = Pt(10)
        run.font.name = "Calibri"
    note = doc.add_paragraph()
    nr = note.add_run(
        "План To-Be: параллелить независимые шаги разных ролей, ввести входной контроль перед согласованиями, "
        "автоматизировать фиксацию в журналах (scriptTask). Целевая схема строится во вкладке «Оптимизация As-Is → To-Be»."
    )
    nr.italic = True
    nr.font.size = Pt(9)
    nr.font.color.rgb = RGBColor(0x15, 0x65, 0xC0)
    nr.font.name = "Calibri"
    if xml_str:
        meta = doc.add_paragraph()
        mr = meta.add_run(f"Исходная BPMN-модель: {len(xml_str)} символов XML.")
        mr.font.size = Pt(8)
        mr.font.color.rgb = RGBColor(0x78, 0x90, 0x9C)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

