from __future__ import annotations

import os
from collections import defaultdict
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, TypedDict

import pytest
from anndata import AnnData

# CLI is locally not imported by default but on travis it is?
import scanpy.cli
from scanpy._utils import _import_name, descend_classes_and_funcs

mod_dir = Path(scanpy.__file__).parent
proj_dir = mod_dir.parent


api_module_names = [
    "sc",
    "sc.pp",
    "sc.tl",
    "sc.pl",
    "sc.experimental.pp",
    "sc.external.pp",
    "sc.external.tl",
    "sc.external.pl",
    "sc.external.exporting",
    "sc.get",
    "sc.logging",
    # "sc.neighbors",  # Not documented
    "sc.datasets",
    "sc.queries",
    "sc.metrics",
]
api_modules = {
    mod_name: _import_name(f"scanpy{mod_name.removeprefix('sc')}")
    for mod_name in api_module_names
}


api_functions = [
    pytest.param(func, f"{mod_name}.{name}", id=f"{mod_name}.{name}")
    for mod_name, mod in api_modules.items()
    for name in sorted(mod.__all__)
    if callable(func := getattr(mod, name))
]


@pytest.fixture
def in_project_dir():
    wd_orig = Path.cwd()
    os.chdir(proj_dir)
    try:
        yield proj_dir
    finally:
        os.chdir(wd_orig)


def test_descend_classes_and_funcs():
    # TODO: unclear if we want this to totally match, let’s see
    funcs = set(descend_classes_and_funcs(scanpy, "scanpy"))
    assert {p.values[0] for p in api_functions} == funcs


@pytest.mark.parametrize(("f", "qualname"), api_functions)
def test_function_headers(f, qualname):
    filename = getsourcefile(f)
    lines, lineno = getsourcelines(f)
    if f.__doc__ is None:
        msg = f"Function `{qualname}` has no docstring"
        text = lines[0]
    else:
        lines = getattr(f, "__orig_doc__", f.__doc__).split("\n")
        broken = [
            i for i, l in enumerate(lines) if l.strip() and not l.startswith("    ")
        ]
        if not any(broken):
            return
        msg = f'''\
Header of function `{qualname}`’s docstring should start with one-line description
and be consistently indented like this:

␣␣␣␣"""\\
␣␣␣␣My one-line␣description.

␣␣␣␣…
␣␣␣␣"""

The displayed line is under-indented.
'''
        text = f">{lines[broken[0]]}<"
    raise SyntaxError(msg, (filename, lineno, 2, text))


def param_is_pos(p: Parameter) -> bool:
    return p.kind in {
        Parameter.POSITIONAL_ONLY,
        Parameter.POSITIONAL_OR_KEYWORD,
    }


@pytest.mark.parametrize(("f", "qualname"), api_functions)
def test_function_positional_args(f, qualname):
    """See https://github.com/astral-sh/ruff/issues/3269#issuecomment-1772632200"""
    sig = signature(f)
    n_pos = sum(1 for p in sig.parameters.values() if param_is_pos(p))
    n_pos_max = 5
    if n_pos <= n_pos_max:
        return

    msg = (
        f"Function `{qualname}` has too many positional arguments ({n_pos}>{n_pos_max})"
    )
    filename = getsourcefile(f)
    lines, lineno = getsourcelines(f)
    text = lines[0]
    raise SyntaxError(msg, (filename, lineno, 1, text))


class ExpectedSig(TypedDict):
    first_name: str
    copy_default: Any
    return_ann: str | None


expected_sigs: defaultdict[str, ExpectedSig | None] = defaultdict(
    lambda: ExpectedSig(first_name="adata", copy_default=False, return_ann=None)
)
# full exceptions
expected_sigs["sc.external.tl.phenograph"] = None  # external
expected_sigs["sc.pp.filter_genes_dispersion"] = None  # deprecated
expected_sigs["sc.pp.filter_cells"] = None  # unclear `inplace` situation
expected_sigs["sc.pp.filter_genes"] = None  # unclear `inplace` situation
expected_sigs["sc.pp.subsample"] = None  # returns indices along matrix
# partial exceptions: “data” instead of “adata”
expected_sigs["sc.pp.log1p"]["first_name"] = "data"
expected_sigs["sc.pp.normalize_per_cell"]["first_name"] = "data"
expected_sigs["sc.pp.pca"]["first_name"] = "data"
expected_sigs["sc.pp.scale"]["first_name"] = "data"
expected_sigs["sc.pp.sqrt"]["first_name"] = "data"
# other partial exceptions
expected_sigs["sc.pp.normalize_total"]["return_ann"] = expected_sigs[
    "sc.experimental.pp.normalize_pearson_residuals"
]["return_ann"] = "AnnData | dict[str, np.ndarray] | None"
expected_sigs["sc.external.pp.magic"]["copy_default"] = None


@pytest.mark.parametrize(
    ("f", "qualname"),
    [f for f in api_functions if f.values[0].__module__.startswith("scanpy.")],
)
def test_sig_conventions(f, qualname):
    sig = signature(f)

    first_param = next(iter(sig.parameters.values()), None)
    if first_param is None:
        return

    if first_param.name == "adata":
        assert first_param.annotation in {"AnnData", AnnData}
    elif first_param.name == "data":
        assert first_param.annotation.startswith("AnnData |")
    elif first_param.name in {"filename", "path"}:
        assert first_param.annotation == "Path | str"

    # Test if functions with `copy` follow conventions
    if (copy_param := sig.parameters.get("copy")) is not None and (
        expected_sig := expected_sigs[qualname]
    ) is not None:
        s = ExpectedSig(
            first_name=first_param.name,
            copy_default=copy_param.default,
            return_ann=sig.return_annotation,
        )
        expected_sig = expected_sig.copy()
        if expected_sig["return_ann"] is None:
            expected_sig["return_ann"] = f"{first_param.annotation} | None"
        assert s == expected_sig


def getsourcefile(obj):
    """inspect.getsourcefile, but supports singledispatch"""
    from inspect import getsourcefile

    if wrapped := getattr(obj, "__wrapped__", None):
        return getsourcefile(wrapped)

    return getsourcefile(obj)


def getsourcelines(obj):
    """inspect.getsourcelines, but supports singledispatch"""
    from inspect import getsourcelines

    if wrapped := getattr(obj, "__wrapped__", None):
        return getsourcelines(wrapped)

    return getsourcelines(obj)
