"""Local timeit comparison; run with Python -B benchmarks/field_injection.py.

No network or optional packages. Startup discovery and singleton construction
are outside the timed section. One field, one already-created singleton, warm
name/selection caches; these numbers do not describe every application graph.
"""

import argparse
from pathlib import Path
import platform
import sys
from timeit import repeat


# Running directly from a source checkout needs no installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from py_autowired import Container, auto_inject


class Repository:
    pass


class Plain:
    def __init__(self):
        self.repository = None


class Injected:
    repository_instance: Repository | None

    def __init__(self):
        self.repository_instance = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", type=int, default=100_000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if args.number < 1 or args.repeat < 1:
        parser.error("--number and --repeat must be positive")
    container = Container()
    container.register_instance(Repository, Repository())
    auto_inject(container, root_dir=str(Path(__file__).resolve().parent))
    assert isinstance(Injected().repository_instance, Repository)
    plain = min(repeat(Plain, number=args.number, repeat=args.repeat)) / args.number
    injected = min(repeat(Injected, number=args.number, repeat=args.repeat)) / args.number
    print(f"Python {platform.python_version()} on {platform.system()} ({platform.machine()})")
    print(f"Best of {args.repeat}; {args.number:,} creations per repeat")
    print(f"Plain: {plain * 1e6:.3f} us/object")
    print(f"Automatic field injection: {injected * 1e6:.3f} us/object")
    print(f"Additional time: {(injected - plain) * 1e6:.3f} us/object ({injected / plain:.2f}x total)")


if __name__ == "__main__":
    main()
