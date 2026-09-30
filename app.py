"""
Веб-интерфейс «Архитектор BPMN-диаграмм» — ПАО «Интер РАО».

Запуск:  streamlit run app.py
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components

from ai_generator import (
    assistant_chat,
    build_diagram_catalog,
    build_canvas_copilot,
    build_process_context,
    cloud_engine_status,
    generate_bpmn_from_text,
    generate_process_passport,
    inspect_task_details,
    normalize_regulation,
    parse_bpmn_structure,
)


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
DIAGRAM_HEIGHT = 720  # высота холста по умолчанию, px (не менее 700)
DIAGRAM_HEIGHT_WIDE = 820  # в широком режиме
VIEW_SPLIT = "🗂  Раздельный вид"
VIEW_WIDE = "🖥  Широкий вид"

QUICK_PROMPTS = [
    ("🔍 Разбор узких мест SLA", "В чём причина срыва SLA? Какие шаги и возвраты съедают срок?"),
    ("⚡ Как ускорить процесс?", "Как ускорить процесс? Что даст наибольший эффект?"),
    ("📝 Регламент для исполнителя", "Составь должностную инструкцию для самой загруженной роли по текущей схеме."),
    ("🔮 Предложить следующий шаг", "Предложи следующий шаг процесса: чего не хватает в регламенте?"),
]

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
[data-testid="stRadio"] div[role="radiogroup"] {{
  display:inline-flex; gap:0; background:#E8EEF7; border:1px solid #D3DFF0; border-radius:14px; padding:4px;
}}
[data-testid="stRadio"] div[role="radiogroup"] > label {{
  margin:0; padding:7px 22px; border-radius:10px; cursor:pointer; transition:background .15s, box-shadow .15s;
}}
[data-testid="stRadio"] div[role="radiogroup"] > label > div:first-child {{ display:none; }}
[data-testid="stRadio"] div[role="radiogroup"] > label p {{ color:{BLUE_DARK}; font-weight:700; font-size:.95rem; }}
[data-testid="stRadio"] div[role="radiogroup"] > label:hover {{ background:rgba(21,101,192,.10); }}
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) {{
  background:linear-gradient(105deg,{BLUE_DARK},{BLUE}); box-shadow:0 4px 12px rgba(21,101,192,.35);
}}
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) p {{ color:#fff; }}
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
[data-testid="stRadio"] div[role="radiogroup"] {{
  display:inline-flex; gap:0; background:#E8EEF7; border:1px solid #D3DFF0; border-radius:14px; padding:4px;
}}
[data-testid="stRadio"] div[role="radiogroup"] > label {{
  margin:0; padding:7px 22px; border-radius:10px; cursor:pointer; transition:background .15s, box-shadow .15s;
}}
[data-testid="stRadio"] div[role="radiogroup"] > label > div:first-child {{ display:none; }}
[data-testid="stRadio"] div[role="radiogroup"] > label p {{ color:{BLUE_DARK}; font-weight:700; font-size:.95rem; }}
[data-testid="stRadio"] div[role="radiogroup"] > label:hover {{ background:rgba(21,101,192,.10); }}
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) {{
  background:linear-gradient(105deg,{BLUE_DARK},{BLUE}); box-shadow:0 4px 12px rgba(21,101,192,.35);
}}
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) p {{ color:#fff; }}
.land-cap {{ color:#607D8B; font-size:.76rem; text-transform:uppercase; letter-spacing:.6px; font-weight:700; margin:10px 0 4px 0; }}
.chip-sys, .chip-doc {{ display:inline-block; border-radius:999px; padding:6px 14px; margin:4px 6px 4px 0; font-size:.86rem; font-weight:600; }}
.chip-sys {{ background:#E3F2FD; border:1px solid #90CAF9; color:{BLUE_DARK}; }}
.chip-doc {{ background:#E8F5E9; border:1px solid #A5D6A7; color:#1B5E20; }}
.chip-sys small, .chip-doc small {{ opacity:.7; font-weight:700; margin-left:6px; }}
.land-empty {{ color:#78909C; font-size:.88rem; font-style:italic; margin:2px 0 6px 0; }}
.engine {{ font-size:.82rem; color:#455A64; background:#E3F2FD; border-radius:10px; padding:8px 12px; margin:10px 0 6px 0; }}
.ir-toast {{ background:#E8F5E9; border:1px solid #A5D6A7; color:#1B5E20; border-radius:12px; padding:10px 14px; font-weight:700; margin:8px 0 14px 0; }}
.file-badge {{ background:#E3F2FD; border:1px solid #90CAF9; color:{BLUE_DARK}; border-radius:12px; padding:8px 12px; font-size:.86rem; margin:8px 0 4px 0; }}
.file-badge b {{ color:{BLUE}; }}
.meth-row {{ display:flex; flex-wrap:wrap; gap:10px; margin:8px 0 14px 0; align-items:stretch; }}
.meth-badge {{ flex:1; min-width:180px; background:#fff; border:1px solid #DCE6F3; border-radius:14px; padding:12px 14px; border-top:4px solid var(--c); }}
.meth-badge .h {{ font-weight:800; color:{BLUE_DARK}; font-size:.92rem; }}
.meth-badge .d {{ color:#546E7A; font-size:.8rem; margin-top:4px; }}
.meth-score {{ background:linear-gradient(105deg,{BLUE_DARK},{BLUE}); color:#fff; border-radius:16px; padding:16px 20px; min-width:160px; }}
.meth-score .v {{ font-size:2.2rem; font-weight:800; line-height:1; }}
.insp-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.insp-card {{ background:#fff; border:1px solid #DCE6F3; border-radius:14px; padding:14px 16px; }}
.insp-card h4 {{ margin:0 0 8px 0; color:{BLUE_DARK}; font-size:.92rem; }}
.insp-card p, .insp-card li {{ color:#37474F; font-size:.9rem; margin:0; }}
.insp-card ol {{ margin:0; padding-left:1.2rem; }}
.insp-card li {{ margin:4px 0; }}
@media (max-width: 900px) {{ .insp-grid {{ grid-template-columns:1fr; }} }}
section[data-testid="stSidebar"] {{ background:#F7FBFF; }}
section[data-testid="stSidebar"] .stMarkdown p {{ font-size:.92rem; }}
</style>
"""


