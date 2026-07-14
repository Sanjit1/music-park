# This is a wrapper for mcn/build_graph
#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from music_mcn.build_graph import main


if __name__ == "__main__":
    raise SystemExit(main())
