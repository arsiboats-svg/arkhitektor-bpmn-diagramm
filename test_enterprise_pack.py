#!/usr/bin/env python3
"""Проверка пакета As-Is/To-Be, RACI и экспорта DOCX."""

from __future__ import annotations

import re
from pathlib import Path

from ai_generator import (
    emulate_generation,
    execute_generated_code,
    export_docx_passport,
    generate_bpmn_from_text,
    generate_raci_matrix,
    heuristic_analysis,
    build_process_context,
    optimize_process_to_be,
    parse_regulation,
    read_docx_regulation,
    _build_blocks,
)


def main() -> None:
    text = (Path(__file__).resolve().parent / "examples" / "example_1_substation_repair.txt").read_text(
        encoding="utf-8"
    )
    xml, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert audit["sla"]["critical_path_hours"] == 9.7
    assert audit["sla"]["with_rework_hours"] == 12.55
    assert len(audit["rework_loops"]) == 2
    assert audit["methodology"]["score"] == 100
    assert 'name="СИЗ"' not in xml
    assert xml.strip().startswith("<?xml") or "<bpmn" in xml or "<definitions" in xml
    xor_ids = set(re.findall(r'<bpmn:exclusiveGateway\b[^>]*\bid="([^"]+)"', xml))
    other_ids = set(re.findall(r'<bpmn:(?:parallelGateway|inclusiveGateway)\b[^>]*\bid="([^"]+)"', xml))
    for sid in xor_ids:
        m = re.search(rf'<bpmndi:BPMNShape\b[^>]*bpmnElement="{re.escape(sid)}"[^>]*>', xml)
        assert m and 'isMarkerVisible="true"' in m.group(0), sid
    for sid in other_ids:
        m = re.search(rf'<bpmndi:BPMNShape\b[^>]*bpmnElement="{re.escape(sid)}"[^>]*>', xml)
        assert m and "isMarkerVisible" not in m.group(0), sid
    assert not re.search(r"<bpmn:exclusiveGateway\b[^>]*isMarkerVisible", xml)
    assert not re.search(r"<bpmn:(?:parallelGateway|inclusiveGateway)\b[^>]*isMarkerVisible", xml)

    opt, delta = optimize_process_to_be(text, audit)
    ppe_re = re.compile(r"(?:подготов|готов\w*).{0,80}(?:сиз|инструмент|переносн\w+\s+заземлен)", re.I)
    ot_hard = re.compile(
        r"наряд[\s-]*допуск|инструктаж|проверк\w+\s+отсутств\w+\s+напряжен|"
        r"установ\w+\s+заземлен|налож\w+\s+заземлен|включ\w+\s+заземляющ",
        re.I,
    )
    ppe_parallel = False
    repair_after_permit = False
    permit_seen = False
    brief_seen = False
    for line in opt.splitlines():
        if ppe_re.search(line) and re.search(r"параллельно|одновременно", line, re.I):
            ppe_parallel = True
        if re.search(r"параллельно|одновременно", line, re.I) and ot_hard.search(line) and not ppe_re.search(line):
            raise AssertionError(f"запрещено распараллеливать охрану труда: {line}")
        if re.search(r"наряд[\s-]*допуск|\bдопуск", line, re.I) and not ppe_re.search(line):
            permit_seen = True
        if re.search(r"инструктаж", line, re.I):
            brief_seen = True
        if re.search(r"аварийн\w+\s+ремонт|выполн\w+.{0,40}ремонт", line, re.I):
            assert not re.search(r"^\s*\d+\.\s*(?:параллельно|одновременно)", line, re.I), line
            repair_after_permit = permit_seen and brief_seen
    assert ppe_parallel, opt
    assert repair_after_permit, opt
    assert float(delta.get("sla_after_hours") or 99) <= float(delta.get("sla_before_hours") or 0) + 1.0 / 60.0
    assert float(delta.get("with_rework_after") or 99) < float(delta.get("with_rework_before") or 0)
    assert int(delta.get("rework_after") if delta.get("rework_after") is not None else 1) == 0
    assert int(delta.get("quality_after") or 0) >= int(delta.get("quality_before") or 0)
    assert any(a.get("kind") in ("zero_rework", "automation", "parallel", "safety_seq") for a in delta.get("actions") or [])
    assert "sla_saved_hours" in delta
    assert delta.get("tobe_xml") or not delta.get("tobe_error")
    assert int(delta.get("rework_after") or 0) <= int(delta.get("rework_before") or 0)

    parsed = parse_regulation(text)
    ppe = next(s for s in parsed.steps if s.num == 12)
    assert ppe.title == "Подготовить СИЗ, инструмент и переносные заземления"
    assert ppe.role == "Ремонтная бригада"

    import app as bpmn_app

    opened = bpmn_app.prepare_regulation(text, use_llm=False)
    opened_facts = opened["facts"]
    assert not opened["error"], opened["error"]
    assert opened_facts["cp_before"] == 9.7
    assert opened_facts["rw_before"] == 12.6
    assert opened_facts["cp_after"] <= 9.6
    assert opened_facts["rw_after"] <= 9.6
    assert opened_facts["loops_before"] == 2 and opened_facts["loops_after"] == 0
    assert opened_facts["quality_before"] == 100 and opened_facts["quality_after"] == 100
    headline = bpmn_app.tobe_card_headline(opened_facts)
    assert headline == "Экономия пути с возвратами: 12.6 ч → 9.6 ч (−24%)", headline
    assert "Без ускорения" not in headline
    assert "узких мест" not in " ".join(str(a.get("detail") or "") for a in opened["delta"].get("actions") or [])
    assert opened["tobe_ok"] and opened_facts.get("show_steps")
    opened_ppe = next(s for s in parse_regulation(opened["asis_text"]).steps if s.num == 12)
    assert opened_ppe.title == ppe.title and opened_ppe.role == "Ремонтная бригада"

    matrix = generate_raci_matrix(parsed.steps, parsed.roles)
    assert matrix, "пустая матрица RACI"
    assert all(any("A" in (row["assignments"].get(r) or []) for r in parsed.roles) for row in matrix)
    assert all(any("R" in (row["assignments"].get(r) or []) for r in parsed.roles) for row in matrix)
    letters = {lt for row in matrix for vals in row["assignments"].values() for lt in vals}
    assert letters <= {"R", "A", "C", "I"}

    payload = export_docx_passport(xml, audit, text)
    assert payload[:2] == b"PK", "DOCX должен быть ZIP/OOXML"
    assert len(payload) > 2000

    grid = (Path(__file__).resolve().parent / "examples" / "example_3_grid_connection.txt").read_text(
        encoding="utf-8"
    )
    parsed_g = parse_regulation(grid)
    code, _ = emulate_generation(grid)
    code_no_sla = "\n".join(l for l in code.splitlines() if "set_sla" not in l)
    _, audit_g, err_g = execute_generated_code(
        code_no_sla, parsed_g.title, parsed_g.sla_hours, regulation_text=grid
    )
    assert not err_g
    assert float(audit_g["sla"]["critical_path_hours"]) >= 350
    _, delta_g = optimize_process_to_be(grid, audit_g)
    assert delta_g.get("engine") == "semantic-optimizer"
    saved = float(delta_g.get("sla_saved_hours") or 0)
    before = float(delta_g["sla_before_hours"])
    after = float(delta_g["sla_after_hours"])
    assert abs(saved - (before - after)) < 1e-6, (saved, before, after)
    assert before > after
    xml_g, audit_g_full, err_g_full = generate_bpmn_from_text(grid, use_llm=False)
    assert not err_g_full, err_g_full
    assert audit_g_full["sla"]["critical_path_hours"] == 400.3
    assert audit_g_full["sla"]["with_rework_hours"] == 584.4
    assert audit_g_full["methodology"]["score"] == 100

    proc = (Path(__file__).resolve().parent / "examples" / "example_2_equipment_procurement.txt").read_text(
        encoding="utf-8"
    )
    xml_p, audit_p, err_p = generate_bpmn_from_text(proc, use_llm=False)
    assert not err_p
    opt_p, delta_p = optimize_process_to_be(proc, audit_p)
    q_after = int(delta_p.get("quality_after") or 0)
    q_before = int(delta_p.get("quality_before") or 0)
    assert q_before == 100, q_before
    assert q_after == 100, (q_after, (delta_p.get("tobe_audit") or {}).get("methodology"))
    assert audit_p["sla"]["critical_path_hours"] == 528.3
    assert audit_p["sla"]["with_rework_hours"] == 856.5
    assert "автоматически:" not in opt_p.lower()
    assert "проводит предварительный входной контроль" in opt_p or "входной контроль" in opt_p.lower()
    ctx_p = build_process_context(proc, xml_p, audit_p, tobe_delta=delta_p)
    why = heuristic_analysis("Как мы сократили время? Объясни подробнее", ctx_p)
    assert "Декомпозиция" in why
    assert "петл" in why.lower() or "возврат" in why.lower()
    assert "Параллелизац" in why or "параллел" in why.lower()
    assert "scriptTask" in why or "Автоматизац" in why
    qual = heuristic_analysis("Почему упал Quality Score?", ctx_p)
    assert "100%" in qual
    assert "метро" not in qual.lower()
    read = heuristic_analysis("Оцени читаемость схемы и анти-метро", ctx_p)
    assert "Quality Score" in read or "читаем" in read.lower()

    from bpmn_framework import BPMNDiagramBuilder
    from test_pipeline import build_emergency_repair

    origin = (40.0, 80.0)
    path = [origin, (180.0, 80.0), (180.0, 120.0), (280.0, 120.0)]
    dummy = BPMNDiagramBuilder("fan-in")
    shifted = dummy._shift_path_end(path, "left", 10.0)
    assert shifted[0] == origin, shifted[0]
    for a, b in zip(shifted, shifted[1:]):
        assert abs(a[0] - b[0]) < 0.05 or abs(a[1] - b[1]) < 0.05, (a, b)
    two = dummy._shift_path_end([origin, (200.0, 80.0)], "left", 12.0)
    assert two[0] == origin, two
    for a, b in zip(two, two[1:]):
        assert abs(a[0] - b[0]) < 0.05 or abs(a[1] - b[1]) < 0.05, (a, b)

    diagram = build_emergency_repair()
    diagram.compute_layout()
    for link in diagram.links:
        src = diagram.nodes[link.source_id]
        x0, y0 = link.waypoints[0]
        on_src = (
            abs(x0 - src.x) < 2.0
            or abs(x0 - (src.x + src.width)) < 2.0
            or abs(y0 - src.y) < 2.0
            or abs(y0 - (src.y + src.height)) < 2.0
        )
        assert on_src, (link.id, link.waypoints[0], src.x, src.y, src.width, src.height)
        for a, b in zip(link.waypoints, link.waypoints[1:]):
            assert abs(a[0] - b[0]) < 0.05 or abs(a[1] - b[1]) < 0.05, (link.id, a, b)

    inverted = parse_regulation(
        "Регламент: Инверсия ТЭК\n"
        "1. Осмотр оборудования проводит начальник смены (30 минут).\n"
        "2. Диспетчер фиксирует результат в оперативном журнале (5 минут).\n"
    )
    assert inverted.steps[0].role == "Начальник смены", inverted.steps[0]
    title0 = (inverted.steps[0].title or "").lower()
    assert title0.startswith("провести") or title0.startswith("осмотреть"), inverted.steps[0].title
    assert "осмотр" in title0 or "осмотреть" in title0
    assert inverted.steps[1].role == "Диспетчер"

    from ai_generator import assistant_chat, canvas_copilot_reply, classify_intent, process_facts

    text1 = (Path(__file__).resolve().parent / "examples" / "example_1_substation_repair.txt").read_text(
        encoding="utf-8"
    )
    xml1, audit1, err1 = generate_bpmn_from_text(text1, use_llm=False)
    assert not err1, err1
    _, delta1 = optimize_process_to_be(text1, audit1)
    facts1 = process_facts(audit1, delta1)
    assert facts1["speedup_via_rework"]
    assert facts1["loops_before"] == 2 and facts1["loops_after"] == 0
    assert facts1["cp_after"] <= facts1["cp_before"]
    assert facts1["rw_after"] < facts1["rw_before"]
    q_tobe = "Сравни As-Is и To-Be, до и после"
    copilot_tobe = canvas_copilot_reply(q_tobe, xml1, audit1, text1, tobe_delta=delta1)
    side_tobe, _, _, _ = assistant_chat(q_tobe, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1)

    def _hours_cited(blob: str, hours: float) -> bool:
        token = f"{hours:.1f}"
        return token in blob.replace(",", ".") or token.replace(".", ",") in blob

    for blob, who in ((copilot_tobe, "copilot"), (side_tobe, "sidebar")):
        compact = blob.replace(" ", "")
        assert "2→0" in compact or "2 → 0" in blob, (who, blob)
        assert "Без ускорения" not in blob, (who, blob)
        assert _hours_cited(blob, float(facts1["rw_before"])), (who, blob, facts1["rw_before"])
        assert _hours_cited(blob, float(facts1["rw_after"])), (who, blob, facts1["rw_after"])
        assert _hours_cited(blob, float(facts1["cp_before"])), (who, blob, facts1["cp_before"])
        assert _hours_cited(blob, float(facts1["cp_after"])), (who, blob, facts1["cp_after"])
    eco_cmd = "Добавь согласование с экологами после шага 3"
    copilot_eco = canvas_copilot_reply(eco_cmd, xml1, audit1, text1, tobe_delta=delta1)
    assert copilot_eco.startswith("Команду в сайдбар:")
    assert xml1  # копайлот XML не возвращает и не меняет
    side_eco, new_text, new_xml, new_audit = assistant_chat(
        eco_cmd, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1
    )
    assert new_xml and new_xml != xml1, "сайдбар должен перестроить XML"
    assert new_text and "со службой" in new_text, new_text
    new_step = next((ln for ln in new_text.splitlines() if "со службой" in ln), "")
    assert new_step, new_text
    eco_svc = "Добавь согласование со службой экологии"
    assert classify_intent(eco_svc) == "edit"
    side_svc, svc_text, svc_xml, _ = assistant_chat(
        eco_svc, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1
    )
    assert svc_xml and svc_text and svc_text != text1
    assert "со службой" in svc_text

    audit_q = "Провести аналитический аудит текущей оптимизации. Не изменяй BPMN."
    assert classify_intent(audit_q) == "analysis"
    assert classify_intent("Провести аналитический аудит текущей оптимизации") == "analysis"
    audit_reply, audit_text, audit_xml, audit_audit = assistant_chat(
        audit_q, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1
    )
    assert audit_text is None and audit_xml is None and audit_audit is None
    assert "Добавь " not in audit_reply
    assert "Удали " not in audit_reply
    assert "Сделай шаги параллельными" not in audit_reply
    assert "| Изменение |" in audit_reply
    assert "Подтверждено регламентом" in audit_reply
    assert "Параллельно" in audit_reply
    assert "это ход оптимизатора, в регламенте такой формулировки нет" in audit_reply
    ppe_rows = [ln for ln in audit_reply.splitlines() if "СИЗ" in ln]
    assert ppe_rows, audit_reply
    assert "| да |" in ppe_rows[0], ppe_rows[0]
    assert "высокая" in ppe_rows[0], ppe_rows[0]
    repair_rows = [ln for ln in audit_reply.splitlines() if "Ремонт остаётся" in ln]
    assert repair_rows, audit_reply
    assert "| да |" in repair_rows[0], repair_rows[0]

    ppe_q = "Что делает подготовка СИЗ, инструмента и переносных заземлений?"
    cop_ppe = canvas_copilot_reply(ppe_q, xml1, audit1, text1, tobe_delta=delta1)
    side_ppe, ppe_text, ppe_xml, ppe_audit = assistant_chat(
        ppe_q, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1
    )
    assert ppe_text is None and ppe_xml is None and ppe_audit is None
    for blob, who in ((cop_ppe, "copilot"), (side_ppe, "sidebar")):
        assert "Принять сообщение" not in blob, (who, blob)
        assert "СИЗ" in blob, (who, blob)
        assert "12" in blob, (who, blob)
    same_q = "Почему в To-Be цифры одинаковые?"
    cop_same = canvas_copilot_reply(same_q, xml1, audit1, text1, tobe_delta=delta1)
    side_same, same_text, same_xml, same_audit = assistant_chat(
        same_q, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1
    )
    assert same_text is None and same_xml is None and same_audit is None
    for blob, who in ((cop_same, "copilot"), (side_same, "sidebar")):
        assert "Принять сообщение" not in blob, (who, blob)
        assert "Добавь " not in blob and "Удали " not in blob, (who, blob)
        assert _hours_cited(blob, float(facts1["rw_before"]))
        assert _hours_cited(blob, float(facts1["rw_after"]))
        assert _hours_cited(blob, float(facts1["cp_before"]))
        assert _hours_cited(blob, float(facts1["cp_after"]))
        assert "добавки за возврат нет" in blob.lower(), blob
    role_q = "Какие шаги у диспетчера?"
    side_role, role_text, role_xml, _ = assistant_chat(
        role_q, [], xml1, audit1, text1, use_llm=False, tobe_delta=delta1
    )
    assert role_text is None and role_xml is None
    assert side_role.index("Принять сообщение") < side_role.index("Зафиксировать аварию")
    assert "5 мин" in side_role
    cop_role = canvas_copilot_reply(role_q, xml1, audit1, text1, tobe_delta=delta1)
    assert "Принять сообщение" in cop_role and "5 мин" in cop_role

    healed_code = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Диспетчер", "Ремонтная бригада"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Принять сообщение об аварии", lanes[0])
