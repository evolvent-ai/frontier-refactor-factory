"""Bounded content scanning shared by source-tree and image-layer delivery checks."""
import hashlib
import io
import re

SECRET = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
                    rb"e2b_[A-Fa-f0-9]{32,}|"
                    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----[ \t]*"
                    rb"(?:\r?\n|\\n)[ \t]*(?:[A-Za-z0-9+/=]{32,}|Proc-Type:))")
SK_PREFIX = re.compile(rb'sk-')
SK_TOKEN = re.compile(rb'sk-[A-Za-z0-9_-]{24,}')
WORD_BYTES = frozenset(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_')
SSH_KEY_TYPE = b'sk-ecdsa-sha2-nistp256-cert-v01@openssh.com'
INTERNAL_MARKERS = (b'/data/evolvent', b'evolvent-ai', b'frontier-refactor-factory',
                    b'llmapi.evolventapi', b'e2b_api_key', b'e2b_dind_template', b'vscode-server')


def credential_matches(data):
    if any(prefix in data for prefix in (b'gh', b'e2b_', b'-----BEGIN')):
        for match in SECRET.finditer(data):
            yield match.span()
    for match in SK_PREFIX.finditer(data):
        start = match.start()
        if (start == 0 or data[start - 1] not in WORD_BYTES) and SK_TOKEN.match(data, start):
            if data.startswith(SSH_KEY_TYPE, start):
                continue
            yield SK_TOKEN.match(data, start).span()


class ContentScanner:
    def __init__(self, secret_values=(), *, track_matches=False):
        self.secrets = tuple(value.encode() if isinstance(value, str) else value
                             for value in secret_values if value)
        if any(len(value) > 16384 for value in self.secrets):
            raise ValueError('configured secret exceeds scanner overlap limit')
        self.overlap = max((1024, *(len(value) + 1 for value in self.secrets)))
        self.digest = hashlib.sha256()
        self.hits = set()
        self.tail = b''
        self.size = 0
        self.track_matches = track_matches
        self.matches = {}

    def _record(self, kind, start, end):
        self.hits.add(kind)
        if self.track_matches:
            key = (kind, start)
            self.matches[key] = max(end, self.matches.get(key, 0))
            if len(self.matches) > 10000:
                raise ValueError('content match count exceeds audit limit')

    def update(self, block):
        offset = self.size - len(self.tail)
        self.size += len(block)
        self.digest.update(block)
        data = self.tail + block
        if self.track_matches or 'internal-identity' not in self.hits:
            lower = data.lower()
            for marker in INTERNAL_MARKERS:
                start = lower.find(marker)
                while start >= 0:
                    self._record('internal-identity', offset + start, offset + start + len(marker))
                    if not self.track_matches:
                        break
                    start = lower.find(marker, start + 1)
        if self.track_matches or 'credential-like-text' not in self.hits:
            for start, end in credential_matches(data):
                self._record('credential-like-text', offset + start, offset + end)
                if not self.track_matches:
                    break
        if self.track_matches or 'configured-secret' not in self.hits:
            for value in self.secrets:
                start = data.find(value)
                while start >= 0:
                    self._record('configured-secret', offset + start, offset + start + len(value))
                    if not self.track_matches:
                        break
                    start = data.find(value, start + 1)
        self.tail = data[-self.overlap:]

    def result(self):
        result = {'hits': sorted(self.hits), 'sha256': self.digest.hexdigest(), 'bytes': self.size}
        if self.track_matches:
            result['matches'] = [{'kind': kind, 'start': start, 'end': end}
                                 for (kind, start), end in sorted(self.matches.items())]
        return result


def scan_stream(handle, *, secret_values=(), max_bytes=None):
    scanner = ContentScanner(secret_values)
    while block := handle.read(1024 * 1024):
        if max_bytes is not None and scanner.size + len(block) > max_bytes:
            raise ValueError('content scan byte limit exceeded')
        scanner.update(block)
    return scanner.result()


def scan_bytes(data, *, secret_values=()):
    return scan_stream(io.BytesIO(data), secret_values=secret_values)
