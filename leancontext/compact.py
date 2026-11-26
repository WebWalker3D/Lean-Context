"""Deterministic, extractive result views with explicit omissions and raw recall."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .store import Store

ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
DIAGNOSTIC = re.compile(
    r'(?i)(\b(?:error|failed|failure|fatal|exception|traceback|panic|warning|warn|mismatch|'
    r'assertionerror|typeerror|valueerror|syntaxerror|keyerror|segmentation|timeout|timed out)\b'
    r'|^\s*E\s+|^\s*FAIL(?:\s|:)|^\s*error\[|^\s*at\s+.*:\d+)')
SUMMARY = re.compile(r'(?i)(\b\d+\s+(?:passed|failed|errors?|warnings?|tests?|skipped)\b|'
                     r'\b(?:test result:|tests run:|total:|build succeeded|build failed)\b)')
SEVERE = re.compile(r'(?i)(\b(?:error|failed|failure|fatal|exception|traceback|panic|mismatch|'
                    r'assertionerror|typeerror|valueerror|syntaxerror|keyerror|segmentation|timeout)\b'
                    r'|^\s*E\s+|^\s*FAIL(?:\s|:))')


@dataclass(frozen=True)
class View:
    text: str
    ref: str | None
    original_chars: int
    emitted_chars: int
    omitted_lines: int
    changed: bool
    strategy: str = 'generic'
    diagnostic_ref: str | None = None
    diagnostic_count: int = 0
    group_count: int = 0
    inventory_complete: bool | None = None
    budget_exceeded: bool = False


def _short_line(line: str, width: int) -> str:
    if len(line) <= width:
        return line
    spare = max(0, width - 30)
    left = spare * 2 // 3
    return line[:left] + ' ... [line excerpt] ... ' + line[-(spare-left):]


def compact(text: str, *, store: Store, max_chars: int = 6000,
            kind: str = 'output', metadata: dict | None = None,
            strategy: str = 'auto') -> View:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError('max_chars must be an integer of at least 512')
    from .diagnostics import STRATEGIES, analyze, archive_report, render_report, source_command
    if strategy not in STRATEGIES:
        raise ValueError('strategy must be one of: ' + ', '.join(STRATEGIES))
    # Worker prose is not an authoritative tool diagnostic, even if it quotes
    # pytest/compiler output. Keep that independent output budget hard.
    selected = 'generic' if kind == 'worker-draft' else strategy
    if selected == 'source' or (selected == 'auto' and source_command(metadata)):
        return _source_view(text, store=store, max_chars=max_chars, kind=kind, metadata=metadata)
    report = analyze(text, strategy=selected, metadata=metadata)
    if report is not None and (report.diagnostics or len(text) > max_chars):
        parser = 'pytest' if report.strategy == 'pytest-failures' else 'compiler' if report.strategy == 'compiler' else 'auto'
        ref = store.save(text, kind=kind, metadata={**(metadata or {}), 'diagnostic_strategy': parser})
        diagnostic_ref = archive_report(store, report, raw_ref=ref)
        rendered = render_report(report, ref=diagnostic_ref, raw_ref=ref, max_chars=max_chars)
        result = rendered['text']
        return View(result, ref, len(text), len(result),
                    max(0, len(text.splitlines()) - len(result.splitlines())), True,
                    report.strategy, diagnostic_ref, rendered['diagnostic_count'], rendered['group_count'],
                    rendered['inventory_complete'], rendered['budget_exceeded'])
    return _generic_view(text, store=store, max_chars=max_chars, kind=kind, metadata=metadata)


def _source_view(text: str, *, store: Store, max_chars: int,
                 kind: str, metadata: dict | None) -> View:
    if len(text) <= max_chars:
        return View(text, None, len(text), len(text), 0, False, 'source')
    ref = store.save(text, kind=kind, metadata=metadata)
    lines = text.splitlines(keepends=True)
    header = '[LeanContext source/search excerpt; exact captured lines, in original order]\n'
    parts = [header]
    used = len(header)
    shown = 0
    for line in lines:
        if used + len(line) + 260 > max_chars:
            break
        parts.append(line)
        used += len(line)
        shown += 1
    parts.append(f'\n[Partial: {len(lines)-shown} captured lines omitted; '
                 f'next line requires {len(lines[shown])} chars.]\n'
                 f'Full text: {ref}. recall {ref} --start {shown+1} --lines 80 '
                 '(same project --root).\n')
    result = ''.join(parts)
    return View(result, ref, len(text), len(result), len(lines)-shown, True, 'source')


def _generic_view(text: str, *, store: Store, max_chars: int,
                  kind: str, metadata: dict | None) -> View:
    if len(text) <= max_chars:
        return View(text, None, len(text), len(text), 0, False)
    # Saving succeeds before we suppress any input. Callers can fail open on I/O errors.
    ref = store.save(text, kind=kind, metadata=metadata)
    lines = ANSI.sub('', text).splitlines()
    if not lines:
        lines = ['']
    footer = (f'\nFull text: {ref}. Use the context tool to recall, or append '
              f'"recall {ref} --start 1 --lines 80" to your configured launcher '
              '(same project --root).\n')
    header = f'[LeanContext excerpt: {len(text)} chars, {len(lines)} lines; original line numbers]\n'
    available = max_chars - len(header) - len(footer) - 95
    width = max(70, min(900, available // 3))
    # Only materialize lines that fit the view. An eager rendered list and
    # priority list scale with the whole log rather than the response budget.
    selected: dict[int, str] = {}
    diagnostics = [i for i, s in enumerate(lines) if DIAGNOSTIC.search(s)]
    summaries = [i for i, s in enumerate(lines) if SUMMARY.search(s)]
    severe = [i for i in diagnostics if SEVERE.search(lines[i])]

    def priorities():
        # Keep the exact early/late error ordering and fallback order.
        for left in range((len(severe)+1)//2):
            yield severe[left]
            yield severe[-1-left]
        yield from reversed(summaries)
        for distance in (1, 2, 3):
            for i in severe:
                yield i + distance
                yield i - distance
        yield from diagnostics
        yield from range(max(0, len(lines)-8), len(lines))
        yield from range(min(8, len(lines)))
        yield from range(min(50, len(lines)))

    def render(i):
        return f'L{i+1}: {_short_line(lines[i], width)}\n'

    spent = 0
    # Even an empty first line costs len("L1: \n") + 32 characters.
    minimum_cost = len('L1: \n') + 32
    for i in priorities():
        if available - spent < minimum_cost:
            break
        if i < 0 or i >= len(lines) or i in selected:
            continue
        line = render(i)
        cost = len(line) + 32  # conservative space for a gap marker
        if spent + cost <= available:
            selected[i] = line
            spent += cost
    if not selected:
        i = diagnostics[0] if diagnostics else 0
        selected[i] = render(i)
    parts = [header]
    previous = -1
    for i in sorted(selected):
        if i > previous + 1:
            parts.append(f'... {i-previous-1} lines omitted ...\n')
        parts.append(selected[i])
        previous = i
    if previous < len(lines)-1:
        parts.append(f'... {len(lines)-previous-1} lines omitted ...\n')
    omitted = len(lines) - len(selected)
    hidden_diag = sum(i not in selected for i in diagnostics)
    if hidden_diag:
        parts.append(f'NOTICE: {hidden_diag} diagnostic lines omitted; search/recall the artifact.\n')
    result = ''.join(parts) + footer
    if len(result) > max_chars:
        # Very small budgets and a giant line: make any hard cut explicit.
        marker = '\n[excerpt budget reached; omitted text is in artifact]\n'
        result = result[:max_chars-len(marker)-len(footer)] + marker + footer
    if len(result) >= len(text):
        return View(text, None, len(text), len(text), 0, False)
    return View(result, ref, len(text), len(result), omitted, True)
