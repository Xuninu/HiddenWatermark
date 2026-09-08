#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiddenWatermark 版本发布脚本
用法: python release.py
功能:
  1. 检查工作区状态
  2. 自动/手动设定新版本号
  3. 输入更新说明
  4. 更新 CHANGELOG.md
  5. git commit + push + tag
  6. 自动创建 GitHub Release（需配置 GH_TOKEN 环境变量，或已安装 gh CLI）
"""
import os
import sys
import subprocess
import json
import urllib.request
import urllib.error
from datetime import datetime

REPO_OWNER = "Xuninu"
REPO_NAME = "HiddenWatermark"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
VERSION_FILE = "version.txt"
CHANGELOG_FILE = "CHANGELOG.md"


def run(cmd, check=True, capture=True):
    """运行 shell 命令"""
    result = subprocess.run(
        cmd, shell=True, capture_output=capture, text=True, encoding="utf-8"
    )
    if check and result.returncode != 0:
        print(f"[错误] 命令失败: {cmd}")
        print(result.stderr or result.stdout)
        sys.exit(1)
    return result


def get_current_version():
    """读取当前版本号"""
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    # 从 git tag 获取最新版本
    result = run("git tag --sort=-v:refname", check=False)
    tags = [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
    if tags:
        return tags[0].lstrip("v")
    return "0.1.0"


def bump_version(current, part="patch"):
    """递增版本号"""
    try:
        major, minor, patch = map(int, current.split("."))
    except ValueError:
        major, minor, patch = 0, 1, 0
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def update_changelog(version, notes):
    """更新 CHANGELOG.md"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    entry = f"## v{version} ({date_str})\n\n{notes}\n\n"

    if os.path.exists(CHANGELOG_FILE):
        with open(CHANGELOG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        # 插入到标题之后
        if content.startswith("# Changelog"):
            lines = content.split("\n", 2)
            if len(lines) > 2:
                content = lines[0] + "\n\n" + entry + lines[2]
            else:
                content = lines[0] + "\n\n" + entry
        else:
            content = "# Changelog\n\n" + entry + content
    else:
        content = "# Changelog\n\n" + entry

    with open(CHANGELOG_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] 已更新 {CHANGELOG_FILE}")


def create_github_release(tag, notes):
    """通过 GitHub API 创建 Release"""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    # 优先使用 gh CLI
    gh_available = run("gh --version", check=False, capture=True).returncode == 0
    if gh_available:
        auth_check = run("gh auth status", check=False, capture=True)
        if auth_check.returncode == 0:
            print("[INFO] 使用 gh CLI 创建 Release...")
            # 写入临时 notes 文件
            notes_file = ".release_notes.md"
            with open(notes_file, "w", encoding="utf-8") as f:
                f.write(notes)
            result = run(
                f'gh release create {tag} --title "v{tag.lstrip("v")}" '
                f'--notes-file {notes_file} --repo {REPO_OWNER}/{REPO_NAME}',
                check=False,
            )
            os.remove(notes_file)
            if result.returncode == 0:
                print(f"[OK] GitHub Release 已创建: {REPO_URL}/releases/tag/{tag}")
                return True
            else:
                print(f"[警告] gh CLI 创建失败: {result.stderr}")

    # 使用 GitHub API
    if token:
        print("[INFO] 使用 GitHub API 创建 Release...")
        api_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases"
        payload = json.dumps({
            "tag_name": tag,
            "name": f"v{tag.lstrip('v')}",
            "body": notes,
            "draft": False,
            "prerelease": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            api_url, data=payload, method="POST",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                print(f"[OK] GitHub Release 已创建: {data.get('html_url')}")
                return True
        except urllib.error.HTTPError as e:
            print(f"[错误] GitHub API 请求失败: {e.code} {e.reason}")
            print(e.read().decode("utf-8", errors="replace"))
            return False
    else:
        print("[提示] 未配置 GH_TOKEN 且 gh CLI 未认证，跳过自动创建 Release")
        print(f"       你可以手动创建: {REPO_URL}/releases/new?tag={tag}")
        print(f"       Release 标题: v{tag.lstrip('v')}")
        print(f"       更新说明已复制到剪贴板（见上方）")
        return False


def main():
    print("=" * 60)
    print("  HiddenWatermark 版本发布工具")
    print("=" * 60)

    # 1. 检查 git 状态
    print("\n[1/6] 检查工作区状态...")
    status = run("git status --porcelain", check=False)
    if status.stdout.strip():
        print("  工作区有未提交的改动:")
        for line in status.stdout.strip().split("\n"):
            print(f"    {line}")
        print("  这些改动将一起提交。")
    else:
        print("  工作区干净，将仅更新版本号和 changelog。")

    # 2. 确定版本号
    current = get_current_version()
    print(f"\n[2/6] 当前版本: v{current}")
    print("  版本号类型:")
    print("    1) patch  修订版 (x.x.+1)  - 小修复、小优化")
    print("    2) minor  次版本  (x.+1.0)  - 新功能")
    print("    3) major  主版本  (+1.0.0)  - 重大变更/不兼容")
    print("    4) 手动输入版本号")
    choice = input("  请选择 [1]: ").strip() or "1"

    if choice == "2":
        new_version = bump_version(current, "minor")
    elif choice == "3":
        new_version = bump_version(current, "major")
    elif choice == "4":
        new_version = input("  请输入新版本号 (如 1.2.3): ").strip().lstrip("v")
        if not new_version:
            print("[错误] 版本号不能为空")
            sys.exit(1)
    else:
        new_version = bump_version(current, "patch")

    tag = f"v{new_version}"
    print(f"  新版本: {tag}")

    # 3. 输入更新说明
    print(f"\n[3/6] 输入更新说明 (输入空行结束):")
    print("  格式建议:")
    print("    ### 新增")
    print("    - xxx")
    print("    ### 修复")
    print("    - xxx")
    print("    ### 优化")
    print("    - xxx")
    print()
    notes_lines = []
    while True:
        line = input()
        if line == "":
            break
        notes_lines.append(line)
    notes = "\n".join(notes_lines).strip()
    if not notes:
        notes = "- 常规更新"

    # 4. 更新文件
    print(f"\n[4/6] 更新版本文件和 changelog...")
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(new_version + "\n")
    update_changelog(new_version, notes)

    # 5. Git 提交和推送
    print(f"\n[5/6] Git 提交和推送...")
    run(f'git add {VERSION_FILE} {CHANGELOG_FILE}')
    # 如果有其他改动也一起提交
    run("git add -A")
    run(f'git commit -m "release: {tag}"')
    run("git push origin main")
    run(f"git tag {tag}")
    run(f"git push origin {tag}")
    print(f"[OK] 已推送代码和标签 {tag}")

    # 6. 创建 GitHub Release
    print(f"\n[6/6] 创建 GitHub Release...")
    create_github_release(tag, notes)

    print("\n" + "=" * 60)
    print(f"  发布完成! {tag}")
    print(f"  Release 页面: {REPO_URL}/releases/tag/{tag}")
    print("=" * 60)


if __name__ == "__main__":
    main()
