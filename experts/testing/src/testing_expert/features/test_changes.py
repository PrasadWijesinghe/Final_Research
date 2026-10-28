"""What changed in tests: tree-sitter over added lines of changed test files (test.md section 4).

Pure code: plain strings in, a dict of `tw_*` features out. The `raw` tables hold only each file's
patch (unified diff), not its content before and after the commit, so every hunk is rebuilt as the
"after" text (context lines plus added lines), parsed on its own, and a pattern counts only when it
starts on an ADDED line. Counts are constructs added by the commit, never constructs already there.

DEFAULT: best-effort fragment parsing. A hunk can start in the middle of a block; tree-sitter
recovers from that, so counts can miss or add a construct at hunk edges. Chosen because no
before/after file contents exist for historical runs and no new collection is allowed.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_javascript as ts_js
import tree_sitter_typescript as ts_ts
from tree_sitter import Language, Node, Parser

from ._types import ChangedTestFile, FeatureDict

FEATURE_NAMES = (
    "tw_test_files_parsed",
    "tw_new_tests",
    "tw_new_async_tests",
    "tw_unawaited_promises",
    "tw_set_timeout",
    "tw_real_timers",
    "tw_date_now",
    "tw_math_random",
    "tw_unmocked_network",
    "tw_timeout_changes",
    "tw_only_added",
    "tw_skip_added",
    "tw_file_level_state",
    "tw_missing_cleanup",
)

# DEFAULT: patches over this size are skipped (generated snapshots, bundles); keeps parsing fast.
MAX_PATCH_CHARS = 200_000
CODE_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TEST_FN = re.compile(r"^(it|test|specify)$")
_SUITE_OBJ = {"it", "test", "describe", "context", "suite", "specify"}
_SKIP_IDS = {"xit", "xdescribe", "xtest", "xcontext", "xspecify"}
_ONLY_IDS = {"fit", "fdescribe"}
_TIMER_FNS = {"setTimeout", "setInterval", "window.setTimeout", "global.setTimeout"}
_NETWORK_FNS = {"fetch", "window.fetch", "global.fetch", "globalThis.fetch", "axios", "got"}
_NETWORK_OBJS = {"axios", "got", "http", "https", "superagent"}
_NETWORK_METHODS = {"get", "post", "put", "patch", "delete", "request", "head", "fetch"}
_AFTER_HOOK = re.compile(r"\bafter(Each|All)?\s*\(")
_MOCK_MARK = re.compile(
    r"\b(jest|vi)\.(mock|doMock|spyOn)\(|\bnock\(|\bmsw\b|setupServer|fetchMock|fetch-mock"
    r"|sinon\.(stub|replace)|MockAdapter|MockAgent|\bglobal(This)?\.fetch\s*=|mockResolvedValue"
)
_SETUP_FNS = {"setInterval", "createServer", "addEventListener"}
_SETUP_METHODS = {"listen", "spyOn", "useFakeTimers", "addEventListener", "createServer"}
_ASYNC_NAMES = {"waitFor", "waitForElementToBeRemoved", "fetch"}


@dataclass(frozen=True, slots=True)
class _Hunk:
    text: str
    added_rows: frozenset[int]


@lru_cache(maxsize=1)
def _parsers() -> dict[str, Parser]:
    ts = Language(ts_ts.language_typescript())
    tsx = Language(ts_ts.language_tsx())
    js = Language(ts_js.language())
    return {"ts": Parser(ts), "tsx": Parser(tsx), "js": Parser(js)}


def _parser_for(path: str) -> Parser:
    p = path.lower()
    if p.endswith((".tsx",)):
        return _parsers()["tsx"]
    if p.endswith((".ts", ".mts", ".cts")):
        return _parsers()["ts"]
    return _parsers()["js"]


def is_code_path(path: str) -> bool:
    """True for JavaScript or TypeScript files (the only ones we parse)."""
    return path.lower().endswith(CODE_SUFFIXES)


def parse_hunks(patch: str) -> list[_Hunk]:
    """Split a unified diff into hunks of new-side text, remembering which rows were added."""
    hunks: list[_Hunk] = []
    lines: list[str] = []
    added: set[int] = set()

    def flush() -> None:
        if lines:
            hunks.append(_Hunk("\n".join(lines), frozenset(added)))
        lines.clear()
        added.clear()

    for raw in patch.split("\n"):
        if _HUNK.match(raw):
            flush()
        elif raw.startswith("+"):
            added.add(len(lines))
            lines.append(raw[1:])
        elif raw.startswith(" "):
            lines.append(raw[1:])
        # "-" lines are the old side; "\ No newline" markers are ignored
    flush()
    return hunks


def _kids(n: Node, named: bool = False) -> list[Node]:
    """Children by index (cheap, and independent of the `.children` list API)."""
    if named:
        return [c for i in range(n.named_child_count) if (c := n.named_child(i)) is not None]
    return [c for i in range(n.child_count) if (c := n.child(i)) is not None]


def _walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(_kids(n)))


_ctx = threading.local()  # the bytes being parsed; node text is sliced from it (see _text)
_MAX_TEXT = 2_000


def _text(n: Node | None) -> str:
    """Source text of a node, sliced from our own buffer and capped for long call chains."""
    if n is None:
        return ""
    end = min(n.end_byte, n.start_byte + _MAX_TEXT)
    return _ctx.src[n.start_byte : end].decode("utf-8", "replace")


def _callee(call: Node) -> str:
    """Callee text without whitespace, e.g. `expect(x).resolves.toBe` stays whole."""
    return re.sub(r"\s+", "", _text(call.child_by_field_name("function")))


def _args(call: Node) -> list[Node]:
    a = call.child_by_field_name("arguments")
    return _kids(a, True) if a is not None else []


def _is_async_fn(n: Node) -> bool:
    return n.type in {"arrow_function", "function_expression", "function"} and any(
        c.type == "async" for c in _kids(n)
    )


def _is_func(n: Node) -> bool:
    return n.type in {"arrow_function", "function_expression", "function", "generator_function"}


def _is_test_call(call: Node) -> bool:
    return bool(_TEST_FN.match(_callee(call).split(".")[0])) and any(
        _is_func(a) for a in _args(call)
    )


def _is_timeout_arg(arg: Node) -> bool:
    if arg.type == "number":
        return True
    if arg.type == "object":
        return any(
            c.type == "pair" and _text(c.child_by_field_name("key")).strip("'\"") == "timeout"
            for c in _kids(arg, True)
        )
    return False


def _is_timeout_call(call: Node, fn: str) -> bool:
    if fn in {"jest.setTimeout", "vi.setConfig", "vi.setTimeout", "test.setTimeout"}:
        return True
    if fn.endswith(".timeout") and fn != "timeout":  # mocha: this.timeout(n), it(...).timeout(n)
        return True
    args = _args(call)
    return bool(_TEST_FN.match(fn.split(".")[0]) and len(args) >= 3 and _is_timeout_arg(args[2]))


def _is_network_call(fn: str) -> bool:
    if fn in _NETWORK_FNS:
        return True
    head, _, tail = fn.rpartition(".")
    return head in _NETWORK_OBJS and tail in _NETWORK_METHODS


def _is_floating_promise(stmt: Node) -> bool:
    """An expression statement that starts a promise and neither awaits nor returns it."""
    expr = _kids(stmt, True)[0] if _kids(stmt, True) else None
    if expr is None:
        return False
    if expr.type == "new_expression":
        return _text(expr.child_by_field_name("constructor")) == "Promise"
    if expr.type != "call_expression":
        return False
    fn = _callee(expr)
    last = fn.rsplit(".", 1)[-1]
    return (
        last in {"then", "catch", "finally"}
        or ".resolves." in fn + "."
        or ".rejects." in fn + "."
        or last in _ASYNC_NAMES
        or (last.startswith("find") and last[4:5].isupper())
        or last.endswith("Async")
    )


def _is_setup(call: Node, fn: str) -> bool:
    last = fn.rsplit(".", 1)[-1]
    return fn in _SETUP_FNS or (("." in fn) and last in _SETUP_METHODS) or last == "useFakeTimers"


def _is_env_or_global_write(n: Node) -> bool:
    if n.type != "assignment_expression":
        return False
    left = re.sub(r"\s+", "", _text(n.child_by_field_name("left")))
    return left.startswith(("process.env.", "global.", "globalThis.", "process.env["))


def _count_hunk(hunk: _Hunk, parser: Parser, mocked: bool, tally: dict[str, int]) -> int:
    """Add this hunk's added constructs to `tally`; return how many cleanup-needing setups."""
    # offsets from tree-sitter are bytes offsets into `src`
    src = hunk.text.encode("utf-8")
    _ctx.src = src
    tree = parser.parse(src)
    setups = 0
    for n in _walk(tree.root_node):
        if n.start_point.row not in hunk.added_rows:
            continue
        t = n.type
        if t == "call_expression":
            fn = _callee(n)
            if _is_test_call(n):
                tally["tw_new_tests"] += 1
                if any(_is_async_fn(a) for a in _args(n)):
                    tally["tw_new_async_tests"] += 1
            if fn in _TIMER_FNS:
                tally["tw_set_timeout"] += 1
            if fn in {"jest.useRealTimers", "vi.useRealTimers"}:
                tally["tw_real_timers"] += 1
            if fn == "Date.now":
                tally["tw_date_now"] += 1
            if fn == "Math.random":
                tally["tw_math_random"] += 1
            if not mocked and _is_network_call(fn):
                tally["tw_unmocked_network"] += 1
            if _is_timeout_call(n, fn):
                tally["tw_timeout_changes"] += 1
            if fn in _ONLY_IDS:
                tally["tw_only_added"] += 1
            if fn in _SKIP_IDS:
                tally["tw_skip_added"] += 1
            if _is_setup(n, fn):
                setups += 1
        elif t == "new_expression":
            ctor = _text(n.child_by_field_name("constructor"))
            args = n.child_by_field_name("arguments")
            if ctor == "Date" and (args is None or not _kids(args, True)):
                tally["tw_date_now"] += 1
            if ctor == "XMLHttpRequest" and not mocked:
                tally["tw_unmocked_network"] += 1
        elif t == "member_expression":
            prop = _text(n.child_by_field_name("property"))
            obj = _text(n.child_by_field_name("object"))
            if obj in _SUITE_OBJ and prop == "only":
                tally["tw_only_added"] += 1
            elif obj in _SUITE_OBJ and prop == "skip":
                tally["tw_skip_added"] += 1
        elif t == "expression_statement":
            if _is_floating_promise(n):
                tally["tw_unawaited_promises"] += 1
        elif t == "assignment_expression" and _is_env_or_global_write(n):
            setups += 1
    for n in _kids(tree.root_node, True):  # file level = program child starting in column 0
        if n.start_point.row not in hunk.added_rows or n.start_point.column != 0:
            continue
        decl = n.child_by_field_name("declaration") if n.type == "export_statement" else n
        if decl is not None and _is_shared_state(decl):
            tally["tw_file_level_state"] += 1
    return setups


