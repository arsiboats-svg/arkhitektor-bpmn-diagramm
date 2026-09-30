#!/usr/bin/env python3
"""Проверка пакета As-Is/To-Be, RACI и экспорта DOCX."""

from __future__ import annotations

from pathlib import Path

from ai_generator import (
    export_docx_passport,
    generate_bpmn_from_text,
    generate_raci_matrix,
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
    assert "Параллельно" in opt or any(a.get("kind") == "parallel" for a in delta.get("actions") or [])
    assert any(a.get("kind") in ("zero_rework", "automation", "parallel") for a in delta.get("actions") or [])
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

    print(
        "ok",
        f"steps={len(parsed.steps)}",
        f"raci={len(matrix)}",
        f"saved_h={delta.get('sla_saved_hours')}",
        f"rework={delta.get('rework_before')}→{delta.get('rework_after')}",
        f"docx={len(payload)}",
        f"engine={delta.get('engine')}",
    )


if __name__ == "__main__":
    main()