b = DIAGRAM.add_user_task("Выполнить аварийный ремонт оборудования", lanes[1])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, ROOT_END_TASK_ID)
"""
    heal_text = (
        "Регламент: Проверка heal\n"
        "1. Диспетчер принимает сообщение об аварии (10 минут).\n"
        "2. Ремонтная бригада выполняет аварийный ремонт оборудования (30 минут).\n"
    )
    xml_h, audit_h, err_h = execute_generated_code(healed_code, "Проверка heal", regulation_text=heal_text)
    assert not err_h, err_h
    journal = " ".join(audit_h.get("auto_healed") or [])
    assert "без входа" in journal or "Тупик" in journal, journal
    assert audit_h["quality"]["ok"], audit_h["quality"]
    assert not audit_h.get("dead_ends")
    assert not audit_h.get("orphans_without_incoming")
    assert audit_h.get("xsd_valid") is not False

    from io import BytesIO
    from docx import Document

    doc = Document()
    for item in ("Принять сообщение", "Зафиксировать аварию", "Оценить масштаб"):
        doc.add_paragraph(item, style="List Number")
    buf = BytesIO()
    doc.save(buf)
    docx_text, docx_err = read_docx_regulation(buf.getvalue())
    assert not docx_err, docx_err
    docx_lines = [ln.strip() for ln in (docx_text or "").splitlines() if ln.strip()]
    assert docx_lines == [
        "1. Принять сообщение",
        "2. Зафиксировать аварию",
        "3. Оценить масштаб",
    ], docx_lines

    glued = (
        "1. Диспетчер принимает сообщение (5 минут). Диспетчер фиксирует журнал (30 мин).\n"
        "2. Начальник смены оценивает масштаб (2 часа).\n"
        "3. Служба безопасности проверяет допуск (1,5 ч).\n"
    )
    owned = parse_regulation(glued)
    assert [round(s.hours, 4) for s in owned.steps] == [round(5 / 60, 4), 0.5, 2.0, 1.5], [
        (s.hours, s.title) for s in owned.steps
    ]

    parallel = (
        "1. Диспетчер принимает заявку (1 час).\n"
        "2. Параллельно: Ремонтная бригада проводит осмотр (3 часа).\n"
    )
    xml_par, audit_par, err_par = generate_bpmn_from_text(parallel, use_llm=False)
    assert not err_par, err_par
    assert audit_par["sla"]["critical_path_hours"] == 3.0, audit_par["sla"]
    assert "parallelGateway" in xml_par

    lettered = (
        "1. Параллельно:\n"
        "а) Ремонтная бригада готовит инструмент (1 час).\n"
        "б) Служба безопасности проводит осмотр площадки (3 часа).\n"
    )
    xml_let, audit_let, err_let = generate_bpmn_from_text(lettered, use_llm=False)
    assert not err_let, err_let
    assert audit_let["sla"]["critical_path_hours"] == 3.0, audit_let["sla"]
    assert "parallelGateway" in xml_let

    either = (
        "1. Служба безопасности проверяет допуск (10 минут). "
        "Либо комплект полный — перейти к п.2, либо отказ — завершить процесс.\n"
        "2. Диспетчер закрывает заявку (20 минут).\n"
    )
    xml_xor, _, err_xor = generate_bpmn_from_text(either, use_llm=False)
    assert not err_xor, err_xor
    assert "exclusiveGateway" in xml_xor
    casual = (
        "1. Диспетчер сверяет схему или журнал (10 минут).\n"
        "2. Начальник смены закрывает наряд (20 минут).\n"
    )
    xml_or, _, err_or = generate_bpmn_from_text(casual, use_llm=False)
    assert not err_or, err_or
    assert "exclusiveGateway" not in xml_or

    chain = "\n".join(
        [
            "Регламент: Цепочка одной роли",
            "1. Диспетчер принимает заявку (10 минут).",
            "2. Диспетчер фиксирует журнал (10 минут).",
            "3. Диспетчер сверяет схему (10 минут).",
            "4. Диспетчер готовит бланк переключений (10 минут).",
            "5. Диспетчер передаёт смену (10 минут).",
            "6. Диспетчер закрывает заявку (10 минут).",
        ]
    )
    chain_blocks, _ = _build_blocks(parse_regulation(chain))
    assert [(b.kind, len(b.steps)) for b in chain_blocks] == [("subprocess", 4), ("subprocess", 2)]
    _, audit_chain, err_chain = generate_bpmn_from_text(chain, use_llm=False)
    assert not err_chain, err_chain
    assert audit_chain["methodology"]["score"] == 100
    assert not audit_chain["methodology"]["long_chains"]

    bare = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Диспетчер"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Принять заявку диспетчера", lanes[0])
b = DIAGRAM.add_user_task("Зафиксировать запись в журнале", lanes[0])
c = DIAGRAM.add_user_task("Сверить оперативную схему", lanes[0])
d = DIAGRAM.add_user_task("Подготовить бланк переключений", lanes[0])
e = DIAGRAM.add_user_task("Передать смену диспетчеру", lanes[0])
f = DIAGRAM.add_user_task("Закрыть оперативную заявку", lanes[0])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, b)
DIAGRAM.add_link(b, c)
DIAGRAM.add_link(c, d)
DIAGRAM.add_link(d, e)
DIAGRAM.add_link(e, f)
DIAGRAM.add_link(f, ROOT_END_TASK_ID)
"""
    _, audit_bare, err_bare = execute_generated_code(bare, "Цепочка без разрезания")
    assert not err_bare, err_bare
    assert audit_bare["methodology"]["long_chains"], audit_bare["methodology"]
    assert audit_bare["methodology"]["score"] < 100

    from ai_generator import _explicit_return_count

    two_returns = (
        "Регламент: Два возврата\n"
        "1. Диспетчер принимает заявку (10 минут).\n"
        "2. Начальник смены проверяет комплект (20 минут). "
        "Если комплект полный — перейти к п.3, иначе «замечания» — вернуть на п.1.\n"
        "3. Служба готовит ответ (15 минут). "
        "Если согласовано — завершить процесс, иначе «на доработку» — вернуть на п.2.\n"
    )
    one_return = (
        "Регламент: Один возврат\n"
        "1. Диспетчер готовит пакет (10 минут).\n"
        "2. Начальник смены согласовывает пакет (20 минут). Если не согласовано — назад.\n"
        "3. Служба закрывает заявку (15 минут).\n"
    )
    no_return = (
        "Регламент: Без возврата\n"
        "1. Диспетчер принимает заявку (10 минут).\n"
        "2. Начальник смены проверяет комплект (20 минут).\n"
        "3. Служба закрывает заявку (15 минут).\n"
    )
    _, audit_two, err_two = generate_bpmn_from_text(two_returns, use_llm=False)
    _, audit_one, err_one = generate_bpmn_from_text(one_return, use_llm=False)
    _, audit_zero, err_zero = generate_bpmn_from_text(no_return, use_llm=False)
    assert not err_two and not err_one and not err_zero, (err_two, err_one, err_zero)
    loops_two = len(audit_two["rework_loops"])
    loops_one = len(audit_one["rework_loops"])
    loops_zero = len(audit_zero["rework_loops"])
    assert loops_two == 2, loops_two
    assert loops_one == 1, loops_one
    assert loops_zero == 0, loops_zero
    assert _explicit_return_count(two_returns) == loops_two
    assert _explicit_return_count(one_return) == loops_one
    assert _explicit_return_count(no_return) == loops_zero
    _, delta_zero = optimize_process_to_be(no_return, audit_zero)
    assert not any(
        "цикл заменён эскалацией" in str(a.get("detail") or "")
        for a in (delta_zero.get("actions") or [])
    ), delta_zero.get("actions")

    said = _explicit_return_count(text)
    assert said == len(audit["rework_loops"]), (said, len(audit["rework_loops"]))
    assert int(delta.get("rework_before") or 0) == said
    assert int(delta.get("rework_after") or 0) == 0
    removed_edges = int(delta.get("rework_before") or 0) - int(delta.get("rework_after") or 0)
    escalations = [
        a for a in (delta.get("actions") or [])
        if "цикл заменён эскалацией" in str(a.get("detail") or "")
    ]
    assert len(escalations) == removed_edges, (len(escalations), removed_edges, escalations)

    linear = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Диспетчер"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Принять заявку", lanes[0])
