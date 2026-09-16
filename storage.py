"""结果文件生命周期：校验值、容量统计、孤儿文件发现与清理。

本模块只处理磁盘上的结果产物，不依赖 Web 层、任务调度或认证逻辑，
因此可以在不启动服务的情况下单独做单元测试。

设计边界：
- 结果正文仍然保存在 JSONL / JSON 文件里，本模块不做格式转换。
- 删除任务时由调用方显式调用 `unlink_artifact`，本模块不猜测归属。
- `scan_orphans` 只做“文件存在但没有任务引用”的报告，默认不删除任何东西。
"""
import hashlib
import os
import time

# 结果文件命名前缀。与 route_test_ui.py 中 open_output / QualityTask.out_path 保持一致。
RUN_PREFIX = "run_"
QUALITY_PREFIX = "quality_run_"
ARTIFACT_PREFIXES = (RUN_PREFIX, QUALITY_PREFIX)
ARTIFACT_SUFFIXES = (".jsonl", ".json")

# 单次读取的块大小；校验大文件时不把整个文件读进内存。
CHUNK_SIZE = 1024 * 1024


def is_artifact_name(name):
    """判断文件名是否为本项目的结果产物。"""
    base = os.path.basename(str(name or ""))
    if not base.startswith(ARTIFACT_PREFIXES):
        return False
    return base.endswith(ARTIFACT_SUFFIXES)


def file_fingerprint(path, chunk_size=CHUNK_SIZE):
    """返回 (字节数, sha256 十六进制摘要)；文件不存在时返回 (None, None)。

    摘要用于事后核对“报告引用的结果文件”与磁盘上的文件是否为同一份，
    避免结果文件被截断或替换后仍被当作可信证据。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None, None
    return size, digest.hexdigest()


def unlink_artifact(path):
    """删除单个结果文件。返回 True 表示文件已不存在（含本来就不存在）。

    只删除本模块认定为本项目产物的文件；其余路径一律拒绝，避免误删。
    """
    if not path:
        return False
    if not is_artifact_name(path):
        return False
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def scan_orphans(base_dir, referenced_names):
    """列出结果目录里存在、但没有被任何任务引用的产物。

    `referenced_names` 是任务当前引用的文件名（或路径）集合。
    返回按修改时间倒序的列表，每项包含 name/path/size/mtime。
    本函数只报告，不删除。
    """
    referenced = {os.path.basename(str(x)) for x in (referenced_names or ()) if x}
    orphans = []
    try:
        entries = os.scandir(base_dir)
    except OSError:
        return orphans
    with entries:
        for entry in entries:
            try:
                if not entry.is_file() or not is_artifact_name(entry.name):
                    continue
                if entry.name in referenced:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            orphans.append({
                "name": entry.name,
                "path": entry.path,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
    orphans.sort(key=lambda item: item["mtime"], reverse=True)
    return orphans


def storage_summary(base_dir, referenced, orphan_scan_limit=50):
    """汇总结果目录容量，用于“容量告警”和清理入口。

    `referenced` 是 {文件名: 归属描述} 的映射，用于区分“已被任务引用”与“孤儿文件”。
    """
    referenced = referenced or {}
    total_bytes = 0
    artifact_count = 0
    referenced_bytes = 0
    largest = None
    try:
        entries = os.scandir(base_dir)
    except OSError:
        entries = None
    if entries is not None:
        with entries:
            for entry in entries:
                try:
                    if not entry.is_file() or not is_artifact_name(entry.name):
                        continue
                    size = entry.stat().st_size
                except OSError:
                    continue
                artifact_count += 1
                total_bytes += size
                if entry.name in referenced:
                    referenced_bytes += size
                if largest is None or size > largest["size"]:
                    largest = {"name": entry.name, "size": size}
    orphans = scan_orphans(base_dir, referenced.keys())
    orphan_bytes = sum(item["size"] for item in orphans)
    return {
        "artifact_count": artifact_count,
        "total_bytes": total_bytes,
        "referenced_bytes": referenced_bytes,
        # 没有被任何任务引用的产物所占空间；删除任务后残留的文件会统计到这里。
        "orphan_bytes": orphan_bytes,
        "orphan_count": len(orphans),
        "largest": largest,
        "orphans": orphans[: max(0, int(orphan_scan_limit))],
        "warn": total_bytes >= 512 * 1024 * 1024,
        "updated_at": time.time(),
    }


def cleanup_orphans(base_dir, referenced, older_than_seconds=0):
    """删除孤儿产物。默认只删 mtime 早于 `older_than_seconds` 的文件。

    调用方（HTTP 层）负责权限校验和二次确认；本函数只执行磁盘操作并返回结果。
    """
    cutoff = time.time() - max(0, float(older_than_seconds or 0))
    removed = []
    freed = 0
    skipped = []
    for item in scan_orphans(base_dir, referenced):
        if item["mtime"] > cutoff:
            skipped.append(item["name"])
            continue
        if unlink_artifact(item["path"]):
            removed.append(item["name"])
            freed += item["size"]
        else:
            skipped.append(item["name"])
    return {"removed": removed, "freed_bytes": freed, "skipped": skipped}