# --------------------------------------------------------------------------- #
# Компонент просмотра BPMN (bpmn-js)
# --------------------------------------------------------------------------- #
# Заголовки раскрытых подпроцессов — крупнее и жирнее (bpmn-js рисует все подписи одним кеглем).
# Один и тот же код выполняется и в просмотрщике, и в странице экспорта SVG — файл выглядит как на экране.
EMPHASIZE_JS = """
  function emphasizeSubprocessTitles() {
    const registry = viewer.get('elementRegistry');
    registry.filter(el => el.type === 'bpmn:SubProcess').forEach(el => {
      const label = registry.getGraphics(el).querySelector('text.djs-label');
      if (!label) return;
      label.style.fontSize = '16px';
      label.style.fontWeight = '700';
      label.querySelectorAll('tspan').forEach(t => {
        t.setAttribute('x', Math.max(4, (el.width - t.getComputedTextLength()) / 2));
        t.setAttribute('y', parseFloat(t.getAttribute('y')) + 4);  // крупный кегль не должен касаться рамки
      });
    });
  }
"""


def _bpmn_js_tags() -> Tuple[str, str]:
    js_inline = read_asset("bpmn-navigated-viewer.production.min.js")
    css_inline = read_asset("diagram-js.css")
    js_tag = f"<script>{js_inline}</script>" if js_inline else f'<script src="{BPMN_JS_CDN}"></script>'
    css_tag = f"<style>{css_inline}</style>" if css_inline else f'<link rel="stylesheet" href="{DIAGRAM_CSS_CDN}">'
    return js_tag, css_tag


def viewer_html(
    xml: str,
    height: int,
    catalog: Optional[Dict[str, Any]] = None,
    copilot: Optional[Dict[str, Any]] = None,
) -> str:
    js_tag, css_tag = _bpmn_js_tags()
    payload = json.dumps(xml).replace("</", "<\\/")
    catalog_js = json.dumps(catalog or {}, ensure_ascii=False).replace("</", "<\\/")
    copilot_js = json.dumps(copilot or {}, ensure_ascii=False).replace("</", "<\\/")
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
  .djs-element.ir-selected .djs-visual > :nth-child(1) {{
    stroke:#0D47A1 !important; stroke-width:4px !important;
    filter:drop-shadow(0 0 7px rgba(13,71,161,.55));
  }}
  #tip {{
    display:none; position:absolute; z-index:20; width:340px; max-width:calc(100% - 24px);
    background:#fff; border:1px solid #90CAF9; border-left:6px solid #1565C0;
    border-radius:14px; box-shadow:0 12px 32px rgba(0,51,102,.22); padding:12px 14px 14px 14px;
    font-size:13px; color:#263238; line-height:1.4;
  }}
  #tip .x {{ position:absolute; top:8px; right:8px; border:0; background:#E3F2FD; color:#003366;
      width:28px; height:28px; border-radius:8px; font-weight:800; cursor:pointer; }}
  #tip .x:hover {{ background:#1565C0; color:#fff; }}
  #tip .kind {{ color:#1565C0; font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:.4px; padding-right:28px; }}
  #tip .name {{ color:#003366; font-weight:800; font-size:15px; margin:4px 0 8px 0; }}
  #tip .meta {{ color:#455A64; margin:3px 0; }}
  #tip .crit {{ display:none; margin:8px 0; background:#FFF3E0; color:#E65100; border-radius:8px; padding:5px 8px; font-weight:700; font-size:12px; }}
  #tip .ai {{ margin-top:8px; background:#E8F1FB; border-radius:10px; padding:8px 10px; color:#0D47A1; }}
  #close {{ display:none; position:absolute; top:16px; right:16px; z-index:9; border:1px solid rgba(255,255,255,.55);
      background:rgba(0,51,102,.55); color:#fff; font-weight:700; font-size:14px; border-radius:12px; padding:9px 16px;
      cursor:pointer; backdrop-filter:blur(4px); opacity:.72; transition:opacity .15s, background .15s; }}
  #close:hover {{ opacity:1; background:rgba(0,51,102,.9); }}
  #wrap.pano {{ position:fixed; top:0; left:0; width:100vw; height:100vh !important; border:0; border-radius:0; z-index:999999; }}
  #wrap.pano .bar {{ display:none; }}
  #wrap.pano #close {{ display:block; }}
  #wrap.pano .hint {{ opacity:.75; left:12px; right:auto; max-width:calc(100% - 100px); }}
  #ai-fab {{
    position:absolute; bottom:24px; right:24px; z-index:1000001;
    width:58px; height:58px; border:0; border-radius:50%;
    background:linear-gradient(135deg,#003366,#1565C0); color:#fff; font-size:22px;
    cursor:pointer; box-shadow:0 8px 22px rgba(0,51,102,.38);
    display:flex; align-items:center; justify-content:center;
    transition:transform .15s, box-shadow .15s;
  }}
  #ai-fab:hover {{ transform:scale(1.07); box-shadow:0 10px 28px rgba(21,101,192,.45); }}
  #ai-fab span {{ font-size:11px; font-weight:800; display:none; }}
  #wrap.ai-open #ai-fab {{ display:none; }}
  #ai-drawer {{
    display:none; position:absolute; bottom:24px; right:24px; z-index:1000002;
    width:380px; height:500px; max-width:calc(100% - 36px); max-height:calc(100% - 48px);
    flex-direction:column; overflow:hidden;
    background:rgba(255,255,255,.93); backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px);
    border:1px solid #90CAF9; border-radius:18px;
    box-shadow:0 16px 40px rgba(0,51,102,.28);
  }}
  #wrap.ai-open #ai-drawer {{ display:flex; }}
  #ai-drawer .ai-head {{
    display:flex; align-items:center; justify-content:space-between; gap:8px;
    padding:12px 14px; color:#fff; font-weight:800; font-size:14px;
    background:linear-gradient(105deg,#003366,#1565C0);
  }}
  #ai-min {{ border:0; background:rgba(255,255,255,.18); color:#fff; border-radius:10px;
      padding:5px 10px; font-weight:700; cursor:pointer; font-size:12px; }}
  #ai-min:hover {{ background:rgba(255,255,255,.32); }}
  #ai-chips {{ display:flex; flex-wrap:wrap; gap:6px; padding:10px 12px 6px 12px; }}
  #ai-chips button {{
    border:1px solid #90CAF9; background:#E3F2FD; color:#003366; border-radius:999px;
    padding:5px 10px; font-size:12px; font-weight:700; cursor:pointer;
  }}
  #ai-chips button:hover {{ background:#1565C0; color:#fff; border-color:#1565C0; }}
  #ai-log {{ flex:1; overflow:auto; padding:4px 12px 10px 12px; font-size:13px; line-height:1.45; }}
  #ai-log .msg {{ margin:8px 0; padding:8px 10px; border-radius:12px; max-width:95%; }}
  #ai-log .u {{ background:#E3F2FD; color:#003366; margin-left:18%; }}
  #ai-log .a {{ background:#F4F8FD; border:1px solid #DCE6F3; color:#263238; }}
  #ai-form {{ display:flex; gap:6px; padding:10px 12px 12px 12px; border-top:1px solid #DCE6F3; background:rgba(255,255,255,.7); }}
  #ai-in {{ flex:1; border:1px solid #90CAF9; border-radius:10px; padding:8px 10px; font-size:13px; outline:none; }}
  #ai-send {{ border:0; border-radius:10px; width:40px; background:linear-gradient(135deg,#003366,#1565C0);
      color:#fff; font-weight:800; cursor:pointer; }}
