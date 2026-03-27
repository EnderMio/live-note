from __future__ import annotations


def wrap_action_rows(
    available_width: int, item_widths: list[int], *, gap: int
) -> list[tuple[int, int]]:
    if not item_widths:
        return []
    width_limit = max(available_width, max(item_widths))
    used_width = 0
    row = 0
    column = 0
    layout: list[tuple[int, int]] = []
    for item_width in item_widths:
        proposed_width = item_width if column == 0 else used_width + gap + item_width
        if column > 0 and proposed_width > width_limit:
            row += 1
            column = 0
            used_width = item_width
        else:
            used_width = proposed_width
        layout.append((row, column))
        column += 1
    return layout
