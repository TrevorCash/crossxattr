#!/usr/bin/env python3
"""
Cross-platform extended file attributes manager.

Stores and restores extended file and directory attributes (xattr) using .xattr.json files.
Supports Windows (NTFS extended attributes), macOS, and Linux.

Usage:
  python crossxattr.py --mode=fromFiles
  python crossxattr.py --mode=toFiles

Modes:
  fromFiles  Scan all files and directories recursively and store their xattrs in .xattr.json files.
  toFiles    Read .xattr.json files and restore xattrs to the files and directories.

The script must be run from the directory where this script resides.
JSON keys are canonical cross-platform names; the script translates them to/from
platform-specific xattr names at runtime.
Directory entries in JSON are suffixed with "/" to distinguish them from files.
When in fromFiles mode, files inside a git repository automatically have their
attributes stored in a .xattr.json at the git repository root.
Requires Python 3.13+ for os.getxattr / os.setxattr support.
"""

import argparse
import base64
import json
import os
import sys
from typing import Any


_KEY_MAP: dict[str, dict[str, str]] = {
    "comment": {
        "linux": "user.xdg.comment",
        "darwin": "com.apple.metadata:comment",
        "windows": "comment",
    },
    "quarantine": {
        "linux": "user.quarantine",
        "darwin": "com.apple.quarantine",
        "windows": "quarantine",
    },
    "tags": {
        "linux": "user.xdg.tags",
        "darwin": "com.apple.metadata:tags",
        "windows": "tags",
    },
}

_PLATFORM_PREFIXES: dict[str, list[str]] = {
    "linux": ["user.xdg.", "system.", "trusted.", "security."],
    "darwin": ["com.apple.", "com.apple.metadata."],
    "windows": [],
}


def _get_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    return "unknown"


def to_canonical(os_key: str) -> str:
    platform = _get_platform()
    for canonical, mapping in _KEY_MAP.items():
        if mapping.get(platform) == os_key:
            return canonical
    for prefix in sorted(_PLATFORM_PREFIXES.get(platform, []), key=len, reverse=True):
        if os_key.startswith(prefix):
            return os_key[len(prefix):]
    return os_key


def from_canonical(canonical: str) -> str:
    platform = _get_platform()
    mapping = _KEY_MAP.get(canonical)
    if mapping and platform in mapping:
        return mapping[platform]
    return canonical


def _encode(value: bytes) -> str:
    return value.decode("utf-8")


def _decode(value: str) -> bytes:
    return value.encode("utf-8")


