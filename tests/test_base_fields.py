"""0.5.4 review (base fields): a project class inherits a *_instance field from a base OUTSIDE the
project root - set in the base's __init__, in its class body, in a mixin method or another method
of the base. The class's field discovery only looked at the class's own members, so the field was
never a candidate: the class was not patched at all, a registered field stayed None and an
unregistered one said nothing (validation neither). Now the bases the library has not patched are
read with the same rule as the class's own members (only `self.` assignments in their methods);
the bases themselves are never patched."""

import logging
import sys
import textwrap
import uuid

import pytest

from py_autowired import AutowiringValidationError, Container, auto_inject, autowired


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(("bf_", "bfo_")):
            sys.modules.pop(name, None)


OUTSIDE = {
    "init": """
        class Base:
            def __init__(self):
                self.repo_instance = None
                self.missing_instance = None
    """,
    "body": """
        class Base:
            repo_instance = None
            missing_instance = None
    """,
    "mixin": """
        class Base:
            def setup(self):
                self.repo_instance = None
                self.missing_instance = None
    """,
    "method": """
        class Base:
            def connect(self):
                self.repo_instance = None
                self.missing_instance = None
    """,
    "annotation": """
        class Base:
            repo_instance: object = None
            missing_instance: object = None
    """,
    "captured": """
        class Base:
            def __init__(self):
                self.repo_instance = None
                self.missing_instance = None
                self.callback = lambda: self      # self is a cell: LOAD_DEREF, not LOAD_FAST
    """,
    "own_field_too": """
        class Base:
            def __init__(self):
                self.repo_instance = None
                self.missing_instance = None
    """,
}
INSIDE = {
    "init": "class Sub(Base):\n    pass\n",
    "body": "class Sub(Base):\n    pass\n",
    "mixin": "class Sub(Base):\n    def __init__(self):\n        self.setup()\n",
    "method": "class Sub(Base):\n    def __init__(self):\n        self.connect()\n",
    "annotation": "class Sub(Base):\n    pass\n",
    "captured": "class Sub(Base):\n    pass\n",
    "own_field_too": ("class Sub(Base):\n    def __init__(self):\n        super().__init__()\n"
                      "        self.other_instance = None\n"),
}


def _build(tmp_path, monkeypatch, case, validate):
    tag = uuid.uuid4().hex[:8]
    outside = tmp_path / f"out_{tag}"
    root = tmp_path / f"root_{tag}"
    outside.mkdir()
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (outside / f"bfo_{tag}.py").write_text(textwrap.dedent(OUTSIDE[case]))
    (root / f"bf_servis_{tag}.py").write_text("class Repo:\n    pass\n")
    (root / f"bf_ic_{tag}.py").write_text(f"from bfo_{tag} import Base\n\n\n" + INSIDE[case])
    monkeypatch.syspath_prepend(str(outside))
    monkeypatch.syspath_prepend(str(root))
    servis = __import__(f"bf_servis_{tag}")
    container = Container()
    container.register_singleton(servis.Repo)
    auto_inject(container, root_dir=str(root), validate=validate)
    return sys.modules[f"bf_ic_{tag}"], sys.modules[f"bfo_{tag}"], servis


@pytest.mark.parametrize("case", list(OUTSIDE))
def test_a_field_from_a_base_outside_the_project_is_filled_or_reported(case, tmp_path, monkeypatch, caplog, isolated):
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        inside, outside, servis = _build(tmp_path, monkeypatch, case, validate=False)
        obj = inside.Sub()
    assert isinstance(obj.repo_instance, servis.Repo), f"{case}: the registered field stayed None"
    assert obj.missing_instance is None
    lines = [r.getMessage() for r in caplog.records if "Sub.missing_instance" in r.getMessage()]
    assert lines, f"{case}: the unregistered field said nothing"
    assert "__ai_ioc_init__" not in outside.Base.__dict__, f"{case}: the base outside the project was patched"


@pytest.mark.parametrize("case", list(OUTSIDE))
def test_validation_names_an_unregistered_field_from_a_base_outside_the_project(case, tmp_path, monkeypatch, isolated):
    with pytest.raises(AutowiringValidationError, match="missing_instance"):
        _build(tmp_path, monkeypatch, case, validate=True)


