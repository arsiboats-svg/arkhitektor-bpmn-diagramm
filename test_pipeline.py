#!/usr/bin/env python3
"""Демонстрационный конвейер: аварийный ремонт на подстанции ПАО «Интер РАО»."""

from __future__ import annotations

import json
from pathlib import Path

from bpmn_framework import BPMNDiagramBuilder

ROOT_PROCESS_ID = "Process_EmergencyRepair"
ROOT_START_TASK_ID = "Event_RootStart"
ROOT_END_TASK_ID = "Event_RootEnd"

OUTPUT_PATH = Path(__file__).resolve().parent / "demo_process.bpmn"


def build_emergency_repair() -> BPMNDiagramBuilder:
    DIAGRAM = BPMNDiagramBuilder(
        process_name="Аварийный ремонт оборудования на подстанции с согласованием наряда-допуска",
        process_id=ROOT_PROCESS_ID,
    )

    pool_id, lanes = DIAGRAM.add_pool(
        ROOT_PROCESS_ID,
        [
            "Диспетчер",
            "Начальник смены",
            "Ремонтная бригада",
            "Служба безопасности",
        ],
    )
    lane_dispatcher, lane_shift, lane_crew, lane_safety = lanes
    DIAGRAM.pools[pool_id].name = "ПАО «Интер РАО» — оперативно-диспетчерское управление подстанцией"

    DIAGRAM.add_start_event(
        "Сигнал аварии на подстанции", lane_dispatcher, node_id=ROOT_START_TASK_ID
    )

    t_accept = DIAGRAM.add_user_task("Принять и классифицировать аварийное событие", lane_dispatcher)
    t_scope = DIAGRAM.add_user_task("Оценить масштаб повреждения и зону отключения", lane_shift)
    g_permit = DIAGRAM.add_exclusive_gateway("Требуется наряд-допуск?", lane_shift)

    safety_group = DIAGRAM.add_group("Контур допуска к работам", lane_safety)
    t_permit = DIAGRAM.add_user_task("Проверить наряд-допуск и состав бригады", safety_group)
    g_decision = DIAGRAM.add_exclusive_gateway("Решение по допуску", lane_safety)
    t_remarks = DIAGRAM.add_user_task("Устранить замечания в наряде-допуске", lane_dispatcher)

    sub_id = DIAGRAM.create_subprocess("Комплексная диагностика повреждений", lane_crew)
    inner_start = DIAGRAM.subprocess_entry(sub_id)
    inner_end = DIAGRAM.subprocess_exit(sub_id)
    t_visual = DIAGRAM.add_user_task("Визуальный осмотр ячейки и ошиновки", sub_id)
    t_thermo = DIAGRAM.add_script_task("Тепловизионный контроль контактных соединений", sub_id)
    t_insul = DIAGRAM.add_user_task("Проверка изоляции и цепей вторичной коммутации", sub_id)
    g_inner = DIAGRAM.add_exclusive_gateway("Повреждение локализовано?", sub_id)

    g_split = DIAGRAM.add_parallel_gateway("Подготовка фронта работ", lane_crew)
    t_isolate = DIAGRAM.add_user_task("Вывести оборудование в ремонт (оперативные переключения)", lane_dispatcher)
    t_tools = DIAGRAM.add_task("Подготовить СИЗ, инструмент и заземления", lane_crew)
    g_join = DIAGRAM.add_parallel_gateway("Фронт работ готов", lane_crew)

    t_repair = DIAGRAM.add_user_task("Выполнить аварийный ремонт оборудования", lane_crew)
    t_accept_work = DIAGRAM.add_user_task("Принять работы и восстановить схему", lane_shift)
    t_log = DIAGRAM.add_script_task("Зафиксировать событие в оперативном журнале", lane_dispatcher)

    DIAGRAM.add_end_event(
        "Подстанция в нормальном режиме", lane_dispatcher, node_id=ROOT_END_TASK_ID
    )

    DIAGRAM.add_link(ROOT_START_TASK_ID, t_accept)
    DIAGRAM.add_link(t_accept, t_scope)
    DIAGRAM.add_link(t_scope, g_permit)
    DIAGRAM.add_link(g_permit, t_permit, "Требуется допуск")
    DIAGRAM.add_link(g_permit, g_split, "Работы без наряда (исключение)")
    DIAGRAM.add_link(t_permit, g_decision)
    DIAGRAM.add_link(g_decision, t_remarks, "Замечания")
    DIAGRAM.add_link(t_remarks, t_permit)
    DIAGRAM.add_link(g_decision, sub_id, "Допуск выдан")

    DIAGRAM.add_link(inner_start, t_visual)
    DIAGRAM.add_link(t_visual, t_thermo)
    DIAGRAM.add_link(t_thermo, t_insul)
    DIAGRAM.add_link(t_insul, g_inner)
    DIAGRAM.add_link(g_inner, t_visual, "Требуется повторный осмотр")
    DIAGRAM.add_link(g_inner, inner_end, "Повреждение подтверждено")

    DIAGRAM.add_link(sub_id, g_split)
    DIAGRAM.add_link(g_split, t_isolate)
    DIAGRAM.add_link(g_split, t_tools)
    DIAGRAM.add_link(t_isolate, g_join)
    DIAGRAM.add_link(t_tools, g_join)
    DIAGRAM.add_link(g_join, t_repair)
    DIAGRAM.add_link(t_repair, t_accept_work)
    DIAGRAM.add_link(t_accept_work, t_log)
    DIAGRAM.add_link(t_log, ROOT_END_TASK_ID)

    # Self-healing: несуществующие ID не роняют песочницу жюри.
    DIAGRAM.add_link("missing_source", t_accept)
    DIAGRAM.add_link(t_log, "missing_target", "SLA")

    return DIAGRAM


def main() -> None:
    diagram = build_emergency_repair()
    xml = diagram.to_bpmn_xml(ROOT_PROCESS_ID, ROOT_START_TASK_ID, ROOT_END_TASK_ID)
    OUTPUT_PATH.write_text(xml, encoding="utf-8")
    audit = diagram.analyze_bottlenecks()

    print(f"BPMN сохранён: {OUTPUT_PATH}")
    print(f"Узлов: {audit['stats']['nodes']}, связей: {audit['stats']['valid_links']}, дорожек: {audit['stats']['lanes']}")
    print("\n=== Аудит узких мест (ПАО «Интер РАО») ===")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
