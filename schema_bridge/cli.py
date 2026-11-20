"""命令行入口：``python -m schema_bridge.cli serve|seed``。"""

from __future__ import annotations

import argparse

from .seed import seed
from .web import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="schema-bridge", description="事件模式转换服务（v1 <-> v2）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--db", default="schema_bridge.sqlite3",
                         help="SQLite 数据库路径（默认 schema_bridge.sqlite3）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    p_seed = sub.add_parser("seed", help="写入演示数据")
    p_seed.add_argument("db", nargs="?", default="schema_bridge.sqlite3")

    args = parser.parse_args()
    if args.cmd == "serve":
        serve(args.db, args.host, args.port)
    elif args.cmd == "seed":
        svc = seed(args.db)
        svc.close()


if __name__ == "__main__":
    main()
