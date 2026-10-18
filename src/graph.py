"""DAG helpers: cycle detection for task dependencies.

An edge ``task -> blocker`` means "task is blocked by blocker".
"""
from __future__ import annotations

from typing import Iterable, Mapping

WHITE, GRAY, BLACK = 0, 1, 2


def find_cycle(adj: Mapping[str, Iterable[str]]) -> list[str] | None:
    """Return one cycle as a path ``[a, b, ..., a]`` or None.

    Iterative DFS with white/gray/black colouring (no recursion limit).
    """
    color: dict[str, int] = {}
    nodes = set(adj)
    for targets in adj.values():
        nodes.update(targets)

    for root in sorted(nodes):
        if color.get(root, WHITE) != WHITE:
            continue
        color[root] = GRAY
        path = [root]
        stack = [iter(sorted(adj.get(root, ())))]
        while stack:
            try:
                nxt = next(stack[-1])
            except StopIteration:
                color[path.pop()] = BLACK
                stack.pop()
                continue
            state = color.get(nxt, WHITE)
            if state == GRAY:
                return path[path.index(nxt):] + [nxt]
            if state == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append(iter(sorted(adj.get(nxt, ()))))
    return None


def cycle_after_setting(adj: Mapping[str, Iterable[str]], task_id: str,
                        blocked_by: Iterable[str]) -> list[str] | None:
    """Cycle that would exist if ``task_id`` were blocked by ``blocked_by``."""
    new_adj = {k: set(v) for k, v in adj.items()}
    new_adj[task_id] = set(blocked_by)
    return find_cycle(new_adj)
