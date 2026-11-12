"""Portable PNG export for V3 causal graphs."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from aware_models.executor import CausalGraph


def export_causal_graph_png(
    graph: CausalGraph | dict[str, Any],
    output_path: str | Path,
    *,
    scale: int = 3,
    timezone_offset_minutes: int = 0,
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
        "evidence": "Evidence",
        "component": "Components",
        "hypothesis": "Hypotheses",
        "outcome": "Selected Root Cause",
    }
    columns = {"evidence": 30, "component": 300, "hypothesis": 570, "outcome": 840}
    node_width, node_height, row_gap = 220, 108, 24
    grouped = {
        kind: [item for item in nodes if isinstance(item, dict) and item.get("kind") == kind]
        for kind in kinds
    }
    linked_ids = {
        str(edge.get(endpoint) or "")
        for edge in edges if isinstance(edge, dict)
        for endpoint in ("source", "target")
    }
    other_evidence = [node for node in grouped["evidence"] if str(node.get("id") or "") not in linked_ids]
    grouped["evidence"] = [node for node in grouped["evidence"] if str(node.get("id") or "") in linked_ids]
    outcome = grouped["outcome"][0] if grouped["outcome"] else None
    summary_height = 108 if outcome else 0
    node_top = summary_height + 45
    rows = max(1, *(len(items) for items in grouped.values()))
    other_start = node_top + rows * (node_height + row_gap) + 35
    other_card_top = other_start + 48
    width, height = 1090, max(
        190 + summary_height,
        other_card_top + math.ceil(len(other_evidence) / 4) * (node_height + row_gap) + 18
        if other_evidence else node_top + rows * (node_height + row_gap) + 10,
    )
    # Keep large batches within a practical raster size. The UI also offers SVG.
    scale = min(scale, max(1, int(math.sqrt(30_000_000 / (width * height)))))
    image = Image.new("RGB", (width * scale, height * scale), "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(10 * scale, bold=True)
    node_font = _font(12 * scale, bold=True)
    meta_font = _font(10 * scale)
    positions: dict[str, tuple[int, int, dict[str, Any]]] = {}

    def point(value: int) -> int:
        return int(value * scale)

    if outcome:
        component = str(outcome.get("component") or "Component not determined")
        reason = str(outcome.get("reason") or "Cause not determined")
        confidence = _format_confidence(outcome.get("confidence"))
        occurrence = _format_timestamp(outcome.get("timestamp"), timezone_offset_minutes)
        draw.rounded_rectangle(
            (point(18), point(10), point(width - 18), point(94)),
            radius=point(5), fill="#fdf7f6", outline="#c89490", width=point(1),
        )
        draw.text((point(30), point(20)), "Selected Root Cause", fill="#9c3933", font=title_font)
        draw.text((point(30), point(44)), _fit_lines(draw, component, node_font, point(255), 1)[0], fill="#202124", font=node_font)
        draw.text((point(310), point(44)), _fit_lines(draw, reason, node_font, point(410), 1)[0], fill="#202124", font=node_font)
        draw.text((point(765), point(43)), occurrence, fill="#202124", font=meta_font)
        draw.text((point(765), point(64)), confidence, fill="#687076", font=meta_font)

    for kind in kinds:
        draw.text(
            (point(columns[kind]), point(summary_height + 18)),
            f"{labels[kind]} · {len(grouped[kind])}",
            fill="#767d82",
            font=title_font,
        )
        for index, node in enumerate(grouped[kind]):
            node_id = str(node.get("id") or "")
            positions[node_id] = (columns[kind], node_top + index * (node_height + row_gap), node)

    if other_evidence:
        draw.rounded_rectangle(
            (point(18), point(other_start), point(width - 18), point(height - 12)),
            radius=point(5), fill="#f5f7f7", outline="#cbd1d4", width=point(1),
        )
        draw.text((point(30), point(other_start + 10)),
                  f"Other findings · {len(other_evidence)}", fill="#49565c", font=title_font)
        draw.text((point(30), point(other_start + 27)),
                  "No graph relation shown for these observations.", fill="#687076", font=meta_font)
        for index, node in enumerate(other_evidence):
            node_id = str(node.get("id") or "")
            positions[node_id] = (
                columns[kinds[index % 4]],
                other_card_top + (index // 4) * (node_height + row_gap),
                node,
            )

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
        label = (
            " · ".join(str(node.get(key) or "") for key in ("component", "reason") if node.get(key))
            if kind == "outcome" else str(node.get("label") or "")
        ) or str(node.get("label") or "")
        lines = _fit_lines(draw, label, node_font, point(node_width - 20), 2 if kind == "outcome" else 3)
        for line_index, line in enumerate(lines):
            draw.text(
                (point(x + 10), point(y + 10 + line_index * 18)),
                line,
                fill="#202124",
                font=node_font,
            )
        if kind == "outcome":
            draw.text((point(x + 10), point(y + 72)), _format_confidence(node.get("confidence")), fill="#687076", font=meta_font)
            draw.text((point(x + 10), point(y + 89)), _format_timestamp(node.get("timestamp"), timezone_offset_minutes), fill="#687076", font=meta_font)
        else:
            meta = str(node.get("source_agent") or node.get("reason") or node.get("component") or "")
            draw.text(
                (point(x + 10), point(y + 87)),
                _fit_lines(draw, meta, meta_font, point(node_width - 20), 1)[0],
                fill="#687076",
                font=meta_font,
            )

    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination


def _format_confidence(value: object) -> str:
    try:
        return f"{round(float(value) * 100)}% confidence"
    except (TypeError, ValueError):
        return "Confidence not determined"


def _format_timestamp(value: object, timezone_offset_minutes: int = 0) -> str:
    try:
        timestamp = float(value)
        if timestamp <= 0:
            return "Time not determined"
        offset = int(timezone_offset_minutes)
        local_time = datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(minutes=offset)
        zone = "UTC" if offset == 0 else f"UTC{'+' if offset > 0 else '-'}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}"
        return f"{local_time:%Y-%m-%d %H:%M:%S} {zone}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "Time not determined"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size=size)
    except OSError:
        return ImageFont.load_default()


def _fit_lines(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    remaining = " ".join(value.split())
    lines: list[str] = []
    while remaining and len(lines) < max_lines:
        end = 1
        while end < len(remaining) and draw.textlength(remaining[: end + 1], font=font) <= max_width:
            end += 1
        if end < len(remaining):
            space = remaining.rfind(" ", 0, end + 1)
            if space > 0:
                end = space
        lines.append(remaining[:end].strip())
        remaining = remaining[end:].strip()
    if remaining and lines:
        final = lines[-1]
        while final and draw.textlength(final + "…", font=font) > max_width:
            final = final[:-1]
        lines[-1] = final + "…"
    return lines or [""]


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
