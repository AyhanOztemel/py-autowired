"""0.5.1: no *_instance field is missed because of WHERE or WHEN a module loads.

Startup discovery skips some places on purpose (virtual environments, build
copies, ``tests``, ``_private.py`` files, ...). A project module from such a
place, or any project module imported only after startup, is now patched the
moment it is imported.
"""

import importlib
import sys
import threading
import uuid

import pytest

from py_autowired import Container, autowired


CASES = {
    "deep": "a/b/c/d/e/f/g/h/i/j/deep_consumer.py",
    "dist_folder": "dist/dist_consumer.py",
    "env_folder": "env/env_consumer.py",
    "underscore_file": "pkg/_private_consumer.py",
    "package_init": "init_pkg/__init__.py",
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"proj_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"clock_{tag}.py").write_text("class Clock:\n    def now(self):\n        return 'tick'\n")
    for case, rel in CASES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"class Consumer_{case}:\n"
            "    def __init__(self):\n"
            "        self.clock_instance = None\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    names = {case: rel[:-3].replace("/", ".").removesuffix(".__init__") for case, rel in CASES.items()}
    before = set(sys.modules)
    yield root, tag, names
    for name in set(sys.modules) - before:
        if any(name == n or name.startswith(n.split(".")[0] + ".") or name == n.split(".")[0]
               for n in names.values()) or name == f"clock_{tag}":
            sys.modules.pop(name, None)


def _start(root, tag):
    clock = importlib.import_module(f"clock_{tag}")
    container = Container()
    container.register_singleton(clock.Clock)
    autowired.auto_inject(container, root_dir=str(root))
    return container


@pytest.mark.parametrize("case", list(CASES))
def test_field_is_filled_wherever_and_whenever_the_module_loads(project, case):
    root, tag, names = project
    _start(root, tag)
    module = importlib.import_module(names[case])        # e.g. a lazy import at runtime
    consumer = getattr(module, f"Consumer_{case}")()
    assert consumer.clock_instance is not None
    assert consumer.clock_instance.now() == "tick"


def test_modules_outside_the_project_are_left_alone(project, tmp_path, monkeypatch):
    root, tag, _names = project
    _start(root, tag)
    outside = tmp_path / f"outside_{tag}"
    outside.mkdir()
    (outside / f"lib_{tag}.py").write_text(
        "class Library:\n    def __init__(self):\n        self.clock_instance = None\n")
    monkeypatch.syspath_prepend(str(outside))
    lib = importlib.import_module(f"lib_{tag}")
    try:
        assert lib.Library().clock_instance is None
        assert "__di_attrs__" not in lib.Library.__dict__
    finally:
        sys.modules.pop(f"lib_{tag}", None)


def test_late_import_while_another_thread_holds_the_lock_is_patched_afterwards(project):
    root, tag, names = project
    container = _start(root, tag)
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        module = importlib.import_module(names["dist_folder"])   # cannot take the lock now
    finally:
        release.set()
        holder.join(5)
    autowired.refresh()                                         # next lock holder catches up
    assert module.Consumer_dist_folder().clock_instance is not None
    assert autowired._NAME_MAP_CACHE._container is container



# ---- F3 (request time): a name outside the project costs one search, not two ----

def _count_full_scans(monkeypatch, wanted):
    """Counts the searches of the whole sys.path for ``wanted`` (PathFinder with no path list)."""
    scans = []
    original = importlib.machinery.PathFinder.find_spec

    def spy(cls, fullname, path=None, target=None):
        if fullname == wanted and path is None:
            scans.append(fullname)
        return original(fullname, path, target)

    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", classmethod(spy))
    return scans


def test_a_missing_module_outside_the_project_is_looked_up_once(project, monkeypatch):
    """F3: the hook asked every other finder itself and, finding nothing, let Python ask them all
    again - every failing import scanned the whole sys.path twice (a real application: +0.5 ms per
    request for an optional import of its test client). A name whose top level is not in the
    project is not the hook's: one scan per import."""
    root, tag, names = project
    _start(root, tag)
    wanted = f"surely_missing_{uuid.uuid4().hex[:8]}"
    scans = _count_full_scans(monkeypatch, wanted)
    for _ in range(3):
        with pytest.raises(ImportError):
            importlib.import_module(wanted)
    assert len(scans) == 3, f"sys.path scanned {len(scans)} times for 3 imports"


def test_a_project_module_created_after_a_failed_import_is_still_patched(project):
    """No negative cache: a project file that appears after a failed import of its name is found
    and patched (the directory's change time renews what the hook knows)."""
    root, tag, names = project
    _start(root, tag)
    name = f"later_{uuid.uuid4().hex[:8]}"
    with pytest.raises(ImportError):
        importlib.import_module(name)
    (root / f"{name}.py").write_text("class Later:\n    def __init__(self):\n        self.clock_instance = None\n")
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(name)
        assert module.Later().clock_instance is not None, "the late project module was not patched"
    finally:
        sys.modules.pop(name, None)


def test_a_namespace_package_in_the_project_is_still_patched(project):
    """A package without __init__.py (a namespace package) is a project name too."""
    root, tag, names = project
    _start(root, tag)
    pkg = f"nspkg_{uuid.uuid4().hex[:8]}"
    (root / pkg).mkdir()
    (root / pkg / "inner.py").write_text("class Inner:\n    def __init__(self):\n        self.clock_instance = None\n")
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(f"{pkg}.inner")
        assert module.Inner().clock_instance is not None, "a namespace package's module was not patched"
    finally:
        for key in [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]:
            sys.modules.pop(key, None)


def test_the_project_name_check_counts_every_kind_of_entry(tmp_path, monkeypatch):
    """Whatever Python itself would import from a sys.path entry under a root is a project name:
    packages, namespace directories, sources, bytecode, this Python's extension modules, and
    modules inside a zip entry. A name Python cannot import from there is not."""
    import zipfile

    root = tmp_path / "root"
    root.mkdir()
    (root / "pkgdir").mkdir()
    (root / "pkgdir" / "__init__.py").write_text("")
    (root / "ns_dir").mkdir()
    (root / "plain.py").write_text("")
    (root / "compiled.pyc").write_bytes(b"")
    (root / ("ext" + importlib.machinery.EXTENSION_SUFFIXES[0])).write_bytes(b"")
    zipped = root / "bundle.zip"
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("zipped_mod.py", "")
    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])
    autowired._remember_late_root(root)
    monkeypatch.setattr(sys, "path", [str(root), str(zipped)] + sys.path)
    importlib.invalidate_caches()
    for name in ("pkgdir", "ns_dir", "plain", "compiled", "ext", "zipped_mod"):
        assert autowired._maybe_project_name(name, None), name
    assert not autowired._maybe_project_name("sniffio_like_missing", None)


