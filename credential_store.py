"""OS credential storage; no plaintext fallback and no secrets in subprocess argv."""
import hashlib
import os
import sys

SERVICE = 'route-load-workbench'


def account(url):
    return hashlib.sha256(str(url or '').strip().rstrip('/').lower().encode()).hexdigest()[:32]


def backend():
    if os.environ.get("ROUTE_TEST_DISABLE_CREDENTIAL_STORE") == "1":
        return None
    # Explicit, trusted OS backends. Never use an installed plaintext plugin.
    try:
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
        elif sys.platform == 'win32':
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        elif sys.platform.startswith('linux'):
            from keyring.backends.SecretService import Keyring
        else:
            return None
        result = Keyring()
        return result if result.priority > 0 else None
    except Exception:
        return None


def available():
    return backend() is not None


def load(url):
    if not url:
        return ''
    try:
        store = backend()
        return (store.get_password(SERVICE, account(url)) or '') if store else ''
    except Exception:
        return ''


def save(url, key):
    if not url or not str(key or '').strip():
        return False
    try:
        store = backend()
        if store is None:
            return False
        store.set_password(SERVICE, account(url), str(key).strip())
        return True
    except Exception:
        return False


def delete(url):
    if not url:
        return False
    try:
        store = backend()
        if store is None:
            return False
        store.delete_password(SERVICE, account(url))
        return True
    except Exception:
        return False
