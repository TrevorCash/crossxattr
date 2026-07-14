#!/usr/bin/env python3
"""
Cross-platform extended file attributes manager.

Stores and restores extended file attributes (xattr) using .xattr.json files.
Supports Windows (NTFS extended attributes), macOS, and Linux.

Usage:
  python xattr_manager.py --mode=fromFiles
  python xattr_manager.py --mode=toFiles

Modes:
  fromFiles  Scan all files recursively and store their xattrs in .xattr.json files.
  toFiles    Read .xattr.json files and restore xattrs to the files.

JSON keys are canonical cross-platform names; the script translates them to/from
platform-specific xattr names at runtime.
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


def _find_closest_json(file_path: str, json_dirs: set[str]) -> str | None:
    """Find the closest ancestor directory containing a .xattr.json."""
    dir_path = os.path.dirname(os.path.abspath(file_path))
    while True:
        if dir_path in json_dirs:
            return dir_path
        parent = os.path.dirname(dir_path)
        if parent == dir_path:
            break
        dir_path = parent
    return None


def _scan_tree(root_dir: str) -> tuple[dict[str, str], list[str]]:
    """
    Walk root_dir and return (json_dir_map, all_files).
    json_dir_map: {directory_path: .xattr.json path}
    all_files: absolute paths of all regular files (excluding .xattr.json itself).
    """
    json_dirs: dict[str, str] = {}
    all_files: list[str] = []

    for dirpath, _dirnames, filenames in os.walk(root_dir, followlinks=False):
        json_path = os.path.join(dirpath, ".xattr.json")
        if os.path.isfile(json_path):
            json_dirs[dirpath] = json_path

        for filename in filenames:
            if filename == ".xattr.json":
                continue
            file_path = os.path.join(dirpath, filename)
            if os.path.isfile(file_path):
                all_files.append(file_path)

    return json_dirs, all_files


def from_files_mode(root_dir: str) -> None:
    if not _has_xattr_support():
        print(
            "Error: Extended attributes are not supported on this platform "
            "(requires Python 3.13+).",
            file=sys.stderr,
        )
        sys.exit(1)

    json_dirs, all_files = _scan_tree(root_dir)
    json_dir_set = set(json_dirs.keys())

    file_to_json_dir: dict[str, str] = {}
    for file_path in all_files:
        closest = _find_closest_json(file_path, json_dir_set)
        if closest is None:
            closest = root_dir
        file_to_json_dir[file_path] = closest

    for json_dir in file_to_json_dir.values():
        if json_dir not in json_dirs:
            json_dirs[json_dir] = os.path.join(json_dir, ".xattr.json")

    json_to_files: dict[str, list[str]] = {}
    for file_path, json_dir in file_to_json_dir.items():
        json_to_files.setdefault(json_dir, []).append(file_path)

    for json_dir, files in json_to_files.items():
        json_path = json_dirs[json_dir]
        data: dict[str, Any] = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}

        files_set = set(files)

        keys_to_remove = []
        for file_key in list(data.keys()):
            file_abs = os.path.join(json_dir, file_key)
            if file_abs not in files_set:
                closest = _find_closest_json(file_abs, json_dir_set)
                if closest is not None and closest != json_dir:
                    keys_to_remove.append(file_key)

        for key in keys_to_remove:
            del data[key]

        for file_path in files:
            rel_path = os.path.relpath(file_path, json_dir)
            if rel_path.startswith(".."):
                continue

            xattr_keys = _list_xattrs(file_path)
            if not xattr_keys:
                continue

            file_entry: dict[str, Any] = {}
            for key in xattr_keys:
                value = _get_xattr(file_path, key)
                if value is None:
                    continue
                canonical = to_canonical(key)
                try:
                    file_entry[canonical] = {"text": _encode(value)}
                except UnicodeDecodeError:
                    file_entry[canonical] = {"raw": _encode_raw(value)}

            if file_entry:
                data[rel_path] = file_entry

        try:
            os.makedirs(json_dir, exist_ok=True)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except IOError as e:
            print(f"Error writing {json_path}: {e}", file=sys.stderr)

    print(
        f"Processed {len(all_files)} files across {len(json_dirs)} .xattr.json files."
    )


def to_files_mode(root_dir: str) -> None:
    if not _has_xattr_support():
        print(
            "Error: Extended attributes are not supported on this platform "
            "(requires Python 3.13+).",
            file=sys.stderr,
        )
        sys.exit(1)

    json_dirs, all_files = _scan_tree(root_dir)
    json_dir_set = set(json_dirs.keys())

    file_to_json_dir: dict[str, str] = {}
    for file_path in all_files:
        closest = _find_closest_json(file_path, json_dir_set)
        if closest is None:
            continue
        if closest not in json_dirs:
            continue
        file_to_json_dir[file_path] = closest

    json_to_files: dict[str, list[str]] = {}
    for file_path, json_dir in file_to_json_dir.items():
        json_to_files.setdefault(json_dir, []).append(file_path)

    success_count = 0
    error_count = 0

    for json_dir, files in json_to_files.items():
        json_path = json_dirs[json_dir]
        if not os.path.exists(json_path):
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading {json_path}: {e}", file=sys.stderr)
            continue

        for file_path in files:
            rel_path = os.path.relpath(file_path, json_dir)
            if rel_path not in data:
                continue

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
                        if _set_xattr(file_path, os_key, value):
                            success_count += 1
                        else:
                            error_count += 1
                            print(
                                f"Failed to set xattr '{os_key}' on {file_path}",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        error_count += 1
                        print(
                            f"Error setting xattr '{os_key}' on {file_path}: {e}",
                            file=sys.stderr,
                        )
                if "raw" in entry:
                    try:
                        value = _decode_raw(entry["raw"])
                        if _set_xattr(file_path, os_key, value):
                            success_count += 1
                        else:
                            error_count += 1
                            print(
                                f"Failed to set xattr '{os_key}' on {file_path}",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        error_count += 1
                        print(
                            f"Error setting xattr '{os_key}' on {file_path}: {e}",
                            file=sys.stderr,
                        )

    print(f"Set {success_count} extended attributes ({error_count} errors).")


def main() -> None:
    root_dir = cwd
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

    args = parser.parse_args()

    if not os.path.isdir(root_dir):
        print(f"Error: {root_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if args.mode == "fromFiles":
        from_files_mode(root_dir)
    elif args.mode == "toFiles":
        to_files_mode(root_dir)


if __name__ == "__main__":
    main()
