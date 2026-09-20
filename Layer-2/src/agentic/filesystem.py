"""Small known-folder surface. No file content, execution, deletion or arbitrary roots."""
import json
import os
from pathlib import Path, PureWindowsPath
import stat

ROOT_NAMES = ("desktop", "documents", "downloads", "workspace")
RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}


def parts(relative):
    if not relative:
        return ()
    path = PureWindowsPath(relative)
    if path.is_absolute() or path.drive or path.root:
        raise ValueError("Only relative paths inside a known folder are allowed")
    raw = relative.replace("\\", "/").split("/")
    if len(raw) > 12:
        raise ValueError("Path too deep")
    for name in raw:
        if (not name or name.startswith(".") or name.endswith((".", " ")) or len(name) > 100
                or any(ord(c) < 32 or c in '<>:"|?*' for c in name)
                or name.split(".")[0].upper() in RESERVED):
            raise ValueError("Invalid or protected path component")
    return tuple(raw)


def known_root(name):
    if name == "workspace":
        return Path(__file__).resolve().parents[3] / ".epis-runtime" / "files"
    from win32com.shell import shell
    folder_id = {"desktop": shell.FOLDERID_Desktop, "documents": shell.FOLDERID_Documents,
                 "downloads": shell.FOLDERID_Downloads}[name]
    return Path(shell.SHGetKnownFolderPath(folder_id, 0, None))


class FolderTools:
    def __init__(self, resolver=known_root):
        self.resolver = resolver

    def directory(self, root, relative=""):
        if root not in ROOT_NAMES:
            raise ValueError("Unknown root")
        base = self.resolver(root)
        if not base.is_absolute() or str(base).startswith(("\\\\", "//")):
            raise ValueError("Remote folders not supported")
        # Check ancestors too: a junction above the selected root is not trusted.
        for item in reversed((base, *base.parents)):
            if item.exists() and os.lstat(item).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Reparse folders not supported")
        if root == "workspace" and not base.exists():
            base.mkdir(parents=True, exist_ok=False)
        target = base
        for name in parts(relative):
            target /= name
            attrs = os.lstat(target).st_file_attributes
            if attrs & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
                raise ValueError("Protected folders not supported")
        if not target.is_dir():
            raise ValueError("Folder does not exist")
        return target

    def list(self, args):
        target = self.directory(args["root"], args.get("relative_path", ""))
        items = []
        truncated = False
        encoded_size = 0
        with os.scandir(target) as entries:
            for index, entry in enumerate(entries):
                if index >= 500 or len(items) >= 100:
                    truncated = True
                    break
                try:
                    attrs = entry.stat(follow_symlinks=False).st_file_attributes
                    if entry.name.startswith(".") or attrs & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
                        continue
                    item = {"name": entry.name, "kind": "folder" if entry.is_dir(follow_symlinks=False) else "file"}
                    encoded_size += len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
                    if encoded_size > 24000:
                        truncated = True
                        break
                    items.append(item)
                except OSError:
                    continue
        return {"ok": True, "entries": sorted(items, key=lambda e: e["name"].casefold()), "truncated": truncated,
                "message": "One directory only; no file contents read, hidden/system/reparse entries omitted"}

    def open(self, args):
        target = self.directory(args["root"], args.get("relative_path", ""))
        os.startfile(str(target))
        return {"ok": True, "message": "Folder opening requested; visible Explorer window unverified"}

    def create(self, args):
        # New directories only, in dedicated EPIS workspace; never startup/system folders.
        names = parts(args["name"])
        if len(names) != 1:
            return {"ok": False, "error": "Use one folder name"}
        parent = self.directory("workspace", args.get("relative_path", ""))
        target = parent / names[0]
        if target.exists():
            return {"ok": False, "error": "Destination already exists; nothing overwritten"}
        target.mkdir(exist_ok=False)
        return {"ok": True, "message": "New folder created in EPIS workspace", "name": names[0]}


def register_folder_tools(registry):
    from .tools import ToolSpec
    folders = FolderTools()
    device = {"device_id": {"type": "string"}}
    location = {"root": {"type": "string", "enum": list(ROOT_NAMES)},
                "relative_path": {"type": "string", "maxLength": 500, "description": "Relative folder path only; omit for root. No absolute paths, .. or links."}}
    def schema(properties, required):
        return {"type": "object", "properties": {**properties, **device}, "required": required, "additionalProperties": False}
    registry.register(ToolSpec("list_folder", "List names in desktop/documents/downloads/workspace, one directory, no file contents. Requires approval before names reach the model.",
        schema(location, ["root"]), "files.list", "yellow", True,
        confirmation_notice="Bu klasördeki görünür dosya/klasör adları model API'sine gönderilecek. İçerikler okunmaz."), folders.list)
    registry.register(ToolSpec("open_folder", "Open a known folder or its safe relative subfolder in Explorer; cannot open files or run executables.",
        schema(location, ["root"]), "files.open_folder", "yellow", True), folders.open)
    registry.register(ToolSpec("create_workspace_folder", "Create one new folder ONLY in EPIS workspace (not desktop/documents/downloads). No overwrite, deletion or file writes.",
        schema({"name": {"type": "string", "maxLength": 100}, "relative_path": location["relative_path"]}, ["name"]),
        "files.create_folder", "yellow", True), folders.create)
