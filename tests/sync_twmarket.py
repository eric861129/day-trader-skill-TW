"""Copy one twmarket.py over every other copy in the skill folders.

    python tests/sync_twmarket.py trader-scanner/twmarket.py

Every skill folder that imports twmarket keeps its own copy so it works when
copied alone; tests/test_twmarket.py fails until the copies match again.
"""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def users():
    """Skill folders whose tools import twmarket."""
    out = []
    for d in sorted(os.listdir(ROOT)):
        folder = os.path.join(ROOT, d)
        if not (d.startswith("trader-") and os.path.isdir(folder)):
            continue
        for f in os.listdir(folder):
            if f.endswith(".py") and f != "twmarket.py":
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    if "import twmarket" in fh.read():
                        out.append(d)
                        break
    return out


def main(argv):
    if len(argv) != 1 or not os.path.isfile(argv[0]):
        print(__doc__.strip())
        return 2
    src = os.path.abspath(argv[0])
    for d in users():
        dst = os.path.join(ROOT, d, "twmarket.py")
        if os.path.abspath(dst) != src:
            shutil.copyfile(src, dst)
            print("copied to %s" % os.path.relpath(dst, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
