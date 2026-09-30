"""
Веб-интерфейс «Архитектор BPMN-диаграмм» — ПАО «Интер РАО».

Запуск:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
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
    export_docx_passport,
    generate_bpmn_from_text,
    generate_process_passport,
    generate_raci_matrix,
    inspect_task_details,
    normalize_regulation,
    optimize_process_to_be,
    parse_bpmn_structure,
    parse_regulation,
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
DIAGRAM_HEIGHT_WIDE = 900  # широкий вид — схема сразу крупная
VIEW_SPLIT = "🗂  Раздельный вид"
VIEW_WIDE = "🖥  Широкий вид"
ASIS_LABEL = "Текущий процесс (As-Is)"
TOBE_LABEL = "Целевой оптимизированный (To-Be)"

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


def _reg_hash(text: str, *parts: object) -> str:
    payload = "\u001f".join([text or "", *[str(p) for p in parts]])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_cache_slice(audit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Компактный срез аудита для ключа кэша To-Be (без code/trace)."""
    data = audit or {}
    sla = data.get("sla") or {}
    meth = data.get("methodology") or {}
    return {
        "critical_path_hours": sla.get("critical_path_hours"),
        "with_rework_hours": sla.get("with_rework_hours"),
        "rework_hours": sla.get("rework_hours"),
        "breach": sla.get("breach"),
        "quality": meth.get("score"),
        "rework_n": len(data.get("rework_loops") or []),
    }


@st.cache_data(show_spinner=False)
def cached_generate_bpmn(text_hash: str, text: str, use_llm: bool) -> Tuple[str, Dict[str, Any], str]:
    """Генерация XML + аудит по sha256 текста регламента. Повторный вызов — из кэша Streamlit."""
    _ = text_hash
    return generate_bpmn_from_text(text, use_llm=use_llm)