def test_only_assignments_on_the_first_parameter_are_fields():
    class Outside:
        def setup(self, other, value):
            self.a_instance = None
            self.b_instance = value          # 3.13+: one paired load ('value', 'self')
            other.c_instance = None           # another object: not a field of this class
            self.d_instance = other.e_instance = None
            alias = self
            alias.f_instance = None           # through another name: not seen (Known limits)

        @staticmethod
        def make(target):
            target.g_instance = None          # round 20: counted - it may be given self (in doubt, reported)

        @classmethod
        def build(cls):
            cls.h_instance = None             # round 20: counted too

        @staticmethod
        def pair(first, second):
            second.k_instance = None          # round 20: any object - every assignment counts here

    assert autowired._self_stores(Outside.setup) == {"a_instance", "b_instance", "d_instance"}
    assert autowired._read_outside_base(Outside) == {"a_instance", "b_instance", "d_instance",
                                                     "g_instance", "h_instance", "k_instance"}


def test_a_project_base_is_not_read_again(tmp_path, monkeypatch, isolated):
    inside, _outside, _servis = _build(tmp_path, monkeypatch, "init", validate=False)
    assert autowired._in_project(inside.Sub)
    assert autowired._outside_base_fields(inside.Sub) == frozenset()


def test_an_outside_base_is_read_once(monkeypatch):
    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])  # this test file is outside every root here
    reads = []
    real = autowired._read_outside_base
    monkeypatch.setattr(autowired, "_read_outside_base", lambda base: reads.append(base) or real(base))

    class Outside:
        def __init__(self):
            self.repo_instance = None

    assert autowired._outside_base_fields(Outside) == {"repo_instance"}
    assert autowired._outside_base_fields(Outside) == {"repo_instance"}
    assert reads == [Outside]


def test_a_base_with_an_odd_key_is_a_warning_not_silence(monkeypatch):
    from py_autowired import container as container_module

    queued = []
    monkeypatch.setattr(container_module._BACKGROUND_LOG, "put", lambda *a, **k: queued.append(a))

    class Key(str):
        pass
    Odd2 = type("Odd2", (), {Key("weird"): 1})
    assert autowired._read_outside_base(Odd2) == frozenset()
    lines = [args[0] % args[1] for args in queued]
    assert any("Odd2" in m and "not a plain str" in m for m in lines), lines