def test_a_submodule_whose_parent_path_is_in_the_project_is_the_hook_s(tmp_path, monkeypatch):
    """A submodule is searched in its parent's path, not in sys.path: a directory of that path under
    a root that holds it makes it the hook's; a parent path where Python finds nothing does not."""
    root = tmp_path / "root2"
    (root / "portion").mkdir(parents=True)
    (root / "portion" / "sub.py").write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])
    autowired._remember_late_root(root)
    assert autowired._maybe_project_name("outside_top.sub", [str(root / "portion")])
    assert not autowired._maybe_project_name("outside_top.sub", [str(elsewhere)])



# ---- F3, auditor's reading: three gaps of the first version ----

def test_the_root_package_itself_imported_late_is_still_patched(tmp_path, monkeypatch):
    """(1) The root is a package directory and its PARENT is on sys.path: `import <root name>` is a
    project module (its origin is under the root). The first F3 version only looked at sys.path
    entries under a root and silently left the root package unpatched."""
    tag = uuid.uuid4().hex[:8]
    parent = tmp_path / f"parent_{tag}"
    root = parent / f"app_{tag}"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("class RootConsumer:\n    def __init__(self):\n        self.clock_instance = None\n")
    (root / f"clockmod_{tag}.py").write_text("class Clock:\n    pass\n")
    monkeypatch.syspath_prepend(str(parent))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    try:
        clock = importlib.import_module(f"app_{tag}.clockmod_{tag}")
        container = Container()
        container.register_singleton(clock.Clock)
        sys.modules.pop(f"app_{tag}", None)  # the package itself loads late, after startup
        sys.modules.pop(f"app_{tag}.clockmod_{tag}", None)
        autowired.auto_inject(container, root_dir=str(root))
        sys.modules.pop(f"app_{tag}", None)
        package = importlib.import_module(f"app_{tag}")
        assert package.RootConsumer().clock_instance is not None, "the root package was not patched"
    finally:
        for key in [k for k in sys.modules if k.startswith(f"app_{tag}")]:
            sys.modules.pop(key, None)


