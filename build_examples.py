#!/usr/bin/env python3
"""Пересобирает эталонные .bpmn (и .audit.json) для всех регламентов из examples/*.txt."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Для воспроизводимости эталонов используем только встроенный эмулятор.
os.environ.setdefault("BPMN_AI_MODE", "emulator")

from ai_generator import generate_bpmn_from_text  # noqa: E402

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"


def main() -> int:
    failed = 0
    for txt in sorted(EXAMPLES_DIR.glob("*.txt")):
        xml, audit, error = generate_bpmn_from_text(txt.read_text(encoding="utf-8"))
        if error:
            failed += 1
            print(f"[FAIL] {txt.name}: {error}")
            continue
        txt.with_suffix(".bpmn").write_text(xml, encoding="utf-8")
        audit_slim = {k: v for k, v in audit.items() if k != "generation"}
        audit_slim["generation"] = {k: v for k, v in audit["generation"].items() if k != "code"}
        txt.with_suffix(".audit.json").write_text(
            json.dumps(audit_slim, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        g = audit["generation"]["parsed"]
        s = audit["stats"]
        print(
            f"[ OK ] {txt.stem}: шагов={g['steps']} ролей={len(g['roles'])} подпроцессов={g['subprocesses']} "
            f"шлюзов={g['decisions']}+{g['parallel_groups']}‖ узлов={s['nodes']} связей={s['valid_links']} "
            f"КП={s['critical_path_hours']} ч"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
