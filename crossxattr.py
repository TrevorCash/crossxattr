#!/usr/bin/env python3
"""
Cross-platform extended file attributes manager.

Stores and restores extended file and directory attributes (xattr) using .xattr.json files.
Supports Windows, macOS, and Linux.  Aim to support all extended attribute formats.

Tested So Far: 
[KDE user.xdg.comment, user.xdg.tags]

Native attributes for a project/directory are stored in a generic json hidden file. (.xattr.json)

Running the script will recurse downwards and add all attributes to the .xattr.json where the script was called.
If .xattr.json files are further into the directory structure - attributes will be added to that (closer to the file) rather than the root.xattr.json

.xattr.json files are meant to be store the attributes and can be tracked in projects (git repos etc.)

Place an empty .xattr.skip file in a directory to exclude that directory and all descendants from scanning.


Usage:
  python crossxattr.py --mode=filesToJson
  python crossxattr.py --mode=jsonToFiles
  python crossxattr.py --mode=flatten

Modes:
  filesToJson  Scan all files and directories recursively and store their xattrs in .xattr.json files.
  jsonToFiles  Read .xattr.json files and restore xattrs to the files and directories.
  flatten      Propagate directory attributes from the filesystem to all descendant files and directories.

The script must be run from the directory where this script resides.
JSON keys are canonical cross-platform names; the script translates them to/from
platform-specific xattr names at runtime.
Directory entries in JSON are suffixed with "/" to distinguish them from files.
List-type attributes (e.g., tags) are stored as comma-separated values in JSON and
converted to newline-separated bytes when applied to the filesystem.
When in filesToJson mode, files inside a git repository automatically have their
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
        if os.path.isfile(os.path.join(dir_path, ".xattr.skip")):
            return
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


def _is_list_key(canonical: str) -> bool:
    return canonical == "tags"


def _list_separator(canonical: str) -> str:
    return {"tags": ","}.get(canonical, "\n")


def _list_to_items(raw: bytes, separator: str) -> list[str]:
    text = raw.decode("utf-8")
    return [item for item in text.split(separator) if item]


def _items_to_list(items: list[str], separator: str) -> bytes:
    return separator.join(items).encode("utf-8")


def _merge_list_value(dir_value: bytes, existing_value: bytes | None, separator: str) -> bytes | None:
    dir_items = _list_to_items(dir_value, separator)
    existing_items = _list_to_items(existing_value, separator) if existing_value else []
    merged = sorted(set(dir_items) | set(existing_items))
    if merged == existing_items:
        return None
    return _items_to_list(merged, separator)


def from_files_mode(root_dir: str, traverse_hidden: bool = True) -> None:
    if not _has_xattr_support():
        print(
            "Error: Extended attributes are not supported on this platform "
            "(requires Python 3.13+).",
            file=sys.stderr,
        )
        sys.exit(1)

    json_dirs, _ = _scan_tree(root_dir, traverse_hidden=traverse_hidden)

    if not json_dirs:
        json_dirs = {root_dir: os.path.join(root_dir, ".xattr.json")}

    json_dir_set = set(json_dirs.keys())

    for json_dir in sorted(json_dirs.keys(), key=lambda p: p.count(os.sep)):
        json_path = json_dirs[json_dir]
        data: dict[str, Any] = {}

        json_dir_abs = os.path.abspath(json_dir)

        def _walk(dir_path: str) -> None:
            if os.path.isfile(os.path.join(dir_path, ".xattr.skip")):
                return
            try:
                with os.scandir(dir_path) as it:
                    for entry in it:
                        if entry.is_symlink():
                            continue
                        if entry.name == ".xattr.json":
                            continue
                        if not traverse_hidden and entry.name.startswith("."):
                            continue

                        actual_path = entry.path
                        xattr_keys = _list_xattrs(actual_path)
                        if not xattr_keys:
                            if entry.is_dir(follow_symlinks=False):
                                entry_abs = os.path.abspath(entry.path)
                                if entry_abs not in json_dir_set:
                                    _walk(entry.path)
                            continue

                        rel_path = os.path.relpath(actual_path, json_dir).replace("\\", "/")
                        if entry.is_dir(follow_symlinks=False):
                            rel_path = rel_path + "/"

                        entry_abs = os.path.abspath(actual_path)
                        if not entry_abs.startswith(json_dir_abs + os.sep) and entry_abs != json_dir_abs:
                            if entry.is_dir(follow_symlinks=False):
                                _walk(entry.path)
                            continue

                        entry_data: dict[str, Any] = {}
                        for key in xattr_keys:
                            value = _get_xattr(actual_path, key)
                            if value is None:
                                continue
                            canonical = to_canonical(key)
                            try:
                                text_value = _encode(value)
                            except UnicodeDecodeError:
                                entry_data[canonical] = {"raw": _encode_raw(value)}
                                continue

                            if _is_list_key(canonical):
                                separator = _list_separator(canonical)
                                entry_data[canonical] = _list_to_items(value, separator)
                            else:
                                entry_data[canonical] = {"text": text_value}

                        if entry_data:
                            data[rel_path] = entry_data

                        if entry.is_dir(follow_symlinks=False):
                            entry_abs = os.path.abspath(entry.path)
                            if entry_abs not in json_dir_set:
                                _walk(entry.path)
            except PermissionError:
                pass

        _walk(json_dir)

        try:
            os.makedirs(json_dir, exist_ok=True)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except IOError as e:
            print(f"Error writing {json_path}: {e}", file=sys.stderr)

    print(
        f"Processed entries across {len(json_dirs)} .xattr.json files."
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
                os_key = from_canonical(canonical)
                separator = _list_separator(canonical)
                if isinstance(entry, list):
                    try:
                        value = _items_to_list(entry, separator)
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
                elif isinstance(entry, dict):
                    if "text" in entry:
                        try:
                            text_value = entry["text"]
                            if _is_list_key(canonical) and isinstance(text_value, str):
                                value = _items_to_list([text_value], separator)
                            else:
                                value = _decode(text_value)
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


def flatten_mode(root_dir: str, traverse_hidden: bool = True) -> None:
    if not _has_xattr_support():
        print(
            "Error: Extended attributes are not supported on this platform "
            "(requires Python 3.13+).",
            file=sys.stderr,
        )
        sys.exit(1)

    dirs_with_attrs: dict[str, dict[str, Any]] = {}

    for dir_path, dir_names, file_names in os.walk(root_dir, followlinks=False):
        if not traverse_hidden:
            dir_names[:] = [d for d in dir_names if not d.startswith(".")]

        if dir_path == root_dir and os.path.basename(dir_path).startswith("."):
            continue

        if os.path.isfile(os.path.join(dir_path, ".xattr.skip")):
            dir_names[:] = []
            continue

        xattr_keys = _list_xattrs(dir_path)
        if not xattr_keys:
            continue

        attrs: dict[str, Any] = {}
        for key in xattr_keys:
            value = _get_xattr(dir_path, key)
            if value is None:
                continue
            canonical = to_canonical(key)
            try:
                text_value = _encode(value)
            except UnicodeDecodeError:
                attrs[canonical] = {"raw": _encode_raw(value)}
                continue

            if _is_list_key(canonical):
                separator = _list_separator(canonical)
                attrs[canonical] = _list_to_items(value, separator)
            else:
                attrs[canonical] = {"text": text_value}

        if attrs:
            dirs_with_attrs[dir_path] = attrs

    if not dirs_with_attrs:
        print("No directory attributes found to flatten.")
        return

    affected_paths: set[str] = set()
    affected = 0
    attrs_set = 0

    for dir_path, attrs in sorted(dirs_with_attrs.items(), key=lambda x: x[0].count(os.sep)):
        if not attrs:
            continue

        for root, dirs, files in os.walk(dir_path, followlinks=False):
            if not traverse_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".")]

            if os.path.isfile(os.path.join(root, ".xattr.skip")):
                dirs[:] = []
                entries = [root]
            else:
                entries = []
                for name in files:
                    if name == ".xattr.json":
                        continue
                    entries.append(os.path.join(root, name))
                for name in dirs:
                    if name == ".xattr.json":
                        continue
                    entries.append(os.path.join(root, name))

            for entry_path in entries:
                new_count = _count_new_attrs(entry_path, attrs)
                if new_count > 0:
                    _apply_missing_attrs(entry_path, attrs)
                    if entry_path not in affected_paths:
                        affected_paths.add(entry_path)
                        affected += 1
                    attrs_set += new_count

    print(f"Flattened attributes to {affected} entries, set {attrs_set} attributes.")


def _apply_missing_attrs(path: str, attrs: dict[str, Any]) -> None:
    existing_xattrs = {key: _get_xattr(path, key) for key in _list_xattrs(path)}
    for canonical, entry in attrs.items():
        os_key = from_canonical(canonical)
        existing_value = existing_xattrs.get(os_key)
        separator = _list_separator(canonical)
        if existing_value is not None:
            if _is_list_key(canonical):
                if isinstance(entry, list):
                    new_bytes = _items_to_list(entry, separator)
                    merged = _merge_list_value(new_bytes, existing_value, separator)
                    if merged is None:
                        continue
                    _set_xattr(path, os_key, merged)
                elif "raw" in entry:
                    merged = _merge_list_value(_decode_raw(entry["raw"]), existing_value, separator)
                    if merged is None:
                        continue
                    _set_xattr(path, os_key, merged)
                continue
            print(
                f"Could not apply '{os_key}' to '{path}' - attribute already exists and is not a list-type.",
                file=sys.stderr,
            )
            continue
        if isinstance(entry, list):
            _set_xattr(path, os_key, _items_to_list(entry, separator))
        elif "text" in entry:
            _set_xattr(path, os_key, _decode(entry["text"]))
        if "raw" in entry:
            _set_xattr(path, os_key, _decode_raw(entry["raw"]))


def _count_new_attrs(path: str, attrs: dict[str, Any]) -> int:
    existing_xattrs = {key: _get_xattr(path, key) for key in _list_xattrs(path)}
    count = 0
    for canonical, entry in attrs.items():
        os_key = from_canonical(canonical)
        existing_value = existing_xattrs.get(os_key)
        separator = _list_separator(canonical)
        if existing_value is None:
            count += 1
            continue
        if _is_list_key(canonical):
            if isinstance(entry, list):
                new_bytes = _items_to_list(entry, separator)
                merged = _merge_list_value(new_bytes, existing_value, separator)
                if merged is not None:
                    count += 1
            elif "raw" in entry:
                merged = _merge_list_value(_decode_raw(entry["raw"]), existing_value, separator)
                if merged is not None:
                    count += 1
    return count


def main() -> None:
    root_dir = os.getcwd()

    parser = argparse.ArgumentParser(
        description="Cross-platform extended file attributes manager"
    )
    parser.add_argument(
        "--mode",
        choices=["jsonToFiles", "filesToJson", "flatten"],
        required=True,
        help="Operation mode: jsonToFiles (restore xattrs from JSON), filesToJson (scan and update JSON), or flatten (propagate directory attributes to descendants)",
    )
    parser.add_argument(
        "--traverseHiddenDirs",
        type=lambda x: x.lower() == "true",
        default=False,
        help="Whether to traverse hidden directories (default: false)",
    )

    args = parser.parse_args()

    if not os.path.isdir(root_dir):
        print(f"Error: {root_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if args.mode in ("filesToJson", "jsonToFiles"):
        json_path = os.path.join(root_dir, ".xattr.json")
        if not os.path.exists(json_path):
            try:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({}, f)
                    f.write("\n")
            except IOError as e:
                print(f"Error creating {json_path}: {e}", file=sys.stderr)
                sys.exit(1)

    if args.mode == "filesToJson":
        from_files_mode(root_dir, traverse_hidden=args.traverseHiddenDirs)
    elif args.mode == "jsonToFiles":
        to_files_mode(root_dir, traverse_hidden=args.traverseHiddenDirs)
    elif args.mode == "flatten":
        flatten_mode(root_dir, traverse_hidden=args.traverseHiddenDirs)


if __name__ == "__main__":
    main()
