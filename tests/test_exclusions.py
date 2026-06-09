#!/usr/bin/env python3
"""Tests for mount-selection / network-skip exclusion logic in app.py.

These cover the selection-file (TSV) writer that drives backup.sh, with focus
on the v1.3.1 fix: a container without an explicit per-container skip_network
override must INHERIT the run-level (schedule/manual) skip_network default, so
the global toggle is never silently lost when a selection file is written for
other containers.

Run:  python tests/test_exclusions.py
"""
import os
import sys
import tempfile

# Point the app at a throwaway backups root BEFORE importing it, so the test
# never touches a real backups directory.
_TMP_ROOT = tempfile.mkdtemp(prefix="dbk-test-")
os.environ["BACKUP_ROOT_CONTAINER"] = _TMP_ROOT

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def _parse(path):
    """Read a selection TSV into {container: (skip_network, spec)}."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            cols = line.split("\t")
            assert len(cols) == 3, f"expected 3 TSV columns, got {cols!r}"
            name, sn, spec = cols
            out[name] = (sn, spec)
    return out


_failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        _failures.append(label)


def main():
    # --- THE BUG: unconfigured container inherits global skip_network=True ----
    names = ["bazarr", "radarr", "immich_server"]
    cmounts = {"immich_server": {"all": False, "mounts": [], "skip_network": True}}
    path = app._write_selection_file(names, cmounts, skip_network=True)
    sel = _parse(path)
    check("bazarr (no override) inherits global skip_network=1",
          sel["bazarr"] == ("1", "*"))
    check("radarr (no override) inherits global skip_network=1",
          sel["radarr"] == ("1", "*"))
    check("immich_server explicit skip_network=1, all=False -> __none__",
          sel["immich_server"] == ("1", "__none__"))
    os.remove(path)

    # --- explicit per-container override KEEPS a network mount (sn=0) ---------
    cmounts = {"bazarr": {"all": True, "mounts": [], "skip_network": False}}
    path = app._write_selection_file(["bazarr", "radarr"], cmounts, skip_network=True)
    sel = _parse(path)
    check("bazarr explicit skip_network=False overrides global True (sn=0)",
          sel["bazarr"] == ("0", "*"))
    check("radarr (no override) still inherits global True (sn=1)",
          sel["radarr"] == ("1", "*"))
    os.remove(path)

    # --- global skip_network=False, no overrides -> all sn=0 -----------------
    cmounts = {"immich_server": {"all": False, "mounts": ["vol:immich_model-cache"]}}
    path = app._write_selection_file(["bazarr", "immich_server"], cmounts,
                                     skip_network=False)
    sel = _parse(path)
    check("bazarr (no override) inherits global skip_network=0",
          sel["bazarr"] == ("0", "*"))
    check("immich_server all=False with mounts -> explicit mount key spec",
          sel["immich_server"] == ("0", "vol:immich_model-cache"))
    os.remove(path)

    # --- spec generation variants --------------------------------------------
    cmounts = {
        "a": {"all": True},
        "b": {"all": False, "mounts": ["vol:x", "bind:/data"]},
        "c": {"all": False, "mounts": []},
    }
    path = app._write_selection_file(["a", "b", "c"], cmounts, skip_network=False)
    sel = _parse(path)
    check("all=True -> '*'", sel["a"][1] == "*")
    check("all=False with mounts -> comma keys", sel["b"][1] == "vol:x,bind:/data")
    check("all=False empty -> '__none__' sentinel", sel["c"][1] == "__none__")
    os.remove(path)

    # --- no container_mounts -> no selection file (backup.sh uses global) ----
    check("no container_mounts -> None (global SKIP_NETWORK_MOUNTS applies)",
          app._write_selection_file(["bazarr"], None, skip_network=True) is None)
    check("empty container_mounts -> None",
          app._write_selection_file(["bazarr"], {}, skip_network=True) is None)

    # --- network-fstype classification (drives is_network_volume in sh) ------
    check("nfs is a network volume type", "nfs" in app._NETWORK_VOL_TYPES)
    check("cifs is a network volume type", "cifs" in app._NETWORK_VOL_TYPES)
    check("ext4/local is NOT a network volume type",
          "ext4" not in app._NETWORK_VOL_TYPES and "local" not in app._NETWORK_VOL_TYPES)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {_failures}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