def _encode_raw(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_raw(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _has_xattr_support() -> bool:
    if hasattr(os, "getxattr"):
        return True
    try:
        import xattr  # type: ignore[import-untyped]

        return True
    except ImportError:
        return False


def _get_xattr(path: str, key: str) -> bytes | None:
    if hasattr(os, "getxattr"):
        try:
            return os.getxattr(path, key)
        except (OSError, AttributeError):
            return None
    try:
        import xattr  # type: ignore[import-untyped]

        return xattr.getxattr(path, key)
    except (ImportError, OSError):
        return None


def _set_xattr(path: str, key: str, value: bytes) -> bool:
    if hasattr(os, "setxattr"):
        try:
            os.setxattr(path, key, value)
            return True
        except (OSError, AttributeError):
            return False
    try:
        import xattr  # type: ignore[import-untyped]

        xattr.setxattr(path, key, value)
        return True
    except (ImportError, OSError):
        return False


def _list_xattrs(path: str) -> list[str]:
    if hasattr(os, "listxattr"):
        try:
            return list(os.listxattr(path))
        except (OSError, AttributeError):
            return []
    try:
        import xattr  # type: ignore[import-untyped]

        return list(xattr.listxattr(path))
    except (ImportError, OSError):
        return []


def _remove_xattr(path: str, key: str) -> bool:
    if hasattr(os, "removexattr"):
        try:
            os.removexattr(path, key)
            return True
        except (OSError, AttributeError):
            return False
    try:
        import xattr  # type: ignore[import-untyped]

        xattr.removexattr(path, key)
        return True
    except (ImportError, OSError):
        return False


def _find_closest_json_for_entry(entry_path: str, json_dirs: set[str]) -> str | None:
    """Find the closest .xattr.json for an entry, respecting git repo boundaries."""
    git_root = _find_git_root(entry_path)

    if entry_path.endswith("/"):
        dir_path = os.path.abspath(entry_path.rstrip("/"))
    else:
        dir_path = os.path.dirname(os.path.abspath(entry_path))

    while True:
        if dir_path in json_dirs:
            return dir_path
        if git_root is not None and dir_path == git_root:
            return git_root
        parent = os.path.dirname(dir_path)
        if parent == dir_path:
            break
        if git_root is not None and not (parent == git_root or parent.startswith(git_root + os.sep)):
            break
        dir_path = parent

    return None


def _find_git_root(path: str) -> str | None:
    """Find the root of the nearest git repository containing the given path."""
    actual_path = path.rstrip("/")
    if os.path.isdir(actual_path):
        dir_path = actual_path
    else:
        dir_path = os.path.dirname(actual_path)
    while True:
        git_path = os.path.join(dir_path, ".git")
        if os.path.isdir(git_path) or os.path.isfile(git_path):
            return dir_path
        parent = os.path.dirname(dir_path)
        if parent == dir_path:
            break
        dir_path = parent
    return None


def _scan_tree(root_dir: str, traverse_hidden: bool = True) -> tuple[dict[str, str], list[str]]:
    """
    Walk root_dir and return (json_dir_map, all_entries).
    Uses os.scandir() for speed.
    json_dir_map: {directory_path: .xattr.json path}
    all_entries: absolute paths of all regular files and directories
                 (excluding .xattr.json itself). Directory paths end with "/".
    """
    json_dirs: dict[str, str] = {}
    all_entries: list[str] = []

    def _scan(dir_path: str) -> None:
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != ".xattr.json":
                            all_entries.append(entry.path + "/")
                        if not traverse_hidden and entry.name.startswith("."):
                            continue
                        _scan(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name == ".xattr.json":
                            json_dirs[dir_path] = entry.path
                        else:
                            all_entries.append(entry.path)
        except PermissionError:
            pass

    _scan(root_dir)
    return json_dirs, all_entries


def from_files_mode(root_dir: str, traverse_hidden: bool = True) -> None:
    if not _has_xattr_support():
        print(
            "Error: Extended attributes are not supported on this platform "
            "(requires Python 3.13+).",
            file=sys.stderr,
        )
        sys.exit(1)

    json_dirs, all_entries = _scan_tree(root_dir, traverse_hidden=traverse_hidden)
    json_dir_set = set(json_dirs.keys())

    entry_to_json_dir: dict[str, str] = {}
    for entry_path in all_entries:
        closest = _find_closest_json_for_entry(entry_path, json_dir_set)
        if closest is None:
            closest = root_dir
        entry_to_json_dir[entry_path] = closest

    for json_dir in entry_to_json_dir.values():
        if json_dir not in json_dirs:
            json_dirs[json_dir] = os.path.join(json_dir, ".xattr.json")

    json_dir_set = set(json_dirs.keys())

    json_to_entries: dict[str, list[str]] = {}
    for entry_path, json_dir in entry_to_json_dir.items():
        json_to_entries.setdefault(json_dir, []).append(entry_path)

    for json_dir, entries in json_to_entries.items():
        json_path = json_dirs[json_dir]
        data: dict[str, Any] = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}

        entries_set = set(entries)

        keys_to_remove = []
        for entry_key in list(data.keys()):
            entry_abs = os.path.join(json_dir, entry_key)
            if entry_abs not in entries_set:
                closest = _find_closest_json_for_entry(entry_abs, json_dir_set)
                if closest is not None and closest != json_dir:
                    keys_to_remove.append(entry_key)

        for key in keys_to_remove:
            del data[key]

        for entry_path in entries:
            is_dir = entry_path.endswith("/")
            rel_path = os.path.relpath(entry_path, json_dir)
            if is_dir:
                rel_path = rel_path.rstrip("/") + "/"
            if rel_path.startswith(".."):
                continue

            actual_path = entry_path.rstrip("/")
            xattr_keys = _list_xattrs(actual_path)
            if not xattr_keys:
                continue

            entry_data: dict[str, Any] = {}
            for key in xattr_keys:
                value = _get_xattr(actual_path, key)
                if value is None:
                    continue
                canonical = to_canonical(key)
                try:
                    entry_data[canonical] = {"text": _encode(value)}
                except UnicodeDecodeError:
                    entry_data[canonical] = {"raw": _encode_raw(value)}

            if entry_data:
                data[rel_path] = entry_data

        try:
            os.makedirs(json_dir, exist_ok=True)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except IOError as e:
            print(f"Error writing {json_path}: {e}", file=sys.stderr)

    print(
        f"Processed {len(all_entries)} entries across {len(json_dirs)} .xattr.json files."
    )


def to_files_mode(root_dir: str, traverse_hidden: bool = True) -> None:
    if not _has_xattr_support():
        print(
            "Error: Extended attributes are not supported on this platform "
            "(requires Python 3.13+).",
            file=sys.stderr,
        )
        sys.exit(1)

    json_dirs, all_entries = _scan_tree(root_dir, traverse_hidden=traverse_hidden)
    json_dir_set = set(json_dirs.keys())

    entry_to_json_dir: dict[str, str] = {}
    for entry_path in all_entries:
        closest = _find_closest_json_for_entry(entry_path, json_dir_set)
        if closest is None:
            continue
        if closest not in json_dirs:
            continue
        entry_to_json_dir[entry_path] = closest

    json_to_entries: dict[str, list[str]] = {}
    for entry_path, json_dir in entry_to_json_dir.items():
        json_to_entries.setdefault(json_dir, []).append(entry_path)

    success_count = 0
    error_count = 0

    for json_dir, entries in json_to_entries.items():
        json_path = json_dirs[json_dir]
        if not os.path.exists(json_path):
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading {json_path}: {e}", file=sys.stderr)
            continue

        for entry_path in entries:
            is_dir = entry_path.endswith("/")
            rel_path = os.path.relpath(entry_path, json_dir)
            if is_dir:
                rel_path = rel_path.rstrip("/") + "/"
            if rel_path not in data:
                continue

            actual_path = entry_path.rstrip("/")
            file_data = data[rel_path]
            if not isinstance(file_data, dict):
                continue
            for canonical, entry in file_data.items():
                if not isinstance(entry, dict):
                    continue
                os_key = from_canonical(canonical)
                if "text" in entry:
                    try:
                        value = _decode(entry["text"])
                        if _set_xattr(actual_path, os_key, value):
                            success_count += 1
                        else:
                            error_count += 1
                            print(
                                f"Failed to set xattr '{os_key}' on {actual_path}",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        error_count += 1
                        print(
                            f"Error setting xattr '{os_key}' on {actual_path}: {e}",
                            file=sys.stderr,
                        )
                if "raw" in entry:
                    try:
                        value = _decode_raw(entry["raw"])
                        if _set_xattr(actual_path, os_key, value):
                            success_count += 1
                        else:
                            error_count += 1
                            print(
                                f"Failed to set xattr '{os_key}' on {actual_path}",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        error_count += 1
                        print(
                            f"Error setting xattr '{os_key}' on {actual_path}: {e}",
                            file=sys.stderr,
                        )

    print(f"Set {success_count} extended attributes ({error_count} errors).")


def main() -> None:
    root_dir = os.getcwd()
    json_path = os.path.join(root_dir, ".xattr.json")
    if not os.path.exists(json_path):
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
                f.write("\n")
        except IOError as e:
            print(f"Error creating {json_path}: {e}", file=sys.stderr)
            sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Cross-platform extended file attributes manager"
    )
    parser.add_argument(
        "--mode",
        choices=["toFiles", "fromFiles"],
        required=True,
        help="Operation mode: toFiles (restore xattrs from JSON) or fromFiles (scan and update JSON)",
    )
    parser.add_argument(
        "--traverseHiddenDirs",
        type=lambda x: x.lower() == "true",
        default=True,
        help="Whether to traverse hidden directories (default: true)",
    )

    args = parser.parse_args()

    if not os.path.isdir(root_dir):
        print(f"Error: {root_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if args.mode == "fromFiles":
        from_files_mode(root_dir, traverse_hidden=args.traverseHiddenDirs)
    elif args.mode == "toFiles":
        to_files_mode(root_dir, traverse_hidden=args.traverseHiddenDirs)


if __name__ == "__main__":
    main()
