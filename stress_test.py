"""
Стресс-тест на «кривых» регламентах: сплошной текст, мусор из PDF, опечатки, длинные процессы.

    python3 stress_test.py                  # только эмулятор (быстро, без LLM)
    python3 stress_test.py --llm            # с LLM (Ollama / OpenAI-совместимый API)
    python3 stress_test.py --llm stress_tests/03_typos_lowercase.txt
    python3 stress_test.py --llm --pause 25 examples/*.txt stress_tests/*.txt   # пауза под лимит Groq Free

С --llm ключи берутся из переменных окружения или из .streamlit/secrets.toml.

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


def _load_secrets() -> None:
    """Ключи LLM из .streamlit/secrets.toml → окружение (как делает app.py); значения не печатаются."""
    path = ROOT / ".streamlit" / "secrets.toml"
    if not path.exists():
        return
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return
    for key, value in tomllib.loads(path.read_text(encoding="utf-8")).items():
        if isinstance(value, (str, int, float)):
            os.environ.setdefault(key, str(value))


def main(argv: List[str]) -> int:
    use_llm = "--llm" in argv
    pause = 0.0
    if "--pause" in argv:
        i = argv.index("--pause")
        pause = float(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    files = [Path(a) for a in argv if not a.startswith("--")] or sorted((ROOT / "stress_tests").glob("*.txt"))
    if use_llm:
        _load_secrets()
    else:
        os.environ["BPMN_AI_MODE"] = "emulator"

    from ai_generator import generate_bpmn_from_text
    from validate_bpmn import validate

    OUT.mkdir(parents=True, exist_ok=True)
    rows = ["| Файл | Движок | Время | Роли | Узлы | Подпроц. | Качество графа | Валидатор | Ошибка |", "|" + "---|" * 9]
    failed = 0
    for n, path in enumerate(files):
        if n and pause:
            time.sleep(pause)  # бесплатный лимит Groq ~8K токенов/мин
        started = time.time()
        xml, audit, err = generate_bpmn_from_text(path.read_text(encoding="utf-8"), use_llm=use_llm)
        elapsed = time.time() - started
        if err:
            failed += (
                "не удалось распознать" not in err.lower() and "не распознан" not in err.lower()
            )  # мусорный ввод обязан давать понятную ошибку, это не сбой
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
