#!/usr/bin/env python3
"""Codex PreToolUse guard. Restrictive argv prefixes; shell composition is denied, not guessed.
The CLI sandbox, scoped GitHub token and upstream MCP policies remain the security boundaries.
"""
import json
import os
from pathlib import Path
import shlex
import sys


def allowed(event, policy):
    name, args = event.get('tool_name'), event.get('tool_input')
    args = args if isinstance(args, dict) else {}
    if name == 'Bash':
        command = args.get('command', '')
        if not isinstance(command, str) or not command or any(c in command for c in ['\n', '\r', '`', '$(', '${', '\x00']):
            return False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
            lexer.whitespace_split = True
            argv = list(lexer)
        except ValueError:
            return False
        if not argv or any(token and all(c in ';&|<>()' for c in token) for token in argv):
            return False
        # Literal command names only: PATH overrides, wrappers, eval, and shell syntax do not
        # turn an allowed program into a second unchecked program.
        if any(c in argv[0] for c in ['/', '=', '$', '\\']):
            return False
        for prefix in policy.get('bash', []):
            words = shlex.split(prefix)
            if words and argv[:len(words)] == words:
                return True
        if argv[0] in ['cat', 'head', 'tail', 'wc', 'ls', 'pwd']:
            return True
        if argv[0] == 'sed':
            import re
            return len(argv) >= 4 and argv[1] == '-n' and bool(re.fullmatch(r'[0-9]+(?:,[0-9]+)?p', argv[2])) and all(not a.startswith('-') for a in argv[3:])
        if argv[0] == 'rg' and not any(a.startswith(('--pre', '--hostname-bin')) for a in argv[1:]):
            return True
        return False
    if name == 'apply_patch':
        return bool(policy.get('edit'))
    if name in ['update_plan', 'view_image']:
        return True
    if isinstance(name, str) and name.startswith('mcp__'):
        import fnmatch
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in policy.get('mcp', []))
    return False


def main():
    root = Path(sys.argv[1])
    try:
        event = json.load(sys.stdin)
        policy = json.loads((root / 'guard-policy.json').read_text())
        # A conservative cross-provider cap: Codex tool calls, rather than Claude turns.
        # Atomic lock makes concurrent hooks fail closed rather than lose budget increments.
        lock = root / 'guard.lock'
        lock.mkdir()
        try:
            counter = root / 'guard-count'
            count = int(counter.read_text()) if counter.exists() else 0
            count += 1
            counter.write_text(str(count))
            if count > policy['maxTools']:
                (root / 'budget-exhausted').touch()
                raise ValueError('budget')
        finally:
            lock.rmdir()
        if not allowed(event, policy):
            raise ValueError('tool')
    except Exception:
        print('Airlock: this tool call is outside the role policy or tool-call budget', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