def _is_shared_state(decl: Node) -> bool:
    if decl.type == "variable_declaration":  # var
        return True
    if decl.type != "lexical_declaration":
        return False
    if any(c.type == "let" for c in _kids(decl)):
        return True
    # const only counts when the value can be mutated by tests: object, array, Map, Set
    for d in _kids(decl, True):
        v = d.child_by_field_name("value")
        if v is not None and (
            v.type in {"object", "array"}
            or (
                v.type == "new_expression"
                and _text(v.child_by_field_name("constructor"))
                in {"Map", "Set", "Array", "WeakMap", "WeakSet"}
            )
        ):
            return True
    return False


def what_changed_in_tests(patches: Sequence[ChangedTestFile]) -> FeatureDict:
    """Counts of risky constructs ADDED in the changed test files of one commit.

    `patches` are the commit's changed test files. Files with no usable patch (missing, truncated by
    GitHub, over the size cap, not JS or TS) are not parsed. When test files changed but none could
    be parsed every feature is None (unknown); a commit with no test file at all gets zeros.
    """
    if not patches:
        return dict.fromkeys(FEATURE_NAMES, 0)
    tally = dict.fromkeys(FEATURE_NAMES, 0)
    parsed = 0
    for p in sorted(patches, key=lambda x: x.path):
        if not p.patch or len(p.patch) > MAX_PATCH_CHARS or not is_code_path(p.path):
            continue
        hunks = parse_hunks(p.patch)
        if not hunks:
            continue
        parsed += 1
        new_side = "\n".join(h.text for h in hunks)
        mocked = bool(_MOCK_MARK.search(new_side))
        parser = _parser_for(p.path)
        setups = sum(_count_hunk(h, parser, mocked, tally) for h in hunks)
        # DEFAULT: a hook outside the hunk is invisible, so "no after* hook in the patch text"
        # is treated as missing cleanup. Over-counts a little; the model sees the same bias.
        if setups and not _AFTER_HOOK.search(new_side):
            tally["tw_missing_cleanup"] += setups
    if parsed == 0:
        return dict.fromkeys(FEATURE_NAMES, None)
    tally["tw_test_files_parsed"] = parsed
    return dict(tally)