def test_a_file_created_within_the_same_directory_tick_is_seen(project):
    """(2) No cached listing decides "not there": a project file created after a failed lookup is
    found even when the directory's modification time did not move (coarse file systems)."""
    import os

    root, tag, names = project
    _start(root, tag)
    name = f"sametick_{uuid.uuid4().hex[:8]}"
    stamp = os.stat(root)
    with pytest.raises(ImportError):
        importlib.import_module(name)
    (root / f"{name}.py").write_text("class SameTick:\n    def __init__(self):\n        self.clock_instance = None\n")
    os.utime(root, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # the directory looks unchanged
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(name)
        assert module.SameTick().clock_instance is not None, "a file of the same tick was not patched"
    finally:
        sys.modules.pop(name, None)


def test_a_relative_entry_follows_the_current_directory(tmp_path, monkeypatch):
    """(3) "" on sys.path means the current directory at each import: a change of directory without
    a change of sys.path must not leave an old answer."""
    root = tmp_path / "rel_root"
    root.mkdir()
    (root / "relmod.py").write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])
    autowired._remember_late_root(root)
    monkeypatch.setattr(sys, "path", [""] + [p for p in sys.path if p not in ("", ".")])
    monkeypatch.chdir(elsewhere)
    assert not autowired._maybe_project_name("relmod", None)
    monkeypatch.chdir(root)
    assert autowired._maybe_project_name("relmod", None), "an answer for the old directory was kept"


def test_a_project_module_found_by_a_mapping_finder_is_still_patched(tmp_path, monkeypatch):
    """An editable install may map a name to a project file through its own meta-path finder, with
    no sys.path entry that reaches the file. Such finders (not Python's standard ones) are asked as
    before; the path pre-check alone left the module unpatched."""
    import importlib.util

    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"mapped_root_{tag}"
    (root / "hidden").mkdir(parents=True)
    target = root / "hidden" / f"mapped_{tag}.py"
    target.write_text("class Mapped:\n    def __init__(self):\n        self.clock_instance = None\n")
    (root / f"clock_{tag}.py").write_text("class Clock:\n    pass\n")
    name = f"mapped_{tag}"

    class MappingFinder:
        def find_spec(self, fullname, path, target_module=None):
            if fullname == name:
                return importlib.util.spec_from_file_location(fullname, str(target))
            return None

    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    monkeypatch.setattr(sys, "meta_path", sys.meta_path + [MappingFinder()])
    try:
        clock = importlib.import_module(f"clock_{tag}")
        container = Container()
        container.register_singleton(clock.Clock)
        autowired.auto_inject(container, root_dir=str(root))
        module = importlib.import_module(name)
        assert module.Mapped().clock_instance is not None, "a module of a mapping finder was not patched"
    finally:
        for key in (name, f"clock_{tag}"):
            sys.modules.pop(key, None)


def test_a_relative_entry_above_a_root_names_the_root_package(tmp_path, monkeypatch):
    """"" on sys.path while the current directory is ABOVE a root: the root package itself is
    imported under its own name from there - a project name, decided by the current directory."""
    parent = tmp_path / "rel_parent"
    root = parent / "rel_app"
    root.mkdir(parents=True)
    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])
    autowired._remember_late_root(root)
    monkeypatch.setattr(sys, "path", [""] + [p for p in sys.path if p not in ("", ".")])
    monkeypatch.chdir(parent)
    assert autowired._maybe_project_name("rel_app", None), "the root package seen from '' was ruled out"


def test_a_finder_that_raises_never_rules_a_name_out(tmp_path, monkeypatch):
    """A broken finder or path hook is not the library's to judge: the name goes the hook's full
    way (as before the pre-check), never "not a project module" - that would leave it unpatched."""
    root = tmp_path / "broken_root"
    root.mkdir()

    class Broken:
        def find_spec(self, fullname, path, target=None):
            raise RuntimeError("broken finder")

    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])
    autowired._remember_late_root(root)
    monkeypatch.setattr(sys, "meta_path", sys.meta_path + [Broken()])
    assert autowired._maybe_project_name("anything_at_all", None), "a raising finder ruled the name out"


# ---- F3, targeted check 22: Python's finder for an entry decides, not the entry's spelling ----

