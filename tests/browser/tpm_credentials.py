"""In-memory synthetic credential boundary for isolated browser tests only."""
import hashlib
import threading

_values = {}
_lock = threading.Lock()


def account(url):
    return hashlib.sha256(str(url).encode()).hexdigest()[:32]


def available():
    return True


def load(url):
    with _lock:
        return _values.get(url, "")


def save(url, key):
    if not str(key).startswith("synthetic-"):
        raise ValueError("Browser fixture accepts synthetic credentials only")
    with _lock:
        _values[url] = key
    return True


def delete(url):
    with _lock:
        _values.pop(url, None)
    return True
