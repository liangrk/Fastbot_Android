"""Standalone HTML report renderer for the Fastbot extension tooling.

Produces self-contained UTF-8 HTML (inline CSS/SVG, no CDN, no external
libraries) with Chinese UI labels. Importable and unit-testable without
a device.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

_SVG_WIDTH = 600
_SVG_HEIGHT = 300
_SVG_PAD = 40

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 24px auto; max-width: 900px; color: #1f2937; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }}
h2 {{ font-size: 17px; margin-top: 24px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
th, td {{ border: 1px solid #d1d5db; padding: 6px 10px; text-align: left; }}
th {{ background: #f3f4f6; }}
svg {{ border: 1px solid #e5e7eb; background: #ffffff; }}
footer {{ margin-top: 32px; color: #6b7280; font-size: 12px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<main>
{sections}
</main>
<footer>生成时间：{generated_at}</footer>
</body>
</html>
"""


def _map_points(points: Sequence[Sequence[float]]) -> List[Tuple[float, float]]:
    """Map data points into SVG pixel coordinates (y is flipped)."""
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_span = (x_max - x_min) or 1.0
    y_span = (y_max - y_min) or 1.0
    inner_w = _SVG_WIDTH - 2 * _SVG_PAD
    inner_h = _SVG_HEIGHT - 2 * _SVG_PAD
    mapped: List[Tuple[float, float]] = []
    for point in points:
        px = _SVG_PAD + (float(point[0]) - x_min) / x_span * inner_w
        py = _SVG_HEIGHT - _SVG_PAD - (float(point[1]) - y_min) / y_span * inner_h
        mapped.append((round(px, 2), round(py, 2)))
    return mapped


def _render_summary(section: Dict[str, Any]) -> str:
    rows = section.get("rows", [])
    lines = [
        '<section class="summary">',
        "<h2>概览</h2>",
        "<table>",
        "<thead><tr><th>项目</th><th>值</th></tr></thead>",
        "<tbody>",
    ]
    for key, value in rows:
        lines.append(
            "<tr><td>%s</td><td>%s</td></tr>"
            % (html.escape(str(key)), html.escape(str(value)))
        )
    lines.append("</tbody></table></section>")
    return "\n".join(lines)


def _render_series(section: Dict[str, Any]) -> str:
    name = html.escape(str(section.get("name", "曲线")))
    points = section.get("points", [])
    lines = ['<section class="series">', "<h2>%s</h2>" % name]
    if points:
        mapped = _map_points(points)
        polyline_points = " ".join("%g,%g" % (px, py) for px, py in mapped)
        dots = "".join(
            '<circle cx="%g" cy="%g" r="3" fill="#2563eb" />' % (px, py)
            for px, py in mapped
        )
        x_axis = (
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" />'
            % (_SVG_PAD, _SVG_HEIGHT - _SVG_PAD,
               _SVG_WIDTH - _SVG_PAD, _SVG_HEIGHT - _SVG_PAD)
        )
        y_axis = (
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" />'
            % (_SVG_PAD, _SVG_PAD, _SVG_PAD, _SVG_HEIGHT - _SVG_PAD)
        )
        svg = (
            '<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
            'xmlns="http://www.w3.org/2000/svg">%s%s'
            '<polyline points="%s" fill="none" stroke="#2563eb" stroke-width="2" />%s</svg>'
            % (_SVG_WIDTH, _SVG_HEIGHT, _SVG_WIDTH, _SVG_HEIGHT,
               x_axis, y_axis, polyline_points, dots)
        )
        lines.append(svg)
    else:
        lines.append(
            '<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
            'xmlns="http://www.w3.org/2000/svg"></svg>'
            % (_SVG_WIDTH, _SVG_HEIGHT, _SVG_WIDTH, _SVG_HEIGHT)
        )
    lines.append("</section>")
    return "\n".join(lines)


def _render_html(section: Dict[str, Any]) -> str:
    return '<section class="html">\n%s\n</section>' % section.get("body", "")


_RENDERERS = {
    "summary": _render_summary,
    "series": _render_series,
    "html": _render_html,
}


def render_report(title: str, sections: Sequence[Dict[str, Any]]) -> str:
    """Render sections into a standalone HTML document string.

    section types:
      {"type": "summary", "rows": [[key, value], ...]}
      {"type": "series",  "name": str, "points": [(x, y), ...]}
      {"type": "html",    "body": str}
    """
    parts: List[str] = []
    for index, section in enumerate(sections):
        kind = section.get("type")
        renderer = _RENDERERS.get(kind)
        if renderer is None:
            raise ValueError(
                "unknown section type: %r (sections[%d])" % (kind, index)
            )
        parts.append(renderer(section))
    return _PAGE_TEMPLATE.format(
        title=html.escape(title),
        sections="\n".join(parts),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def write_report(path, content: str) -> Path:
    """Write report content to path (creating parent dirs); returns the Path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
