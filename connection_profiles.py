"""Named local connections; metadata is separate from OS-held credentials."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path


class ConnectionStorageError(RuntimeError):
    """An authored error safe to return without internal exception details."""


class ConnectionStore:
    def __init__(self, path, *, load_key, save_key, delete_key, available):
        self.path = Path(path)
        self.load_key = load_key
        self.save_key = save_key
        self.delete_key = delete_key
        self.available = available
        self.lock = threading.RLock()

    @staticmethod
    def reference(identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{24}", identifier):
            raise ValueError("常用连接编号无效")
        return "connection:" + identifier

    @staticmethod
    def validate_url(value):
        url = str(value or "").strip()
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed.port
        except ValueError:
            raise ValueError("请求地址或端口无效") from None
        if (len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError("请求地址须为有效的 HTTP（超文本传输协议）或 HTTPS（安全超文本传输协议）地址；密钥请填写在独立字段")
        return url

    def _read(self):
        try:
            with self.path.open(encoding="utf-8") as source:
                rows = json.load(source)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeError):
            raise ConnectionStorageError("无法读取常用连接文件，请检查本地文件权限或内容") from None
        if not isinstance(rows, list):
            raise ConnectionStorageError("常用连接文件格式无效，未覆盖现有内容")
        clean, ids = [], set()
        try:
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"id", "name", "url", "updated_at"}:
                    raise ValueError()
                self.reference(row["id"])
                if row["id"] in ids or not isinstance(row["name"], str) or not row["name"].strip():
                    raise ValueError()
                ids.add(row["id"])
                self.validate_url(row["url"])
                if (isinstance(row["updated_at"], bool) or not isinstance(row["updated_at"], (int, float))
                        or not math.isfinite(row["updated_at"])):
                    raise ValueError()
                clean.append(dict(row))
        except (TypeError, ValueError):
            raise ConnectionStorageError("常用连接文件格式无效，未覆盖现有内容") from None
        return clean

    def _write(self, rows):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                json.dump(rows, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            raise ConnectionStorageError("常用连接未保存：无法写入本地连接文件") from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _public(self, row):
        return {**row, "api_key_saved": bool(self.load_key(self.reference(row["id"])))}

    def list(self):
        with self.lock:
            return [self._public(row) for row in sorted(self._read(), key=lambda row: (-row["updated_at"], row["name"]))]

    def _find(self, rows, identifier):
        self.reference(identifier)
        row = next((row for row in rows if row["id"] == identifier), None)
        if row is None:
            raise ValueError("常用连接不存在，请重新选择或手动输入")
        return row

    def resolve(self, identifier, url):
        with self.lock:
            row = self._find(self._read(), identifier)
            if str(url or "").strip() != row["url"]:
                raise ValueError("请求地址与所选常用连接不一致，请重新选择或切换为手动输入")
            return self._require_key(identifier)

    def _require_key(self, identifier):
        key = self.load_key(self.reference(identifier))
        if not key:
            raise ValueError("所选常用连接的密钥无法读取，请重新保存密钥")
        return key

    def reveal(self, identifier):
        with self.lock:
            self._find(self._read(), identifier)
            return self._require_key(identifier)

    def save(self, *, name, url, api_key="", identifier=None):
        name = str(name or "").strip()
        url = self.validate_url(url)
        key = str(api_key or "").strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("常用连接名称须为 1 到 80 个字符，且不能含控制字符")
        if len(key) > 8192 or any(ord(char) < 32 for char in key):
            raise ValueError("接口密钥格式无效")
        if key and (key in url or key in name):
            raise ValueError("密钥只能填写在独立密钥字段中")
        with self.lock:
            rows = self._read()
            old = self._find(rows, identifier) if identifier else None
            if any(row["id"] != identifier and row["name"].casefold() == name.casefold() for row in rows):
                raise ValueError("常用连接名称已存在，请换个名称或编辑已有连接")
            identifier = identifier or uuid.uuid4().hex[:24]
            reference = self.reference(identifier)
            previous_key = self.load_key(reference) if old else ""
            if not key:
                if old and old["url"] == url and previous_key:
                    key = previous_key
                else:
                    raise ValueError("请填写接口密钥；更换地址时需要重新填写密钥")
            if key in url or key in name:
                raise ValueError("密钥只能填写在独立密钥字段中")
            if not self.available():
                raise ConnectionStorageError("常用连接未保存：操作系统凭据库不可用，请检查钥匙串或凭据库设置")
            if not self.save_key(reference, key):
                raise ConnectionStorageError("常用连接未保存：密钥写入操作系统凭据库失败")
            row = {"id": identifier, "name": name, "url": url, "updated_at": time.time()}
            try:
                self._write([item for item in rows if item["id"] != identifier] + [row])
            except ConnectionStorageError:
                if previous_key:
                    self.save_key(reference, previous_key)
                else:
                    self.delete_key(reference)
                raise
            return {**row, "api_key_saved": True}

    def delete(self, identifier):
        with self.lock:
            rows = self._read()
            self._find(rows, identifier)
            reference = self.reference(identifier)
            if not self.available():
                raise ConnectionStorageError("常用连接未删除：操作系统凭据库不可用")
            key = self.load_key(reference)
            deleted = self.delete_key(reference)
            if key and not deleted:
                raise ConnectionStorageError("常用连接未删除：操作系统凭据库中的密钥删除失败")
            try:
                self._write([row for row in rows if row["id"] != identifier])
            except ConnectionStorageError:
                if key:
                    self.save_key(reference, key)
                raise
