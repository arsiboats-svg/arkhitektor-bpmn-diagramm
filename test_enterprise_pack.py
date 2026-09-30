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
)


def main() -> None:
    text = (Path(__file__).resolve().parent / "examples" / "example_1_substation_repair.txt").read_text(
        encoding="utf-8"
    )
    xml, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert xml.strip().startswith("<?xml") or "<bpmn" in xml or "<definitions" in xml

    opt, delta = optimize_process_to_be(text, audit)
    ot_stop = re.compile(
        r"допуск|наряд[\s-]*допуск|инструктаж|проверк|заземлен|отключен|разрешен|согласован|утвержден",
        re.I,
    )
    for line in opt.splitlines():
        if re.search(r"параллельно|одновременно", line, re.I) and ot_stop.search(line):
            raise AssertionError(f"запрещено распараллеливать охрану труда: {line}")
        if re.search(r"аварийн\w+\s+ремонт|выполн\w+.{0,40}ремонт", line, re.I):
            assert not re.search(r"^\s*\d+\.\s*(?:параллельно|одновременно)", line, re.I), line
    assert any(a.get("kind") == "safety_seq" for a in delta.get("actions") or []), delta.get("actions")
    assert any(a.get("kind") in ("zero_rework", "automation", "parallel", "safety_seq") for a in delta.get("actions") or [])
    assert "sla_saved_hours" in delta
    assert delta.get("tobe_xml") or not delta.get("tobe_error")
    assert int(delta.get("rework_after") or 0) <= int(delta.get("rework_before") or 0)

    parsed = parse_regulation(text)
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
    )


if __name__ == "__main__":
    main()
