"""Data-only parsing at the desktop execution boundary."""
import re

_TOKEN = re.compile(r'''\s*(?: (?P<string>"(?:[^"\\\r\n]|\\[^\r\n])*"|'(?:[^'\\\r\n]|\\[^\r\n])*') | (?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?) | (?P<word>[A-Za-z_][A-Za-z_0-9]*) | (?P<punct>[{}\[\],;=()]) )''', re.X)


def dispatch_literal_error(expression):
    """Accept one dispatcher with literal arguments, never executable Lua."""
    if not isinstance(expression, str) or len(expression) > 16000:
        return 'dispatcher must be text under 16000 characters'
    match = re.match(r'^\s*hl\.dsp(?:\.[A-Za-z_][A-Za-z_0-9]*)+\s*\(', expression)
    if not match:
        return 'expression must be a single hl.dsp.* call'
    tokens = []
    pos = match.end()
    while pos < len(expression) and expression[pos:].strip():
        token = _TOKEN.match(expression, pos)
        if not token:
            return 'dispatcher arguments must be literals, not Lua expressions or comments'
        tokens.append((token.lastgroup, token.group(token.lastgroup)))
        pos = token.end()
        if len(tokens) > 2048:
            return 'too many dispatcher argument tokens'
    index = 0
    def peek(offset=0):
        return tokens[index+offset][1] if index+offset < len(tokens) else None
    def take(expected):
        nonlocal index
        if peek() != expected:
            raise ValueError
        index += 1
    def value(depth=0):
        nonlocal index
        if depth > 16 or index >= len(tokens):
            raise ValueError
        kind, text = tokens[index]
        if kind in ('string', 'number') or text in ('true', 'false', 'nil'):
            index += 1
        elif text == '{':
            index += 1
            while peek() != '}':
                if index >= len(tokens):
                    raise ValueError
                if tokens[index][0] == 'word' and peek(1) == '=':
                    index += 2
                elif peek() == '[':
                    index += 1
                    value(depth+1)
                    take(']'); take('=')
                value(depth+1)
                if peek() in (',', ';'):
                    index += 1
                elif peek() != '}':
                    raise ValueError
            take('}')
        else:
            raise ValueError
    try:
        if peek() != ')':
            value()
            while peek() == ',':
                index += 1
                value()
        take(')')
        if peek() == ';':
            index += 1
        if index != len(tokens):
            raise ValueError
    except ValueError:
        return 'dispatcher arguments must contain only strings, numbers, booleans, nil, or literal tables'
    return None


# Known credential shapes and credentials configured in this process. This is
# defense in depth; arbitrary private page text is still private log content.
_CREDENTIAL = re.compile(r"(?i)\b(?:sk-(?:proj-|svcacct-)?[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{30,}|github_pat_[a-z0-9_]{40,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b|(?<=Bearer )[a-zA-Z0-9._~-]{16,}")

def sensitive_name(name):
    return any(word in name.upper() for word in ('API_KEY', 'TOKEN', 'PASSWORD', 'SECRET', 'AUTHORIZATION'))

def redact_text(text):
    import os
    for name, value in os.environ.items():
        if sensitive_name(name) and len(value) >= 8:
            text = text.replace(value, '[redacted]')
    return _CREDENTIAL.sub('[redacted]', text)

def child_env():
    import os
    return {name:value for name,value in os.environ.items()
            if not sensitive_name(name) and not _CREDENTIAL.fullmatch(value)}
