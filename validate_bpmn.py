#!/usr/bin/env python3
"""Офлайн-валидатор BPMN-файлов: официальная XSD-схема BPMN 2.0 (OMG), целостность ссылок,
ортогональность, наезды, подписи.

Использование:  python3 validate_bpmn.py file1.bpmn [file2.bpmn ...]
Код возврата 0 — все проверки пройдены.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
}
Rect = Tuple[float, float, float, float]


def _q(prefix: str, tag: str) -> str:
    return "{%s}%s" % (NS[prefix], tag)


def _rect(el: ET.Element) -> Rect:
    b = el.find("dc:Bounds", NS)
    assert b is not None
    x, y, w, h = (float(b.get(k)) for k in ("x", "y", "width", "height"))  # type: ignore[arg-type]
    return x, y, x + w, y + h


def _inter(a: Rect, b: Rect, eps: float = 0.5) -> bool:
    return a[0] + eps < b[2] and b[0] + eps < a[2] and a[1] + eps < b[3] and b[1] + eps < a[3]


def _contains(outer: Rect, inner: Rect) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _seg_hits(a: Tuple[float, float], b: Tuple[float, float], r: Rect) -> bool:
    (x1, y1), (x2, y2) = a, b
    if abs(y1 - y2) < 0.01:
        lo, hi = sorted((x1, x2))
        return r[1] < y1 < r[3] and lo < r[2] and hi > r[0]
    if abs(x1 - x2) < 0.01:
        lo, hi = sorted((y1, y2))
        return r[0] < x1 < r[2] and lo < r[3] and hi > r[1]
    return True


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "BPMN20.xsd"  # OMG formal/2010-05-04


@lru_cache(maxsize=1)
def _xsd():  # type: ignore[no-untyped-def]
    try:
        from lxml import etree
    except ImportError:
        return None
    return etree.XMLSchema(etree.parse(str(SCHEMA_PATH)))


def xsd_errors(path: str) -> Optional[List[str]]:
    """Ошибки по официальной XSD BPMN 2.0; None — lxml не установлен (проверка пропущена)."""
    return xsd_errors_xml(Path(path).read_bytes())


def xsd_errors_xml(xml: "str | bytes") -> Optional[List[str]]:
    schema = _xsd()
    if schema is None:
        return None
    from lxml import etree

    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    if schema.validate(etree.fromstring(data)):
        return []
    return [f"XSD, строка {e.line}: {e.message}" for e in schema.error_log]


def validate(path: str) -> List[str]:
    problems: List[str] = list(xsd_errors(path) or [])
    root = ET.parse(path).getroot()
    ids = [e.get("id") for e in root.iter() if e.get("id")]
    idset = set(ids)
    for dup in {i for i in ids if ids.count(i) > 1}:
        problems.append(f"дубликат id {dup}")

    text = open(path, encoding="utf-8").read()
    if "isMarkerVisible" in text:
        problems.append("присутствует isMarkerVisible")
    for tag in ("xmlns:bioc=", "xmlns:color="):
        if tag not in text:
            problems.append(f"нет объявления {tag}")

    flows = {f.get("id"): f for f in root.iter(_q("bpmn", "sequenceFlow"))}
    for fid, f in flows.items():
        for attr in ("sourceRef", "targetRef"):
            if f.get(attr) not in idset:
                problems.append(f"{fid}: битая ссылка {attr}")

    shapes: Dict[str, Rect] = {}
    for s in root.iter(_q("bpmndi", "BPMNShape")):
        shapes[s.get("bpmnElement", "")] = _rect(s)
    edges = list(root.iter(_q("bpmndi", "BPMNEdge")))
    if len(edges) != len(flows):
        problems.append(f"рёбер DI {len(edges)} != sequenceFlow {len(flows)}")

    containers = {k for k in shapes if k.startswith(("Participant", "Lane", "Group"))}
    node_rects = {k: v for k, v in shapes.items() if k not in containers}

    ids_seq = list(node_rects)
    for i, a in enumerate(ids_seq):
        for b in ids_seq[i + 1:]:
            if _inter(node_rects[a], node_rects[b]) and not (
                _contains(node_rects[a], node_rects[b]) or _contains(node_rects[b], node_rects[a])
            ):
                problems.append(f"наезд блоков {a} и {b}")

    label_rects: List[Tuple[str, Rect]] = []
    all_segments: List[Tuple[str, Tuple[float, float], Tuple[float, float]]] = []
    for e in edges:
        eid = e.get("bpmnElement", "")
        flow = flows.get(eid)
        pts = [(float(w.get("x")), float(w.get("y"))) for w in e.findall("di:waypoint", NS)]  # type: ignore[arg-type]
        if flow is None or len(pts) < 2:
            problems.append(f"{eid}: нет waypoint")
            continue
        src, tgt = flow.get("sourceRef", ""), flow.get("targetRef", "")
        skip = {src, tgt}
        for k, r in node_rects.items():
            if k not in skip and src in node_rects and _contains(r, node_rects[src]):
                skip.add(k)  # контейнер источника (подпроцесс)
        for a, b in zip(pts, pts[1:]):
            all_segments.append((eid, a, b))
            if abs(a[0] - b[0]) > 0.2 and abs(a[1] - b[1]) > 0.2:
                problems.append(f"{eid}: диагональный сегмент {a}->{b}")
            for k, r in node_rects.items():
                if k in skip:
                    continue
                if _seg_hits(a, b, (r[0] + 1, r[1] + 1, r[2] - 1, r[3] - 1)):
                    problems.append(f"{eid}: пересекает блок {k}")
        lb = e.find("bpmndi:BPMNLabel", NS)
        if lb is not None and lb.find("dc:Bounds", NS) is not None:
            label_rects.append((eid, _rect(lb)))

    for s in root.iter(_q("bpmndi", "BPMNShape")):
        lb = s.find("bpmndi:BPMNLabel", NS)  # подпись без Bounds (только labelStyle) рисуется внутри фигуры
        if lb is not None and lb.find("dc:Bounds", NS) is not None:
            label_rects.append((s.get("bpmnElement", ""), _rect(lb)))

    for i, (la, ra) in enumerate(label_rects):
        for k, r in node_rects.items():
            if k == la:
                continue
            if _inter(ra, r) and not _contains(r, ra):
                problems.append(f"подпись {la} налезает на блок {k}")
        for lb_id, rb in label_rects[i + 1:]:
            if _inter(ra, rb):
                problems.append(f"подписи {la} и {lb_id} перекрываются")
        for eid, a, b in all_segments:
            if eid == la:
                continue
            if _seg_hits(a, b, ra):
                problems.append(f"подпись {la} пересекает линию {eid}")
    return problems


def main(argv: List[str]) -> int:
    if _xsd() is None:
        print("[WARN] lxml не установлен — проверка по XSD BPMN 2.0 пропущена (pip install lxml)")
    failed = 0
    for path in argv:
        problems = validate(path)
        status = "OK " if not problems else "FAIL"
        print(f"[{status}] {path}: проблем {len(problems)}")
        for p in problems[:25]:
            print("   -", p)
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if len(sys.argv) > 1 else 2)