@st.cache_data(show_spinner=False)
def cached_optimize_to_be(text_hash: str, text: str, asis_core: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """To-Be по хешу регламента: вкладки и смена вида не пересчитывают граф."""
    _ = text_hash
    return optimize_process_to_be(text, asis_core)


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
.bar-row {{ display:grid; grid-template-columns: minmax(72px, 30%) minmax(48px, 1fr) auto; align-items:center; gap:8px 10px; margin:9px 0; font-size:.88rem; color:#37474F; }}
.bar-row .n {{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.bar-row .t {{ background:#E8EEF7; border-radius:8px; height:14px; overflow:hidden; }}
.bar-row .f {{ height:100%; border-radius:8px; background:linear-gradient(90deg,{BLUE},{BLUE_DARK}); }}
.bar-row .f.hot {{ background:linear-gradient(90deg,#EF6C00,{BAD}); }}
.bar-row .f.cool {{ background:linear-gradient(90deg,#81C784,{OK}); }}
.bar-row .p {{ min-width:72px; text-align:right; font-weight:700; color:{BLUE_DARK}; white-space:nowrap; }}
.bar-ok {{ display:inline-block; margin-left:6px; background:#E8F5E9; color:{OK}; border:1px solid #A5D6A7;
    border-radius:999px; padding:0 7px; font-size:.68rem; font-weight:800; vertical-align:middle; }}
.bar-delta {{ display:block; font-size:.72rem; font-weight:600; color:#78909C; }}
.chip-path-tobe .chip {{ background:#E8F5E9; border-color:#A5D6A7; }}
.chip-path-tobe .chip b {{ color:{OK}; }}
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
.raci-wrap {{ overflow-x:auto; background:#fff; border:1px solid #DCE6F3; border-radius:16px; padding:8px 10px 12px 10px; }}
table.raci {{ border-collapse:collapse; font-size:.82rem; width:100%; min-width:520px; }}
table.raci th {{ background:{BLUE_DARK}; color:#fff; padding:8px 10px; text-align:left; font-weight:700; white-space:nowrap; }}
table.raci td {{ padding:7px 8px; border-bottom:1px solid #EEF3FA; color:#263238; vertical-align:middle; }}
table.raci td.step {{ min-width:180px; font-weight:600; color:{BLUE_DARK}; }}
table.raci td.cell {{ text-align:center; white-space:nowrap; }}
.raci-b {{ display:inline-block; min-width:22px; height:22px; line-height:22px; text-align:center; border-radius:6px; font-weight:800; font-size:.72rem; color:#fff; margin:0 2px; padding:0 5px; }}
.raci-b.R {{ background:{BLUE}; }} .raci-b.A {{ background:{OK}; }} .raci-b.C {{ background:{WARN}; }} .raci-b.I {{ background:#607D8B; }}
.raci-legend {{ color:#546E7A; font-size:.82rem; margin:6px 0 10px 0; }}
.tobe-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:8px 0 14px 0; }}
.tobe-card {{ background:#E8F5E9; border:1px solid #A5D6A7; border-radius:16px; padding:14px 16px; color:#1B5E20; }}
.tobe-card .k {{ font-size:.75rem; font-weight:800; letter-spacing:.4px; text-transform:uppercase; opacity:.8; }}
.tobe-card .v {{ font-size:1.7rem; font-weight:800; line-height:1.15; margin:4px 0; }}
.tobe-card .s {{ font-size:.84rem; }}
.tobe-act {{ background:#fff; border:1px solid #DCE6F3; border-left:6px solid {OK}; border-radius:12px; padding:10px 14px; margin:6px 0; font-size:.92rem; color:#263238; }}
@media (max-width: 900px) {{ .insp-grid {{ grid-template-columns:1fr; }} .tobe-grid {{ grid-template-columns:1fr; }} }}
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
  #ai-drawer, #copilot-box {{
    display:none; position:absolute; bottom:24px; right:24px; z-index:1000002;
    width:380px; height:500px; max-width:calc(100% - 36px); max-height:calc(100% - 48px);
    flex-direction:column; overflow:hidden;
    background:rgba(255,255,255,.93); backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px);
    border:1px solid #90CAF9; border-radius:18px;
    box-shadow:0 16px 40px rgba(0,51,102,.28);
  }}
  #wrap.ai-open #ai-drawer, #wrap.ai-open #copilot-box {{ display:flex; }}
  #ai-drawer .ai-head, #copilot-box .ai-head {{
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
  #ai-log, #copilot-messages, .copilot-messages {{
    flex:1; min-height:0; overflow-y:auto !important; max-height:380px;
    padding:4px 12px 10px 12px; font-size:13px; line-height:1.45; overscroll-behavior:contain;
  }}
  #ai-log .msg, #copilot-messages .msg {{ margin:8px 0; padding:8px 10px; border-radius:12px; max-width:95%; }}
  #ai-log .u, #copilot-messages .u {{ background:#E3F2FD; color:#003366; margin-left:18%; }}
  #ai-log .a, #copilot-messages .a {{ background:#F4F8FD; border:1px solid #DCE6F3; color:#263238; }}
  #ai-form {{ display:flex; gap:6px; padding:10px 12px 12px 12px; border-top:1px solid #DCE6F3; background:rgba(255,255,255,.7); }}
  #ai-in {{ flex:1; border:1px solid #90CAF9; border-radius:10px; padding:8px 10px; font-size:13px; outline:none; }}
  #ai-send {{ border:0; border-radius:10px; width:40px; background:linear-gradient(135deg,#003366,#1565C0);
      color:#fff; font-weight:800; cursor:pointer; }}
  .inspector-card {{ overflow-y:auto !important; max-height:min(380px, 70vh); overscroll-behavior:contain; }}
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
  <div id="tip" class="inspector-card">
    <button class="x" id="tip-x" title="Закрыть">✕</button>
    <div class="kind" id="t-kind"></div>
    <div class="name" id="t-name"></div>
    <div class="meta" id="t-role"></div>
    <div class="crit" id="t-crit">⚡ На критическом пути SLA</div>
    <div class="ai" id="t-ai"></div>
  </div>
  <div class="hint" id="hint"></div>
  <button type="button" id="ai-fab" title="AI-Ассистент процесса">💬</button>
  <div id="copilot-box" class="copilot-box" aria-hidden="true">
    <div class="ai-head"><div>💬 AI-Ассистент процесса</div><button type="button" id="ai-min">✕ Свернуть</button></div>
    <div id="ai-chips"></div>
    <div id="copilot-messages" class="copilot-messages"></div>
    <form id="ai-form" autocomplete="off">
      <input id="ai-in" placeholder="Спросите про SLA, To-Be, роли…" maxlength="400">
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
  function focusStart() {{
    try {{
      const registry = viewer.get('elementRegistry');
      const starts = registry.filter(el => el.type === 'bpmn:StartEvent');
      const start = starts.find(el => !(el.parent && /SubProcess/i.test((el.parent.type || '')))) || starts[0];
      if (start && canvas().scrollToElement) canvas().scrollToElement(start);
    }} catch (e) {{}}
  }}
  function fitSoon() {{
    fit();
    requestAnimationFrame(fit);
    setTimeout(fit, 120);
    setTimeout(focusStart, 380);
  }}
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
    fitSoon();
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
  // В панораме колесо = зум, кроме оверлеев (чат, тултип инспектора) — там нативный скролл.
  const overlaySel = '#copilot-box, #copilot-messages, #ai-drawer, #ai-log, #tip, .copilot-box, .copilot-messages, .inspector-card, #ai-form, #ai-chips, #ai-fab';
  wrap.addEventListener('wheel', e => {{
    if (!wrap.classList.contains('pano')) return;
    const hit = e.target && e.target.closest && e.target.closest(overlaySel);
    if (hit) return;
    e.preventDefault(); e.stopPropagation();
    const r = document.getElementById('canvas').getBoundingClientRect();
    const scale = Math.min(6, Math.max(0.03, canvas().zoom() * Math.exp(-e.deltaY * 0.0016)));
    canvas().zoom(scale, {{ x: e.clientX - r.left, y: e.clientY - r.top }});
  }}, {{ capture: true, passive: false }});

  // ---------------- Плавающий AI-ассистент (обычный вид и панорама) ----------------
  const drawer = document.getElementById('copilot-box') || document.getElementById('ai-drawer');
  const logEl = document.getElementById('copilot-messages') || document.getElementById('ai-log');
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
    if (!q) return 'Напишите вопрос о процессе — SLA, сравнение As-Is/To-Be, роли или читаемость.';
    const low = q.toLowerCase();
    const chips = COPILOT.chips || [];
    const exact = chips.find(c => c.q === q || (c.label && c.label.toLowerCase() === low));
    if (exact) return exact.a;
    if (/quality|качеств|почему.*(?:score|балл|нотац|линтер)|(?:упал|изменил).*quality/.test(low))
      return COPILOT.quality || COPILOT.readability || COPILOT.fallback;
    if (/как\\s+(?:мы\\s+)?сократ|за сч[её]т|за счет чего|объясни подробн|почему.*(?:сократ|ускор|экономи)|в ч[её]м причина.*(?:сократ|экономи)/.test(low))
      return COPILOT.why || chipBy('tobe') || COPILOT.fallback;
    if (/сравни|as-is|as is|to-be|tobe|до и после|до\\/после|целев/.test(low))
      return chipBy('tobe') || COPILOT.compare || COPILOT.fallback;
    if (/читаем|метро|анти-метро|подпроцесс|методолог/.test(low))
      return COPILOT.readability || chipBy('tobe') || COPILOT.fallback;
    if (/ускор|оптимиз|быстрее|параллел/.test(low)) return chipBy('speed') || COPILOT.fallback;
    if (/sla|срок|срыв|задерж|критич|длительн/.test(low)) return chipBy('sla') || COPILOT.fallback;
    if (/цикл|возврат|доработ|rework/.test(low)) return COPILOT.loops || COPILOT.fallback;
    if (/систем|it\b|ит-|документ|ландшафт/.test(low)) return COPILOT.landscape || COPILOT.fallback;
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
  const stopScroll = e => e.stopPropagation();
  document.getElementById('copilot-box')?.addEventListener('wheel', stopScroll, {{ capture: true, passive: false }});
  document.getElementById('copilot-messages')?.addEventListener('wheel', stopScroll, {{ capture: true, passive: false }});
  document.getElementById('ai-drawer')?.addEventListener('wheel', stopScroll, {{ capture: true, passive: false }});
  document.getElementById('ai-log')?.addEventListener('wheel', stopScroll, {{ capture: true, passive: false }});
  document.querySelectorAll('.inspector-card, .copilot-box, .copilot-messages').forEach(el => {{
    el.addEventListener('wheel', stopScroll, {{ capture: true, passive: false }});
  }});
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


def _bus_threshold(bus: Optional[Dict[str, Any]]) -> float:
    try:
        return float((bus or {}).get("threshold") or 0.45)
    except (TypeError, ValueError):
        return 0.45


def _share_by_role(lane_load: Optional[List[Dict[str, Any]]]) -> Dict[str, float]:
    return {str(it.get("role") or ""): float(it.get("share") or 0) for it in (lane_load or []) if it.get("role")}


def lane_load_html(
    lane_load: Optional[List[Dict[str, Any]]],
    bus_factor: Optional[Dict[str, Any]] = None,
    prev_load: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Прогресс-бары нагрузки ролей. Зелёный — роль вышла из красной зоны > порога bus-factor."""
    threshold = _bus_threshold(bus_factor)
    prev = _share_by_role(prev_load)
    compare = bool(prev)
    rows: List[str] = []
    for item in sorted(lane_load or [], key=lambda it: -float(it.get("share") or 0)):
        share = float(item.get("share") or 0)
        role = str(item.get("role") or "")
        was = prev.get(role)
        relieved = compare and was is not None and was > threshold and share <= threshold
        klass = "cool" if relieved else ("hot" if share > threshold else "")
        delta_bits = []
        if compare and was is not None and abs(was - share) >= 0.005:
            delta_bits.append(f"было {was:.0%}")
        if relieved:
            delta_bits.append('<span class="bar-ok">разгружена</span>')
        delta = f'<span class="bar-delta">{" · ".join(delta_bits)}</span>' if delta_bits else ""
        rows.append(
            f'<div class="bar-row"><div class="n" title="{esc(role)}">{esc(role)}</div>'
            f'<div class="t"><div class="f {klass}" style="width:{min(share, 1) * 100:.0f}%"></div></div>'
            f'<div class="p">{share:.0%} · {int(item.get("tasks") or 0)} шаг.{delta}</div></div>'
        )
    return "".join(rows) or '<div class="land-empty">Нет данных о нагрузке ролей.</div>'


_PATH_KEEP_KINDS = {
    "userTask", "scriptTask", "task", "manualTask", "serviceTask",
    "sendTask", "receiveTask", "businessRuleTask", "subProcess",
}


def _path_tasks_only(critical_path: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Шлюзы и события с нулевой длительностью не показываем в цепочке КП."""
    out: List[Dict[str, Any]] = []
    for item in critical_path or []:
        kind = str(item.get("kind") or "")
        if "Gateway" in kind or kind.endswith("Event") or kind in {
            "exclusiveGateway", "parallelGateway", "inclusiveGateway",
            "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
        }:
            continue
        hours = float(item.get("hours") or 0)
        if kind not in _PATH_KEEP_KINDS and hours < 0.05:
            continue
        out.append(item)
    return out


def critical_path_html(critical_path: Optional[List[Dict[str, Any]]]) -> str:
    """Цепочка чипов критического пути: только задачи и подпроцессы, без шлюзов/событий с нулевым временем."""
    chips: List[str] = []
    visible = _path_tasks_only(critical_path)
    for i, item in enumerate(visible):
        arrow = '<span class="arrow">→</span>' if i else ""
        name = str(item.get("name") or "")
        name = name if len(name) <= 42 else name[:41] + "…"
        chips.append(
            f'{arrow}<span class="chip" title="{esc(item.get("role") or "")}">'
            f'<b>{fmt_hours(float(item.get("hours") or 0))}</b> · {esc(name)}</span>'
        )
    return "".join(chips) or "—"


def render_load_and_path(
    audit: Dict[str, Any],
    *,
    load_title: str = "Нагрузка по ролям (доля шагов процесса)",
    path_title: str = "Критический путь (алгоритм Беллмана — Форда)",
    prev_audit: Optional[Dict[str, Any]] = None,
    load_caption: str = "",
    path_caption: str = "",
    path_variant: str = "",
) -> None:
    """Двухколоночный блок: нагрузка ролей слева, критический путь справа."""
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown(f'<div class="ir-title">{load_title}</div>', unsafe_allow_html=True)
        if load_caption:
            st.caption(load_caption)
        st.markdown(
            lane_load_html(
                audit.get("lane_load") or [],
                audit.get("bus_factor") or {},
                (prev_audit or {}).get("lane_load") if prev_audit else None,
            ),
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(f'<div class="ir-title">{path_title}</div>', unsafe_allow_html=True)
        if path_caption:
            st.caption(path_caption)
        wrap = "chip-path-tobe" if path_variant == "tobe" else ""
        st.markdown(
            f'<div class="{wrap}">{critical_path_html(audit.get("critical_path") or [])}</div>',
            unsafe_allow_html=True,
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


_METH_TITLE_RU = {
    "naming": "Стандарт названий задач (Глагол + Объект)",
    "gateway": "Корректность развилок и условий",
    "subway": "Индекс декомпозиции (Анти-метро)",
    "topology": "Топологическая связность графа",
    "Naming compliance": "Стандарт названий задач (Глагол + Объект)",
    "Gateway semantics": "Корректность развилок и условий",
    "Anti-Subway Index": "Индекс декомпозиции (Анти-метро)",
    "Topology check": "Топологическая связность графа",
}


def _meth_title(chk: Dict[str, Any]) -> str:
    key = str(chk.get("key") or "")
    title = str(chk.get("title") or "")
    return _METH_TITLE_RU.get(key) or _METH_TITLE_RU.get(title) or title


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
        title = _meth_title(chk)
        badges.append(
            f'<div class="meth-badge" style="--c:{c}"><div class="h">{mark} {esc(title)}</div>'
            f'<div class="d">{esc(chk.get("detail"))}{extra}</div></div>'
        )
    st.markdown('<div class="meth-row">' + "".join(badges) + "</div>", unsafe_allow_html=True)
    with st.expander("Детали проверок нотации"):
        for chk in meth.get("checks") or []:
            st.markdown(f"**{_meth_title(chk)}** — {chk.get('points')} / {chk.get('weight')} баллов. {chk.get('detail')}")
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
    rework_value = fmt_hours(total_h) if total_h < 48 else fmt_days(total_h)
    rework_sub = (
        "возвратов в модели нет — срок совпадает с базовым КП"
        if not loops
        else f"циклов: {len(loops)} · худший: +{fmt_hours(float(sla['rework_hours']))} к КП"
    )

    loop_color = OK if not loops else (BAD if len(loops) >= 3 else WARN)
    loop_pill = "Возвратов нет" if not loops else f"Доля в сроке: {float(sla['rework_share']):.0%}"

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
    c2.markdown(
        card("Критический путь (базовый, без возвратов)", sla_value, sla_sub, sla_color, sla_pill),
        unsafe_allow_html=True,
    )
    c3.markdown(
        card("Срок с худшим возвратом (rework)", rework_value, rework_sub, loop_color, loop_pill),
        unsafe_allow_html=True,
    )
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
    render_load_and_path(audit)

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
    key = _reg_hash(text, bool(use_llm))
    mem = st.session_state.get("_gen_mem")
    if isinstance(mem, dict) and mem.get("key") == key and mem.get("result"):
        st.session_state["result"] = mem["result"]
        return
    progress = st.progress(0, text="Запуск конвейера…") if show_progress else None
    if progress:
        progress.progress(45, text="Генерация BPMN 2.0 (кэш по sha256 регламента)…")
    xml, audit, error = cached_generate_bpmn(key, text, bool(use_llm))
    if progress:
        progress.progress(100, text="Аудит узких мест готов")
        progress.empty()
    result = {"xml": xml, "audit": audit, "error": error}
    st.session_state["result"] = result
    st.session_state["_gen_mem"] = {"key": key, "result": result}
    st.session_state.pop("inspector_cache", None)
    st.session_state.pop("inspector_choice", None)
    st.session_state.pop("tobe_pack", None)


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
    st.session_state.pop("tobe_pack", None)


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
    st.session_state.pop("tobe_pack", None)


def render_downloads(key: str, stacked: bool = False) -> None:
    """Скачивание: BPMN 2.0, SVG, Паспорт (.md) и официальный регламент (.docx)."""
    result = st.session_state.get("result")
    ok = bool(result and not result["error"])
    stem = st.session_state.get("file_stem", "process")
    passport = ""
    docx_bytes = b""
    if ok:
        try:
            passport = generate_process_passport(
                result.get("xml") or "",
                result.get("audit") or {},
                st.session_state.get("reg_text") or "",
            )
        except Exception:  # noqa: BLE001 — кнопка просто недоступна, UI не падает
            passport = ""
        try:
            docx_bytes = export_docx_passport(
                result.get("xml") or "",
                result.get("audit") or {},
                st.session_state.get("reg_text") or "",
            )
        except Exception:  # noqa: BLE001
            docx_bytes = b""

    def _slot():
        return st.container()

    if stacked:
        slots = [_slot(), _slot(), _slot(), _slot()]
    else:
        row1 = st.columns(2, gap="small")
        row2 = st.columns(2, gap="small")
        slots = [row1[0], row1[1], row2[0], row2[1]]
    with slots[0]:
        st.download_button(
            "⬇️  Скачать .bpmn",
            data=(result["xml"] if ok else ""),
            file_name=f"{stem}.bpmn",
            mime="application/xml",
            disabled=not ok,
            key=f"dl_bpmn_{key}",
            use_container_width=True,
        )
    with slots[1]:
        if ok:
            page = svg_export_html(result["xml"], f"{stem}.svg")
            if hasattr(st, "iframe"):
                st.iframe(page, height=48)
            else:
                components.html(page, height=48, scrolling=False)
        else:
            st.button("⬇️  Скачать .svg", disabled=True, key=f"dl_svg_{key}", use_container_width=True)
    with slots[2]:
        st.download_button(
            "⬇️  Скачать Паспорт (.md)",
            data=passport or "",
            file_name=f"{stem}_passport.md",
            mime="text/markdown",
            disabled=not (ok and bool(passport)),
            key=f"dl_passport_{key}",
            use_container_width=True,
        )
    with slots[3]:
        st.download_button(
            "⬇️  Скачать регламент (.docx)",
            data=docx_bytes or b"",
            file_name=f"{stem}_reglament.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            disabled=not (ok and bool(docx_bytes)),
            key=f"dl_docx_{key}",
            use_container_width=True,
        )


def ensure_tobe_pack() -> Optional[Dict[str, Any]]:
    """Строит (и кэширует) целевой процесс To-Be для активного As-Is."""
    result = st.session_state.get("result")
    if not result or result.get("error"):
        return None
    src = result.get("xml") or ""
    cached = st.session_state.get("tobe_pack")
    if cached and cached.get("source_xml") == src:
        return cached
    text = st.session_state.get("reg_text") or ""
    asis_audit = result.get("audit") or {}
    asis_core = {
        "sla": asis_audit.get("sla") or {},
        "methodology": asis_audit.get("methodology") or {},
        "rework_loops": asis_audit.get("rework_loops") or [],
    }
    tobe_key = _reg_hash(text, "tobe", json.dumps(_audit_cache_slice(asis_audit), ensure_ascii=False, sort_keys=True))
    mem = st.session_state.get("_tobe_mem")
    if isinstance(mem, dict) and mem.get("key") == tobe_key and mem.get("pack") and mem["pack"].get("source_xml") == src:
        st.session_state["tobe_pack"] = mem["pack"]
        return mem["pack"]
    with st.spinner("Реинжиниринг As-Is → To-Be: параллелизация, Zero-Rework, автоматизация…"):
        opt_text, delta = cached_optimize_to_be(tobe_key, text, asis_core)
    xml = str((delta or {}).get("tobe_xml") or "")
    audit = (delta or {}).get("tobe_audit") or {}
    err = str((delta or {}).get("tobe_error") or "")
    if not xml:
        xml, audit, err = cached_generate_bpmn(_reg_hash(opt_text, False), opt_text, False)
    pack = {
        "source_xml": src,
        "text": opt_text,
        "delta": delta or {},
        "xml": xml,
        "audit": audit,
        "error": err,
    }
    st.session_state["tobe_pack"] = pack
    st.session_state["_tobe_mem"] = {"key": tobe_key, "pack": pack}
    return pack


def _active_canvas() -> Tuple[str, Dict[str, Any], str]:
    """XML / аудит / текст схемы, которая сейчас на холсте."""
    result = st.session_state.get("result") or {}
    asis_xml = result.get("xml") or ""
    asis_audit = result.get("audit") or {}
    asis_text = st.session_state.get("reg_text") or ""
    if st.session_state.get("canvas_variant") != TOBE_LABEL:
        return asis_xml, asis_audit, asis_text
    pack = st.session_state.get("tobe_pack") or {}
    if pack.get("xml") and not pack.get("error"):
        return pack["xml"], pack.get("audit") or asis_audit, pack.get("text") or asis_text
    return asis_xml, asis_audit, asis_text


def render_tobe_tab() -> None:
    st.markdown('<div class="ir-section">Оптимизация As-Is → To-Be</div>', unsafe_allow_html=True)
    st.caption("Переключатель над холстом меняет схему: текущий процесс или целевой To-Be. Карточка — эффект реинжиниринга.")
    pack = ensure_tobe_pack()
    if not pack:
        st.info("Сначала сгенерируйте диаграмму As-Is.")
        return
    delta = pack.get("delta") or {}
    if pack.get("error") and not pack.get("xml"):
        st.warning(f"Целевую диаграмму построить не удалось: {pack['error']}")
    saved_h = float(delta.get("sla_saved_hours") or 0)
    before_h = float(delta.get("sla_before_hours") or 0)
    after_h = float(delta.get("sla_after_hours") or 0)
    asis_audit = ((st.session_state.get("result") or {}).get("audit") or {})
    tobe_audit = pack.get("audit") or {}
    asis_cp = before_h or float((asis_audit.get("sla") or {}).get("critical_path_hours") or 0)
    tobe_cp = after_h or float((tobe_audit.get("sla") or {}).get("critical_path_hours") or 0)
    saved_h = asis_cp - tobe_cp
    saved_pct = round(100.0 * max(0.0, saved_h) / asis_cp, 1) if asis_cp and saved_h > 0 else 0.0
    removed = int(delta.get("rework_removed") or 0)
    q_gain = int(delta.get("quality_gain") or 0)
    asis_rw = float(delta.get("with_rework_before") or (asis_audit.get("sla") or {}).get("with_rework_hours") or asis_cp)
    tobe_rw = float(delta.get("with_rework_after") or (tobe_audit.get("sla") or {}).get("with_rework_hours") or tobe_cp)
    if saved_h >= 1.0 / 60.0:
        eco_value = f"Экономия: {fmt_hours(saved_h)} ({saved_pct:.0f}%)"
    else:
        eco_value = "Без ускорения"
    eco_sub = (
        f"КП без возвратов: {fmt_hours(asis_cp)} → {fmt_hours(tobe_cp)} · "
        f"rework: {fmt_hours(asis_rw)} → {fmt_hours(tobe_rw)}"
    )
    st.markdown(
        f'<div class="tobe-grid">'
        f'<div class="tobe-card"><div class="k">Экономия SLA</div>'
        f'<div class="v">{esc(eco_value)}</div>'
        f'<div class="s">{esc(eco_sub)}</div></div>'
        f'<div class="tobe-card"><div class="k">Циклы доработки</div>'
        f'<div class="v">{int(delta.get("rework_before") or 0)} → {int(delta.get("rework_after") or 0)}</div>'
        f'<div class="s">устранено петель: {removed}</div></div>'
        f'<div class="tobe-card"><div class="k">Качество нотации</div>'
        f'<div class="v">+{q_gain} п.п.</div>'
        f'<div class="s">{int(delta.get("quality_before") or 0)}% → {int(delta.get("quality_after") or 0)}%</div></div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Критический путь (базовый, без возвратов): {fmt_hours(asis_cp)} → {fmt_hours(tobe_cp)} · "
        f"Срок с худшим возвратом (rework): {fmt_hours(asis_rw)} → {fmt_hours(tobe_rw)}"
    )
    engine = delta.get("engine") or "semantic-optimizer"
    st.caption(f"Движок оптимизации: {'облачная LLM' if engine == 'llm' else 'семантический оптимизатор'} · {engine}")
    st.markdown('<div class="ir-title" style="margin-top:8px">Применённые мероприятия</div>', unsafe_allow_html=True)
    for act in delta.get("actions") or []:
        st.markdown(f'<div class="tobe-act">{esc(act.get("detail") or act.get("kind"))}</div>', unsafe_allow_html=True)

    asis_audit = ((st.session_state.get("result") or {}).get("audit") or {})
    tobe_audit = pack.get("audit") or {}
    if tobe_audit.get("lane_load") or tobe_audit.get("critical_path"):
        asis_bus = asis_audit.get("bus_factor") or {}
        tobe_bus = tobe_audit.get("bus_factor") or {}
        threshold = _bus_threshold(tobe_bus or asis_bus)
        prev_shares = _share_by_role(asis_audit.get("lane_load") or [])
        relieved = [
            str(item.get("role") or "")
            for item in (tobe_audit.get("lane_load") or [])
            if prev_shares.get(str(item.get("role") or ""), 0) > threshold
            and float(item.get("share") or 0) <= threshold
        ]
        before_share = float(asis_bus.get("max_share") or 0)
        after_share = float(tobe_bus.get("max_share") or 0)
        load_bits = []
        if before_share or after_share:
            load_bits.append(f"Bus-factor {before_share:.0%} → {after_share:.0%}")
        if relieved:
            load_bits.append("разгружены: " + ", ".join(f"«{r}»" for r in relieved) + f" (выход из зоны >{threshold:.0%})")
        elif tobe_bus.get("status") == "ok":
            load_bits.append("нагрузка сбалансирована")
        asis_path = _path_tasks_only(asis_audit.get("critical_path") or [])
        tobe_path = _path_tasks_only(tobe_audit.get("critical_path") or [])
        asis_cp = float((asis_audit.get("sla") or {}).get("critical_path_hours") or 0)
        tobe_cp = float((tobe_audit.get("sla") or {}).get("critical_path_hours") or 0)
        asis_rw = float((asis_audit.get("sla") or {}).get("with_rework_hours") or asis_cp)
        tobe_rw = float((tobe_audit.get("sla") or {}).get("with_rework_hours") or tobe_cp)
        shortened = len(tobe_path) < len(asis_path) or tobe_cp + 1e-6 < asis_cp
        path_label = "Цепочка спрямилась" if shortened else "Критический путь"
        path_cap = (
            f"{path_label}: {len(asis_path)} шагов / {fmt_hours(asis_cp)}"
            f" → {len(tobe_path)} шагов / {fmt_hours(tobe_cp)}. "
            f"Критический путь (базовый, без возвратов): {fmt_hours(asis_cp)} → {fmt_hours(tobe_cp)}. "
            f"Срок с худшим возвратом (rework): {fmt_hours(asis_rw)} → {fmt_hours(tobe_rw)}."
        )
        render_load_and_path(
            tobe_audit,
            load_title="Новое распределение нагрузки по ролям в To-Be",
            path_title="Новый критический путь To-Be (Беллман — Форд)",
            prev_audit=asis_audit,
            load_caption=" · ".join(load_bits),
            path_caption=path_cap,
            path_variant="tobe",
        )

    st.write("")
    stem = st.session_state.get("file_stem", "process")
    tobe_xml = pack.get("xml") or ""
    st.download_button(
        "⬇️  Скачать To-Be .bpmn",
        data=tobe_xml,
        file_name=f"{stem}_tobe.bpmn",
        mime="application/xml",
        disabled=not bool(tobe_xml),
        key="dl_tobe_bpmn",
    )
    with st.expander("Целевой текст регламента To-Be"):
        st.code(pack.get("text") or "", language="text")


def render_raci_tab(xml: str, audit: Dict[str, Any], text: str) -> None:
    st.markdown('<div class="ir-section">Матрица ответственности RACI</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="raci-legend">'
        '<span class="raci-b R" title="Responsible">R</span> исполнитель · '
        '<span class="raci-b A" title="Accountable">A</span> итоговая ответственность · '
        '<span class="raci-b C" title="Consulted">C</span> консультирует · '
        '<span class="raci-b I" title="Informed">I</span> уведомляется'
        "</div>",
        unsafe_allow_html=True,
    )
    try:
        parsed = parse_regulation(normalize_regulation(text)) if text.strip() else None
    except Exception:  # noqa: BLE001
        parsed = None
    steps = list(parsed.steps) if parsed else []
    roles = list(parsed.roles) if parsed else [str(x.get("role") or "") for x in (audit.get("lane_load") or [])]
    if not steps:
        ctx = build_process_context(text, xml, audit)
        steps = ctx.get("steps") or []
        roles = roles or _roles_from_audit(audit, steps)
    if not steps:
        st.info("Не удалось собрать шаги процесса для матрицы.")
        return
    if not roles:
        roles = _roles_from_audit(audit, steps)
    matrix = generate_raci_matrix(steps, roles)
    head = "".join(f"<th>{esc(r)}</th>" for r in roles)
    body_rows = []
    for item in matrix:
        cells = []
        for role in roles:
            letters = (item.get("assignments") or {}).get(role) or []
            if not letters:
                cells.append("<td class='cell'>—</td>")
                continue
            badges = "".join(
                f'<span class="raci-b {esc(lt)}" title="{esc(_raci_tip(lt, role))}">{esc(lt)}</span>'
                for lt in letters
            )
            cells.append(f"<td class='cell'>{badges}</td>")
        body_rows.append(
            f"<tr><td>{esc(item.get('num'))}</td><td class='step'>{esc(item.get('title'))}</td>{''.join(cells)}</tr>"
        )
    st.markdown(
        "<div class='raci-wrap'><table class='raci'><tr><th>№</th><th>Шаг</th>"
        + head
        + "</tr>"
        + "".join(body_rows)
        + "</table></div>",
        unsafe_allow_html=True,
    )


def _roles_from_audit(audit: Dict[str, Any], steps: List[Any]) -> List[str]:
    roles = [str(x.get("role") or "") for x in (audit.get("lane_load") or []) if x.get("role")]
    for s in steps:
        role = s.role if hasattr(s, "role") else (s.get("role") if isinstance(s, dict) else "")
        if role and role not in roles:
            roles.append(role)
    return roles


def _raci_tip(letter: str, role: str) -> str:
    titles = {
        "R": f"Responsible — {role} исполняет шаг",
        "A": f"Accountable — {role} несёт итоговую ответственность",
        "C": f"Consulted — {role} консультирует на развилке",
        "I": f"Informed — {role} получает уведомление",
    }
    return titles.get(letter, letter)


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
        # Имена секретов — только если ключа нет (диагностика настройки), иначе строка для жюри лишняя.
        secrets_note = f" · найдены секреты: {', '.join(SECRET_NAMES)}" if SECRET_NAMES and not os.getenv("OPENAI_API_KEY") else ""
        st.caption(f"Облачная модель: {cloud_engine_status()}{secrets_note}")
        if st.button("🚀  Сгенерировать BPMN 2.0", type="primary"):
            st.session_state["quickstart"] = False
            if not st.session_state["reg_text"].strip():
                st.warning("Введите текст регламента.")
            else:
                run_generation(st.session_state["reg_text"], use_llm)

        result = st.session_state.get("result")
        if result and not result["error"]:
            if st.session_state.get("quickstart"):
                st.markdown(
                    '<div class="engine">Эталонный процесс подготовлен Groq GPT-OSS 120B · режим быстрого старта</div>',
                    unsafe_allow_html=True,
                )
            else:
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
            render_downloads("left", stacked=True)


def _diagram_height(wide: bool) -> int:
    """Автоподгонка высоты холста по числу дорожек и узлов, чтобы схема не сжималась в точку."""
    result = st.session_state.get("result") or {}
    audit = result.get("audit") or {}
    if st.session_state.get("canvas_variant") == TOBE_LABEL:
        pack = st.session_state.get("tobe_pack") or {}
        audit = pack.get("audit") or audit
    stats = audit.get("stats") or {}
    lanes = int(stats.get("lanes") or 3)
    nodes = int(stats.get("nodes") or 12)
    base = DIAGRAM_HEIGHT_WIDE if wide else DIAGRAM_HEIGHT
    extra = min(360, max(0, (lanes - 3) * 80 + max(0, nodes - 18) * 8))
    return int(base + extra)


def render_diagram(canvas_height: int) -> None:
    result = st.session_state.get("result")
    if not result:
        st.info("Выберите регламент и нажмите «Сгенерировать BPMN 2.0».")
    elif result["error"]:
        st.error(result["error"])
    else:
        st.radio(
            "Схема на холсте",
            [ASIS_LABEL, TOBE_LABEL],
            key="canvas_variant",
            horizontal=True,
        )
        if st.session_state.get("canvas_variant") == TOBE_LABEL:
            pack = ensure_tobe_pack()
            if pack and pack.get("error") and not pack.get("xml"):
                st.warning("To-Be недоступен — показан As-Is. " + str(pack.get("error") or ""))
            elif st.session_state.get("_last_canvas_variant") != TOBE_LABEL:
                st.session_state.pop("inspector_choice", None)
        if st.session_state.get("_last_canvas_variant") != st.session_state.get("canvas_variant"):
            st.session_state["_last_canvas_variant"] = st.session_state.get("canvas_variant")
            st.session_state.pop("inspector_choice", None)
        xml, audit, text = _active_canvas()
        catalog = build_diagram_catalog(xml, audit, text)
        pack = st.session_state.get("tobe_pack")
        asis = st.session_state.get("result") or {}
        copilot = build_canvas_copilot(
            asis.get("xml") or xml,
            asis.get("audit") or audit,
            st.session_state.get("reg_text") or text,
            (pack or {}).get("delta"),
        )
        page = viewer_html(xml, canvas_height, catalog, copilot)
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
    low = prompt.lower()
    pack = st.session_state.get("tobe_pack")
    if re.search(r"сравни|as-is|to-be|tobe|до и после|читаем|метро|сократ|почему|за сч[её]т|за счет|quality|объясни", low):
        pack = ensure_tobe_pack() or pack
    with st.spinner("Ассистент анализирует процесс…"):
        reply, new_text, new_xml, new_audit = assistant_chat(
            prompt,
            messages[:-1],
            result.get("xml") or "",
            result.get("audit") or {},
            st.session_state.get("reg_text") or "",
            use_llm=True,
            tobe_delta=(pack or {}).get("delta"),
        )
    messages.append({"role": "assistant", "content": reply})
    if new_text and new_xml and new_audit:
        st.session_state["reg_text"] = new_text
        st.session_state["result"] = {"xml": new_xml, "audit": new_audit, "error": ""}
        st.session_state["diagram_updated_by_assistant"] = True
        st.session_state["file_stem"] = st.session_state.get("file_stem") or "custom_process"
        st.session_state.pop("inspector_cache", None)
        st.session_state.pop("inspector_choice", None)
        st.session_state.pop("tobe_pack", None)


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
            st.session_state["quickstart"] = True

    if "view_mode" not in st.session_state:
        st.session_state["view_mode"] = VIEW_WIDE

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
    canvas_h = _diagram_height(wide)
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
        render_diagram(canvas_h)
    else:
        left, right = st.columns([5, 7], gap="large")
        with left:
            render_input_panel(labels)
        with right:
            render_diagram(canvas_h)

    result = st.session_state.get("result")
    if result and not result["error"]:
        xml, audit, text = _active_canvas()
        tab_audit, tab_tobe, tab_raci, tab_inspector = st.tabs(
            [
                "📊 Аудит и SLA",
                "⚡ Оптимизация As-Is → To-Be",
                "👥 Матрица RACI",
                "🔍 Инспектор задачи",
            ]
        )
        with tab_audit:
            render_audit(result["audit"])
            render_details(result["audit"])
        with tab_tobe:
            render_tobe_tab()
        with tab_raci:
            render_raci_tab(
                result["xml"],
                result["audit"],
                st.session_state.get("reg_text") or "",
            )
        with tab_inspector:
            render_task_inspector(xml, audit, text)

main()