def test_a_dot_entry_keeps_the_directory_python_froze_for_it(project, tmp_path, monkeypatch):
    """E1: Python makes its finder for "." with the current directory OF THAT MOMENT and keeps it
    after a change of directory. The pre-check read "." against the new directory, called the name
    "not a project module", and Python then loaded it from the root - unpatched."""
    root, tag, names = project
    _start(root, tag)
    sub = root / f"dotsub_{tag}"
    sub.mkdir()
    name = f"dotmod_{tag}"
    (sub / f"{name}.py").write_text("class Dotted:\n    def __init__(self):\n        self.clock_instance = None\n")
    elsewhere = tmp_path / "dot_elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sys, "path", ["."] + [p for p in sys.path if p not in ("", ".")])
    monkeypatch.setattr(sys, "path_importer_cache",
                        {key: value for key, value in sys.path_importer_cache.items() if key != "."})
    monkeypatch.chdir(sub)
    assert importlib.machinery.PathFinder.find_spec(name, ["."]) is not None  # Python's "." finder: sub
    monkeypatch.chdir(elsewhere)
    try:
        module = importlib.import_module(name)  # Python still finds it through that finder
        assert module.Dotted().clock_instance is not None, "a module found through '.' was not patched"
    finally:
        sys.modules.pop(name, None)


def test_a_parent_path_above_a_root_reaches_the_root_package(tmp_path, monkeypatch):
    """E2: a package whose __path__ is a directory ABOVE a root imports the root package as its
    submodule. The pre-check looked only for parent-path directories under a root."""
    import types

    tag = uuid.uuid4().hex[:8]
    parent = tmp_path / f"above_{tag}"
    root = parent / f"app_{tag}"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("class RootConsumer:\n    def __init__(self):\n        self.clock_instance = None\n")
    (root / f"clockmod_{tag}.py").write_text("class Clock:\n    pass\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    holder = types.ModuleType(f"holder_{tag}")
    holder.__path__ = [str(parent)]
    monkeypatch.setitem(sys.modules, f"holder_{tag}", holder)
    try:
        clock = importlib.import_module(f"clockmod_{tag}")
        container = Container()
        container.register_singleton(clock.Clock)
        autowired.auto_inject(container, root_dir=str(root))
        module = importlib.import_module(f"holder_{tag}.app_{tag}")
        assert module.RootConsumer().clock_instance is not None, "the root package under a parent path was not patched"
    finally:
        for key in [k for k in sys.modules if k.startswith((f"holder_{tag}.", f"clockmod_{tag}", f"app_{tag}"))]:
            sys.modules.pop(key, None)


def test_a_root_added_later_is_seen_by_the_pre_check(tmp_path, monkeypatch):
    """Where a finder's directory lies is remembered per set of roots: a root added later (a second
    auto_inject) must not meet the answer worked out before it."""
    first = tmp_path / "first_root"
    first.mkdir()
    later = tmp_path / "later_root"
    later.mkdir()
    (later / "latermod.py").write_text("")
    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])
    autowired._remember_late_root(first)
    monkeypatch.setattr(sys, "path", [str(later)] + sys.path)
    importlib.invalidate_caches()
    assert importlib.machinery.PathFinder.find_spec("latermod", [str(later)]) is not None  # its finder exists
    assert not autowired._maybe_project_name("latermod", None)
    autowired._remember_late_root(later)
    assert autowired._maybe_project_name("latermod", None), "a root added later was not seen"


def test_a_reload_found_only_with_its_target_is_still_patched(project, monkeypatch):
    """reload() passes the module being reloaded (``target``) to the finders, and a path finder may
    find a module only then. The pre-check searched each entry without the target, ruled the
    name out, and Python then reloaded the module from the root - unpatched (targeted check 23)."""
    import importlib.util

    root, tag, names = project
    _start(root, tag)
    hidden = root / f"hidden_{tag}"
    hidden.mkdir()
    name = f"tgt_{tag}"
    source = hidden / f"{name}.py"
    source.write_text("class Reloaded:\n    def __init__(self):\n        self.clock_instance = None\n")
    entry = f"target-only::{tag}"

    class TargetOnly:
        def find_spec(self, fullname, target=None):
            if fullname != name or target is None:
                return None
            return importlib.util.spec_from_file_location(fullname, str(source))

        def invalidate_caches(self):
            pass

    def hook(path):
        if path != entry:
            raise ImportError(path)
        return TargetOnly()

    monkeypatch.setattr(sys, "path_hooks", [hook] + sys.path_hooks)
    monkeypatch.setattr(sys, "path_importer_cache", dict(sys.path_importer_cache))
    monkeypatch.setattr(sys, "path", sys.path + [entry])
    spec = importlib.util.spec_from_file_location(name, str(source))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        module = importlib.reload(module)
        assert module.Reloaded().clock_instance is not None, "a module reloaded through its target was not patched"
    finally:
        sys.modules.pop(name, None)