def test_the_reason_for_an_unpatched_project_class_is_not_outside_the_root(tmp_path, monkeypatch, caplog, isolated):
    """KÜTÜPHANE's family scan: a registered PROJECT class with no field found was told 'outside the
    project root'. The reason now says what happened."""
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"root_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"bf_dyn_{tag}.py").write_text(textwrap.dedent("""
        class Dynamic:
            def __init__(self):
                setattr(self, "clock" + "_instance", None)   # a computed name: no candidate
    """))
    monkeypatch.syspath_prepend(str(root))
    container = Container()
    auto_inject(container, root_dir=str(root))
    module = sys.modules[f"bf_dyn_{tag}"]
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(module.Dynamic())
    lines = [r.getMessage() for r in caplog.records if "Dynamic.clock_instance" in r.getMessage()]
    assert lines and "in the project but was never patched" in lines[0], lines
    assert "outside the project root" not in lines[0]


def test_a_store_on_an_attribute_of_self_or_without_parameters_is_not_a_field():
    class Outside:
        def wire(self):
            self.inner.h_instance = None      # a field of self.inner, not of this class

        @staticmethod
        def helper():
            return None                        # no parameter at all

    assert autowired._read_outside_base(Outside) == frozenset()


def test_a_store_after_many_names_is_still_seen():
    """A name past the 256th needs an EXTENDED_ARG prefix before STORE_ATTR - for self it counts, for
    another object it does not (TB11, gate: the prefix must not reset the known owner)."""
    body = "".join(f"    self.n{i} = None\n" for i in range(300))
    namespace = {}
    exec("def setup(self, other):\n" + body + "    other.x_instance = None\n"
         "    self.repo_instance = None\n", namespace)
    assert autowired._self_stores(namespace["setup"]) == {"repo_instance"}


def test_reading_a_base_runs_no_code_of_its_members():
    asked = []

    class Member:
        def __getattr__(self, name):
            asked.append(name)
            raise AttributeError(name)

    Outside = type("Outside", (), {"thing": Member(), "repo_instance": None})
    assert autowired._read_outside_base(Outside) == {"repo_instance"}
    assert asked == []


def test_a_reused_id_never_returns_another_base_s_fields(monkeypatch):
    import weakref

    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])  # this test file is outside every root here

    class Other:
        pass

    class Outside:
        repo_instance = None

    monkeypatch.setitem(autowired._BASE_FIELDS, id(Outside), (weakref.ref(Other), frozenset({"wrong_instance"})))
    assert autowired._outside_base_fields(Outside) == {"repo_instance"}


def test_a_base_that_dies_leaves_no_entry():
    import gc

    def make():
        class Short:
            repo_instance = None
        autowired._outside_base_fields(Short)
        return id(Short)

    key = make()
    gc.collect()
    assert key not in autowired._BASE_FIELDS


def test_a_project_base_s_field_is_left_to_the_base_s_own_patch(tmp_path, monkeypatch, caplog, isolated):
    """TB3 (consensus): a base INSIDE the project is not read for its subclass - it is patched
    itself. Child stays unpatched, the base's patch fills the field, and validation still names the
    inherited unannotated field."""
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"root_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"bf_servis_{tag}.py").write_text("class Repo:\n    pass\n")
    (root / f"bf_proj_{tag}.py").write_text(
        "class Base:\n    repo_instance = None\n\n\nclass Child(Base):\n    pass\n")
    monkeypatch.syspath_prepend(str(root))
    servis = __import__(f"bf_servis_{tag}")
    container = Container()
    container.register_singleton(servis.Repo)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        auto_inject(container, root_dir=str(root), validate=True)
    module = sys.modules[f"bf_proj_{tag}"]
    assert module.Base.__dict__.get("__ai_ioc_init__") is True
    assert module.Child.__dict__["__di_attrs__"] == ()
    assert "__ai_ioc_init__" not in module.Child.__dict__
    assert isinstance(module.Child().repo_instance, servis.Repo)
    assert "Child.repo_instance: inherited unannotated field" in caplog.text


def test_an_unrecognised_bytecode_counts_every_assignment_and_says_so(monkeypatch):
    """Etki haritası 19, question 2: if a future Python's bytecode is not recognised, no field is
    lost silently - every assignment counts and one WARNING names the Python version."""
    from py_autowired import container as container_module

    queued = []
    monkeypatch.setattr(container_module._BACKGROUND_LOG, "put", lambda *a, **k: queued.append(a))
    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_STORE_PROBE", [])
    monkeypatch.setattr(autowired, "_owner_stores", lambda function: set())  # recognises nothing

    class Outside:
        def setup(self, other):
            self.a_instance = None
            other.c_instance = None

    assert autowired._self_stores(Outside.setup) == {"a_instance", "c_instance"}
    autowired._self_stores(Outside.setup)
    lines = [args[0] % args[1] for args in queued if "is not recognised" in args[0]]
    assert len(lines) == 1, lines


def test_this_python_s_bytecode_is_recognised():
    """The probe passes on every version the suite runs on (3.10, 3.11, 3.14, 3.14t)."""
    assert autowired._self_stores_recognised()


# ---- review round 19 ----

def test_a_store_on_a_captured_self_is_a_field():
    """Y+Z (round 19): `self` captured by a closure is loaded with LOAD_DEREF."""
    class Outside:
        def __init__(self):
            self.repo_instance = None
            self.callback = lambda: self

    assert autowired._self_stores(Outside.__init__) == {"repo_instance"}


def test_an_unknown_owner_shape_counts_the_store():
    """Y+Z (round 19): an owner the scan cannot name (a call result here) is counted - in doubt,
    a field is reported rather than lost; an owner it can name as another object is not."""
    class Outside:
        def wire(self, other):
            make().a_instance = None          # not recognised as another object: counted
            other.b_instance = None            # another parameter: not counted
            self.inner.c_instance = None       # an attribute of self: not counted

    assert autowired._self_stores(Outside.wire) == {"a_instance"}


def _shortcut_project(tmp_path, monkeypatch, base_body="    repo_instance = None\n"):
    tag = uuid.uuid4().hex[:8]
    outside = tmp_path / f"out_{tag}"
    root = tmp_path / f"root_{tag}"
    outside.mkdir()
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (outside / f"bfo_{tag}.py").write_text("class Base:\n" + base_body)
    (root / f"bf_servis_{tag}.py").write_text("class Repo:\n    pass\n\n\nclass Dep:\n    pass\n")
    (root / f"bf_ic_{tag}.py").write_text(
        f"from bfo_{tag} import Base\nfrom bf_servis_{tag} import Dep\n\n\n"
        "class Sub(Base):\n    def __init__(self, dep: Dep = None):\n        self.dep = dep\n\n\n"
        "class Consumer:\n    sub_instance = None\n")
    monkeypatch.syspath_prepend(str(outside))
    monkeypatch.syspath_prepend(str(root))
    return root, __import__(f"bf_servis_{tag}"), __import__(f"bf_ic_{tag}")


def test_a_constructor_dependency_is_still_resolved_with_a_base_field(tmp_path, monkeypatch, isolated):
    """Y (round 19): the base field made the class a 'field injection only' class, so its
    constructor `__init__(self, dep: Dep = None)` was no longer resolved - dep stayed None."""
    root, servis, inside = _shortcut_project(tmp_path, monkeypatch)
    container = Container()
    container.register_singleton(servis.Repo)
    container.register_singleton(servis.Dep)
    container.register_transient(inside.Sub)
    auto_inject(container, root_dir=str(root))
    sub = container.resolve(inside.Sub)
    assert isinstance(sub.dep, servis.Dep), "the constructor dependency stayed None"
    assert isinstance(sub.repo_instance, servis.Repo)


def test_validation_still_checks_the_constructor_with_a_base_field(tmp_path, monkeypatch, isolated):
    """Y (round 19): validation mirrors the same shortcut - a singleton capturing a scoped
    constructor dependency was no longer reported once a base field appeared."""
    root, servis, inside = _shortcut_project(tmp_path, monkeypatch)
    container = Container()
    container.register_singleton(servis.Repo)
    container.register_scoped(servis.Dep)
    container.register_singleton(inside.Sub)
    with pytest.raises(AutowiringValidationError, match="captures scoped"):
        auto_inject(container, root_dir=str(root), validate=True)


def test_a_base_s_values_are_let_go_after_the_library_lock(monkeypatch):
    """Y+Z (round 19): the copy of a base's dictionary could hold the last reference of a value
    another thread removed meanwhile; dropping it under the library lock ran its __del__ there."""
    import threading

    seen = []

    class Resource:
        def __del__(self):
            seen.append(autowired._GLOBAL_LOCK._owner == threading.get_ident())

    class Outside:
        resource = Resource()
        repo_instance = None

    real = autowired._plain_snapshot

    def snapshot(mapping):
        copy = real(mapping)
        if copy is not None and "resource" in copy and "resource" in Outside.__dict__:
            del Outside.resource  # "another thread", right after the copy
        return copy
    monkeypatch.setattr(autowired, "_plain_snapshot", snapshot)
    with autowired._GLOBAL_LOCK:
        assert autowired._read_outside_base(Outside) == {"repo_instance"}
        assert seen == [], "the value was dropped while the library lock was held"
    assert seen == [False], seen


def _direct_records_under_lock(monkeypatch):
    import threading

    from py_autowired import container as container_module

    queued, direct = [], []
    monkeypatch.setattr(container_module._BACKGROUND_LOG, "put", lambda *a, **k: queued.append(a))

    class Handler(logging.Handler):
        def emit(self, record):
            direct.append(autowired._GLOBAL_LOCK._owner == threading.get_ident())
    handler = Handler(logging.WARNING)
    logging.getLogger("py_autowired").addHandler(handler)
    return queued, direct, lambda: logging.getLogger("py_autowired").removeHandler(handler)


def test_the_unrecognised_bytecode_warning_is_not_written_under_the_lock(monkeypatch):
    """Y+Z (round 19): a log handler may take locks of its own - nothing is logged directly while
    the library lock is held; the warning goes to the background writer (not lost)."""
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_STORE_PROBE", [])
    monkeypatch.setattr(autowired, "_owner_stores", lambda function: set())
    queued, direct, done = _direct_records_under_lock(monkeypatch)
    try:
        def method(self):
            self.a_instance = None
        with autowired._GLOBAL_LOCK:
            autowired._self_stores(method)
    finally:
        done()
    assert True not in direct, "logged under the library lock"
    assert any("is not recognised" in (args[0] % args[1]) for args in queued), queued


def test_the_unreadable_base_warning_is_not_written_under_the_lock(monkeypatch):
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    queued, direct, done = _direct_records_under_lock(monkeypatch)

    class Key(str):
        pass
    Odd3 = type("Odd3", (), {Key("weird"): 1})
    try:
        with autowired._GLOBAL_LOCK:
            assert autowired._read_outside_base(Odd3) == frozenset()
    finally:
        done()
    assert True not in direct, "logged under the library lock"
    assert any("Odd3" in (args[0] % args[1]) for args in queued), queued


def test_the_module_type_check_runs_no_metaclass_eq(monkeypatch):
    """Y (round 19): `ModuleType not in mro` compared with == - a module type whose metaclass
    defines __eq__ ran it."""
    import types as types_module

    calls = []

    class Meta(type):
        def __eq__(cls, other):
            calls.append(other)
            return False
        __hash__ = type.__hash__

    class OddModule(types_module.ModuleType, metaclass=Meta):
        pass

    module = OddModule("bf_odd_module_" + uuid.uuid4().hex[:8])
    module.__file__ = "/nowhere/odd.py"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    Cls = type("Cls", (), {"__module__": module.__name__})
    autowired._in_project(Cls)
    assert calls == [], "the metaclass __eq__ ran"


def test_the_constructor_signature_is_read_once_per_class(monkeypatch):
    """Round 19 (speed): the no-argument check of a field-injection class is cached per class - a
    misplaced decorator once moved the cache to another function and every creation read the
    signature again (+70% per transient injected() call)."""
    from py_autowired import container as container_module

    reads = []
    real = container_module._init_signature
    monkeypatch.setattr(container_module, "_init_signature", lambda impl: reads.append(impl) or real(impl))
    container_module._accepts_no_arguments.cache_clear()

    class Repo:
        pass

    class Service:
        def __init__(self):
            self.repo_instance = None

    container = Container()
    container.register_singleton(Repo)
    container.register_transient(Service)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(Service, container, cache.get_raw_map())
    autowired._patch_init(Service, container, cache.get_raw_map())
    for _ in range(3):
        assert isinstance(container.resolve(Service).repo_instance, Repo)
    assert reads.count(Service) == 1, reads


def test_the_field_injection_measure_is_never_cached():
    from py_autowired import container as container_module

    class Service:
        __di_own_attrs__ = ("repo_instance",)

    assert container_module._field_injection_only(Service)
    Service.__di_own_attrs__ = ()
    assert not container_module._field_injection_only(Service), "a stale answer"


def test_a_store_on_another_captured_object_is_not_a_field():
    """Round 19 (TB mutants): a closure-captured parameter other than self is loaded with LOAD_DEREF
    too - it is a named other object, not counted."""
    class Outside:
        def wire(self, other):
            other.a_instance = None
            return lambda: other

    assert autowired._self_stores(Outside.wire) == set()


def test_patching_a_class_directly_notes_its_own_candidates(monkeypatch):
    """Round 19: _patch_init (used directly by late patching and tests) writes the class's own
    candidates too - a field only a base outside the project gives is not one of them."""
    from py_autowired import Container

    monkeypatch.setattr(autowired, "_LATE_ROOTS", [])  # this test file is outside every root here

    class Outside:
        repo_instance = None

    class Sub(Outside):
        def __init__(self):
            self.other_instance = None

    autowired._patch_init(Sub, Container(), {})
    assert Sub.__dict__["__di_attrs__"] == ("other_instance", "repo_instance")
    assert Sub.__dict__["__di_own_attrs__"] == ("other_instance",)


def test_the_probe_checks_the_captured_self_shape(monkeypatch):
    """T19m: a Python whose bytecode loses only the captured-self shape is not recognised either
    (every assignment then counts, with a WARNING)."""
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module._BACKGROUND_LOG, "put", lambda *a, **k: None)
    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_STORE_PROBE", [])
    real = autowired._owner_stores
    monkeypatch.setattr(autowired, "_owner_stores",
                        lambda function: set() if function.__name__ == "captured" else real(function))
    assert autowired._self_stores_recognised() is False



# ---- review round 20 ----

def test_a_base_annotation_does_not_make_the_class_field_injection_only(tmp_path, monkeypatch, isolated):
    """Y (round 20): annotations were collected across the whole MRO and counted as the class's OWN
    candidates - a base's annotation made `__init__(self, dep: Dep = None)` skip its dependency."""
    root, servis, inside = _shortcut_project(tmp_path, monkeypatch, base_body="    repo_instance: object = None\n")
    container = Container()
    container.register_singleton(servis.Repo)
    container.register_singleton(servis.Dep)
    container.register_transient(inside.Sub)
    auto_inject(container, root_dir=str(root))
    sub = container.resolve(inside.Sub)
    assert isinstance(sub.dep, servis.Dep), "the constructor dependency stayed None"
    assert isinstance(sub.repo_instance, servis.Repo)
    assert inside.Sub.__dict__["__di_own_attrs__"] == ()


