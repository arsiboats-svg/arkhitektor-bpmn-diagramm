"""
Стресс-тест на «кривых» регламентах: сплошной текст, мусор из PDF, опечатки, длинные процессы.

    python3 stress_test.py                  # только эмулятор (быстро, без LLM)
    python3 stress_test.py --llm            # с LLM (Ollama / OpenAI-совместимый API)
    python3 stress_test.py --llm stress_tests/03_typos_lowercase.txt

Результаты (.bpmn и сводка report.md) пишутся в stress_tests/out/.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "stress_tests" / "out"


def main(argv: List[str]) -> int:
    use_llm = "--llm" in argv
    files = [Path(a) for a in argv if not a.startswith("--")] or sorted((ROOT / "stress_tests").glob("*.txt"))
    if not use_llm:
        os.environ["BPMN_AI_MODE"] = "emulator"

    from ai_generator import generate_bpmn_from_text
    from validate_bpmn import validate

    OUT.mkdir(parents=True, exist_ok=True)
    rows = ["| Файл | Движок | Время | Роли | Узлы | Подпроц. | Качество графа | Валидатор | Ошибка |", "|" + "---|" * 9]
    failed = 0
    for path in files:
        started = time.time()
        xml, audit, err = generate_bpmn_from_text(path.read_text(encoding="utf-8"), use_llm=use_llm)
        elapsed = time.time() - started
        if err:
            failed += "не распознан" not in err  # мусорный ввод обязан давать понятную ошибку, это не сбой
            rows.append(f"| {path.name} | — | {elapsed:.0f} с | — | — | — | — | — | {err[:80]} |")
            print(f"[ERR] {path.name}: {err}")
            continue
        target = OUT / (path.stem + ".bpmn")
        target.write_text(xml, encoding="utf-8")
        problems = validate(str(target))
        stats, quality = audit.get("stats", {}), audit.get("quality", {})
        gen = audit.get("generation", {})
        issues = quality.get("issues", [])
        failed += bool(problems)
        rows.append(
            f"| {path.name} | {gen.get('engine', '?')} | {elapsed:.0f} с | {stats.get('lanes')} | {stats.get('nodes')} | "
            f"{stats.get('subprocesses')} | {'OK' if not issues else f'{len(issues)} замеч.'} | "
            f"{'OK' if not problems else f'{len(problems)} пробл.'} | |"
        )
        print(f"[{'OK ' if not problems else 'BAD'}] {path.name}: {gen.get('engine')} {elapsed:.0f} с, "
              f"роли={audit.get('lane_load') and [l['role'] for l in audit['lane_load']]}")
        for line in issues + problems + gen.get("trace", []):
            print("      ", line)

    (OUT / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
