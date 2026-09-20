"""Manual benchmark: python tests/benchmark_workspace.py --baseline <git-ref>."""

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from sdlc_builder import workspaces as current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--files", type=int, default=300)
    args = parser.parse_args()
    source = subprocess.run(
        ["git", "show", f"{args.baseline}:apps/sdlc/src/sdlc_builder/workspaces.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    baseline = {"__name__": "sdlc_builder._baseline", "__package__": "sdlc_builder"}
    exec(compile(source, "baseline_workspaces.py", "exec"), baseline)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        current.git(root, "init", "-b", "main")
        for i in range(args.files):
            (root / f"file-{i:04}.txt").write_text("unmatched source text\n" * 40)
        current.git(root, "add", ".")
        current.git(
            root,
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@localhost",
            "commit",
            "-m",
            "Snapshot",
        )
        (root / "file-0000.txt").write_text("one changed file\n")
        measurements = {}
        for name, function_args in [("diff", ()), ("search_files", ("absent needle",))]:
            values = {}
            outputs = []
            for label, implementation in [
                ("before", baseline[name]),
                ("after", getattr(current, name)),
            ]:
                started = time.perf_counter()
                outputs.append(implementation(root, *function_args))
                values[label + "_ms"] = round((time.perf_counter() - started) * 1000, 2)
            assert outputs[0] == outputs[1], name
            values["speedup"] = round(
                values["before_ms"] / max(values["after_ms"], 0.01), 1
            )
            measurements[name] = values
        print(
            json.dumps(
                {
                    "baseline": args.baseline,
                    "files": args.files,
                    "results": measurements,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