def test_a_field_set_by_a_base_s_static_helper_is_filled(tmp_path, monkeypatch, isolated):
    """Z (round 20): a static helper of a base outside the project, given self, sets the field -
    leaving it out made the field silently None. Counted again (in doubt, reported)."""
    tag = uuid.uuid4().hex[:8]
    outside = tmp_path / f"out_{tag}"
    root = tmp_path / f"root_{tag}"
    outside.mkdir()
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (outside / f"bfo_{tag}.py").write_text(textwrap.dedent("""
        class Base:
            @staticmethod
            def prepare(target):
                target.repo_instance = None
    """))
    (root / f"bf_servis_{tag}.py").write_text("class Repo:\n    pass\n")
    (root / f"bf_ic_{tag}.py").write_text(
        f"from bfo_{tag} import Base\n\n\nclass Sub(Base):\n    def __init__(self):\n"
        "        Base.prepare(self)\n")
    monkeypatch.syspath_prepend(str(outside))
    monkeypatch.syspath_prepend(str(root))
    servis = __import__(f"bf_servis_{tag}")
    container = Container()
    container.register_singleton(servis.Repo)
    auto_inject(container, root_dir=str(root))
    inside = sys.modules[f"bf_ic_{tag}"]
    assert isinstance(inside.Sub().repo_instance, servis.Repo), "the field stayed None"


