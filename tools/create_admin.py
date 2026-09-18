"""在服务器上创建第一个管理员账号。

服务对外开放后，网页初始化只接受本机来源；部署时在服务器上运行本工具：

    .venv/bin/python tools/create_admin.py

与服务使用同一数据目录（遵循 ROUTE_TEST_DATA_DIR）。已有账号时拒绝执行，
后续账号请登录后在“账号管理”里添加。
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import route_test_ui as ui  # noqa: E402


def main():
    if not ui._auth_setup_required():
        print("已存在账号，无需初始化。请用管理员登录后在账号管理中添加成员。")
        return 1
    print(f"数据目录：{ui.BASE_DIR}")
    username = input("管理员账号（3–40 个字符）：").strip()
    password = getpass.getpass("管理员密码（至少 8 个字符）：")
    if password != getpass.getpass("再次输入密码："):
        print("两次输入的密码不一致。")
        return 1
    try:
        account = ui.create_initial_admin(username, password)
    except (ValueError, PermissionError) as exc:
        print(f"创建失败：{exc}")
        return 1
    ui.AUDIT.record("auth.setup", actor="cli", target=account["username"], detail="命令行初始化管理员")
    print(f"已创建管理员 {account['username']}，可以在浏览器登录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
