"""命令行入口：
  python -m seeglow <视频链接>            总结单个视频
  python -m seeglow --web                 启动网页版
"""

import argparse
import sys

BANNER = r"""
  拾光 SeeGlow —— B站长视频一键总结
  ===================================
"""

STAGE_LABELS = {
    "queue": "排队中",
    "parse": "解析链接",
    "info": "获取信息",
    "subtitle": "获取字幕",
    "download": "下载音频",
    "omni": "AI 听音频",
    "transcribe": "本地转写",
    "summarize": "AI 总结",
    "save": "保存",
    "done": "完成",
}


def run_cli(url: str, args) -> int:
    from . import pipeline
    from .config import load_config

    options = {
        "page": args.p,
        "all_pages": bool(args.all_pages),
    }
    if args.output:
        from .config import save_config

        save_config({"output_dir": args.output})

    last_stage = None

    def progress(d):
        nonlocal last_stage
        stage = STAGE_LABELS.get(d["stage"], d["stage"])
        line = f"\r[{stage}] {d['percent']:5.1f}%  {d['message']}"
        sys.stdout.write(line.ljust(60))
        sys.stdout.flush()
        if stage != last_stage and d["stage"] in ("done",):
            sys.stdout.write("\n")
        last_stage = stage

    try:
        result = pipeline.run_pipeline(url, options, progress)
    except Exception as e:
        sys.stdout.write("\n")
        print(f"[失败] {e}", file=sys.stderr)
        return 1

    sys.stdout.write("\n\n")
    print(f"标题：{result['title']}")
    print(f"UP主：{result['author']} · 时长 {result['duration']} · 转写方式：{result['source']}")
    print(f"已保存：{result['output_file']}\n")
    print(result["summary_md"])
    return 0


def interactive(args):
    """双击运行模式：循环读取链接，逐个总结。"""
    print("交互模式：粘贴B站视频链接后回车开始总结；输入 q 或直接回车退出。\n")
    while True:
        try:
            url = input("视频链接> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not url or url.lower() in ("q", "exit", "quit"):
            break
        run_cli(url, args)
        print("\n再粘贴下一个链接继续，或输入 q 退出。\n")

    try:
        input("按回车关闭窗口…")
    except (EOFError, KeyboardInterrupt):
        pass


def main():
    parser = argparse.ArgumentParser(
        prog="seeglow",
        description="拾光 SeeGlow —— B站长视频一键总结工具",
    )
    parser.add_argument("url", nargs="?", help="B站视频链接 / BV号 / av号 / b23.tv 短链")
    parser.add_argument("--web", action="store_true", help="启动网页版界面")
    parser.add_argument("--host", default="127.0.0.1", help="Web 监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="Web 端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--p", type=int, default=None, help="多P视频：指定总结第几个分P")
    parser.add_argument("--all-pages", action="store_true", help="多P视频：总结全部分P并合并为一份笔记")
    parser.add_argument("--output", default=None, help="总结 Markdown 输出目录")
    args = parser.parse_args()

    print(BANNER)

    if args.web:
        import threading
        import webbrowser

        open_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        url = f"http://{open_host}:{args.port}"
        print(f"网页版启动中：{url} （Ctrl+C 退出）\n")
        if not args.no_browser:
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        from .web import main as web_main

        web_main(args.host, args.port)
        return

    if not args.url:
        # 交互模式：双击 EXE / 直接运行时进入
        interactive(args)
        return

    sys.exit(run_cli(args.url, args))


if __name__ == "__main__":
    main()
