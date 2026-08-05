"""Portable PNG export for V3 causal graphs."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from aware_models.executor import CausalGraph


def export_causal_graph_png(
    graph: CausalGraph | dict[str, Any],
    output_path: str | Path,
    *,
    scale: int = 2,
) -> Path:
    """Render evidence→component→hypothesis→outcome as a readable PNG."""
    payload = graph.model_dump(mode="json") if isinstance(graph, CausalGraph) else graph
    nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    edges = payload.get("edges", []) if isinstance(payload, dict) else []
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("Cannot export an empty causal graph.")
    scale = max(1, min(int(scale), 4))
    kinds = ("evidence", "component", "hypothesis", "outcome")
    labels = {
        "evidence": "EVIDENCE",
        "component": "COMPONENTS",
        "hypothesis": "HYPOTHESES",
        "outcome": "OUTCOME",
    }
    columns = {"evidence": 30, "component": 300, "hypothesis": 570, "outcome": 840}
    node_width, node_height, row_gap = 220, 66, 24
    grouped = {
        kind: [item for item in nodes if isinstance(item, dict) and item.get("kind") == kind]
        for kind in kinds
    }
    rows = max((len(items) for items in grouped.values()), default=1)
    width, height = 1090, max(190, 70 + rows * (node_height + row_gap))
    image = Image.new("RGB", (width * scale, height * scale), "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(10 * scale, bold=True)
    node_font = _font(11 * scale, bold=True)
    meta_font = _font(9 * scale)
    positions: dict[str, tuple[int, int, dict[str, Any]]] = {}

    def point(value: int) -> int:
        return int(value * scale)

    for kind in kinds:
        draw.text(
            (point(columns[kind]), point(18)),
            labels[kind],
            fill="#767d82",
            font=title_font,
        )
        for index, node in enumerate(grouped[kind]):
            node_id = str(node.get("id") or "")
            positions[node_id] = (columns[kind], 45 + index * (node_height + row_gap), node)

    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, dict):
            continue
        source = positions.get(str(edge.get("source") or ""))
        target = positions.get(str(edge.get("target") or ""))
        if source is None or target is None:
            continue
        x1, y1 = source[0] + node_width, source[1] + node_height // 2
        x2, y2 = target[0], target[1] + node_height // 2
        relation = str(edge.get("relation") or "")
        color = "#b4241b" if relation == "contradicts" else "#438059" if relation == "supports" else "#aab0b4"
        dash = point(5) if relation == "contradicts" else 0
        if dash:
            _dashed_line(draw, (point(x1), point(y1)), (point(x2), point(y2)), color, point(2), dash)
        else:
            draw.line((point(x1), point(y1), point(x2), point(y2)), fill=color, width=point(2))
        angle = math.atan2(y2 - y1, x2 - x1)
        size = 7
        arrow = [
            (point(x2), point(y2)),
            (point(x2 - size * math.cos(angle - 0.55)), point(y2 - size * math.sin(angle - 0.55))),
            (point(x2 - size * math.cos(angle + 0.55)), point(y2 - size * math.sin(angle + 0.55))),
        ]
        draw.polygon(arrow, fill=color)

    root_id = str(payload.get("root_node_id") or "") if isinstance(payload, dict) else ""
    fills = {"evidence": "#f8f9f9", "component": "#f4f0e8", "hypothesis": "#f8eeee", "outcome": "#f3e5e3"}
    strokes = {"evidence": "#cdd1d4", "component": "#b9ab8b", "hypothesis": "#c89490", "outcome": "#b4241b"}
    for node_id, (x, y, node) in positions.items():
        kind = str(node.get("kind") or "evidence")
        root = node_id == root_id
        draw.rounded_rectangle(
            (point(x), point(y), point(x + node_width), point(y + node_height)),
            radius=point(3),
            fill=fills.get(kind, "#ffffff"),
            outline="#b4241b" if root else strokes.get(kind, "#cdd1d4"),
            width=point(2 if root or kind == "outcome" else 1),
        )
        label = str(node.get("label") or "")
        lines = textwrap.wrap(label, width=31)[:2] or [""]
        for line_index, line in enumerate(lines):
            draw.text(
                (point(x + 10), point(y + 9 + line_index * 15)),
                line,
                fill="#202124",
                font=node_font,
            )
        meta = str(node.get("source_agent") or node.get("reason") or node.get("component") or "")
        draw.text(
            (point(x + 10), point(y + 49)),
            meta[:36],
            fill="#687076",
            font=meta_font,
        )

    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size=size)
    except OSError:
        return ImageFont.load_default()


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    color: str,
    width: int,
    dash: int,
) -> None:
    x1, y1 = start
    x2, y2 = end
    distance = math.hypot(x2 - x1, y2 - y1)
    if distance == 0:
        return
    ux, uy = (x2 - x1) / distance, (y2 - y1) / distance
    cursor = 0.0
    while cursor < distance:
        stop = min(cursor + dash, distance)
        draw.line(
            (x1 + ux * cursor, y1 + uy * cursor, x1 + ux * stop, y1 + uy * stop),
            fill=color,
            width=width,
        )
        cursor += dash * 2