def test_an_odd_key_snapshot_drops_its_values_after_the_library_lock():
    """Y (round 20): _plain_snapshot gave up on a key that is not a plain str and dropped its list of
    items - the last reference of a value - while the library lock was held."""
    import threading

    seen = []

    class Resource:
        def __del__(self):
            seen.append(autowired._GLOBAL_LOCK._owner == threading.get_ident())

    class Key(str):
        pass

    class Mapping:
        def items(self):
            return [(Key("odd"), Resource())]  # the list holds the only reference

    with autowired._GLOBAL_LOCK:
        assert autowired._plain_snapshot(Mapping()) is None
        assert seen == [], "the value was dropped while the library lock was held"
    assert seen == [False], seen


def test_a_base_s_annotation_is_still_a_candidate_of_the_subclass(tmp_path, monkeypatch, isolated):
    """R20b: a base's annotation is no OWN candidate any more, but it is still a candidate - a
    subclass with its own __init__ (no super call) gets the field only from it."""
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"root_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"bf_servis_{tag}.py").write_text("class Repo:\n    pass\n")
    (root / f"bf_proj_{tag}.py").write_text(textwrap.dedent(f"""
        from bf_servis_{tag} import Repo


        class Base:
            repo_instance: Repo = None


        class Sub(Base):
            def __init__(self):
                pass
    """))
    monkeypatch.syspath_prepend(str(root))
    servis = __import__(f"bf_servis_{tag}")
    container = Container()
    container.register_singleton(servis.Repo)
    auto_inject(container, root_dir=str(root))
    module = sys.modules[f"bf_proj_{tag}"]
    assert isinstance(module.Sub().repo_instance, servis.Repo), "the field stayed None"
    assert module.Sub.__dict__["__di_own_attrs__"] == ()



def test_a_plain_function_without_parameters_in_a_base_is_read_without_error():
    """TB14 (gate): a plain function without any parameter in the body of a base outside the project
    (not a static method) has no first parameter - it gives no field and never breaks the read."""
    class Outside:
        def helper():  # noqa: N805 - no parameter on purpose
            return 1

        repo_instance = None

    assert autowired._read_outside_base(Outside) == {"repo_instance"}


def test_the_bytecode_probe_runs_once_per_process(monkeypatch):
    """KP3 (gate): the bytecode check runs once per process; every later read reuses its answer
    (a check on every call made patching about 150 times slower)."""
    monkeypatch.setattr(autowired, "_STORE_PROBE", [])
    probes = []
    real = autowired._owner_stores

    def counting(function):
        if function.__name__ in ("probe", "captured"):
            probes.append(function.__name__)
        return real(function)
    monkeypatch.setattr(autowired, "_owner_stores", counting)

    class Outside:
        def a(self):
            self.a_instance = None

    for _ in range(3):
        assert autowired._self_stores(Outside.a) == {"a_instance"}
    assert probes == ["probe", "captured"], probes