</style></head>
<body>
<div id="wrap">
  <div class="bar">
    <button id="zin" title="Приблизить">＋</button><button id="zout" title="Отдалить">－</button>
    <button id="fit" title="Вписать в окно">По размеру</button><button id="one" title="Масштаб 100%">100%</button>
    <button id="full" title="Панорама на весь экран (выход — Esc)">⛶ Панорама на весь экран</button>
  </div>
  <button id="close" title="Закрыть панораму (Esc)">✕ Закрыть панораму</button>
  <div id="canvas"></div><div id="err"></div>
  <div id="tip">
    <button class="x" id="tip-x" title="Закрыть">✕</button>
    <div class="kind" id="t-kind"></div>
    <div class="name" id="t-name"></div>
    <div class="meta" id="t-role"></div>
    <div class="crit" id="t-crit">⚡ На критическом пути SLA</div>
    <div class="ai" id="t-ai"></div>
  </div>
  <div class="hint" id="hint"></div>
  <button type="button" id="ai-fab" title="AI-Ассистент процесса">💬</button>
  <div id="ai-drawer" aria-hidden="true">
    <div class="ai-head"><div>💬 AI-Ассистент процесса</div><button type="button" id="ai-min">✕ Свернуть</button></div>
    <div id="ai-chips"></div>
    <div id="ai-log"></div>
    <form id="ai-form" autocomplete="off">
      <input id="ai-in" placeholder="Спросите про SLA, роли, узкие места…" maxlength="400">
      <button type="submit" id="ai-send" title="Отправить">➤</button>
    </form>
  </div>
