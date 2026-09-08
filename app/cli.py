# -*- coding: utf-8 -*-
"""命令行入口定义（main.py 调用）。"""
import argparse
import os

from . import algorithms
from .batch import batch_embed, batch_verify, summarize
from .config import load_profiles
from .engine import WatermarkEngine


def _find_profile(profiles: list, name: str):
    for p in profiles:
        if p.name == name:
            return p
    raise SystemExit(f"找不到名为 {name!r} 的预设水印，可用: {[p.name for p in profiles]}")


def _apply_overrides(profile, owner, note):
    if owner:
        profile.owner = owner
    if note is not None:
        profile.note = note
    return profile


def build_parser():
    parser = argparse.ArgumentParser(
        prog="main", description="HiddenWatermark 图片隐藏水印软件（方案二：文件级隐藏标记）")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("profiles", help="查看预设水印档位")

    p = sub.add_parser("embed", help="给单张图片加水印")
    p.add_argument("src")
    p.add_argument("out")
    p.add_argument("--profile", default="个人专属")
    p.add_argument("--owner", help="临时覆盖归属人")
    p.add_argument("--note", help="临时覆盖备注")

    p = sub.add_parser("verify", help="验证单张图片")
    p.add_argument("img")
    p.add_argument("--profile", help="核对的水印档位（验证频域水印时必填）")

    p = sub.add_parser("batch-embed", help="批量给一个文件夹加水印")
    p.add_argument("input_dir")
    p.add_argument("output_dir")
    p.add_argument("--profile", default="个人专属")
    p.add_argument("--owner")
    p.add_argument("--note")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    p.add_argument("--report", help="把报告写入指定文件")

    p = sub.add_parser("batch-verify", help="批量验证一个文件夹里的图片")
    p.add_argument("folder")
    p.add_argument("--profile", help="核对的水印档位（验证频域水印时必填）")
    p.add_argument("--report")

    return parser


def _print_report(results: list, action: str, report: str = None):
    ok, fail, total = summarize(results)
    print(f"\n===== 批量{action}报告（{total} 个文件）=====")
    for r in results:
        mark = "[OK]  " if r.ok else "[FAIL]"
        print(f"  {mark} {r.path}  {r.message}")
    print(f"完成 {ok}/{total}，失败 {fail}")
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"批量{action}报告\n")
            for r in results:
                f.write(f"{'OK' if r.ok else 'FAIL'}\t{r.path}\t{r.message}\n")
            f.write(f"完成 {ok}/{total}，失败 {fail}\n")
        print(f"报告已写入: {os.path.abspath(report)}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    engine = WatermarkEngine()

    if not args.cmd:
        build_parser().print_help()
        return

    if args.cmd == "profiles":
        for p in load_profiles():
            print(f"- {p.name:8s} [算法 {p.algorithm}] owner={p.owner!r} note={p.note!r}")
        print(f"可用算法: {algorithms.available_algorithms()}")
        return

    if args.cmd == "embed":
        profile = _apply_overrides(_find_profile(load_profiles(), args.profile), args.owner, args.note)
        detail = engine.embed(args.src, args.out, profile)
        if "appended_bytes" in detail:
            print(f"[OK] 已加水印 -> {args.out}（追加 {detail['appended_bytes']} 字节，字节级无损）")
        else:
            print(f"[OK] 已加水印 -> {args.out}（频域水印，{detail.get('blocks', '?')} 块，强度 {detail.get('strength', '?')}）")

    elif args.cmd == "verify":
        profile = _find_profile(load_profiles(), args.profile) if args.profile else None
        r = engine.verify(args.img, profile=profile)
        if r is None:
            print("[结果] 未检测到水印。")
        else:
            print(f"[结果] 检测到水印: {r}")

    elif args.cmd == "batch-embed":
        profile = _apply_overrides(_find_profile(load_profiles(), args.profile), args.owner, args.note)
        results = batch_embed(engine, args.input_dir, args.output_dir, profile, overwrite=args.overwrite)
        _print_report(results, "加水印", args.report)

    elif args.cmd == "batch-verify":
        profile = _find_profile(load_profiles(), args.profile) if args.profile else None
        results = batch_verify(engine, args.folder, profile=profile)
        _print_report(results, "验证", args.report)
