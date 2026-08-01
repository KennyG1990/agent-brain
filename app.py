#!/usr/bin/env python3
"""
Agent Brain - entry point.

Deliberately written in Python-2-compatible syntax down to the version check, so that
running it on an old interpreter produces a SENTENCE rather than a SyntaxError. Every
other module uses modern syntax freely; this file is the airlock.
"""

import sys

MIN = (3, 10)

if sys.version_info < MIN:
    sys.stderr.write(
        "\n"
        "  Agent Brain needs Python %d.%d or newer.\n"
        "  You are running %s\n"
        "  from %s\n"
        "\n"
        "  Install a current Python from https://www.python.org/downloads/\n"
        "  On Windows, TICK BOTH BOXES in the installer:\n"
        "     [x] Add python.exe to PATH\n"
        "     [x] tcl/tk and IDLE          <- the window will not open without this\n"
        "\n" % (MIN[0], MIN[1], sys.version.split()[0], sys.executable)
    )
    sys.exit(1)


def _check_tkinter():
    """The single most common failure on a fresh Windows install."""
    try:
        import tkinter  # noqa: F401  -- availability probe, not a use

        return True, ""
    except ImportError as e:
        return False, (
            "\n"
            "  Python is installed, but tkinter (the GUI toolkit) is missing.\n"
            "  Detail: %s\n"
            "\n"
            "  This happens with minimal or stripped-down Python builds.\n"
            "  Fix: reinstall from https://www.python.org/downloads/ and tick\n"
            "       [x] tcl/tk and IDLE\n"
            "\n"
            "  Verify with:   python -m tkinter\n"
            "  (a small window should appear)\n"
            "\n"
            "  Everything except the window still works from the command line:\n"
            "     python -m agentbrain.cli scan\n"
            '     python -m agentbrain.cli search "your question"\n'
            "\n" % e
        )


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-V"):
        print("agent-brain 1.0.0")
        return 0
    if args and args[0] in ("--help", "-h"):
        print(__doc__.strip())
        print("\n  (no arguments)     launch the desktop app")
        print("  --version          print the version")
        print("  --check            verify this machine can run it")
        print("\n  Command line, no GUI needed:")
        print("    python -m agentbrain.cli --help")
        print("    python -m agentbrain.mcp_server --install-help")
        return 0
    if args and args[0] == "--check":
        ok, msg = _check_tkinter()
        print("Python     : %s  (need %d.%d+)" % (sys.version.split()[0], MIN[0], MIN[1]))
        print("Executable : %s" % sys.executable)
        print("tkinter    : %s" % ("yes" if ok else "MISSING"))
        if not ok:
            sys.stderr.write(msg)
        from agentbrain import sources

        print("\nSession logs found on this computer:")
        for s in sources.discover():
            mark = "  OK " if (s.found and s.files) else ("  -- " if s.found else "     ")
            print("%s%-28s %5d files  %s" % (mark, s.name, s.files, s.path))
        return 0 if ok else 1

    ok, msg = _check_tkinter()
    if not ok:
        sys.stderr.write(msg)
        return 1
    from agentbrain.gui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