b = DIAGRAM.add_user_task("Проверить комплект", lanes[0])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, b)
DIAGRAM.add_link(b, ROOT_END_TASK_ID)
"""
    _, audit_gap, err_gap = execute_generated_code(linear, "Дыра возврата", regulation_text=one_return)
    assert not err_gap, err_gap
    assert audit_gap["quality"]["ok"] is False
    assert any("явных возврата" in item for item in audit_gap["quality"]["critical"])

    sla_code = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Диспетчер"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Принять сообщение диспетчера", lanes[0])
b = DIAGRAM.add_user_task("Совершенно постороннее действие", lanes[0])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, b)
DIAGRAM.add_link(b, ROOT_END_TASK_ID)
"""
    sla_reg = (
        "1. Диспетчер принимает сообщение (5 часов).\n"
        "2. Диспетчер пишет отдельный журнал (9 часов).\n"
    )
    _, audit_sla, err_sla = execute_generated_code(sla_code, "Сроки по имени", regulation_text=sla_reg)
    assert not err_sla, err_sla
    by_name = {str(item.get("name") or ""): float(item.get("hours") or 0) for item in audit_sla["critical_path"]}
    assert abs(by_name["Принять сообщение диспетчера"] - 5.0) < 1e-6, by_name
    assert abs(by_name["Совершенно постороннее действие"] - 2.0) < 1e-6, by_name

    import os
    import time

    hang = "for i in range(2000):\n    for j in range(2000):\n        for k in range(2000):\n            x = i + j + k\n"
    os.environ["DIAGRAM_EXEC_TIMEOUT"] = "0.4"
    started = time.time()
    try:
        xml_hang, _, err_hang = execute_generated_code(hang, "Зависание")
    finally:
        os.environ.pop("DIAGRAM_EXEC_TIMEOUT", None)
    assert time.time() - started < 3, time.time() - started
    assert not xml_hang
    assert "таймаут" in err_hang.lower(), err_hang

    from ai_generator import _iter_tobe_selections

    assert len(_iter_tobe_selections([0], [1], [2, 3])) == 36
    huge = _iter_tobe_selections(list(range(5)), list(range(5)), list(range(3)))
    assert len(huge) <= 80, len(huge)

    print("COPILOT_TOBE", copilot_tobe.replace("\n", " | "))
    print("SIDEBAR_TOBE", side_tobe.replace("\n", " | "))
    print("NEW_STEP", new_step)

    print(
        "ok",
        f"steps={len(parsed.steps)}",
        f"raci={len(matrix)}",
        f"saved_h={delta.get('sla_saved_hours')}",
        f"rework={delta.get('rework_before')}→{delta.get('rework_after')}",
        f"docx={len(payload)}",
        f"engine={delta.get('engine')}",
        f"grid={delta_g.get('sla_before_hours')}→{delta_g.get('sla_after_hours')}",
        f"proc_q={q_before}→{q_after}",
        f"returns={loops_two}/{loops_one}/{loops_zero}",
        f"example1_loops={said}→0",
    )


if __name__ == "__main__":
    main()
