"""
Веб-интерфейс «Архитектор BPMN-диаграмм» — ПАО «Интер РАО».

Запуск:  streamlit run app.py
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import streamlit.components.v1 as components

from ai_generator import cloud_engine_status, generate_bpmn_from_text


def _secrets_to_env() -> List[str]:
    """Ключи LLM из .streamlit/secrets.toml / Streamlit Cloud Secrets → переменные окружения для ai_generator.

    Возвращает имена найденных секретов (без значений) — для диагностики в интерфейсе.
    """
    names: List[str] = []
    try:
        for key, value in st.secrets.items():
            names.append(str(key))
            if isinstance(value, (str, int, float)):
                os.environ.setdefault(str(key), str(value).strip())
    except Exception:  # noqa: BLE001 — secrets.toml нет: работаем на переменных окружения / эмуляторе
        pass
    return names


SECRET_NAMES = _secrets_to_env()

ROOT = Path(__file__).resolve().parent
EXAMPLES_DIR = ROOT / "examples"
ASSETS_DIR = ROOT / "assets"
CUSTOM_LABEL = "✍️  Свой текст регламента"

BLUE_DARK, BLUE = "#003366", "#1565C0"
OK, WARN, BAD = "#2E7D32", "#F57F17", "#C62828"

BPMN_JS_CDN = "https://unpkg.com/bpmn-js@17.11.1/dist/bpmn-navigated-viewer.production.min.js"
DIAGRAM_CSS_CDN = "https://unpkg.com/bpmn-js@17.11.1/dist/assets/diagram-js.css"


# --------------------------------------------------------------------------- #
# Данные и утилиты
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_examples() -> Dict[str, Dict[str, str]]:
    items: Dict[str, Dict[str, str]] = {}
    for path in sorted(EXAMPLES_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^Регламент:\s*(.+)$", text, re.M)
        number = re.match(r"example_(\d+)_", path.stem)
        prefix = f"{number.group(1)}. " if number else ""
        label = prefix + (m.group(1).strip() if m else path.stem)
        items[label] = {"text": text, "stem": path.stem}
    return items


@st.cache_data(show_spinner=False)
def read_asset(name: str) -> Optional[str]:
    path = ASSETS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt_hours(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} мин"
    if hours < 48:
        return f"{hours:.1f} ч".replace(".0 ", " ")
    return f"{hours:.0f} ч"


def fmt_days(hours: float) -> str:
    return f"≈ {hours / 8:.0f} раб. дн."


# --------------------------------------------------------------------------- #
# Стили
# --------------------------------------------------------------------------- #
CSS = f"""
<style>
:root {{ --dark:{BLUE_DARK}; --blue:{BLUE}; --ok:{OK}; --warn:{WARN}; --bad:{BAD}; }}
.stApp {{ background: #F4F8FD; }}
.block-container {{ padding-top: 1.2rem; max-width: 1500px; }}
header[data-testid="stHeader"] {{ background: transparent; }}
#MainMenu, footer {{ visibility: hidden; }}
.ir-hero {{
  background: linear-gradient(105deg, {BLUE_DARK} 0%, {BLUE} 100%);
  border-radius: 18px; padding: 22px 30px; color: #fff; margin-bottom: 18px;
  box-shadow: 0 8px 24px rgba(0,51,102,.22); display:flex; justify-content:space-between; align-items:center; gap:24px;
}}
.ir-hero h1 {{ color:#fff; font-size: 1.75rem; margin:0; padding:0; letter-spacing:.2px; }}
.ir-hero p {{ margin:4px 0 0 0; color:#DCE9FA; font-size:.98rem; }}
.ir-badges span {{
  display:inline-block; background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.35);
  color:#fff; padding:5px 12px; border-radius:999px; font-size:.78rem; margin-left:6px; white-space:nowrap;
}}
.ir-panel {{
  background:#fff; border:1px solid #DCE6F3; border-radius:16px; padding:18px 20px 8px 20px;
  box-shadow:0 2px 10px rgba(0,51,102,.06);
}}
.ir-title {{ color:{BLUE_DARK}; font-weight:700; font-size:1.05rem; margin:0 0 10px 0; }}
.ir-section {{ color:{BLUE_DARK}; font-weight:800; font-size:1.35rem; margin:26px 0 12px 0; display:flex; align-items:center; gap:10px; }}
.ir-section:before {{ content:""; width:6px; height:26px; background:{BLUE}; border-radius:3px; display:inline-block; }}
.stButton > button, .stDownloadButton > button {{ width:100%; border-radius:12px; font-weight:700; padding:.65rem 1rem; }}
.stButton > button[kind="primary"] {{
  background: linear-gradient(105deg, {BLUE_DARK}, {BLUE}); border:none; color:#fff;
  box-shadow:0 6px 16px rgba(21,101,192,.35);
}}
.stButton > button[kind="primary"]:hover {{ filter:brightness(1.08); }}
.stDownloadButton > button {{ border:2px solid {BLUE}; color:{BLUE}; background:#fff; }}
.stDownloadButton > button:hover {{ background:#E3F2FD; color:{BLUE_DARK}; border-color:{BLUE_DARK}; }}
textarea {{ font-size:.9rem !important; line-height:1.45 !important; }}
.ir-card {{
  background:#fff; border:1px solid #DCE6F3; border-radius:16px; padding:16px 18px; height:100%;
  border-top:5px solid var(--c); box-shadow:0 2px 10px rgba(0,51,102,.06);
}}
.ir-card .k {{ color:#607D8B; font-size:.78rem; text-transform:uppercase; letter-spacing:.6px; font-weight:700; }}
.ir-card .v {{ color:{BLUE_DARK}; font-size:2.05rem; font-weight:800; line-height:1.15; margin:4px 0; }}
.ir-card .s {{ color:#455A64; font-size:.86rem; }}
.pill {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:.74rem; font-weight:700; color:#fff; background:var(--c); margin-top:8px; }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:9px 0; font-size:.88rem; color:#37474F; }}
.bar-row .n {{ width:190px; flex:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.bar-row .t {{ flex:1; background:#E8EEF7; border-radius:8px; height:14px; position:relative; overflow:hidden; }}
.bar-row .f {{ height:100%; border-radius:8px; background:linear-gradient(90deg,{BLUE},{BLUE_DARK}); }}
.bar-row .f.hot {{ background:linear-gradient(90deg,#EF6C00,{BAD}); }}
.bar-row .p {{ width:96px; text-align:right; flex:none; font-weight:700; color:{BLUE_DARK}; }}
.chip {{ display:inline-block; background:#E3F2FD; border:1px solid #90CAF9; color:{BLUE_DARK}; border-radius:10px; padding:4px 10px; margin:3px 4px 3px 0; font-size:.8rem; }}
.chip b {{ color:{BLUE}; }}
.arrow {{ color:#90A4AE; margin-right:4px; }}
.rec {{ background:#fff; border:1px solid #DCE6F3; border-left:6px solid {BLUE}; border-radius:12px; padding:11px 16px; margin:8px 0; color:#263238; font-size:.93rem; }}
.rec.bad {{ border-left-color:{BAD}; }} .rec.warn {{ border-left-color:{WARN}; }} .rec.ok {{ border-left-color:{OK}; }}
table.loops {{ width:100%; border-collapse:collapse; font-size:.86rem; }}
table.loops th {{ text-align:left; color:#607D8B; font-weight:700; border-bottom:2px solid #DCE6F3; padding:6px 8px; }}
table.loops td {{ padding:7px 8px; border-bottom:1px solid #EEF3FA; color:#263238; }}
.engine {{ font-size:.82rem; color:#455A64; background:#E3F2FD; border-radius:10px; padding:8px 12px; margin:10px 0 6px 0; }}
</style>
"""


# --------------------------------------------------------------------------- #
# Компонент просмотра BPMN (bpmn-js)
# --------------------------------------------------------------------------- #
def viewer_html(xml: str, height: int) -> str:
    js_inline = read_asset("bpmn-navigated-viewer.production.min.js")
    css_inline = read_asset("diagram-js.css")
    js_tag = f"<script>{js_inline}</script>" if js_inline else f'<script src="{BPMN_JS_CDN}"></script>'
    css_tag = f"<style>{css_inline}</style>" if css_inline else f'<link rel="stylesheet" href="{DIAGRAM_CSS_CDN}">'
    payload = json.dumps(xml).replace("</", "<\\/")
    return f"""
<!doctype html><html><head><meta charset="utf-8">{css_tag}
<style>
  html,body {{ margin:0; height:100%; font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:#fff; }}
  #wrap {{ position:relative; height:{height}px; border:1px solid #DCE6F3; border-radius:14px; overflow:hidden; background:
      linear-gradient(#F4F8FD 1px, transparent 1px) 0 0/24px 24px, linear-gradient(90deg,#F4F8FD 1px, transparent 1px) 0 0/24px 24px, #fff; }}
  #canvas {{ position:absolute; inset:0; cursor:grab; }}
  #canvas:active {{ cursor:grabbing; }}
  .bar {{ position:absolute; top:10px; right:10px; z-index:5; display:flex; gap:6px; }}
  .bar button {{ border:1px solid #90CAF9; background:#fff; color:#003366; font-weight:700; border-radius:10px;
      padding:6px 12px; cursor:pointer; box-shadow:0 2px 6px rgba(0,51,102,.12); font-size:13px; }}
  .bar button:hover {{ background:#E3F2FD; }}
  .hint {{ position:absolute; left:12px; bottom:10px; z-index:5; font-size:12px; color:#455A64; background:rgba(255,255,255,.92);
      border:1px solid #DCE6F3; border-radius:8px; padding:4px 10px; }}
  #err {{ position:absolute; inset:0; display:none; align-items:center; justify-content:center; color:#C62828; padding:24px; text-align:center; font-weight:600; }}
  .bjs-powered-by {{ opacity:.55; }}
</style></head>
<body>
<div id="wrap">
  <div class="bar">
    <button id="zin" title="Приблизить">＋</button><button id="zout" title="Отдалить">－</button>
    <button id="fit" title="Вписать в окно">По размеру</button><button id="one" title="Масштаб 100%">100%</button>
  </div>
  <div id="canvas"></div><div id="err"></div>
  <div class="hint">Перетаскивание — панорама · Ctrl + колесо / кнопки ＋ － — масштаб</div>
</div>
{js_tag}
<script>
  const XML = {payload};
  const viewer = new BpmnJS({{ container: '#canvas' }});
  const canvas = () => viewer.get('canvas');
  function fit() {{ canvas().zoom('fit-viewport', 'auto'); }}
  // Заголовки раскрытых подпроцессов — крупнее и жирнее (bpmn-js рисует все подписи одним кеглем).
  function emphasizeSubprocessTitles() {{
    const registry = viewer.get('elementRegistry');
    registry.filter(el => el.type === 'bpmn:SubProcess').forEach(el => {{
      const label = registry.getGraphics(el).querySelector('text.djs-label');
      if (!label) return;
      label.style.fontSize = '16px';
      label.style.fontWeight = '700';
      label.querySelectorAll('tspan').forEach(t => {{
        t.setAttribute('x', Math.max(4, (el.width - t.getComputedTextLength()) / 2));
        t.setAttribute('y', parseFloat(t.getAttribute('y')) + 4);  // крупный кегль не должен касаться рамки
      }});
    }});
  }}
  viewer.importXML(XML).then(() => {{ emphasizeSubprocessTitles(); fit(); }}).catch(e => {{
    const el = document.getElementById('err'); el.style.display = 'flex'; el.textContent = 'Ошибка отображения BPMN: ' + e.message;
  }});
  const zoomBy = k => canvas().zoom(canvas().zoom() * k, 'auto');
  document.getElementById('zin').onclick = () => zoomBy(1.25);
  document.getElementById('zout').onclick = () => zoomBy(0.8);
  document.getElementById('fit').onclick = fit;
  document.getElementById('one').onclick = () => canvas().zoom(1, 'auto');
  window.addEventListener('resize', fit);
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# Аудит: визуальные блоки
# --------------------------------------------------------------------------- #
def card(title: str, value: str, sub: str, color: str, pill: str) -> str:
    return (
        f'<div class="ir-card" style="--c:{color}"><div class="k">{esc(title)}</div>'
        f'<div class="v">{esc(value)}</div><div class="s">{sub}</div>'
        f'<span class="pill">{esc(pill)}</span></div>'
    )


def render_audit(audit: Dict[str, Any]) -> None:
    st.markdown('<div class="ir-section">Аудит бизнес-архитектуры</div>', unsafe_allow_html=True)
    bus = audit["bus_factor"]
    sla = audit["sla"]
    loops: List[Dict[str, Any]] = audit["rework_loops"]
    stats = audit["stats"]

    bus_color = BAD if bus["status"] == "risk" else OK
    bus_pill = "Риск ключевого исполнителя" if bus["status"] == "risk" else "Нагрузка сбалансирована"

    base_h, total_h = float(sla["critical_path_hours"]), float(sla["with_rework_hours"])
    target = sla.get("target_hours")
    if sla["breach"]:
        sla_color, sla_pill = BAD, "Риск срыва SLA"
    elif float(sla["rework_share"]) > 0.25:
        sla_color, sla_pill = WARN, "Возвраты съедают запас"
    else:
        sla_color, sla_pill = OK, "В пределах SLA"
    sla_value = fmt_hours(base_h) if base_h < 48 else fmt_days(base_h)
    sla_sub = f"{stats['critical_path_layers']} слоёв согласования"
    if target:
        sla_sub += f" · цель {fmt_hours(float(target)) if float(target) < 48 else fmt_days(float(target))}"
    sla_sub += f"<br>с худшим возвратом: {fmt_hours(total_h) if total_h < 48 else fmt_days(total_h)}"

    loop_color = OK if not loops else (BAD if len(loops) >= 3 else WARN)
    loop_pill = "Возвратов нет" if not loops else f"Доля в сроке: {float(sla['rework_share']):.0%}"
    loop_sub = (
        "Циклов доработки в модели"
        if not loops
        else f"худший: +{fmt_hours(float(sla['rework_hours']))} к критическому пути"
    )

    c1, c2, c3, c4 = st.columns(4, gap="medium")
    c1.markdown(
        card(
            "Bus-factor",
            f"{float(bus['max_share']):.0%}",
            f"Роль «{esc(bus['top_role'])}» · порог {float(bus['threshold']):.0%}",
            bus_color,
            bus_pill,
        ),
        unsafe_allow_html=True,
    )
    c2.markdown(card("Критический путь SLA", sla_value, sla_sub, sla_color, sla_pill), unsafe_allow_html=True)
    c3.markdown(card("Циклы возврата", str(len(loops)), loop_sub, loop_color, loop_pill), unsafe_allow_html=True)
    c4.markdown(
        card(
            "Структура процесса",
            f"{stats['nodes']} узлов",
            f"{stats['lanes']} ролей · {stats['subprocesses']} подпроцессов<br>{stats['valid_links']} потоков управления",
            BLUE,
            "Декомпозиция применена" if stats["subprocesses"] else "Без декомпозиции",
        ),
        unsafe_allow_html=True,
    )

    st.write("")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown('<div class="ir-title">Нагрузка по ролям (доля шагов процесса)</div>', unsafe_allow_html=True)
        rows = []
        for item in sorted(audit["lane_load"], key=lambda it: -float(it["share"])):
            share = float(item["share"])
            hot = "hot" if share > float(bus["threshold"]) else ""
            rows.append(
                f'<div class="bar-row"><div class="n" title="{esc(item["role"])}">{esc(item["role"])}</div>'
                f'<div class="t"><div class="f {hot}" style="width:{min(share, 1) * 100:.0f}%"></div></div>'
                f'<div class="p">{share:.0%} · {int(item["tasks"])} шаг.</div></div>'
            )
        st.markdown("".join(rows), unsafe_allow_html=True)
    with right:
        st.markdown('<div class="ir-title">Критический путь (алгоритм Беллмана — Форда)</div>', unsafe_allow_html=True)
        chips = []
        for i, item in enumerate(audit["critical_path"]):
            arrow = '<span class="arrow">→</span>' if i else ""
            name = str(item["name"])
            name = name if len(name) <= 42 else name[:41] + "…"
            chips.append(f'{arrow}<span class="chip" title="{esc(item["role"])}"><b>{fmt_hours(float(item["hours"]))}</b> · {esc(name)}</span>')
        st.markdown("".join(chips) or "—", unsafe_allow_html=True)

    if loops:
        st.markdown('<div class="ir-title" style="margin-top:14px">Циклы возврата на доработку</div>', unsafe_allow_html=True)
        body = "".join(
            f"<tr><td><b>{esc(l['label'])}</b></td><td>{esc(l['from'])}</td><td>→ {esc(l['to'])}</td>"
            f"<td>{esc(l['lane'])}</td><td><b>{fmt_hours(float(l['cycle_hours']))}</b></td></tr>"
            for l in loops
        )
        st.markdown(
            '<table class="loops"><tr><th>Условие</th><th>Откуда</th><th>Куда</th><th>Роль</th><th>Стоимость цикла</th></tr>'
            + body
            + "</table>",
            unsafe_allow_html=True,
        )

    st.markdown('<div class="ir-title" style="margin-top:18px">Рекомендации по оптимизации</div>', unsafe_allow_html=True)
    risk_messages = [r for r in audit["sla_risks"] if r.get("severity") == "high"]
    recs = "".join(f'<div class="rec bad">{esc(r["message"])}</div>' for r in risk_messages)
    for text in audit["recommendations"]:
        css = "ok" if text.startswith("Существенных") else ("warn" if text.startswith(("Цикл", "Bus")) else "")
        recs += f'<div class="rec {css}">{esc(text)}</div>'
    if audit["dead_ends"]:
        recs += "".join(f'<div class="rec bad">{esc(d["message"])}</div>' for d in audit["dead_ends"])
    st.markdown(recs, unsafe_allow_html=True)


def render_details(audit: Dict[str, Any]) -> None:
    gen = audit.get("generation", {})
    with st.expander("Технические детали: движок генерации, самовосстановление, код, JSON аудита"):
        st.markdown(
            f"**Движок:** `{gen.get('engine', '—')}` · время {gen.get('elapsed_s', 0)} с · "
            f"fail-safe: {'да' if gen.get('fallback') else 'нет'}"
        )
        if gen.get("trace"):
            st.markdown("**Трасса выбора движка:**\n" + "\n".join(f"- {t}" for t in gen["trace"]))
        if audit.get("auto_healed"):
            st.markdown("**Самовосстановление графа:**\n" + "\n".join(f"- {t}" for t in audit["auto_healed"]))
        if audit.get("engine_warnings"):
            st.markdown("**Предупреждения движка:**\n" + "\n".join(f"- {t}" for t in audit["engine_warnings"]))
        if gen.get("code"):
            st.markdown("**Сгенерированный код для DIAGRAM:**")
            st.code(gen["code"], language="python")
        for item in gen.get("rejected", []):
            st.markdown(
                f"**Отклонённый ответ {item['engine']}, попытка {item['attempt']}:** " + "; ".join(item["problems"][:5])
            )
            st.code(item["code"], language="python")
        st.markdown("**JSON аудита:**")
        st.json({k: v for k, v in audit.items() if k != "generation"}, expanded=False)


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #
def run_generation(text: str, use_llm: bool, show_progress: bool = True) -> None:
    progress = st.progress(0, text="Запуск конвейера…") if show_progress else None
    stages = [(18, "Семантический разбор регламента: роли, условия, параллельность…")]
    for pct, label in stages:
        if progress:
            progress.progress(pct, text=label)
            time.sleep(0.25)
    if progress:
        progress.progress(
            45, text="Генерация кода DIAGRAM (LLM или встроенный эмулятор)… Локальной модели может потребоваться до 5 минут."
        )
    xml, audit, error = generate_bpmn_from_text(text, use_llm=use_llm)
    if progress:
        progress.progress(78, text="Раскладка по слоям, ортогональные стрелки, BPMN in Color…")
        time.sleep(0.25)
        progress.progress(100, text="Аудит узких мест и валидация XML завершены")
        time.sleep(0.2)
        progress.empty()
    st.session_state["result"] = {"xml": xml, "audit": audit, "error": error}


def on_example_change() -> None:
    examples = load_examples()
    choice = st.session_state["example_choice"]
    if choice in examples:
        st.session_state["reg_text"] = examples[choice]["text"]
        st.session_state["file_stem"] = examples[choice]["stem"]
    else:
        st.session_state["file_stem"] = "custom_process"


def main() -> None:
    st.set_page_config(page_title="Архитектор BPMN — Интер РАО", page_icon="⚡", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        """
<div class="ir-hero">
  <div><h1>⚡ Архитектор BPMN-диаграмм</h1>
  <p>ПАО «Интер РАО» · Дирекция бизнес-архитектуры · регламент → BPMN 2.0 → аудит процесса</p></div>
  <div class="ir-badges"><span>BPMN 2.0.2</span><span>demo.bpmn.io ready</span><span>ИИ + fail-safe</span></div>
</div>""",
        unsafe_allow_html=True,
    )

    examples = load_examples()
    labels = list(examples)
    if "reg_text" not in st.session_state:
        first = labels[0] if labels else CUSTOM_LABEL
        st.session_state["example_choice"] = first
        st.session_state["reg_text"] = examples[first]["text"] if labels else ""
        st.session_state["file_stem"] = examples[first]["stem"] if labels else "custom_process"
    if "result" not in st.session_state and st.session_state["reg_text"].strip():
        with st.spinner("Готовим эталонный пример…"):
            # Эталон при открытии — эмулятором: страница не должна минутами ждать LLM.
            run_generation(st.session_state["reg_text"], use_llm=False, show_progress=False)

    left, right = st.columns([5, 7], gap="large")

    with left:
        st.markdown('<div class="ir-title">1 · Регламент</div>', unsafe_allow_html=True)
        st.selectbox(
            "Готовый отраслевой регламент",
            [*labels, CUSTOM_LABEL],
            key="example_choice",
            on_change=on_example_change,
            help="Выберите эталонный кейс или введите свой текст ниже.",
        )
        st.text_area(
            "Текст регламента (шаги нумеруются; условия — «Если … — перейти к п.N, иначе …»)",
            key="reg_text",
            height=400,
        )
        use_llm = st.checkbox(
            "Использовать LLM (Ollama / OpenAI), если доступна",
            value=True,
            help="Если модель недоступна, автоматически включается встроенный семантический эмулятор.",
        )
        secrets_note = f" · секреты: {', '.join(SECRET_NAMES)}" if SECRET_NAMES else ""
        st.caption(f"Облачная модель: {cloud_engine_status()}{secrets_note}")
        if st.button("🚀  Сгенерировать BPMN 2.0", type="primary"):
            if not st.session_state["reg_text"].strip():
                st.warning("Введите текст регламента.")
            else:
                run_generation(st.session_state["reg_text"], use_llm)

        result = st.session_state.get("result")
        if result and not result["error"]:
            gen = result["audit"].get("generation", {})
            engine = gen.get("engine", "—")
            note = ""
            if gen.get("fallback") and engine == "semantic-emulator":
                reasons = gen.get("trace") or ["LLM недоступна"]
                note = "<br>⚠️ fail-safe: " + "<br>".join(f"· {esc(r)}" for r in reasons)
            elif gen.get("attempts", 1) > 1:
                note = f" · исправлено со {gen['attempts']}-й попытки"
            xsd = " · ✓ XSD BPMN 2.0" if result["audit"].get("xsd_valid") else ""
            st.markdown(
                f'<div class="engine">Движок: <b>{esc(engine)}</b> · {gen.get("elapsed_s", 0)} с{xsd}{note}</div>',
                unsafe_allow_html=True,
            )
        st.download_button(
            "⬇️  Скачать .bpmn",
            data=(result["xml"] if result and not result["error"] else ""),
            file_name=f"{st.session_state.get('file_stem', 'process')}.bpmn",
            mime="application/xml",
            disabled=not (result and not result["error"]),
        )

    with right:
        st.markdown('<div class="ir-title">2 · Интерактивная диаграмма</div>', unsafe_allow_html=True)
        result = st.session_state.get("result")
        if not result:
            st.info("Выберите регламент и нажмите «Сгенерировать BPMN 2.0».")
        elif result["error"]:
            st.error(result["error"])
        else:
            page = viewer_html(result["xml"], 660)
            if hasattr(st, "iframe"):  # Streamlit ≥ 1.5x: st.components.v1.html объявлен устаревшим
                st.iframe(page, height=676)
            else:
                components.html(page, height=676, scrolling=False)

    result = st.session_state.get("result")
    if result and not result["error"]:
        render_audit(result["audit"])
        render_details(result["audit"])


main()
