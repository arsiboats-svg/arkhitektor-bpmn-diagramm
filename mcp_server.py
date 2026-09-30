#!/usr/bin/env python3
"""MCP-сервер «Архитектор BPMN» — ПАО «Интер РАО».

Легковесный JSON-RPC 2.0 по Model Context Protocol (stdio, 2024-11-05).
Подключается к корпоративным ИИ-агентам как внешний инструмент: генерация BPMN,
аудит узких мест, ИТ-ландшафт и документооборот.

Запуск (автономно):

    python3 mcp_server.py

Клиент (Cursor / Claude Desktop / внутренний агент Интер РАО) читает и пишет
JSON-RPC кадры в stdin/stdout. Журнал — только в stderr.

Инструменты:
    generate_bpmn(regulation_text)              → BPMN XML + Audit JSON
    analyze_process_bottlenecks(regulation_text_or_code) → SLA, bus-factor, риски
    get_process_it_landscape(regulation_text)   → системы и артефакты
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Dict, List, Optional

from ai_generator import (
    execute_generated_code,
    extract_artifacts,
    extract_it_systems,
    generate_bpmn_from_text,
    parse_regulation,
)

PROTOCOL = "2024-11-05"
SERVER_NAME = "inter-rao-bpmn-architect"
SERVER_VERSION = "1.0.0"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "generate_bpmn",
        "description": (
            "Превращает текст отраслевого регламента на русском языке в валидный BPMN 2.0.2 XML "
            "и аудит бизнес-архитектуры (bus-factor, критический путь SLA, циклы возврата, "
            "ИТ-системы и документы). Результат открывается на demo.bpmn.io без ручной доводки."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "regulation_text": {
                    "type": "string",
                    "description": "Полный текст регламента (нумерованные шаги, роли, условия «Если … — перейти к п.N»).",
                },
                "use_llm": {
                    "type": "boolean",
                    "description": "Вызывать облачную/локальную LLM (по умолчанию true). При недоступности — эмулятор.",
                    "default": True,
                },
            },
            "required": ["regulation_text"],
        },
    },
    {
        "name": "analyze_process_bottlenecks",
        "description": (
            "Аудит узких мест процесса: bus-factor (порог 45%), критический путь SLA "
            "(алгоритм Беллмана — Форда), циклы возврата, тупики, рекомендации. "
            "На вход — текст регламента ИЛИ Python-код для DIAGRAM (песочница жюри)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "regulation_text_or_code": {
                    "type": "string",
                    "description": "Текст регламента или Python-код, вызывающий методы DIAGRAM.",
                }
            },
            "required": ["regulation_text_or_code"],
        },
    },
    {
        "name": "get_process_it_landscape",
        "description": (
            "Извлекает из регламента ИТ-системы (АСУ ТП / SCADA, оперативный журнал, CRM, "
            "электронная площадка, биллинг, 1С / SAP / ERP, СЭД) и документы процесса "
            "(наряд-допуск, дефектная ведомость, технические условия, договор, заявка, акт) "
            "с номерами шагов и ролями."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "regulation_text": {
                    "type": "string",
                    "description": "Текст регламента на русском языке.",
                }
            },
            "required": ["regulation_text"],
        },
    },
]


def _ok(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _text(payload: Any, title: str = "") -> Dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    text = f"{title}\n\n{body}" if title else body
    return {"content": [{"type": "text", "text": text}]}


def _looks_like_diagram_code(src: str) -> bool:
    return "DIAGRAM." in src and any(
        token in src for token in ("add_pool", "add_task", "add_user_task", "add_link", "create_subprocess")
    )


def tool_generate_bpmn(args: Dict[str, Any]) -> Dict[str, Any]:
    text = str(args.get("regulation_text") or "").strip()
    use_llm = bool(args.get("use_llm", True))
    xml, audit, error = generate_bpmn_from_text(text, use_llm=use_llm)
    if error:
        return _text({"ok": False, "error": error})
    return _text(
        {
            "ok": True,
            "bpmn_xml": xml,
            "audit": {k: v for k, v in audit.items() if k != "generation"},
            "generation": audit.get("generation", {}),
        },
        title="BPMN 2.0 XML и аудит бизнес-архитектуры",
    )


def tool_analyze_bottlenecks(args: Dict[str, Any]) -> Dict[str, Any]:
    src = str(args.get("regulation_text_or_code") or "").strip()
    if _looks_like_diagram_code(src):
        xml, audit, error = execute_generated_code(src)
    else:
        xml, audit, error = generate_bpmn_from_text(src, use_llm=False)
    if error:
        return _text({"ok": False, "error": error})
    payload = {
        "ok": True,
        "bus_factor": audit.get("bus_factor"),
        "sla": audit.get("sla"),
        "sla_risks": audit.get("sla_risks"),
        "rework_loops": audit.get("rework_loops"),
        "dead_ends": audit.get("dead_ends"),
        "lane_load": audit.get("lane_load"),
        "critical_path": audit.get("critical_path"),
        "recommendations": audit.get("recommendations"),
        "stats": audit.get("stats"),
        "xsd_valid": audit.get("xsd_valid"),
        "xml_bytes": len(xml or ""),
    }
    return _text(payload, title="Узкие места процесса (SLA, bus-factor, риски)")


def tool_it_landscape(args: Dict[str, Any]) -> Dict[str, Any]:
    text = str(args.get("regulation_text") or "").strip()
    try:
        parsed = parse_regulation(text)
        systems, artifacts = parsed.it_systems, parsed.artifacts
    except Exception:  # noqa: BLE001
        systems = [{"name": n, "mentions": 1, "steps": [], "roles": []} for n in extract_it_systems(text)]
        artifacts = [{"name": n, "mentions": 1, "steps": [], "roles": []} for n in extract_artifacts(text)]
    return _text(
        {"ok": True, "it_systems": systems, "artifacts": artifacts},
        title="ИТ-ландшафт и документооборот процесса",
    )


HANDLERS = {
    "generate_bpmn": tool_generate_bpmn,
    "analyze_process_bottlenecks": tool_analyze_bottlenecks,
    "get_process_it_landscape": tool_it_landscape,
}


def handle(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(
            req_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _ok(req_id, {})
    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _err(req_id, -32601, f"Неизвестный инструмент: {name}")
        try:
            return _ok(req_id, handler(args if isinstance(args, dict) else {}))
        except Exception as exc:  # noqa: BLE001 — RPC не должен ронять сервер
            traceback.print_exc(file=sys.stderr)
            return _ok(req_id, {"content": [{"type": "text", "text": f"Ошибка инструмента: {type(exc).__name__}: {exc}"}], "isError": True})
    if method in ("resources/list", "prompts/list"):
        return _ok(req_id, {method.split("/")[0]: []})
    if req_id is None:
        return None
    return _err(req_id, -32601, f"Метод не поддерживается: {method}")


def main() -> int:
    sys.stderr.write(
        f"{SERVER_NAME} v{SERVER_VERSION} · MCP {PROTOCOL} · stdin/stdout JSON-RPC\n"
        "Инструменты: generate_bpmn, analyze_process_bottlenecks, get_process_it_landscape\n"
    )
    sys.stderr.flush()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps(_err(None, -32700, f"Некорректный JSON: {exc}"), ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        reply = handle(req)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
