"""
Движок BPMN 2.0.2 для трека «Архитектор BPMN-диаграмм» (ПАО «Интер РАО»).

Возможности:
  * контракт API песочницы жюри (`DIAGRAM.add_pool`, `add_task`, `create_subprocess`, ...);
  * Layered Graph Auto-Layout: X — по слоям исполнения, Y — строго по дорожкам;
  * ортогональная (Манхэттенская) маршрутизация стрелок с обходом чужих блоков;
  * BPMN in Color (bioc / color) и валидный для bpmn.io XML (MODEL + DI);
  * self-healing: несуществующие ID и межуровневые связи не роняют движок;
  * бизнес-аудит: bus-factor, тупики, критический путь SLA (Беллман-Форд),
    циклы возврата на доработку и рекомендации по оптимизации.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from html import escape
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------- #
# Пространства имён
# --------------------------------------------------------------------------- #
BPMN_MODEL = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DI = "http://www.omg.org/spec/DD/20100524/DI"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
BIOC = "http://bpmn.io/schema/bpmn/biocolor/1.0"
COLOR = "http://www.omg.org/spec/BPMN/non-normative/color/1.0"
TNS = "http://bpmn.io/schema/bpmn"

# --------------------------------------------------------------------------- #
# Геометрия
# --------------------------------------------------------------------------- #
TASK_W, TASK_H = 140.0, 80.0
GATEWAY_S = 50.0
EVENT_S = 36.0
SUBPROCESS_MIN_W, SUBPROCESS_MIN_H = 560.0, 240.0
COLUMN_GAP_X = 84.0            # зазор между колонками слоёв на верхнем уровне
SUB_PAD_X = 56.0               # горизонтальный padding внутри подпроцесса
SUB_PAD_TOP = 64.0             # место под заголовок подпроцесса
SUB_PAD_BOTTOM = 44.0
SUB_COLUMN_GAP_X = 120.0       # зазор между колонками внутри подпроцесса
SUB_ROW_GAP_Y = 30.0
LANE_PAD_Y = 38.0
LANE_MIN_H = 170.0
LEFT_PAD = 64.0
RIGHT_PAD = 64.0
POOL_HEADER_W = 30.0
POOL_X, POOL_Y = 160.0, 80.0
POOL_GAP_Y = 70.0
NODE_ROW_GAP_Y = 26.0
SUBPROCESS_LABEL_STYLE = "LabelStyle_SubprocessTitle"
SUBPROCESS_TITLE_PT = 15         # заголовок раскрытого подпроцесса — крупнее подписей задач (12)
LABEL_LIFT = 12.0
LABEL_GAP = 6.0                # минимальный зазор между подписями              # сдвиг подписей над стрелками (y - 12)
CHAR_W = 6.8                   # средняя ширина символа подписи (px)
LINE_H = 15.0

BUS_FACTOR_THRESHOLD = 0.45

# --------------------------------------------------------------------------- #
# BPMN in Color
# --------------------------------------------------------------------------- #
KIND_COLORS: Dict[str, Tuple[str, str]] = {
    "startEvent": ("#2E7D32", "#E8F5E9"),
    "endEvent": ("#C62828", "#FFEBEE"),
    "task": ("#1565C0", "#E3F2FD"),
    "userTask": ("#1565C0", "#E3F2FD"),
    "scriptTask": ("#1565C0", "#E3F2FD"),
    "exclusiveGateway": ("#F57F17", "#FFF8E1"),
    "parallelGateway": ("#F57F17", "#FFF8E1"),
    "inclusiveGateway": ("#F57F17", "#FFF8E1"),
    "subProcess": ("#4527A0", "#EDE7F6"),
}
POOL_COLORS = ("#003366", "#FFFFFF")
LANE_COLORS = (("#1565C0", "#F5F9FF"), ("#1565C0", "#FFFFFF"))
GROUP_COLORS = ("#546E7A", "#FAFAFA")
EDGE_COLOR = "#455A64"
REWORK_EDGE_COLOR = "#C62828"

GATEWAY_KINDS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway"}
EVENT_KINDS = {"startEvent", "endEvent"}
WORK_KINDS = {"task", "userTask", "scriptTask"}

# Ориентировочные трудозатраты шага, часы (переопределяются set_sla()).
DEFAULT_HOURS: Dict[str, float] = {
    "startEvent": 0.0,
    "endEvent": 0.0,
    "task": 1.0,
    "userTask": 2.0,
    "scriptTask": 0.25,
    "exclusiveGateway": 0.1,
    "parallelGateway": 0.0,
    "inclusiveGateway": 0.1,
    "subProcess": 0.0,
}

XML_TAGS = {
    "startEvent": "bpmn:startEvent",
    "endEvent": "bpmn:endEvent",
    "task": "bpmn:task",
    "userTask": "bpmn:userTask",
    "scriptTask": "bpmn:scriptTask",
    "subProcess": "bpmn:subProcess",
    "exclusiveGateway": "bpmn:exclusiveGateway",
    "parallelGateway": "bpmn:parallelGateway",
    "inclusiveGateway": "bpmn:inclusiveGateway",
}

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]  # x1, y1, x2, y2


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
def _xml_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32)
    return escape(text, quote=True)


def _fmt(value: float) -> str:
    return f"{value:.1f}"


def _kind_size(kind: str) -> Tuple[float, float]:
    if kind in EVENT_KINDS:
        return EVENT_S, EVENT_S
    if kind in GATEWAY_KINDS:
        return GATEWAY_S, GATEWAY_S
    if kind == "subProcess":
        return SUBPROCESS_MIN_W, SUBPROCESS_MIN_H
    return TASK_W, TASK_H


def _label_box(text: str, min_w: float, max_w: float) -> Tuple[float, float]:
    """Размер подписи: ширина по длине текста, высота по числу строк."""
    width = max(min_w, min(max_w, CHAR_W * len(text) + 10.0))
    lines = max(1, math.ceil(CHAR_W * len(text) / max(width - 8.0, 1.0)))
    return width, LINE_H * lines


def _color_attrs(stroke: str, fill: Optional[str] = None) -> str:
    attrs = f' bioc:stroke="{stroke}"'
    if fill:
        attrs += f' bioc:fill="{fill}"'
    attrs += f' color:border-color="{stroke}"'
    if fill:
        attrs += f' color:background-color="{fill}"'
    return attrs


def _clean_path(points: Sequence[Point]) -> List[Point]:
    """Удаляет дубликаты и промежуточные коллинеарные точки, сохраняя 90°."""
    out: List[Point] = []
    for pt in points:
        if out and abs(out[-1][0] - pt[0]) < 0.05 and abs(out[-1][1] - pt[1]) < 0.05:
            continue
        out.append((pt[0], pt[1]))
    changed = True
    while changed and len(out) > 2:
        changed = False
        for i in range(1, len(out) - 1):
            a, b, c = out[i - 1], out[i], out[i + 1]
            same_x = abs(a[0] - b[0]) < 0.05 and abs(b[0] - c[0]) < 0.05
            same_y = abs(a[1] - b[1]) < 0.05 and abs(b[1] - c[1]) < 0.05
            if same_x or same_y:
                del out[i]
                changed = True
                break
    return out


def _hits_rect(a: Point, b: Point, rect: Tuple[float, float, float, float]) -> bool:
    (x1, y1), (x2, y2) = a, b
    rx1, ry1, rx2, ry2 = rect
    if abs(y1 - y2) < 0.01:
        lo, hi = sorted((x1, x2))
        return ry1 < y1 < ry2 and lo < rx2 and hi > rx1
    if abs(x1 - x2) < 0.01:
        lo, hi = sorted((y1, y2))
        return rx1 < x1 < rx2 and lo < ry2 and hi > ry1
    return True


def _rects_intersect(a: Rect, b: Rect, eps: float = 0.5) -> bool:
    return a[0] + eps < b[2] and b[0] + eps < a[2] and a[1] + eps < b[3] and b[1] + eps < a[3]


def _rect_contains(outer: Rect, inner: Rect) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _overlap_len(a: Tuple[Point, Point], b: Tuple[Point, Point]) -> float:
    (ax1, ay1), (ax2, ay2) = a
    (bx1, by1), (bx2, by2) = b
    if abs(ay1 - ay2) < 0.01 and abs(by1 - by2) < 0.01 and abs(ay1 - by1) < 1.0:
        lo = max(min(ax1, ax2), min(bx1, bx2))
        hi = min(max(ax1, ax2), max(bx1, bx2))
        return max(0.0, hi - lo)
    if abs(ax1 - ax2) < 0.01 and abs(bx1 - bx2) < 0.01 and abs(ax1 - bx1) < 1.0:
        lo = max(min(ay1, ay2), min(by1, by2))
        hi = min(max(ay1, ay2), max(by1, by2))
        return max(0.0, hi - lo)
    return 0.0


# --------------------------------------------------------------------------- #
# Модель
# --------------------------------------------------------------------------- #
@dataclass
class FlowNode:
    id: str
    name: str
    kind: str
    parent_id: str
    owner_id: str
    lane_id: Optional[str] = None
    group_id: Optional[str] = None
    width: float = TASK_W
    height: float = TASK_H
    x: float = 0.0
    y: float = 0.0
    order: int = 0
    sla_hours: Optional[float] = None
    inner_start_id: Optional[str] = None
    inner_end_id: Optional[str] = None
    label_rect: Optional[Rect] = None


@dataclass
class SequenceLink:
    id: str
    source_id: str
    target_id: str
    condition_name: str = ""
    waypoints: List[Point] = field(default_factory=list)
    is_back: bool = False
    label_rect: Optional[Rect] = None


@dataclass
class Pool:
    id: str
    participant_id: str
    process_id: str
    name: str
    lane_ids: List[str]
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass
class Lane:
    id: str
    name: str
    pool_id: str
    process_id: str
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = LANE_MIN_H


@dataclass
class Group:
    id: str
    name: str
    parent_id: str
    lane_id: Optional[str]
    owner_id: str
    category_value_id: str
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


# --------------------------------------------------------------------------- #
# Построитель диаграмм
# --------------------------------------------------------------------------- #
class BPMNDiagramBuilder:
    """Промышленный построитель BPMN 2.0.2 с API объекта DIAGRAM песочницы жюри."""

    def __init__(
        self,
        process_name: str = "Корневой процесс",
        process_id: str = "Process_1",
        root_start_id: str = "Event_RootStart",
        root_end_id: str = "Event_RootEnd",
        root_start_name: str = "Старт процесса",
        root_end_name: str = "Процесс завершён",
        sla_target_hours: Optional[float] = None,
    ) -> None:
        self.process_name = process_name
        self.process_id = process_id
        self.collaboration_id = "Collaboration_1"
        self.root_start_id = root_start_id
        self.root_end_id = root_end_id
        self.root_start_name = root_start_name
        self.root_end_name = root_end_name
        self.sla_target_hours = sla_target_hours

        self.nodes: Dict[str, FlowNode] = {}
        self.links: List[SequenceLink] = []
        self.pools: Dict[str, Pool] = {}
        self.lanes: Dict[str, Lane] = {}
        self.groups: Dict[str, Group] = {}
        self.warnings: List[str] = []
        self.skipped_links: List[Dict[str, str]] = []
        self.healed: List[str] = []

        self._seq: Dict[str, int] = defaultdict(int)
        self._used_ids: Set[str] = set()
        self._order = 0
        self._default_lane_id: Optional[str] = None
        self._implicit_ready = False

    # ------------------------------------------------------------------ ids
    def _next_id(self, prefix: str) -> str:
        while True:
            self._seq[prefix] += 1
            candidate = f"{prefix}_{self._seq[prefix]}"
            if candidate not in self._used_ids:
                self._used_ids.add(candidate)
                return candidate

    def _claim(self, wanted: str, prefix: str) -> str:
        if wanted and wanted not in self._used_ids:
            self._used_ids.add(wanted)
            return wanted
        return self._next_id(prefix)

    @staticmethod
    def _as_id(value: object) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    # ------------------------------------------------------------- contexts
    def _ensure_implicit_process(self) -> None:
        if self.pools or self._implicit_ready:
            return
        self._implicit_ready = True
        self.add_pool(self.process_id, ["Исполнители"])

    def _resolve_context(self, parent_id: object) -> Tuple[str, Optional[str], Optional[str]]:
        """Возвращает (owner_id, lane_id, group_id) для родителя размещения."""
        pid = self._as_id(parent_id) or ""
        if pid in self.lanes:
            lane = self.lanes[pid]
            return lane.process_id, lane.id, None
        if pid in self.groups:
            group = self.groups[pid]
            return group.owner_id, group.lane_id, group.id
        node = self.nodes.get(pid)
        if node is not None and node.kind == "subProcess":
            return node.id, node.lane_id, node.group_id
        if pid in self.pools:
            pool = self.pools[pid]
            return pool.process_id, (pool.lane_ids[0] if pool.lane_ids else None), None
        for pool in self.pools.values():
            if pool.process_id == pid:
                return pool.process_id, (pool.lane_ids[0] if pool.lane_ids else None), None
        self._ensure_implicit_process()
        if pid and pid != self.process_id:
            self.warnings.append(
                f"Неизвестный parent_id={pid!r}: узел размещён в первой дорожке (self-healing)."
            )
        lane_id = self._default_lane_id
        owner = self.lanes[lane_id].process_id if lane_id else self.process_id
        return owner, lane_id, None

    def _register_node(
        self,
        name: str,
        kind: str,
        parent_id: object,
        prefix: str,
        node_id: Optional[str] = None,
    ) -> str:
        owner_id, lane_id, group_id = self._resolve_context(parent_id)
        node_id = self._claim(node_id or "", prefix)
        width, height = _kind_size(kind)
        self._order += 1
        self.nodes[node_id] = FlowNode(
            id=node_id,
            name=str(name or ""),
            kind=kind,
            parent_id=self._as_id(parent_id) or "",
            owner_id=owner_id,
            lane_id=lane_id,
            group_id=group_id,
            width=width,
            height=height,
            order=self._order,
        )
        return node_id

    # ------------------------------------------------------- контракт жюри
    def add_pool(self, parent_id: str, lane_names: list) -> Tuple[str, List[str]]:
        names = [str(n) for n in lane_names] if lane_names else ["Участник"]
        first = not self.pools
        pool_id = self._next_id("Pool")
        participant_id = self._next_id("Participant")
        process_id = self.process_id if first else self._next_id("Process")
        if first:
            self._used_ids.add(process_id)
        pool = Pool(
            id=pool_id,
            participant_id=participant_id,
            process_id=process_id,
            name=self.process_name if first else f"Участник {len(self.pools) + 1}",
            lane_ids=[],
        )
        self.pools[pool_id] = pool
        for title in names:
            lane_id = self._next_id("Lane")
            self.lanes[lane_id] = Lane(id=lane_id, name=title, pool_id=pool_id, process_id=process_id)
            pool.lane_ids.append(lane_id)
        if self._default_lane_id is None:
            self._default_lane_id = pool.lane_ids[0]
        _ = parent_id
        return pool_id, list(pool.lane_ids)

    def add_task(self, name: str, parent_id: str) -> str:
        return self._register_node(name, "task", parent_id, "Activity")

    def add_user_task(self, name: str, parent_id: str) -> str:
        return self._register_node(name, "userTask", parent_id, "Activity")

    def add_script_task(self, name: str, parent_id: str) -> str:
        return self._register_node(name, "scriptTask", parent_id, "Activity")

    def create_subprocess(self, name: str, parent_id: str) -> str:
        sub_id = self._register_node(name, "subProcess", parent_id, "Activity")
        sub = self.nodes[sub_id]
        for kind, label in (("startEvent", "Начало"), ("endEvent", "Завершено")):
            event_id = self._next_id("Event")
            width, height = _kind_size(kind)
            self._order += 1
            self.nodes[event_id] = FlowNode(
                id=event_id,
                name=label,
                kind=kind,
                parent_id=sub_id,
                owner_id=sub_id,
                lane_id=sub.lane_id,
                group_id=sub.group_id,
                width=width,
                height=height,
                order=self._order,
            )
            if kind == "startEvent":
                sub.inner_start_id = event_id
            else:
                sub.inner_end_id = event_id
        return sub_id

    def add_exclusive_gateway(self, name: str, parent_id: str) -> str:
        return self._register_node(name, "exclusiveGateway", parent_id, "Gateway")

    def add_parallel_gateway(self, name: str, parent_id: str) -> str:
        return self._register_node(name, "parallelGateway", parent_id, "Gateway")

    def add_inclusive_gateway(self, name: str, parent_id: str) -> str:
        return self._register_node(name, "inclusiveGateway", parent_id, "Gateway")

    def add_group(self, name: str, parent_id: str) -> str:
        owner_id, lane_id, _ = self._resolve_context(parent_id)
        group_id = self._next_id("Group")
        value_id = self._next_id("CategoryValue")
        self.groups[group_id] = Group(
            id=group_id,
            name=str(name or ""),
            parent_id=self._as_id(parent_id) or "",
            lane_id=lane_id,
            owner_id=owner_id,
            category_value_id=value_id,
        )
        return group_id

    def add_link(self, source_id: str, target_id: str, condition_name: str = "") -> Optional[str]:
        """Связь потока управления. Никогда не бросает KeyError (self-healing)."""
        label = str(condition_name or "")
        src_id, tgt_id = self._as_id(source_id), self._as_id(target_id)
        self._ensure_root_node(src_id, tgt_id)
        self._ensure_root_node(tgt_id, src_id)

        missing = []
        if not src_id or src_id not in self.nodes:
            missing.append(f"source={source_id!r}")
        if not tgt_id or tgt_id not in self.nodes:
            missing.append(f"target={target_id!r}")
        if missing:
            return self._skip_link(source_id, target_id, "Несуществующие идентификаторы: " + ", ".join(missing))
        assert src_id and tgt_id
        if src_id == tgt_id:
            return self._skip_link(source_id, target_id, f"Петля на узле {src_id}")

        src, tgt = self.nodes[src_id], self.nodes[tgt_id]
        if src.kind == "endEvent":
            return self._skip_link(src_id, tgt_id, f"У завершающего события {src_id} не может быть исходящих потоков")
        if tgt.kind == "startEvent":
            return self._skip_link(src_id, tgt_id, f"У стартового события {tgt_id} не может быть входящих потоков")
        if src.owner_id != tgt.owner_id:
            lifted = self._lift_to_common_scope(src, tgt)
            if lifted is None:
                return self._skip_link(src_id, tgt_id, "Узлы принадлежат разным процессам/пулам")
            new_src, new_tgt = lifted
            self.warnings.append(
                f"Связь {src_id}→{tgt_id} пересекала границу подпроцесса и переадресована: "
                f"{new_src.id}→{new_tgt.id}."
            )
            src, tgt = new_src, new_tgt
            src_id, tgt_id = src.id, tgt.id
            if src_id == tgt_id:
                return self._skip_link(src_id, tgt_id, "После переадресации связь стала петлёй")

        for existing in self.links:
            if existing.source_id == src_id and existing.target_id == tgt_id and existing.condition_name == label:
                self.warnings.append(f"Дубликат связи {src_id}→{tgt_id} пропущен.")
                return existing.id

        link_id = self._next_id("Flow")
        self.links.append(SequenceLink(id=link_id, source_id=src_id, target_id=tgt_id, condition_name=label))
        return link_id

    def _skip_link(self, source: object, target: object, reason: str) -> None:
        self.warnings.append(f"add_link пропущен: {reason}")
        self.skipped_links.append({"source": str(source), "target": str(target), "reason": reason})
        return None

    def _chain(self, node: FlowNode) -> List[FlowNode]:
        chain = [node]
        while chain[-1].owner_id in self.nodes:
            chain.append(self.nodes[chain[-1].owner_id])
        return chain

    def _lift_to_common_scope(self, src: FlowNode, tgt: FlowNode) -> Optional[Tuple[FlowNode, FlowNode]]:
        for a in self._chain(src):
            for b in self._chain(tgt):
                if a.owner_id == b.owner_id:
                    return a, b
        return None

    def _ensure_root_node(self, node_id: Optional[str], other_id: Optional[str]) -> None:
        """Лениво создаёт ROOT_START_TASK_ID / ROOT_END_TASK_ID, если их ещё нет."""
        if not node_id or node_id in self.nodes:
            return
        if node_id == self.root_start_id:
            kind, name = "startEvent", self.root_start_name
        elif node_id == self.root_end_id:
            kind, name = "endEvent", self.root_end_name
        else:
            return
        self._ensure_implicit_process()
        other = self.nodes.get(other_id or "")
        lane_id = other.lane_id if other and other.lane_id else self._default_lane_id
        if lane_id is None:
            return
        self._register_node(name, kind, lane_id, "Event", node_id=node_id)

    # ---------------------------------------------------------- расширения
    def add_start_event(self, name: str, parent_id: str, node_id: Optional[str] = None) -> str:
        return self._register_node(name, "startEvent", parent_id, "Event", node_id=node_id)

    def add_end_event(self, name: str, parent_id: str, node_id: Optional[str] = None) -> str:
        return self._register_node(name, "endEvent", parent_id, "Event", node_id=node_id)

    def subprocess_entry(self, subprocess_id: str) -> Optional[str]:
        node = self.nodes.get(subprocess_id)
        return node.inner_start_id if node else None

    def subprocess_exit(self, subprocess_id: str) -> Optional[str]:
        node = self.nodes.get(subprocess_id)
        return node.inner_end_id if node else None

    def set_sla(self, node_id: str, hours: float) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            self.warnings.append(f"set_sla: узел {node_id!r} не найден.")
            return
        try:
            node.sla_hours = max(0.0, float(hours))
        except (TypeError, ValueError):
            self.warnings.append(f"set_sla: некорректное значение {hours!r}.")

    def set_sla_target(self, hours: float) -> None:
        try:
            self.sla_target_hours = max(0.0, float(hours))
        except (TypeError, ValueError):
            self.warnings.append(f"set_sla_target: некорректное значение {hours!r}.")

    # ------------------------------------------------------ self-healing
    def _scope_start_end(self, owner_id: str) -> Tuple[Optional[str], Optional[str]]:
        node = self.nodes.get(owner_id)
        if node is not None and node.kind == "subProcess":
            return node.inner_start_id, node.inner_end_id
        return self.root_start_id, self.root_end_id

    def heal_graph(self) -> List[str]:
        """Смыкает ветки: тупики → ROOT_END, «висящие» узлы ← ROOT_START.

        Гарантирует целостность «все ветки от старта до финиша». Каждое
        вмешательство фиксируется в `self.healed` и попадает в аудит.
        """
        actions: List[str] = []
        root_owners = [pool.process_id for pool in self.pools.values()][:1] or [self.process_id]
        scopes = root_owners + [n.id for n in list(self.nodes.values()) if n.kind == "subProcess"]

        def connect(src: str, dst: str, message: str, record: bool) -> None:
            if any(l.source_id == src and l.target_id == dst for l in self.links):
                return
            if self.add_link(src, dst) and record:
                actions.append(message)

        for owner in scopes:
            # Обвязка внутренних старта/финала подпроцесса — штатная структура, не «ремонт».
            record = owner in root_owners
            members = [n for n in self.nodes.values() if n.owner_id == owner]
            if not members:
                continue
            start_id, end_id = self._scope_start_end(owner)
            anchor = next((n for n in members if n.kind not in EVENT_KINDS), members[0])
            if start_id and start_id not in self.nodes:
                self._ensure_root_node(start_id, anchor.id)
            if end_id and end_id not in self.nodes:
                self._ensure_root_node(end_id, members[-1].id)
            members = [n for n in self.nodes.values() if n.owner_id == owner]
            ids = {n.id for n in members}
            has_in = {l.target_id for l in self.links if l.target_id in ids}
            has_out = {l.source_id for l in self.links if l.source_id in ids}
            work = [n for n in members if n.id not in (start_id, end_id)]
            start_ok = bool(start_id and start_id in self.nodes and self.nodes[start_id].owner_id == owner)
            end_ok = bool(end_id and end_id in self.nodes and self.nodes[end_id].owner_id == owner)

            if not work:
                if start_ok and end_ok:
                    connect(start_id, end_id, f"Пустой блок: {start_id}→{end_id}", record)  # type: ignore[arg-type]
                continue
            for node in work:
                if node.id not in has_in and start_ok:
                    connect(start_id, node.id, f"Узел «{node.name}» без входа подключён к старту", record)  # type: ignore[arg-type]
            for node in work:
                if node.id not in has_out and end_ok:
                    connect(node.id, end_id, f"Тупик «{node.name}» подключён к финалу", record)  # type: ignore[arg-type]
            if start_ok and start_id not in has_out and not any(l.source_id == start_id for l in self.links):
                connect(start_id, work[0].id, f"Старт подключён к «{work[0].name}»", record)  # type: ignore[arg-type]
            if end_ok and end_id not in has_in and not any(l.target_id == end_id for l in self.links):
                connect(work[-1].id, end_id, f"Финал подключён к «{work[-1].name}»", record)  # type: ignore[arg-type]
        self.healed.extend(actions)
        return actions

    # ----------------------------------------------------------- граф-утилиты
    def _children(self, owner_id: str) -> List[FlowNode]:
        return [n for n in self.nodes.values() if n.owner_id == owner_id]

    def _scope_links(self, ids: Set[str]) -> List[SequenceLink]:
        return [l for l in self.links if l.source_id in ids and l.target_id in ids]

    def _back_links(self, nodes: Sequence[FlowNode], start_id: Optional[str]) -> Set[str]:
        """Идентификаторы обратных связей (циклы возврата) методом DFS."""
        ids = {n.id for n in nodes}
        adj: Dict[str, List[SequenceLink]] = defaultdict(list)
        for link in self._scope_links(ids):
            adj[link.source_id].append(link)
        color: Dict[str, int] = {}
        back: Set[str] = set()
        roots = ([start_id] if start_id in ids else []) + [n.id for n in nodes]
        for root in roots:
            if color.get(root):
                continue
            color[root] = 1
            stack = [(root, iter(adj[root]))]
            while stack:
                node_id, it = stack[-1]
                pushed = False
                for link in it:
                    nxt = link.target_id
                    state = color.get(nxt, 0)
                    if state == 1:
                        back.add(link.id)
                    elif state == 0:
                        color[nxt] = 1
                        stack.append((nxt, iter(adj[nxt])))
                        pushed = True
                        break
                if not pushed:
                    color[node_id] = 2
                    stack.pop()
        return back

    def _layers(self, nodes: Sequence[FlowNode], start_id: Optional[str], back: Set[str]) -> Dict[str, int]:
        """Слои исполнения: длиннейший путь от старта по ациклической части графа."""
        ids = {n.id for n in nodes}
        forward: Dict[str, List[str]] = defaultdict(list)
        indeg = {n.id: 0 for n in nodes}
        for link in self._scope_links(ids):
            if link.id in back:
                continue
            forward[link.source_id].append(link.target_id)
            indeg[link.target_id] += 1
        layer = {n.id: 0 for n in nodes}
        queue = [nid for nid in (n.id for n in nodes) if indeg[nid] == 0]
        while queue:
            current = queue.pop(0)
            for nxt in forward.get(current, []):
                layer[nxt] = max(layer[nxt], layer[current] + 1)
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        _ = start_id
        return layer

    # ---------------------------------------------------------------- layout
    def _layout_subprocess(self, sub: FlowNode) -> Tuple[float, float]:
        """Раскладка внутри подпроцесса в локальных координатах (0,0 = левый верх)."""
        inner = self._children(sub.id)
        for node in inner:
            if node.kind == "subProcess":
                node.width, node.height = self._layout_subprocess(node)
        if not inner:
            return SUBPROCESS_MIN_W, SUBPROCESS_MIN_H
        back = self._back_links(inner, sub.inner_start_id)
        layers = self._layers(inner, sub.inner_start_id, back)
        columns: Dict[int, List[FlowNode]] = defaultdict(list)
        for node in inner:
            columns[layers[node.id]].append(node)
        heights = {
            c: sum(n.height for n in nodes) + SUB_ROW_GAP_Y * (len(nodes) - 1) for c, nodes in columns.items()
        }
        content_h = max(heights.values())
        widths = {c: max(n.width for n in nodes) for c, nodes in columns.items()}
        content_w = sum(widths.values()) + SUB_COLUMN_GAP_X * (len(columns) - 1)
        width = max(SUBPROCESS_MIN_W, content_w + 2 * SUB_PAD_X)
        height = max(SUBPROCESS_MIN_H, SUB_PAD_TOP + content_h + SUB_PAD_BOTTOM)
        x = (width - content_w) / 2.0
        for c in sorted(columns):
            y = SUB_PAD_TOP + (height - SUB_PAD_TOP - SUB_PAD_BOTTOM - heights[c]) / 2.0
            for node in columns[c]:
                node.x = x + (widths[c] - node.width) / 2.0
                node.y = y
                y += node.height + SUB_ROW_GAP_Y
            x += widths[c] + SUB_COLUMN_GAP_X
        return width, height

    def _place_children(self, sub: FlowNode) -> None:
        for child in self._children(sub.id):
            child.x += sub.x
            child.y += sub.y
        for child in self._children(sub.id):
            if child.kind == "subProcess":
                self._place_children(child)

    def _layout_pool(self, pool: Pool, y_offset: float, start_id: Optional[str]) -> float:
        lanes = [self.lanes[lid] for lid in pool.lane_ids]
        children = self._children(pool.process_id)
        for node in children:
            if node.kind == "subProcess":
                node.width, node.height = self._layout_subprocess(node)
            if node.lane_id not in pool.lane_ids:
                node.lane_id = pool.lane_ids[0]
        back = self._back_links(children, start_id)
        for link in self._scope_links({n.id for n in children}):
            link.is_back = link.id in back
        layers = self._layers(children, start_id, back)

        columns: Dict[int, List[FlowNode]] = defaultdict(list)
        buckets: Dict[Tuple[int, str], List[FlowNode]] = defaultdict(list)
        for node in children:
            columns[layers[node.id]].append(node)
            buckets[(layers[node.id], node.lane_id or pool.lane_ids[0])].append(node)

        need: Dict[str, float] = {lane.id: LANE_MIN_H for lane in lanes}
        for (_, lane_id), nodes in buckets.items():
            total = sum(n.height for n in nodes) + NODE_ROW_GAP_Y * (len(nodes) - 1)
            need[lane_id] = max(need[lane_id], total + 2 * LANE_PAD_Y)

        pool.x, pool.y = POOL_X, y_offset
        y = y_offset
        for lane in lanes:
            lane.x = pool.x + POOL_HEADER_W
            lane.y = y
            lane.height = need[lane.id]
            y += lane.height
        pool.height = y - y_offset

        col_left: Dict[int, float] = {}
        col_width: Dict[int, float] = {}
        cursor = pool.x + POOL_HEADER_W + LEFT_PAD
        for c in sorted(columns):
            col_left[c] = cursor
            col_width[c] = max(n.width for n in columns[c])
            cursor += col_width[c] + COLUMN_GAP_X
        pool.width = max(720.0, cursor - COLUMN_GAP_X + RIGHT_PAD - pool.x)

        for (c, lane_id), nodes in buckets.items():
            lane = self.lanes[lane_id]
            total = sum(n.height for n in nodes) + NODE_ROW_GAP_Y * (len(nodes) - 1)
            y_cursor = lane.y + (lane.height - total) / 2.0
            for node in nodes:
                node.x = col_left[c] + (col_width[c] - node.width) / 2.0
                node.y = y_cursor
                y_cursor += node.height + NODE_ROW_GAP_Y
        return y

    def compute_layout(self, root_start_id: Optional[str] = None) -> None:
        if not self.pools:
            self._ensure_implicit_process()
        start_id = root_start_id or self.root_start_id
        y = POOL_Y
        for pool in self.pools.values():
            y = self._layout_pool(pool, y, start_id) + POOL_GAP_Y
        for node in self.nodes.values():
            if node.kind == "subProcess" and node.owner_id in {p.process_id for p in self.pools.values()}:
                self._place_children(node)

        right = max((p.x + p.width for p in self.pools.values()), default=0.0)
        for pool in self.pools.values():
            pool.width = right - pool.x
            for lane_id in pool.lane_ids:
                self.lanes[lane_id].width = pool.width - POOL_HEADER_W
        self._layout_groups()
        self._route_all()
        self._place_labels()
        self._fit_last_pool_to_labels()

    def _fit_last_pool_to_labels(self) -> None:
        """Подпись под нижней стрелкой-обходом может выйти за пул — растим нижний пул и его нижнюю дорожку."""
        if not self.pools:
            return
        pool = max(self.pools.values(), key=lambda p: p.y)
        rects = [l.label_rect for l in self.links if l.label_rect] + [n.label_rect for n in self.nodes.values() if n.label_rect]
        inside = [r for r in rects if pool.y <= r[1] <= pool.y + pool.height + 60]
        overflow = max((r[3] for r in inside), default=0.0) + 6.0 - (pool.y + pool.height)
        if overflow <= 0 or not pool.lane_ids:
            return
        pool.height += overflow
        lowest = max((self.lanes[i] for i in pool.lane_ids), key=lambda lane: lane.y)
        lowest.height += overflow

    # ---------------------------------------------------------------- labels
    def _place_labels(self) -> None:
        """Подбор позиций подписей без коллизий с блоками, линиями и друг с другом.

        Для стрелок приоритет — над линией со сдвигом LABEL_LIFT (y − 12); если место
        занято, перебираются другие точки отрезков и позиции под линией.
        """
        node_rects = [(n, (n.x, n.y, n.x + n.width, n.y + n.height)) for n in self.nodes.values()]
        segments = [(a, b) for l in self.links for a, b in zip(l.waypoints, l.waypoints[1:])]
        placed: List[Rect] = []
        lo_x = min((p.x for p in self.pools.values()), default=0.0) + POOL_HEADER_W
        hi_x = max((p.x + p.width for p in self.pools.values()), default=10_000.0)

        def cost(rect: Rect, home: Set[str]) -> float:
            total = 0.0
            for node, nr in node_rects:
                # Внутри подпроцесса подпись допустима, только если её стрелка/узел сами лежат в нём:
                # иначе подпись возвратной стрелки «прилипает» к чужой рамке и читается как его часть.
                if _rects_intersect(rect, nr) and not (node.id in home and _rect_contains(nr, rect)):
                    total += 100.0
            for other in placed:
                if _rects_intersect(rect, other, 0.0):
                    total += 100.0
                elif _rects_intersect(rect, (other[0] - LABEL_GAP, other[1] - LABEL_GAP, other[2] + LABEL_GAP, other[3] + LABEL_GAP), 0.0):
                    total += 40.0  # подписи «слипаются» и читаются как одна
            inflated = (rect[0] - 1.0, rect[1] - 1.0, rect[2] + 1.0, rect[3] + 1.0)
            for a, b in segments:
                if _hits_rect(a, b, inflated):
                    total += 60.0
            if rect[0] < lo_x or rect[2] > hi_x:
                total += 30.0
            return total

        def choose(cands: Sequence[Rect], home: Set[str]) -> Rect:
            best, best_cost = cands[0], float("inf")
            for idx, rect in enumerate(cands):
                c = cost(rect, home) + idx * 0.05
                if c < best_cost:
                    best, best_cost = rect, c
                if c < 0.5:
                    break
            placed.append(best)
            return best

        for link in self.links:
            if not link.condition_name:
                continue
            w, h = _label_box(link.condition_name, 40.0, 150.0)
            home = {
                n.id
                for n, nr in node_rects
                if n.kind == "subProcess" and all(nr[0] <= p[0] <= nr[2] and nr[1] <= p[1] <= nr[3] for p in link.waypoints)
            }
            from_gateway = self.nodes[link.source_id].kind in GATEWAY_KINDS if link.source_id in self.nodes else False
            link.label_rect = choose(self._edge_label_candidates(link.waypoints, w, h, near_source=from_gateway), home)

        for node in self.nodes.values():
            if not node.name:
                continue
            cx, top, bottom = node.x + node.width / 2.0, node.y, node.y + node.height
            if node.kind in EVENT_KINDS:
                w, h = _label_box(node.name, 70.0, 120.0)
                cands = [
                    (cx - w / 2, bottom + 4, cx + w / 2, bottom + 4 + h),
                    (cx - w / 2, top - 4 - h, cx + w / 2, top - 4),
                    (node.x + node.width + 4, top + node.height / 2 - h / 2, node.x + node.width + 4 + w, top + node.height / 2 + h / 2),
                    (node.x - 4 - w, top + node.height / 2 - h / 2, node.x - 4, top + node.height / 2 + h / 2),
                ]
            elif node.kind in GATEWAY_KINDS:
                w, h = _label_box(node.name, 60.0, 150.0)
                cands = []
                # ярусы: над ромбом (0, −20, −40), затем под ромбом; по горизонтали — влево/центр/вправо и дальше
                dxs = (-6 - w, 6.0, -w / 2, 46.0, -6 - w - 46.0, 92.0, -6 - w - 92.0)
                for dy_top in (0.0, 20.0, 40.0):
                    for dx in dxs:
                        cands.append((cx + dx, top - h - 2 - dy_top, cx + dx + w, top - 2 - dy_top))
                for dy_bot in (0.0, 20.0, 40.0):
                    for dx in dxs:
                        cands.append((cx + dx, bottom + 2 + dy_bot, cx + dx + w, bottom + 2 + dy_bot + h))
                mid = top + node.height / 2
                cands.append((node.x - 6 - w, mid - h / 2, node.x - 6, mid + h / 2))
                cands.append((node.x + node.width + 6, mid - h / 2, node.x + node.width + 6 + w, mid + h / 2))
            else:
                continue
            node.label_rect = choose(cands, {node.owner_id})

    @staticmethod
    def _edge_label_candidates(points: Sequence[Point], w: float, h: float, near_source: bool = False) -> List[Rect]:
        segs = list(zip(points, points[1:]))
        horizontal = sorted(
            (s for s in segs if abs(s[0][1] - s[1][1]) < 0.01), key=lambda s: -abs(s[0][0] - s[1][0])
        )
        vertical = sorted(
            (s for s in segs if abs(s[0][0] - s[1][0]) < 0.01), key=lambda s: -abs(s[0][1] - s[1][1])
        )
        fractions = (0.5, 0.25, 0.75, 0.1, 0.9)
        above: List[Rect] = []
        below: List[Rect] = []
        for tier in (0.0, 18.0):  # над линией: нижний край подписи на LABEL_LIFT + 2 выше стрелки
            for a, b in horizontal:
                x1, x2, y = min(a[0], b[0]), max(a[0], b[0]), a[1]
                for f in fractions:
                    cx = x1 + (x2 - x1) * f
                    above.append((cx - w / 2, y - LABEL_LIFT - 2 - h - tier, cx + w / 2, y - LABEL_LIFT - 2 - tier))
        for tier in (0.0, 18.0):  # под линией
            for a, b in horizontal:
                x1, x2, y = min(a[0], b[0]), max(a[0], b[0]), a[1]
                for f in fractions:
                    cx = x1 + (x2 - x1) * f
                    top = y + LABEL_LIFT - 4 + tier
                    below.append((cx - w / 2, top, cx + w / 2, top + h))
        # Стрелка-обход «понизу» (U-маршрут возврата ниже обоих концов): подпись под линией — над ней
        # она упирается в блоки, мимо которых идёт обход, и читается как их подпись.
        goes_under = bool(horizontal) and points and horizontal[0][0][1] > max(points[0][1], points[-1][1]) + 0.01
        cands: List[Rect] = below + above if goes_under else above + below
        if near_source and not goes_under and len(points) >= 2:
            # Условие ветки — у выхода из шлюза (конвенция BPMN), а не у конца стрелки, где его
            # легко принять за подпись следующего узла.
            (x0, y0), (x1, y1) = points[0], points[1]
            start: List[Rect] = []
            if abs(x0 - x1) < 0.01:  # вертикальный выход: сбоку от линии; узкий вариант — в несколько строк
                sign = 1.0 if y1 > y0 else -1.0
                text_w = max(w - 10.0, 1.0) * max(1, round(h / 15.0))
                for wv in (w, 96.0):
                    hv = 15.0 * math.ceil(text_w / (wv - 8.0)) if wv < w else h
                    if abs(y1 - y0) < hv + 24:
                        continue
                    y_near, y_far = y0 + sign * 12, y0 + sign * (12 + hv)
                    top, bottom = min(y_near, y_far), max(y_near, y_far)
                    start += [(x0 + 8, top, x0 + 8 + wv, bottom), (x0 - 8 - wv, top, x0 - 8, bottom)]
            elif abs(y0 - y1) < 0.01 and abs(x1 - x0) >= w * 0.6:  # горизонтальный выход: над/под линией
                sign = 1.0 if x1 > x0 else -1.0
                cx = x0 + sign * (8 + w / 2)
                start += [
                    (cx - w / 2, y0 - LABEL_LIFT - 2 - h, cx + w / 2, y0 - LABEL_LIFT - 2),
                    (cx - w / 2, y0 + LABEL_LIFT - 4, cx + w / 2, y0 + LABEL_LIFT - 4 + h),
                ]
            cands = start + cands
        for a, b in vertical:  # справа/слева от вертикальных участков
            y1, y2, x = min(a[1], b[1]), max(a[1], b[1]), a[0]
            for f in fractions:
                cy = y1 + (y2 - y1) * f
                cands.append((x + 8, cy - h / 2, x + 8 + w, cy + h / 2))
                cands.append((x - 8 - w, cy - h / 2, x - 8, cy + h / 2))
        if not cands and points:
            x, y = points[0]
            cands.append((x, y - h - LABEL_LIFT, x + w, y - LABEL_LIFT))
        return cands

    def _layout_groups(self) -> None:
        members: Dict[str, List[FlowNode]] = defaultdict(list)
        for node in self.nodes.values():
            if node.group_id:
                members[node.group_id].append(node)
        for group in self.groups.values():
            nodes = members.get(group.id, [])
            if nodes:
                x1 = min(n.x for n in nodes) - 20.0
                y1 = min(n.y for n in nodes) - 30.0
                x2 = max(n.x + n.width for n in nodes) + 20.0
                y2 = max(n.y + n.height for n in nodes) + 20.0
            elif group.lane_id in self.lanes:
                lane = self.lanes[group.lane_id or ""]
                x1, y1 = lane.x + 16.0, lane.y + 10.0
                x2, y2 = lane.x + min(lane.width, 320.0), lane.y + lane.height - 10.0
            else:
                x1, y1, x2, y2 = 200.0, 100.0, 360.0, 180.0
            group.x, group.y = x1, y1
            group.width, group.height = max(80.0, x2 - x1), max(40.0, y2 - y1)

    # -------------------------------------------------------------- routing
    def _scope_bounds(self, scope_owner: str) -> Tuple[float, float]:
        node = self.nodes.get(scope_owner)
        if node is not None and node.kind == "subProcess":
            return node.y + 34.0, node.y + node.height - 8.0
        for pool in self.pools.values():
            if pool.process_id == scope_owner:
                return pool.y + 8.0, pool.y + pool.height - 8.0
        return 0.0, 10_000.0

    def _route_all(self) -> None:
        placed: Dict[str, List[Tuple[Point, Point]]] = defaultdict(list)
        for link in self.links:
            src, tgt = self.nodes[link.source_id], self.nodes[link.target_id]
            scope = src.owner_id
            obstacles = [n for n in self.nodes.values() if n.owner_id == scope]
            bounds = self._scope_bounds(scope)
            link.waypoints = self._best_route(src, tgt, obstacles, placed[scope], bounds)
            for a, b in zip(link.waypoints, link.waypoints[1:]):
                placed[scope].append((a, b))

    @staticmethod
    def _candidates(src: FlowNode, tgt: FlowNode, bounds: Tuple[float, float]) -> List[List[Point]]:
        sx1, sy1, sx2, sy2 = src.x, src.y, src.x + src.width, src.y + src.height
        tx1, ty1, tx2, ty2 = tgt.x, tgt.y, tgt.x + tgt.width, tgt.y + tgt.height
        scx, scy = (sx1 + sx2) / 2.0, (sy1 + sy2) / 2.0
        tcx, tcy = (tx1 + tx2) / 2.0, (ty1 + ty2) / 2.0
        r_s, t_s, b_s = (sx2, scy), (scx, sy1), (scx, sy2)
        l_t, t_t, b_t = (tx1, tcy), (tcx, ty1), (tcx, ty2)
        lo, hi = bounds
        cands: List[List[Point]] = []

        if tx1 >= sx2 + 16.0:
            if abs(scy - tcy) < 1.5:
                cands.append([r_s, l_t])
            for mx in ((sx2 + tx1) / 2.0, sx2 + 26.0, tx1 - 26.0):
                cands.append([r_s, (mx, scy), (mx, tcy), l_t])
            if abs(scy - tcy) >= 1.5 and scx < tx1 - 8.0:
                cands.append([b_s if tcy > scy else t_s, (scx, tcy), l_t])
            if tcx > sx2 + 8.0:
                if scy < ty1 - 6.0:
                    cands.append([r_s, (tcx, scy), t_t])
                if scy > ty2 + 6.0:
                    cands.append([r_s, (tcx, scy), b_t])

        if abs(scx - tcx) < 1.5:
            cands.append([b_s, t_t] if tcy > scy else [t_s, b_t])
        else:
            if ty1 >= sy2 + 12.0:
                mid = (sy2 + ty1) / 2.0
                cands.append([b_s, (scx, mid), (tcx, mid), t_t])
            if ty2 <= sy1 - 12.0:
                mid = (ty2 + sy1) / 2.0
                cands.append([t_s, (scx, mid), (tcx, mid), b_t])

        for off in (30.0, 54.0, 78.0):
            y_below = min(max(sy2, ty2) + off, hi - 4.0)
            y_above = max(min(sy1, ty1) - off, lo + 4.0)
            cands.append([b_s, (scx, y_below), (tcx, y_below), b_t])
            cands.append([t_s, (scx, y_above), (tcx, y_above), t_t])
            for yy in (y_below, y_above):
                cands.append(
                    [r_s, (sx2 + 18.0, scy), (sx2 + 18.0, yy), (tx1 - 18.0, yy), (tx1 - 18.0, tcy), l_t]
                )
        return cands

    def _best_route(
        self,
        src: FlowNode,
        tgt: FlowNode,
        obstacles: Iterable[FlowNode],
        placed: Sequence[Tuple[Point, Point]],
        bounds: Tuple[float, float],
    ) -> List[Point]:
        rects: List[Tuple[float, float, float, float]] = []
        for node in obstacles:
            if node.id in (src.id, tgt.id):
                rects.append((node.x + 1.0, node.y + 1.0, node.x + node.width - 1.0, node.y + node.height - 1.0))
            else:
                rects.append((node.x - 4.0, node.y - 4.0, node.x + node.width + 4.0, node.y + node.height + 4.0))

        best: Optional[List[Point]] = None
        best_cost = float("inf")
        for raw in self._candidates(src, tgt, bounds):
            path = _clean_path(raw)
            if len(path) < 2:
                continue
            segs = list(zip(path, path[1:]))
            hits = sum(1 for a, b in segs for r in rects if _hits_rect(a, b, r))
            overlap = sum(_overlap_len(s, p) > 6.0 for s in segs for p in placed)
            length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segs)
            cost = (len(path) - 2) * 15.0 + length * 0.02 + overlap * 40.0 + hits * 1000.0
            if cost < best_cost:
                best, best_cost = path, cost
        if best is None:
            sx, sy = src.x + src.width, src.y + src.height / 2.0
            tx, ty = tgt.x, tgt.y + tgt.height / 2.0
            mx = (sx + tx) / 2.0
            best = _clean_path([(sx, sy), (mx, sy), (mx, ty), (tx, ty)])
        return best

    # --------------------------------------------------------------- аудит
    def _node_hours(self, node: FlowNode, memo: Dict[str, float]) -> float:
        if node.id in memo:
            return memo[node.id]
        if node.sla_hours is not None:
            value = node.sla_hours
        elif node.kind == "subProcess":
            value = self._critical_path(node.id, node.inner_start_id, memo)[0]
        else:
            value = DEFAULT_HOURS.get(node.kind, 1.0)
        memo[node.id] = value
        return value

    @staticmethod
    def _bellman_ford_longest(
        node_ids: Sequence[str],
        edges: Sequence[Tuple[str, str]],
        hours: Dict[str, float],
        sources: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, str]]:
        """Самый длинный путь по трудозатратам: Беллман-Форд на весах −hours(v).

        Отрицательные веса рёбер — штатный режим алгоритма; цикл возврата
        предварительно удалён, поэтому отрицательных циклов нет.
        """
        inf = float("inf")
        dist = {nid: inf for nid in node_ids}
        pred: Dict[str, str] = {}
        for nid, h in sources.items():
            dist[nid] = -h
        for _ in range(max(len(node_ids) - 1, 1)):
            changed = False
            for u, v in edges:
                if dist[u] == inf:
                    continue
                candidate = dist[u] - hours[v]
                if candidate < dist[v] - 1e-9:
                    dist[v] = candidate
                    pred[v] = u
                    changed = True
            if not changed:
                break
        return dist, pred

    def _critical_path(
        self, owner_id: str, start_id: Optional[str], memo: Dict[str, float]
    ) -> Tuple[float, List[str]]:
        nodes = self._children(owner_id)
        if not nodes:
            return 0.0, []
        ids = [n.id for n in nodes]
        back = self._back_links(nodes, start_id)
        edges = [(l.source_id, l.target_id) for l in self._scope_links(set(ids)) if l.id not in back]
        hours = {n.id: self._node_hours(n, memo) for n in nodes}
        has_in = {v for _, v in edges}
        has_out = {u for u, _ in edges}
        sources = {nid: hours[nid] for nid in ids if nid not in has_in}
        dist, pred = self._bellman_ford_longest(ids, edges, hours, sources)
        sinks = [nid for nid in ids if nid not in has_out and dist[nid] != float("inf")]
        if not sinks:
            return 0.0, []
        best = min(sinks, key=lambda nid: dist[nid])
        path = [best]
        while path[-1] in pred and len(path) <= len(ids):
            path.append(pred[path[-1]])
        path.reverse()
        return -dist[best], path

    def _rework_loops(self, memo: Dict[str, float]) -> List[Dict[str, object]]:
        loops: List[Dict[str, object]] = []
        owners = [p.process_id for p in self.pools.values()][:1] or [self.process_id]
        owners += [n.id for n in self.nodes.values() if n.kind == "subProcess"]
        for owner in owners:
            nodes = self._children(owner)
            if not nodes:
                continue
            start_id = self._scope_start_end(owner)[0]
            back = self._back_links(nodes, start_id)
            ids = [n.id for n in nodes]
            edges = [(l.source_id, l.target_id) for l in self._scope_links(set(ids)) if l.id not in back]
            hours = {n.id: self._node_hours(n, memo) for n in nodes}
            for link in self._scope_links(set(ids)):
                if link.id not in back:
                    continue
                dist, _ = self._bellman_ford_longest(ids, edges, hours, {link.target_id: hours[link.target_id]})
                tail = link.source_id
                cycle = -dist[tail] if dist[tail] != float("inf") else hours[tail] + hours[link.target_id]
                src, tgt = self.nodes[link.source_id], self.nodes[link.target_id]
                loops.append(
                    {
                        "link_id": link.id,
                        "from": src.name or src.id,
                        "to": tgt.name or tgt.id,
                        "label": link.condition_name or "Возврат",
                        "cycle_hours": round(cycle, 2),
                        "lane": self.lanes[src.lane_id].name if src.lane_id in self.lanes else "",
                    }
                )
        return loops

    def analyze_bottlenecks(self) -> Dict[str, object]:
        memo: Dict[str, float] = {}
        root_owner = [p.process_id for p in self.pools.values()][:1] or [self.process_id]
        root = root_owner[0]
        top_nodes = self._children(root)

        # --- нагрузка по ролям: считаем атомарные шаги, включая содержимое подпроцессов
        atomic = [n for n in self.nodes.values() if n.kind in WORK_KINDS]
        per_lane_tasks: Dict[str, int] = defaultdict(int)
        per_lane_hours: Dict[str, float] = defaultdict(float)
        for node in atomic:
            key = node.lane_id or "unassigned"
            per_lane_tasks[key] += 1
            per_lane_hours[key] += self._node_hours(node, memo)
        total_tasks = sum(per_lane_tasks.values()) or 1
        total_hours = sum(per_lane_hours.values()) or 1.0
        lane_load = []
        for lane in self.lanes.values():
            count = per_lane_tasks.get(lane.id, 0)
            hours = per_lane_hours.get(lane.id, 0.0)
            lane_load.append(
                {
                    "lane_id": lane.id,
                    "role": lane.name,
                    "tasks": count,
                    "share": round(count / total_tasks, 3),
                    "hours": round(hours, 2),
                    "hours_share": round(hours / total_hours, 3),
                }
            )
        bus_alerts = []
        for item in lane_load:
            if item["share"] > BUS_FACTOR_THRESHOLD:  # type: ignore[operator]
                bus_alerts.append(
                    {
                        "lane_id": item["lane_id"],
                        "role": item["role"],
                        "tasks": item["tasks"],
                        "share": item["share"],
                        "message": (
                            f"Роль «{item['role']}» выполняет {item['share']:.0%} шагов процесса "  # type: ignore[str-format]
                            f"(порог bus-factor {BUS_FACTOR_THRESHOLD:.0%}): риск зависимости от ключевого исполнителя."
                        ),
                    }
                )
        top_load = max(lane_load, key=lambda it: it["share"], default=None)  # type: ignore[arg-type,return-value]

        # --- тупики и висящие узлы во всех областях видимости
        dead_ends, orphans = [], []
        for owner in [root] + [n.id for n in self.nodes.values() if n.kind == "subProcess"]:
            members = self._children(owner)
            ids = {n.id for n in members}
            has_in = {l.target_id for l in self._scope_links(ids)}
            has_out = {l.source_id for l in self._scope_links(ids)}
            for node in members:
                if node.kind != "endEvent" and node.id not in has_out:
                    dead_ends.append(
                        {
                            "id": node.id,
                            "name": node.name,
                            "kind": node.kind,
                            "message": f"Тупиковый узел «{node.name}» ({node.id}): нет исходящего потока.",
                        }
                    )
                if node.kind != "startEvent" and node.id not in has_in:
                    orphans.append(node.id)

        # --- критический путь и циклы возврата
        start_id = self.root_start_id if self.root_start_id in self.nodes else next(
            (n.id for n in top_nodes if n.kind == "startEvent"), None
        )
        base_hours, path_ids = self._critical_path(root, start_id, memo)
        back = self._back_links(top_nodes, start_id)
        layers = self._layers(top_nodes, start_id, back)
        path_layers = (max(layers.values()) + 1) if layers else 0
        loops = self._rework_loops(memo)
        cycle_values = [float(l["cycle_hours"]) for l in loops]  # type: ignore[arg-type]
        rework_hours = round(max(cycle_values, default=0.0), 2)  # худший одиночный возврат
        rework_total = round(sum(cycle_values), 2)  # если бы отработали все циклы по одному разу
        with_rework = base_hours + rework_hours
        critical_path = [
            {
                "id": nid,
                "name": self.nodes[nid].name,
                "kind": self.nodes[nid].kind,
                "role": self.lanes[self.nodes[nid].lane_id].name if self.nodes[nid].lane_id in self.lanes else "",
                "hours": round(self._node_hours(self.nodes[nid], memo), 2),
            }
            for nid in path_ids
            if self.nodes[nid].kind not in EVENT_KINDS
        ]
        target = self.sla_target_hours
        sla = {
            "target_hours": target,
            "critical_path_hours": round(base_hours, 2),
            "with_rework_hours": round(with_rework, 2),
            "rework_hours": rework_hours,
            "rework_total_hours": rework_total,
            "rework_share": round(rework_hours / base_hours, 3) if base_hours else 0.0,
            "breach": bool(target is not None and with_rework > target),
        }

        sla_risks: List[Dict[str, object]] = []
        if target is not None and base_hours > target:
            sla_risks.append(
                {
                    "metric": "critical_path_over_target",
                    "severity": "high",
                    "value": round(base_hours, 2),
                    "message": f"Критический путь {base_hours:.1f} ч превышает целевой SLA {target:.1f} ч даже без возвратов.",
                }
            )
        elif target is not None and with_rework > target:
            sla_risks.append(
                {
                    "metric": "rework_breaks_sla",
                    "severity": "high",
                    "value": round(with_rework, 2),
                    "message": (
                        f"С учётом худшего цикла возврата путь растёт до {with_rework:.1f} ч "
                        f"и выходит за целевой SLA {target:.1f} ч."
                    ),
                }
            )
        if loops and sla["rework_share"] and float(sla["rework_share"]) > 0.25:  # type: ignore[arg-type]
            sla_risks.append(
                {
                    "metric": "rework_share",
                    "severity": "medium",
                    "value": sla["rework_share"],
                    "message": f"Худший цикл возврата добавляет {float(sla['rework_share']):.0%} к длительности критического пути.",  # type: ignore[arg-type]
                }
            )
        if path_layers >= 10:
            sla_risks.append(
                {
                    "metric": "critical_path_layers",
                    "severity": "medium",
                    "value": path_layers,
                    "message": f"Критический путь состоит из {path_layers} последовательных слоёв согласования.",
                }
            )
        for alert in bus_alerts:
            sla_risks.append(
                {
                    "metric": "overloaded_role",
                    "severity": "medium",
                    "value": alert["share"],
                    "message": f"Перегрузка роли «{alert['role']}» повышает риск простоя и срыва SLA.",
                }
            )

        subprocess_count = sum(1 for n in self.nodes.values() if n.kind == "subProcess")
        recommendations = self._recommendations(
            bus_alerts, loops, critical_path, sla, dead_ends, subprocess_count, len(atomic)
        )

        return {
            "bus_factor_alerts": bus_alerts,
            "bus_factor": {
                "threshold": BUS_FACTOR_THRESHOLD,
                "max_share": top_load["share"] if top_load else 0.0,
                "top_role": top_load["role"] if top_load else "",
                "status": "risk" if bus_alerts else "ok",
            },
            "lane_load": lane_load,
            "dead_ends": dead_ends,
            "sla": sla,
            "sla_risks": sla_risks,
            "critical_path": critical_path,
            "rework_loops": loops,
            "orphans_without_incoming": orphans,
            "recommendations": recommendations,
            "auto_healed": list(self.healed),
            "engine_warnings": list(self.warnings),
            "skipped_links": list(self.skipped_links),
            "stats": {
                "nodes": len(self.nodes),
                "valid_links": len(self.links),
                "lanes": len(self.lanes),
                "subprocesses": subprocess_count,
                "work_items": len(atomic),
                "critical_path_layers": path_layers,
                "critical_path_hours": round(base_hours, 2),
            },
        }

    @staticmethod
    def _recommendations(
        bus_alerts: List[Dict[str, object]],
        loops: List[Dict[str, object]],
        critical_path: List[Dict[str, object]],
        sla: Dict[str, object],
        dead_ends: List[Dict[str, object]],
        subprocess_count: int,
        work_items: int,
    ) -> List[str]:
        recs: List[str] = []
        for alert in bus_alerts:
            recs.append(
                f"Bus-factor: роль «{alert['role']}» держит {float(alert['share']):.0%} шагов — "  # type: ignore[arg-type]
                "назначьте дублирующего исполнителя и вынесите типовые операции в автоматизацию."
            )
        for loop in loops:
            recs.append(
                f"Цикл возврата «{loop['label']}» ({loop['from']} → {loop['to']}) стоит ≈{float(loop['cycle_hours']):.1f} ч: "  # type: ignore[arg-type]
                "введите чек-лист предварительной проверки до отправки на согласование."
            )
        slowest = sorted(critical_path, key=lambda it: float(it["hours"]), reverse=True)[:3]  # type: ignore[arg-type]
        if slowest and float(sla["critical_path_hours"]) > 0:  # type: ignore[arg-type]
            names = "; ".join(f"«{it['name']}» ({float(it['hours']):.1f} ч)" for it in slowest)  # type: ignore[arg-type]
            recs.append(f"Критический путь: приоритет оптимизации — {names}. Проверьте возможность распараллеливания.")
        if sla.get("breach"):
            recs.append("Целевой SLA нарушается: сократите длительность шагов критического пути или уберите возвраты.")
        for node in dead_ends[:3]:
            recs.append(f"Устраните тупик «{node['name']}»: добавьте исходящий поток или завершающее событие.")
        if work_items > 12 and subprocess_count == 0:
            recs.append("Диаграмма без декомпозиции: сгруппируйте цепочки из 4+ шагов одной роли в подпроцессы.")
        if not recs:
            recs.append("Существенных узких мест не выявлено: процесс сбалансирован по ролям и не содержит тупиков.")
        return recs

    # ------------------------------------------------------------------ XML
    def to_bpmn_xml(self, root_process_id: str, root_start_id: str, root_end_id: str) -> str:
        if root_process_id and root_process_id != self.process_id:
            previous = self.process_id
            self.process_id = root_process_id
            for pool in self.pools.values():
                if pool.process_id == previous:
                    pool.process_id = root_process_id
            for lane in self.lanes.values():
                if lane.process_id == previous:
                    lane.process_id = root_process_id
            for node in self.nodes.values():
                if node.owner_id == previous:
                    node.owner_id = root_process_id
            for group in self.groups.values():
                if group.owner_id == previous:
                    group.owner_id = root_process_id

        if root_start_id in self.nodes:
            self.root_start_id = root_start_id
        else:
            self.warnings.append(f"root_start_id={root_start_id!r} отсутствует в диаграмме.")
        if root_end_id in self.nodes:
            self.root_end_id = root_end_id
        else:
            self.warnings.append(f"root_end_id={root_end_id!r} отсутствует в диаграмме.")

        start = self.root_start_id if self.root_start_id in self.nodes else next(
            (n.id for n in self.nodes.values() if n.kind == "startEvent" and n.owner_id == self.process_id), None
        )
        self.compute_layout(start)

        incoming: Dict[str, List[str]] = defaultdict(list)
        outgoing: Dict[str, List[str]] = defaultdict(list)
        for link in self.links:
            outgoing[link.source_id].append(link.id)
            incoming[link.target_id].append(link.id)

        out: List[str] = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            (
                f'<bpmn:definitions xmlns:xsi="{XSI}" xmlns:bpmn="{BPMN_MODEL}" '
                f'xmlns:bpmndi="{BPMN_DI}" xmlns:dc="{DC}" xmlns:di="{DI}" '
                f'xmlns:bioc="{BIOC}" xmlns:color="{COLOR}" '
                f'id="Definitions_InterRAO" targetNamespace="{TNS}" '
                f'exporter="Inter RAO BPMN Architect" exporterVersion="2.0.0">'
            ),
        ]
        if self.groups:
            out.append('  <bpmn:category id="Category_Groups">')
            for group in self.groups.values():
                out.append(f'    <bpmn:categoryValue id="{group.category_value_id}" value="{_xml_text(group.name)}" />')
            out.append("  </bpmn:category>")

        out.append(f'  <bpmn:collaboration id="{self.collaboration_id}">')
        for pool in self.pools.values():
            out.append(
                f'    <bpmn:participant id="{pool.participant_id}" name="{_xml_text(pool.name)}" '
                f'processRef="{pool.process_id}" />'
            )
        for group in self.groups.values():
            out.append(f'    <bpmn:group id="{group.id}" categoryValueRef="{group.category_value_id}" />')
        out.append("  </bpmn:collaboration>")

        for pool in self.pools.values():
            pid = pool.process_id
            out.append(f'  <bpmn:process id="{pid}" name="{_xml_text(self.process_name)}" isExecutable="false">')
            out.append(f'    <bpmn:laneSet id="LaneSet_{pid}">')
            for lane_id in pool.lane_ids:
                lane = self.lanes[lane_id]
                out.append(f'      <bpmn:lane id="{lane.id}" name="{_xml_text(lane.name)}">')
                for node in self.nodes.values():
                    if node.lane_id == lane.id and node.owner_id == pid:
                        out.append(f"        <bpmn:flowNodeRef>{node.id}</bpmn:flowNodeRef>")
                out.append("      </bpmn:lane>")
            out.append("    </bpmn:laneSet>")
            top = [n for n in self.nodes.values() if n.owner_id == pid]
            top_ids = {n.id for n in top}
            for node in top:
                out.append(self._node_xml(node, incoming, outgoing, 4))
            for link in self.links:
                if link.source_id in top_ids and link.target_id in top_ids:
                    out.append(self._flow_xml(link, 4))
            out.append("  </bpmn:process>")

        out.append('  <bpmndi:BPMNDiagram id="BPMNDiagram_1">')
        out.append(f'    <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="{self.collaboration_id}">')
        out.extend(self._di_xml())
        out.append("    </bpmndi:BPMNPlane>")
        # Стиль заголовков подпроцессов (BPMN DI 12.2.3.6 BPMNLabelStyle): крупный жирный шрифт.
        out.append(f'    <bpmndi:BPMNLabelStyle id="{SUBPROCESS_LABEL_STYLE}">')
        out.append(f'      <dc:Font name="Arial" size="{SUBPROCESS_TITLE_PT}" isBold="true" />')
        out.append("    </bpmndi:BPMNLabelStyle>")
        out.append("  </bpmndi:BPMNDiagram>")
        out.append("</bpmn:definitions>")
        return "\n".join(out) + "\n"

    def _di_xml(self) -> List[str]:
        out: List[str] = []

        def bounds(x: float, y: float, w: float, h: float, indent: int = 8) -> str:
            return f'{" " * indent}<dc:Bounds x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(w)}" height="{_fmt(h)}" />'

        for pool in self.pools.values():
            out.append(
                f'      <bpmndi:BPMNShape id="{pool.participant_id}_di" bpmnElement="{pool.participant_id}" '
                f'isHorizontal="true"{_color_attrs(*POOL_COLORS)}>'
            )
            out.append(bounds(pool.x, pool.y, pool.width, pool.height))
            out.append("      </bpmndi:BPMNShape>")
            for i, lane_id in enumerate(pool.lane_ids):
                lane = self.lanes[lane_id]
                out.append(
                    f'      <bpmndi:BPMNShape id="{lane.id}_di" bpmnElement="{lane.id}" '
                    f'isHorizontal="true"{_color_attrs(*LANE_COLORS[i % 2])}>'
                )
                out.append(bounds(lane.x, lane.y, lane.width, lane.height))
                out.append("      </bpmndi:BPMNShape>")

        for group in self.groups.values():
            out.append(
                f'      <bpmndi:BPMNShape id="{group.id}_di" bpmnElement="{group.id}"{_color_attrs(*GROUP_COLORS)}>'
            )
            out.append(bounds(group.x, group.y, group.width, group.height))
            out.append("      </bpmndi:BPMNShape>")

        for node in self.nodes.values():
            stroke, fill = KIND_COLORS[node.kind]
            expanded = ' isExpanded="true"' if node.kind == "subProcess" else ""
            out.append(
                f'      <bpmndi:BPMNShape id="{node.id}_di" bpmnElement="{node.id}"{expanded}'
                f"{_color_attrs(stroke, fill)}>"
            )
            out.append(bounds(node.x, node.y, node.width, node.height))
            if node.label_rect is not None:
                x1, y1, x2, y2 = node.label_rect
                out.append("        <bpmndi:BPMNLabel>")
                out.append(bounds(x1, y1, x2 - x1, y2 - y1, 10))
                out.append("        </bpmndi:BPMNLabel>")
            elif node.kind == "subProcess":
                out.append(f'        <bpmndi:BPMNLabel labelStyle="{SUBPROCESS_LABEL_STYLE}" />')
            out.append("      </bpmndi:BPMNShape>")

        for link in self.links:
            color = REWORK_EDGE_COLOR if link.is_back else EDGE_COLOR
            out.append(
                f'      <bpmndi:BPMNEdge id="{link.id}_di" bpmnElement="{link.id}"{_color_attrs(color)}>'
            )
            for x, y in link.waypoints:
                out.append(f'        <di:waypoint x="{_fmt(x)}" y="{_fmt(y)}" />')
            if link.label_rect is not None:
                x1, y1, x2, y2 = link.label_rect
                out.append("        <bpmndi:BPMNLabel>")
                out.append(bounds(x1, y1, x2 - x1, y2 - y1, 10))
                out.append("        </bpmndi:BPMNLabel>")
            out.append("      </bpmndi:BPMNEdge>")
        return out

    def _node_xml(
        self,
        node: FlowNode,
        incoming: Dict[str, List[str]],
        outgoing: Dict[str, List[str]],
        indent: int,
    ) -> str:
        tag = XML_TAGS[node.kind]
        pad, inner = " " * indent, " " * (indent + 2)
        lines = [f'{pad}<{tag} id="{node.id}" name="{_xml_text(node.name)}">']
        for flow_id in incoming.get(node.id, []):
            lines.append(f"{inner}<bpmn:incoming>{flow_id}</bpmn:incoming>")
        for flow_id in outgoing.get(node.id, []):
            lines.append(f"{inner}<bpmn:outgoing>{flow_id}</bpmn:outgoing>")
        if node.kind == "subProcess":
            children = self._children(node.id)
            child_ids = {c.id for c in children}
            c_in: Dict[str, List[str]] = defaultdict(list)
            c_out: Dict[str, List[str]] = defaultdict(list)
            for link in self._scope_links(child_ids):
                c_out[link.source_id].append(link.id)
                c_in[link.target_id].append(link.id)
            for child in children:
                lines.append(self._node_xml(child, c_in, c_out, indent + 2))
            for link in self._scope_links(child_ids):
                lines.append(self._flow_xml(link, indent + 2))
        lines.append(f"{pad}</{tag}>")
        return "\n".join(lines)

    @staticmethod
    def _flow_xml(link: SequenceLink, indent: int) -> str:
        pad = " " * indent
        name = f' name="{_xml_text(link.condition_name)}"' if link.condition_name else ""
        return (
            f'{pad}<bpmn:sequenceFlow id="{link.id}"{name} '
            f'sourceRef="{link.source_id}" targetRef="{link.target_id}" />'
        )
