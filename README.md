Cross-platform extended file attributes manager.

Stores and restores extended file and directory attributes (xattr) using .xattr.json files.
Supports Windows, macOS, and Linux.  Aim to support all extended attribute formats.

Tested So Far: 
[KDE user.xdg.comment, user.xdg.tags]

Native attributes for a project/directory are stored in a generic json hidden file. (.xattr.json)

Running the script will recurse downwards and add all attributes to the .xattr.json where the script was called.
If .xattr.json files are further into the directory structure - attributes will be added to that (closer to the file) rather than the root.xattr.json

.xattr.json files are meant to be store the attributes and can be tracked in projects (git repos etc.)


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
