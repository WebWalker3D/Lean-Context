"""Deterministic diagnostic selection, grouping, and complete evidence rendering.

Character budgets are targets for these reports. Inventories and representative
tracebacks are never clipped to make a token-saving number look better.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import PurePath, PureWindowsPath
import re
import shlex

from .diagnostic_types import Diagnostic, DiagnosticReport
from .store import Store

STRATEGIES = ('auto', 'pytest', 'compiler', 'source', 'generic')


def validate_target(max_chars: int) -> None:
    if type(max_chars) is not int or not 512 <= max_chars <= 50_000_000:
        raise ValueError('max_chars must be an integer from 512 to 50000000')


def source_command(metadata: dict | None) -> bool:
    """Source-tool intent takes precedence over text that resembles an error."""
    metadata = metadata or {}
    argv = metadata.get('argv')
    if not argv and isinstance(metadata.get('command'), str):
        try:
            argv = shlex.split(metadata['command'])
        except ValueError:
            return False
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(arg, str) for arg in argv):
        return False
    executable = argv[0]
    # Windows paths can occur in archived metadata on another host. Recognize
    # executable suffixes without inferring intent from arbitrary shell scripts.
    if '\\' in executable or executable.lower().endswith('.exe'):
        name = PureWindowsPath(executable).name.lower().removesuffix('.exe')
    else:
        name = PurePath(executable).name
    if name in {'rg', 'grep', 'ag', 'ack', 'cat', 'sed', 'awk', 'head'}:
        return True
    if name == 'git':
        # Skip documented global options with separate values, not arbitrary
        # argument strings that happen to be named "show".
        index = 1
        while index < len(argv):
            arg = argv[index]
            if arg in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'):
                index += 2
            elif arg.startswith('-'):
                index += 1
            else:
                return arg in ('diff', 'show')
    return False


def analyze(text: str, *, strategy: str = 'auto', metadata: dict | None = None) -> DiagnosticReport | None:
    if strategy not in STRATEGIES:
        raise ValueError('strategy must be one of: ' + ', '.join(STRATEGIES))
    if strategy in ('generic', 'source') or (strategy == 'auto' and source_command(metadata)):
        return None
    report = None
    if strategy in ('auto', 'pytest'):
        from .pytest_parser import parse_pytest
        report = parse_pytest(text)
        meta = metadata or {}
        if (report is not None and report.strategy == 'test-success' and
                (meta.get('timed_out') or meta.get('output_limited') or meta.get('exit_code', 0) != 0)):
            report = None
    if report is None and strategy in ('auto', 'compiler'):
        from .compiler_parser import parse_compiler
        report = parse_compiler(text)
    if report is None and strategy == 'auto':
        # unittest's final status is more reliable than occurrences of words such
        # as "failed" inside test names or captured application logs.
        match = re.search(r'(?m)^Ran \d+ tests? in [^\n]+\n\s*\n(OK(?: \([^\n]*\))?)\s*\Z', text)
        if match and (metadata or {}).get('exit_code', 0) == 0:
            report = DiagnosticReport('test-success', match.group(0).strip(), (),
                                      inventory_complete=True, expected_count=0)
    if report is not None:
        meta = metadata or {}
        if report.strategy == 'test-success' and (meta.get('timed_out') or meta.get('output_limited') or meta.get('exit_code', 0) != 0):
            # A summary from a nested command cannot establish wrapper success.
            return None
        if meta.get('timed_out') or meta.get('output_limited'):
            report = replace(report, inventory_complete=False, notices=report.notices + (
                'INCOMPLETE capture: timeout or output cap. The index covers only captured evidence.',))
    return report


def group_diagnostics(items: tuple[Diagnostic, ...]) -> tuple[tuple[str, tuple[Diagnostic, ...]], ...]:
    grouped: dict[tuple, list[Diagnostic]] = {}
    for index, item in enumerate(items):
        key = (item.status, item.phase, item.message, item.signature) if item.signature else ('singleton', index)
        grouped.setdefault(key, []).append(item)
    return tuple((f'G{i}', tuple(members)) for i, members in enumerate(grouped.values(), 1))


def semantic_result(text: str, *, max_chars: int, **fields) -> dict:
    validate_target(max_chars)
    exceeded = len(text) > max_chars
    if exceeded:
        text = (f'NOTICE: required diagnostic evidence exceeds the {max_chars}-character target; '
                'the inventory and requested traces have not been clipped.\n' + text)
    return {'text': text, 'budget_policy': 'semantic-minimum', 'budget_exceeded': exceeded,
            'target_chars': max_chars, 'emitted_chars': len(text), **fields}


def render_report(report: DiagnosticReport, *, ref: str, max_chars: int = 6000,
                  raw_ref: str | None = None) -> dict:
    validate_target(max_chars)
    groups = group_diagnostics(report.diagnostics)
    index = []
    membership = {item.id: group for group, members in groups for item in members}
    if report.strategy == 'test-success':
        parts = ['[LeanContext test-success; progress and captured logs omitted]\n', report.summary.rstrip() + '\n']
    else:
        state = ('matches captured summary count' if report.inventory_complete is True else
                 'INCOMPLETE or unresolved' if report.inventory_complete is False else
                 'completeness unverified')
        parts = [f'[LeanContext {report.strategy}; {len(report.diagnostics)} indexed diagnostics; '
                 f'{len(groups)} signature groups; inventory {state}]\n']
        if report.summary:
            parts.append(report.summary.rstrip() + '\n')
        parts.append('\nFailure/error index (all identified records):\n')
        for item in report.diagnostics:
            group = membership[item.id]
            parts.append(f'{item.id} [{group}] {item.status} {item.label}' +
                         (f' ({item.phase})' if item.phase not in ('call', 'compile', 'unknown') else '') + '\n')
            index.append({'id': item.id, 'group': group, 'label': item.label,
                          'status': item.status, 'phase': item.phase})
        for group, members in groups:
            representative = members[0]
            parts.append(f'\n{group}: {len(members)} record(s); representative {representative.id}\n')
            if representative.traceback:
                parts.append(representative.traceback)
                if not representative.traceback.endswith('\n'):
                    parts.append('\n')
            else:
                parts.append('No traceback available for this record in the captured output.\n')
                if representative.message:
                    parts.append(representative.message.rstrip() + '\n')
        if any(len(members) > 1 for _, members in groups):
            parts.append('\nGroups match observed signatures; a shared root cause is not proven.\n')
        parts.append('Repeated traces, progress and captured logs omitted; full per-record details remain available.\n')
    for notice in report.notices:
        parts.append(f'NOTICE: {notice}\n')
    if report.diagnostics:
        example_ids = ' '.join(item.id for item in report.diagnostics[:2])
        parts.append(f'Artifact {ref}. Recall details in one call: recall-many {ref} {example_ids} '
                     '(or group IDs/exact identifiers; same project --root).\n')
        if raw_ref and raw_ref != ref:
            parts.append(f'Raw output: {raw_ref}; use line recall for omitted logs.\n')
    else:
        raw_ref = raw_ref or ref
        parts.append(f'Artifact {raw_ref}. Recall logs: recall {raw_ref} --start 1 --lines 80 '
                     '(same project --root).\n')
    return semantic_result(''.join(parts), max_chars=max_chars, ref=ref,
                           raw_ref=raw_ref or ref, strategy=report.strategy, diagnostic_count=len(report.diagnostics),
                           group_count=len(groups), inventory_complete=report.inventory_complete,
                           expected_count=report.expected_count, diagnostics=index,
                           groups=[{'id': group, 'representative': members[0].id,
                                    'members': [member.id for member in members]} for group, members in groups])


def archive_report(store: Store, report: DiagnosticReport, *, raw_ref: str) -> str:
    """Keep identities and completeness independent of mutable raw-use metadata."""
    payload = {'leancontext_diagnostics': 1, 'raw_ref': raw_ref, 'report': asdict(report)}
    return store.save(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                      kind='diagnostic-snapshot', metadata={'raw_ref': raw_ref})


def _restore_report(value: dict) -> DiagnosticReport:
    if not isinstance(value, dict) or not isinstance(value.get('diagnostics'), list):
        raise ValueError('Malformed diagnostic snapshot')
    try:
        records = tuple(Diagnostic(**item) for item in value['diagnostics'])
        report = DiagnosticReport(**{**value, 'diagnostics': records, 'notices': tuple(value.get('notices', ()))})
    except (TypeError, KeyError) as exc:
        raise ValueError('Malformed diagnostic snapshot') from exc
    if (report.strategy not in ('pytest-failures', 'test-success', 'compiler') or
            not isinstance(report.summary, str) or any(not isinstance(n, str) for n in report.notices) or
            (report.inventory_complete is not None and type(report.inventory_complete) is not bool) or
            (report.expected_count is not None and type(report.expected_count) is not int) or
            any(not all(isinstance(getattr(item, field), str) for field in
                        ('id', 'label', 'status', 'detail', 'traceback', 'message', 'phase')) or
                (item.signature is not None and not isinstance(item.signature, str)) for item in records) or
            len({item.id for item in records}) != len(records)):
        raise ValueError('Malformed diagnostic snapshot')
    return report


def _load_report(store: Store, ref: str, *, strategy: str = 'auto') -> tuple[DiagnosticReport, str]:
    if strategy not in STRATEGIES:
        raise ValueError('strategy must be one of: ' + ', '.join(STRATEGIES))
    raw = store.read(ref)
    if raw.startswith('{"leancontext_diagnostics":1,'):
        try:
            payload = json.loads(raw)
            raw_ref = payload['raw_ref']
            if not isinstance(raw_ref, str) or not re.fullmatch(r'[0-9a-f]{24}', raw_ref):
                raise ValueError('Malformed diagnostic snapshot reference')
            return _restore_report(payload['report']), raw_ref
        except (KeyError, TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('Malformed diagnostic snapshot') from exc
    metadata = store.info(ref).get('metadata', {})
    if strategy == 'auto' and metadata.get('diagnostic_strategy') in ('pytest', 'compiler'):
        strategy = metadata['diagnostic_strategy']
    report = analyze(raw, strategy=strategy, metadata=metadata)
    if report is None:
        raise ValueError('No supported diagnostic report found; use exact line recall for this artifact')
    return report, ref


def load_report(store: Store, ref: str, *, strategy: str = 'auto') -> DiagnosticReport:
    return _load_report(store, ref, strategy=strategy)[0]


def diagnose(store: Store, ref: str, *, max_chars: int = 6000, strategy: str = 'auto') -> dict:
    report, raw_ref = _load_report(store, ref, strategy=strategy)
    diagnostic_ref = archive_report(store, report, raw_ref=raw_ref)
    result = render_report(report, ref=diagnostic_ref, raw_ref=raw_ref, max_chars=max_chars)
    store.record({'operation': 'diagnostics', 'ref': ref, 'emitted_chars': len(result['text']),
                  'emitted_bytes': len(result['text'].encode('utf-8'))})
    return result
