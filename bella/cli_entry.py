"""
bella.cli_entry — the command-line entry point.

    bella                         -> text REPL
    bella chat                    -> text REPL
    bella voice                   -> wake-word / listen mode
    bella serve                   -> FastAPI server
    bella doctor                  -> diagnostics
    bella "play Do I Wanna Know"   -> run once and exit

    --verbose / -v                 exact tool names, args, timings, raw output

`python jarvis.py [...]` maps here too (jarvis.py is a thin shim).
Implemented with argparse only — no extra dependency.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bella",
        description="Bella — local personal AI workstation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  chat            text REPL (default)\n"
            "  voice           wake-word / listen mode\n"
            "  serve           run the Bella hub (WebSocket + HTTP)\n"
            "  client          connect this machine as a Bella device\n"
            "  doctor          run diagnostics\n"
            "  benchmark       measure path latencies\n"
            '  "<request>"     execute a single request and exit\n'
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show exact tool names, arguments, timings, and raw output")
    p.add_argument("command", nargs="?", help="a command, or a single-shot request in quotes")
    p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # --verbose may appear anywhere (incl. after a quoted request); pull it out
    # before argparse so REMAINDER doesn't swallow it.
    verbose = False
    filtered: list[str] = []
    for a in argv:
        if a in ("-v", "--verbose"):
            verbose = True
        else:
            filtered.append(a)

    parser = _build_parser()
    ns = parser.parse_args(filtered)
    verbose = verbose or ns.verbose

    command = ns.command
    rest = ns.rest or []

    if command is None:
        return _chat(start_mode="silent", verbose=verbose)

    if command in ("-h", "--help", "help"):
        parser.print_help()
        return 0
    if command == "chat":
        return _chat(start_mode="silent", verbose=verbose)
    if command == "voice":
        return _chat(start_mode="listen", verbose=verbose)
    if command == "serve":
        from bella.server import main as serve_main
        serve_main()
        return 0
    if command == "client":
        from clients.desktop.client import main as client_main
        return client_main(rest)
    if command == "doctor":
        from bella.doctor import main as doctor_main
        return doctor_main()
    if command == "benchmark":
        from bella.benchmark import run_benchmark
        from bella.config import get_config
        from bella.interfaces.renderer import Renderer

        Renderer().benchmark(run_benchmark(get_config()))
        return 0

    # Anything else is a single-shot request.
    request = " ".join([command, *rest]).strip()
    return _single_shot(request, verbose=verbose)


def _chat(start_mode: str = "silent", verbose: bool = False) -> int:
    from bella.bootstrap import bootstrap
    from bella.config import get_config
    from bella.interfaces.cli import run_repl
    from bella.runtime import BellaRuntime

    config = get_config()
    report = bootstrap(config)
    runtime = BellaRuntime(config=config)
    try:
        run_repl(runtime, report, start_mode=start_mode, verbose=verbose)
    finally:
        runtime.shutdown()
    return 0


def _single_shot(request: str, verbose: bool = False) -> int:
    from bella.config import get_config
    from bella.interfaces.cli import run_single_shot
    from bella.runtime import BellaRuntime

    if not request:
        print("nothing to do", file=sys.stderr)
        return 2
    runtime = BellaRuntime(config=get_config())
    try:
        run_single_shot(request, runtime, verbose=verbose)
    finally:
        runtime.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