</div>
{js_tag}
<script>
  const XML = {payload};
  const CATALOG = {catalog_js};
  const COPILOT = {copilot_js};
  const viewer = new BpmnJS({{ container: '#canvas' }});
  const canvas = () => viewer.get('canvas');
  const wrap = document.getElementById('wrap');
  const hint = document.getElementById('hint');
  const tip = document.getElementById('tip');
  const HINT_NORMAL = 'Клик по блоку — карточка · 💬 AI в углу · Ctrl + колесо — масштаб';
  const HINT_PANO = 'Перетаскивание — перемещение · колесо — масштаб · Esc — закрыть панораму';
  hint.textContent = HINT_NORMAL;

  function fit() {{ try {{ canvas().zoom('fit-viewport', 'auto'); }} catch (e) {{}} }}
  function fitSoon() {{ fit(); requestAnimationFrame(fit); setTimeout(fit, 120); setTimeout(fit, 350); }}
  {EMPHASIZE_JS}

  let selectedId = null;
  const IGNORE = /bpmn:(Process|Participant|Lane|Collaboration|Group|TextAnnotation|Association|SequenceFlow|DataObject|DataStoreReference|label)/i;
  function resolveEl(el) {{
    if (!el) return null;
    if (el.type === 'label' || (el.businessObject && el.labelTarget)) return el.labelTarget || el;
    return el;
  }}
  function clearPick() {{
    if (selectedId) {{ try {{ canvas().removeMarker(selectedId, 'ir-selected'); }} catch (e) {{}} }}
    selectedId = null;
    tip.style.display = 'none';
  }}
  function placeTip(evt) {{
    const x = (evt && evt.clientX) || 24;
    const y = (evt && evt.clientY) || 24;
    const pad = 12, w = tip.offsetWidth || 340, h = tip.offsetHeight || 180;
    tip.style.left = Math.max(pad, Math.min(x + 14, wrap.clientWidth - w - pad)) + 'px';
    tip.style.top = Math.max(pad, Math.min(y + 14, wrap.clientHeight - h - pad)) + 'px';
  }}
  function showPick(el, evt) {{
    const id = el.id;
    const meta = CATALOG[id] || {{
      name: (el.businessObject && el.businessObject.name) || id,
      type: (el.type || '').replace('bpmn:', ''),
      role: '—',
      critical: false,
      comment: 'Нет карточки аудита для этого узла — откройте операционный инспектор под диаграммой.'
    }};
    clearPick();
    selectedId = id;
    try {{ canvas().addMarker(id, 'ir-selected'); }} catch (e) {{}}
    document.getElementById('t-kind').textContent = meta.type || '';
    document.getElementById('t-name').textContent = meta.name || '';
    document.getElementById('t-role').textContent = 'Роль: ' + (meta.role || '—');
    document.getElementById('t-crit').style.display = meta.critical ? 'block' : 'none';
    document.getElementById('t-ai').textContent = meta.comment || '';
    tip.style.display = 'block';
    placeTip(evt);
  }}
  viewer.importXML(XML).then(() => {{
    emphasizeSubprocessTitles();
    fit();
    viewer.get('eventBus').on('element.click', function(e) {{
      const el = resolveEl(e.element);
      const t = (el && el.type) || '';
      if (!el || IGNORE.test(t) || t === 'label') {{ clearPick(); return; }}
      if (!/Task|Gateway|Event|SubProcess/i.test(t)) {{ clearPick(); return; }}
      showPick(el, e.originalEvent);
    }});
  }}).catch(e => {{
    const el = document.getElementById('err'); el.style.display = 'flex'; el.textContent = 'Ошибка отображения BPMN: ' + e.message;
  }});
  document.getElementById('tip-x').onclick = ev => {{ ev.stopPropagation(); clearPick(); }};
  tip.addEventListener('mousedown', ev => ev.stopPropagation());
  const zoomBy = k => canvas().zoom(canvas().zoom() * k, 'auto');
  document.getElementById('zin').onclick = () => zoomBy(1.25);
  document.getElementById('zout').onclick = () => zoomBy(0.8);
  document.getElementById('fit').onclick = fit;
  document.getElementById('one').onclick = () => canvas().zoom(1, 'auto');
  window.addEventListener('resize', fit);

  // ---------------- Панорама на весь экран ----------------
  // Приоритет: Fullscreen API. Если он недоступен/отклонён — гарантированный оверлей:
  // iframe растягивается на 100vw x 100vh поверх всей страницы (fixed, z-index 999999).
  let panoMode = null;                     // null | 'api' | 'overlay'
  let savedFrameStyle = null, savedParentOverflow = null;
  const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  const frameEl = (() => {{ try {{ return window.frameElement; }} catch (e) {{ return null; }} }})();
  const parentDoc = (() => {{ try {{ return window.parent.document; }} catch (e) {{ return null; }} }})();

  function setPanoUi(on) {{
    wrap.classList.toggle('pano', on);
    hint.textContent = on ? HINT_PANO : HINT_NORMAL;
    fitSoon();
  }}
  function enterOverlay() {{
    if (!frameEl || panoMode) return false;
    savedFrameStyle = frameEl.getAttribute('style');
    frameEl.style.cssText = (savedFrameStyle || '') +
      ';position:fixed!important;top:0!important;left:0!important;width:100vw!important;height:100vh!important;' +
      'max-width:none!important;z-index:999999!important;border:0!important;background:#fff!important;';
    if (parentDoc) {{ savedParentOverflow = parentDoc.documentElement.style.overflow; parentDoc.documentElement.style.overflow = 'hidden'; }}
    panoMode = 'overlay';
    setPanoUi(true);
    return true;
  }}
  function exitOverlay() {{
    if (panoMode !== 'overlay') return;
    if (savedFrameStyle === null) frameEl.removeAttribute('style'); else frameEl.setAttribute('style', savedFrameStyle);
    if (parentDoc) parentDoc.documentElement.style.overflow = savedParentOverflow || '';
    panoMode = null;
    setPanoUi(false);
  }}
  function enterPanorama() {{
    if (panoMode) return;
    const req = wrap.requestFullscreen || wrap.webkitRequestFullscreen;
    const canApi = (document.fullscreenEnabled || document.webkitFullscreenEnabled) && req;
    if (!canApi) {{ enterOverlay(); return; }}
    try {{
      const p = req.call(wrap);
      if (p && p.catch) p.catch(() => {{ if (!fsElement()) enterOverlay(); }});
    }} catch (e) {{ enterOverlay(); }}
  }}
  function exitPanorama() {{
    if (panoMode === 'overlay') exitOverlay();
    else if (fsElement()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  }}
  function onFsChange() {{
    const on = fsElement() === wrap;
    panoMode = on ? 'api' : (panoMode === 'overlay' ? 'overlay' : null);
    setPanoUi(on || panoMode === 'overlay');
  }}
  document.addEventListener('fullscreenchange', onFsChange);
  document.addEventListener('webkitfullscreenchange', onFsChange);
  document.getElementById('full').onclick = enterPanorama;
  document.getElementById('close').onclick = exitPanorama;
  const onKey = e => {{
    if (e.key === 'Escape') {{
      if (wrap.classList.contains('ai-open')) {{ setCopilot(false); e.stopPropagation(); return; }}
      if (tip.style.display === 'block') {{ clearPick(); if (!panoMode) e.stopPropagation(); }}
      if (panoMode) exitPanorama();
    }}
  }};
  document.addEventListener('keydown', onKey);
  if (parentDoc) parentDoc.addEventListener('keydown', onKey);   // Esc, когда фокус вне iframe (режим оверлея)

  // В панораме колесо мышки = зум вокруг курсора (в обычном режиме — как у bpmn-js: Ctrl + колесо)
  wrap.addEventListener('wheel', e => {{
    if (!wrap.classList.contains('pano')) return;
    e.preventDefault(); e.stopPropagation();
    const r = document.getElementById('canvas').getBoundingClientRect();
    const scale = Math.min(6, Math.max(0.03, canvas().zoom() * Math.exp(-e.deltaY * 0.0016)));
    canvas().zoom(scale, {{ x: e.clientX - r.left, y: e.clientY - r.top }});
  }}, {{ capture: true, passive: false }});

  // ---------------- Плавающий AI-ассистент (обычный вид и панорама) ----------------
  const drawer = document.getElementById('ai-drawer');
  const logEl = document.getElementById('ai-log');
  const chipsEl = document.getElementById('ai-chips');
  const inputEl = document.getElementById('ai-in');
  function mdLite(s) {{
    return String(s || '')
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')
      .replace(/\\n/g,'<br>');
  }}
  function addMsg(role, text) {{
    const d = document.createElement('div');
    d.className = 'msg ' + (role === 'user' ? 'u' : 'a');
    d.innerHTML = mdLite(text);
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
  }}
  function chipBy(id) {{
    return ((COPILOT.chips || []).find(c => c.id === id) || {{}}).a;
  }}
  function copilotAnswer(msg) {{
    const q = (msg || '').trim();
    if (!q) return 'Напишите вопрос о процессе — SLA, роли или как ускорить.';
    const low = q.toLowerCase();
    const chips = COPILOT.chips || [];
    const exact = chips.find(c => c.q === q || (c.label && c.label.toLowerCase() === low));
    if (exact) return exact.a;
    if (/ускор|оптимиз|сократ|быстрее|параллел/.test(low)) return chipBy('speed') || COPILOT.fallback;
    if (/sla|срок|срыв|задерж|критич|длительн|узк/.test(low)) return chipBy('sla') || COPILOT.fallback;
    if (/роль|нагруз|bus|риск|исполнител|диспетчер|загруж/.test(low)) return chipBy('roles') || COPILOT.fallback;
    return COPILOT.fallback || COPILOT.greeting || 'Сгенерируйте диаграмму — тогда отвечу по метрикам.';
  }}
  function ask(text) {{
    const q = (text || '').trim();
    if (!q) return;
    addMsg('user', q);
    addMsg('assistant', copilotAnswer(q));
    inputEl.value = '';
  }}
  function setCopilot(on) {{
    wrap.classList.toggle('ai-open', on);
    drawer.setAttribute('aria-hidden', on ? 'false' : 'true');
    if (on) {{
      try {{ clearPick(); }} catch (e) {{}}
      if (!logEl.childElementCount && COPILOT.greeting) addMsg('assistant', COPILOT.greeting);
      setTimeout(() => inputEl.focus(), 30);
    }}
  }}
  (COPILOT.chips || []).forEach(c => {{
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = c.label;
    b.onclick = () => {{ setCopilot(true); ask(c.q); }};
    chipsEl.appendChild(b);
  }});
  document.getElementById('ai-fab').onclick = ev => {{ ev.stopPropagation(); setCopilot(true); }};
  document.getElementById('ai-min').onclick = ev => {{ ev.stopPropagation(); setCopilot(false); }};
  document.getElementById('ai-form').onsubmit = ev => {{ ev.preventDefault(); ask(inputEl.value); }};
  drawer.addEventListener('mousedown', ev => ev.stopPropagation());
  drawer.addEventListener('click', ev => ev.stopPropagation());
  drawer.addEventListener('wheel', ev => ev.stopPropagation(), {{ passive: true }});
  document.getElementById('ai-fab').addEventListener('mousedown', ev => ev.stopPropagation());
</script></body></html>
"""


def svg_export_html(xml: str, file_name: str) -> str:
    """Мини-страница с кнопкой «Скачать .svg»: bpmn-js в невидимом контейнере → viewer.saveSVG() → файл."""
    js_tag, css_tag = _bpmn_js_tags()
    payload = json.dumps(xml).replace("</", "<\\/")
    name = json.dumps(file_name)
    return f"""
<!doctype html><html><head><meta charset="utf-8">{css_tag}
<style>
  html,body {{ margin:0; background:transparent; font-family:"Source Sans Pro",-apple-system,Segoe UI,Roboto,Arial,sans-serif; overflow:hidden; }}
  #host {{ position:absolute; left:-12000px; top:0; width:2600px; height:1600px; }}
  button {{ width:100%; height:44px; box-sizing:border-box; border:2px solid {BLUE}; color:{BLUE}; background:#fff;
      border-radius:12px; font-weight:700; font-size:15px; cursor:pointer; transition:background .15s; }}
  button:hover:not(:disabled) {{ background:#E3F2FD; color:{BLUE_DARK}; border-color:{BLUE_DARK}; }}
  button:disabled {{ opacity:.55; cursor:progress; }}
</style></head>
<body>
<button id="dl" disabled>⏳  Готовим .svg…</button>
<div id="host"></div>
{js_tag}
<script>
  const XML = {payload};
  const FILE_NAME = {name};
  const btn = document.getElementById('dl');
  const viewer = new BpmnJS({{ container: '#host' }});
  {EMPHASIZE_JS}
  viewer.importXML(XML).then(() => {{
    emphasizeSubprocessTitles();
    btn.disabled = false; btn.textContent = '⬇️  Скачать .svg';
  }}).catch(e => {{ btn.textContent = 'SVG недоступен'; btn.title = e.message; }});
  btn.onclick = async () => {{
    try {{
      let {{ svg }} = await viewer.saveSVG();
      svg = svg.replace(/<svg\\b/, '<svg style="background:#fff"');   // белый фон в браузере и просмотрщиках
      const url = URL.createObjectURL(new Blob([svg], {{ type: 'image/svg+xml;charset=utf-8' }}));
      const a = document.createElement('a');
      a.href = url; a.download = FILE_NAME; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    }} catch (e) {{ btn.textContent = 'Ошибка экспорта SVG'; btn.title = e.message; }}
  }};
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


def _landscape_chips(items: List[Dict[str, Any]], css: str, icon: str) -> str:
    chips = []
    for it in items:
        steps = ", ".join(str(n) for n in it.get("steps", []))
        roles = ", ".join(it.get("roles", []))
        tip = f"Шаги регламента: {steps}" + (f" · Роли: {roles}" if roles else "")
        mentions = int(it.get("mentions", 1))
        count = f"<small>×{mentions}</small>" if mentions > 1 else ""
        chips.append(f'<span class="{css}" title="{esc(tip)}">{icon} {esc(it["name"])}{count}</span>')
    return "".join(chips)


def render_landscape(audit: Dict[str, Any]) -> None:
    """ИТ-системы (синие плашки) и документы процесса (зелёные плашки), найденные в тексте регламента."""
    systems: List[Dict[str, Any]] = audit.get("it_systems") or []
    artifacts: List[Dict[str, Any]] = audit.get("artifacts") or []
    st.markdown('<div class="ir-title" style="margin-top:18px">ИТ-ландшафт и документооборот процесса</div>', unsafe_allow_html=True)
    sys_html = _landscape_chips(systems, "chip-sys", "💻") or '<div class="land-empty">ИТ-системы в регламенте не упомянуты — ' \
        "процесс не привязан к системам: это риск ручной обработки.</div>"
    doc_html = _landscape_chips(artifacts, "chip-doc", "📄") or '<div class="land-empty">Документы и артефакты в регламенте не обнаружены.</div>'
    st.markdown(
        f'<div class="land-cap">ИТ-системы ({len(systems)})</div>{sys_html}'
        f'<div class="land-cap">Документы и артефакты ({len(artifacts)})</div>{doc_html}',
        unsafe_allow_html=True,
    )


def render_methodology(audit: Dict[str, Any]) -> None:
    """Блок «Методологический контроль BPMN 2.0» — бейджи проверок и балл качества."""
    meth = audit.get("methodology") if isinstance(audit.get("methodology"), dict) else {}
    if not meth:
        return
    score = int(meth.get("score") or 0)
    color = OK if score >= 85 else (WARN if score >= 65 else BAD)
    st.markdown('<div class="ir-title" style="margin-top:8px">Методологический контроль BPMN 2.0</div>', unsafe_allow_html=True)
    badges = [
        f'<div class="meth-score" style="background:linear-gradient(105deg,{color},{BLUE})">'
        f'<div class="k" style="opacity:.85;font-size:.75rem;letter-spacing:.5px">QUALITY SCORE</div>'
        f'<div class="v">{score}%</div>'
        f'<div style="opacity:.9;font-size:.82rem;margin-top:4px">интегральная оценка модели</div></div>'
    ]
    for chk in meth.get("checks") or []:
        passed = bool(chk.get("passed"))
        c = OK if passed else BAD
        mark = "✓" if passed else "✗"
        findings = chk.get("findings") or []
        extra = f"<br>{esc(findings[0])}" if findings and not passed else ""
        badges.append(
            f'<div class="meth-badge" style="--c:{c}"><div class="h">{mark} {esc(chk.get("title"))}</div>'
            f'<div class="d">{esc(chk.get("detail"))}{extra}</div></div>'
        )
    st.markdown('<div class="meth-row">' + "".join(badges) + "</div>", unsafe_allow_html=True)
    with st.expander("Детали проверок нотации"):
        for chk in meth.get("checks") or []:
            st.markdown(f"**{chk.get('title')}** — {chk.get('points')} / {chk.get('weight')} баллов. {chk.get('detail')}")
            for fnd in chk.get("findings") or []:
                st.markdown(f"- {fnd}")


def render_task_inspector(xml: str, audit: Dict[str, Any], text: str) -> None:
    """🔍 Операционный инспектор задачи — карточка рабочего регламента по выбранному шагу."""
    st.markdown('<div class="ir-section">🔍 Операционный инспектор задачи</div>', unsafe_allow_html=True)
    st.caption("Выберите шаг схемы — квалификацию исполнителя, порядок действий, СИЗ и результат.")
    try:
        struct = parse_bpmn_structure(xml)
    except Exception:  # noqa: BLE001
        st.info("Не удалось разобрать схему для инспектора.")
        return
    tasks = sorted(
        (
            n
            for n in (struct.get("nodes") or {}).values()
            if n.get("type") in ("task", "userTask", "scriptTask") and (n.get("name") or "").strip()
        ),
        key=lambda n: (float(n.get("x") or 0), float(n.get("y") or 0)),
    )
    if not tasks:
        st.info("В схеме нет именованных задач.")
        return
    labels = [f"{n['name']}  ·  {n.get('lane') or '—'}" for n in tasks]
    choice = st.selectbox("Задача текущей схемы", labels, key="inspector_choice")
    if not choice:
        return
    node = tasks[labels.index(choice)]
    ctx = build_process_context(text, xml, audit)
    cache = st.session_state.setdefault("inspector_cache", {})
    cache_key = f"{node['name']}|{node.get('lane') or ''}"
    if cache_key not in cache:
        with st.spinner("Собираем операционную карточку…"):
            cache[cache_key] = inspect_task_details(str(node["name"]), str(node.get("lane") or ""), ctx)
    card_data = cache[cache_key]
    steps_html = "".join(f"<li>{esc(s)}</li>" for s in (card_data.get("procedure_steps") or []))
    st.markdown(
        f'<div class="insp-grid">'
        f'<div class="insp-card"><h4>Квалификация и допуск</h4><p>{esc(card_data.get("role_requirements"))}</p></div>'
        f'<div class="insp-card"><h4>СИЗ, приборы и системы</h4><p>{esc(card_data.get("safety_and_tools"))}</p></div>'
        f'<div class="insp-card"><h4>Порядок действий</h4><ol>{steps_html}</ol></div>'
        f'<div class="insp-card"><h4>Вход / выход</h4><p><b>Основание:</b> {esc(card_data.get("input_trigger"))}</p>'
        f'<p style="margin-top:8px"><b>Результат:</b> {esc(card_data.get("output_artifact"))}</p></div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def render_audit(audit: Dict[str, Any]) -> None:
    st.markdown('<div class="ir-section">Аудит бизнес-архитектуры</div>', unsafe_allow_html=True)
    render_methodology(audit)
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

    render_landscape(audit)

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
    st.session_state.pop("inspector_cache", None)
    st.session_state.pop("inspector_choice", None)


def on_example_change() -> None:
    examples = load_examples()
    choice = st.session_state["example_choice"]
    if choice in examples:
        st.session_state["reg_text"] = examples[choice]["text"]
        st.session_state["file_stem"] = examples[choice]["stem"]
        st.session_state.pop("last_uploaded_filename", None)
        st.session_state.pop("upload_badge", None)
    else:
        stem = Path(st.session_state.get("last_uploaded_filename") or "custom_process").stem
        st.session_state["file_stem"] = re.sub(r"[^\w.\-]+", "_", stem, flags=re.U) or "custom_process"
    st.session_state["chat_messages"] = []
    st.session_state.pop("diagram_updated_by_assistant", None)
    st.session_state.pop("inspector_cache", None)
    st.session_state.pop("inspector_choice", None)


def _read_txt_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_docx_bytes(data: bytes) -> Tuple[Optional[str], Optional[str]]:
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError:
        return None, "Для чтения .docx установите пакет: `pip install python-docx>=1.0.0`"
    try:
        doc = Document(io.BytesIO(data))
        parts: List[str] = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        text = "\n".join(parts).strip()
        if not text:
            return None, "В файле .docx не найден текстовый слой (пустые абзацы и таблицы)."
        return text, None
    except Exception as exc:  # noqa: BLE001
        return None, f"Не удалось прочитать .docx: {type(exc).__name__}: {exc}"


def _read_pdf_bytes(data: bytes) -> Tuple[Optional[str], Optional[str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "Для чтения .pdf установите пакет: `pip install pypdf>=4.0.0`"
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            if extracted.strip():
                pages.append(extracted)
        text = "\n".join(pages).strip()
        if not text:
            return None, "В PDF нет текстового слоя (возможно, скан без OCR)."
        return text, None
    except Exception as exc:  # noqa: BLE001
        return None, f"Не удалось прочитать .pdf: {type(exc).__name__}: {exc}"


def extract_regulation_from_upload(uploaded: Any) -> Tuple[Optional[str], Optional[str]]:
    """Безопасный разбор .txt / .docx / .pdf → сырой текст или сообщение об ошибке."""
    try:
        data = uploaded.getvalue()
    except Exception as exc:  # noqa: BLE001
        return None, f"Не удалось прочитать файл: {exc}"
    name = (getattr(uploaded, "name", "") or "").lower()
    if name.endswith(".txt"):
        try:
            return _read_txt_bytes(data), None
        except Exception as exc:  # noqa: BLE001
            return None, f"Не удалось прочитать .txt: {exc}"
    if name.endswith(".docx"):
        return _read_docx_bytes(data)
    if name.endswith(".pdf"):
        return _read_pdf_bytes(data)
    return None, "Поддерживаются только файлы .txt, .docx и .pdf."


def apply_uploaded_regulation(uploaded: Any) -> None:
    """State Guard: файл обрабатывается один раз, пока не сменится имя."""
    if uploaded is None:
        return
    name = getattr(uploaded, "name", None)
    if not name or name == st.session_state.get("last_uploaded_filename"):
        return
    raw, err = extract_regulation_from_upload(uploaded)
    if err:
        st.warning(err)
        st.session_state["last_uploaded_filename"] = name  # не крутить предупреждение на каждом rerun
        return
    try:
        text = normalize_regulation(raw or "")
    except Exception:  # noqa: BLE001 — даже «грязный» текст должен попасть в поле
        text = (raw or "").strip()
    if not text.strip():
        st.warning("После очистки файл оказался пустым — проверьте содержимое.")
        return
    stem = re.sub(r"[^\w.\-]+", "_", Path(name).stem, flags=re.U) or "custom_process"
    st.session_state["reg_text"] = text
    st.session_state["last_uploaded_filename"] = name
    st.session_state["file_stem"] = stem
    st.session_state["example_choice"] = CUSTOM_LABEL
    st.session_state["upload_badge"] = {"name": name, "chars": len(text)}
    st.session_state["chat_messages"] = []
    st.session_state.pop("diagram_updated_by_assistant", None)
    st.session_state.pop("inspector_cache", None)
    st.session_state.pop("inspector_choice", None)


def render_downloads(key: str) -> None:
    """Скачивание: BPMN 2.0, SVG-картинка и Паспорт процесса (.md) — три равные колонки."""
    result = st.session_state.get("result")
    ok = bool(result and not result["error"])
    stem = st.session_state.get("file_stem", "process")
    passport = ""
    if ok:
        try:
            passport = generate_process_passport(
                result.get("xml") or "",
                result.get("audit") or {},
                st.session_state.get("reg_text") or "",
            )
        except Exception:  # noqa: BLE001 — кнопка просто недоступна, UI не падает
            passport = ""
    col_bpmn, col_svg, col_pass = st.columns(3, gap="small")
    with col_bpmn:
        st.download_button(
            "⬇️  Скачать .bpmn",
            data=(result["xml"] if ok else ""),
            file_name=f"{stem}.bpmn",
            mime="application/xml",
            disabled=not ok,
            key=f"dl_bpmn_{key}",
        )
    with col_svg:
        if ok:
            page = svg_export_html(result["xml"], f"{stem}.svg")
            if hasattr(st, "iframe"):
                st.iframe(page, height=48)
            else:
                components.html(page, height=48, scrolling=False)
        else:
            st.button("⬇️  Скачать .svg", disabled=True, key=f"dl_svg_{key}")
    with col_pass:
        st.download_button(
            "⬇️  Скачать Паспорт (.md)",
            data=passport or "",
            file_name=f"{stem}_passport.md",
            mime="text/markdown",
            disabled=not (ok and bool(passport)),
            key=f"dl_passport_{key}",
        )


def render_input_panel(labels: List[str], compact: bool = False, show_downloads: bool = True) -> None:
    """Блок «регламент + генерация». compact=True — двухколоночная компоновка для аккордеона."""
    box_left, box_right = st.columns([3, 2], gap="large") if compact else (st.container(), st.container())
    with box_left:
        uploaded = st.file_uploader(
            "Загрузить регламент (.docx, .pdf, .txt)",
            type=["docx", "pdf", "txt"],
            key="reg_upload",
            help="Файл обрабатывается один раз: повторный прогон страницы не затирает правки в поле текста.",
        )
        apply_uploaded_regulation(uploaded)
        badge = st.session_state.get("upload_badge") or {}
        if badge.get("name"):
            chars = f"{int(badge.get('chars') or 0):,}".replace(",", " ")
            st.markdown(
                f'<div class="file-badge">📄 <b>{esc(badge["name"])}</b> · распознано {chars} символов</div>',
                unsafe_allow_html=True,
            )
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
            height=220 if compact else 360,
        )
    with box_right:
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
        if show_downloads:
            render_downloads("left")


def render_diagram(canvas_height: int) -> None:
    result = st.session_state.get("result")
    if not result:
        st.info("Выберите регламент и нажмите «Сгенерировать BPMN 2.0».")
    elif result["error"]:
        st.error(result["error"])
    else:
        catalog = build_diagram_catalog(
            result["xml"], result.get("audit") or {}, st.session_state.get("reg_text") or ""
        )
        copilot = build_canvas_copilot(
            result["xml"], result.get("audit") or {}, st.session_state.get("reg_text") or ""
        )
        page = viewer_html(result["xml"], canvas_height, catalog, copilot)
        if hasattr(st, "iframe"):  # Streamlit ≥ 1.5x: st.components.v1.html объявлен устаревшим
            st.iframe(page, height=canvas_height + 16)
        else:
            components.html(page, height=canvas_height + 16, scrolling=False)


def _send_assistant(prompt: str) -> None:
    """Отправляет реплику ассистенту и, если он изменил процесс, обновляет холст и аудит."""
    prompt = (prompt or "").strip()
    if not prompt:
        return
    messages: List[Dict[str, str]] = st.session_state.setdefault("chat_messages", [])
    messages.append({"role": "user", "content": prompt})
    result = st.session_state.get("result") or {}
    with st.spinner("Ассистент анализирует процесс…"):
        reply, new_text, new_xml, new_audit = assistant_chat(
            prompt,
            messages[:-1],
            result.get("xml") or "",
            result.get("audit") or {},
            st.session_state.get("reg_text") or "",
            use_llm=True,
        )
    messages.append({"role": "assistant", "content": reply})
    if new_text and new_xml and new_audit:
        st.session_state["reg_text"] = new_text
        st.session_state["result"] = {"xml": new_xml, "audit": new_audit, "error": ""}
        st.session_state["diagram_updated_by_assistant"] = True
        st.session_state["file_stem"] = st.session_state.get("file_stem") or "custom_process"
        st.session_state.pop("inspector_cache", None)
        st.session_state.pop("inspector_choice", None)


def render_assistant() -> None:
    """Сайдбар: «💬 AI-Ассистент Бизнес-Архитектора» — аналитика, правка на лету, реверс-генерация."""
    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = []
    with st.sidebar:
        st.markdown("### 💬 AI-Ассистент Бизнес-Архитектора")
        st.caption(
            "Знает текущий процесс: роли, SLA, bus-factor, циклы возврата, ИТ-системы. "
            "Может объяснить узкие места, изменить схему командой («Добавь согласование с экологами после шага 3») "
            "или сгенерировать должностную инструкцию по BPMN."
        )
        for label, prompt in QUICK_PROMPTS:
            if st.button(label, key=f"qp_{hash(label)}", use_container_width=True):
                _send_assistant(prompt)
                st.rerun()
        for msg in st.session_state["chat_messages"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
        typed = st.chat_input("Спросите про SLA, роли или измените процесс…")
        if typed:
            _send_assistant(typed)
            st.rerun()
        if st.session_state.get("chat_messages") and st.button("Очистить диалог", use_container_width=True):
            st.session_state["chat_messages"] = []
            st.session_state.pop("diagram_updated_by_assistant", None)
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="Архитектор BPMN — Интер РАО", page_icon="⚡", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    render_assistant()
    # Часть виджетов не рисуется в отдельных режимах — не даём Streamlit стереть их состояние.
    for _k in ("reg_text", "example_choice"):
        if _k in st.session_state:
            st.session_state[_k] = st.session_state[_k]

    st.markdown(
        """
<div class="ir-hero">
  <div><h1>⚡ Архитектор BPMN-диаграмм</h1>
  <p>ПАО «Интер РАО» · Дирекция бизнес-архитектуры · регламент → BPMN 2.0 → аудит процесса</p></div>
  <div class="ir-badges"><span>BPMN 2.0.2</span><span>demo.bpmn.io ready</span><span>ИИ + fail-safe</span><span>MCP</span></div>
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

    mode_col, _, dl_col = st.columns([5, 1, 5], gap="medium", vertical_alignment="center")
    with mode_col:
        view = st.radio(
            "Режим отображения",
            [VIEW_SPLIT, VIEW_WIDE],
            key="view_mode",
            horizontal=True,
            label_visibility="collapsed",
        )
    wide = view == VIEW_WIDE
    if st.session_state.pop("diagram_updated_by_assistant", False):
        st.markdown(
            '<div class="ir-toast">✨ Диаграмма обновлена ассистентом в диалоге</div>',
            unsafe_allow_html=True,
        )
    if wide:
        with dl_col:
            render_downloads("wide")
        with st.expander("Регламент и настройки", expanded=False):
            render_input_panel(labels, compact=True, show_downloads=False)
        render_diagram(DIAGRAM_HEIGHT_WIDE)
    else:
        left, right = st.columns([5, 7], gap="large")
        with left:
            render_input_panel(labels)
        with right:
            render_diagram(DIAGRAM_HEIGHT)

    result = st.session_state.get("result")
    if result and not result["error"]:
        render_task_inspector(result["xml"], result["audit"], st.session_state.get("reg_text") or "")
        render_audit(result["audit"])
        render_details(result["audit"])

main()
