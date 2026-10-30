"""Tree-sitter test-change features: one JS/TS fixture per pattern, plus negative cases."""
# ruff: noqa: E501

from __future__ import annotations

from testing_expert.features._types import ChangedTestFile, FeatureDict
from testing_expert.features.test_changes import (
    FEATURE_NAMES,
    parse_hunks,
    what_changed_in_tests,
)


def added(code: str, path: str = "test/a.test.js") -> ChangedTestFile:
    """A patch where every line of `code` is new."""
    body = "\n".join("+" + line for line in code.strip("\n").split("\n"))
    n = code.strip("\n").count("\n") + 1
    return ChangedTestFile(path, f"@@ -0,0 +1,{n} @@\n{body}")


def feats(code: str, path: str = "test/a.test.js") -> FeatureDict:
    return what_changed_in_tests([added(code, path)])


def test_names_match_output() -> None:
    assert tuple(feats("it('a', () => {});")) == FEATURE_NAMES
    assert all(n.startswith("tw_") for n in FEATURE_NAMES)


def test_no_test_files_gives_zeros() -> None:
    assert set(what_changed_in_tests([]).values()) == {0}


def test_unusable_patches_give_unknown() -> None:
    row = what_changed_in_tests(
        [ChangedTestFile("a.test.js", None), ChangedTestFile("b.test.json", "@@ -1 +1 @@\n+{}")]
    )
    assert set(row.values()) == {None}


def test_parse_hunks_tracks_added_rows_and_ignores_old_side() -> None:
    patch = (
        "@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n ctx2\n\\ No newline at end of file\n@@ -9 +9 @@\n+x"
    )
    h = parse_hunks(patch)
    assert h[0].text == "ctx\nnew\nctx2" and h[0].added_rows == {1}
    assert h[1].text == "x" and h[1].added_rows == {0}


def test_new_and_async_tests() -> None:
    row = feats(
        "it('a', async () => { await x(); });\ntest('b', () => {});\nit('c', async function () {});"
    )
    assert row["tw_new_tests"] == 3 and row["tw_new_async_tests"] == 2
    assert row["tw_test_files_parsed"] == 1


def test_context_lines_do_not_count() -> None:
    patch = "@@ -1,2 +1,3 @@\n it('old', async () => {});\n+it('new', () => {});\n it('old2', async () => {});"
    row = what_changed_in_tests([ChangedTestFile("a.test.js", patch)])
    assert row["tw_new_tests"] == 1 and row["tw_new_async_tests"] == 0


def test_unawaited_promises_positive_and_negative() -> None:
    bad = """
it('a', async () => {
  fetchData().then(x => x);
  expect(p).resolves.toBe(1);
  waitFor(() => {});
  new Promise(r => r());
  doThingAsync();
  screen.findByText('x');
});
"""
    assert feats(bad)["tw_unawaited_promises"] == 6
    good = """
it('a', async () => {
  await fetchData().then(x => x);
  await expect(p).resolves.toBe(1);
  return foo.then(x => x);
  const v = bar.catch(() => 1);
  plain();
});
"""
    assert feats(good)["tw_unawaited_promises"] == 0


def test_timers() -> None:
    row = feats(
        "setTimeout(() => {}, 10);\nsetInterval(f, 5);\njest.useRealTimers();\nvi.useRealTimers();"
    )
    assert row["tw_set_timeout"] == 2 and row["tw_real_timers"] == 2
    assert feats("jest.useFakeTimers();\nmyTimeout(1);")["tw_set_timeout"] == 0


def test_date_and_random() -> None:
    row = feats(
        "const a = Date.now();\nconst b = new Date();\nconst c = new Date(0);\nconst d = Math.random();\nMath.floor(1);"
    )
    assert row["tw_date_now"] == 2 and row["tw_math_random"] == 1


def test_network_unmocked_vs_mocked() -> None:
    code = "it('a', async () => {\n  await fetch('/x');\n  await axios.get('/y');\n  http.request(o);\n});"
    assert feats(code)["tw_unmocked_network"] == 3
    mocked = "jest.mock('axios');\n" + code
    assert feats(mocked)["tw_unmocked_network"] == 0
    assert feats("nock('http://a').get('/').reply(200);\n" + code)["tw_unmocked_network"] == 0
    assert feats("const x = prefetch(1);\nfoo.get('a');")["tw_unmocked_network"] == 0


def test_timeout_changes() -> None:
    code = """
jest.setTimeout(30000);
describe('s', function () {
  this.timeout(5000);
  it('a', () => {}, 10000);
  it('b', () => {}, { timeout: 1000 });
  it('c', () => {});
});
"""
    assert feats(code)["tw_timeout_changes"] == 4
    assert feats("it('a', () => {}, someVar);")["tw_timeout_changes"] == 0


def test_only_and_skip() -> None:
    code = "it.only('a', f);\ndescribe.only('b', f);\nit.skip('c', f);\nxit('d', f);\nxdescribe('e', f);\nfit('g', f);\nit('h', f);"
    row = feats(code)
    assert row["tw_only_added"] == 3 and row["tw_skip_added"] == 3
    assert feats("it('only', f);\nfoo.only();")["tw_only_added"] == 0


def test_file_level_state() -> None:
    code = "let counter = 0;\nvar x;\nconst cache = {};\nconst list = [];\nconst m = new Map();\nconst K = 5;\nexport let shared = 1;\ndescribe('s', () => {\n  let inner = 1;\n});"
    assert feats(code)["tw_file_level_state"] == 6


def test_missing_cleanup() -> None:
    setup = "beforeEach(() => {\n  jest.spyOn(console, 'log');\n  process.env.X = '1';\n  server.listen(3000);\n});"
    assert feats(setup)["tw_missing_cleanup"] == 3
    assert (
        feats(setup + "\nafterEach(() => { jest.restoreAllMocks(); });")["tw_missing_cleanup"] == 0
    )
    assert feats("it('a', () => { expect(1).toBe(1); });")["tw_missing_cleanup"] == 0


def test_typescript_and_tsx_parse() -> None:
    ts = "it('a', async (): Promise<void> => {\n  const x: number = Date.now();\n});"
    row = feats(ts, "src/a.spec.ts")
    assert row["tw_new_async_tests"] == 1 and row["tw_date_now"] == 1
    tsx = "it('r', async () => { render(<App />); });"
    assert feats(tsx, "src/App.test.tsx")["tw_new_async_tests"] == 1


def test_sums_over_files_and_partial_coverage() -> None:
    row = what_changed_in_tests(
        [
            added("it('a', () => {});", "a.test.js"),
            added("it('b', () => {});", "b.test.js"),
            ChangedTestFile("c.test.js", None),
        ]
    )
    assert row["tw_new_tests"] == 2 and row["tw_test_files_parsed"] == 2


def test_garbage_patch_does_not_raise() -> None:
    row = what_changed_in_tests([ChangedTestFile("a.test.js", "@@ -1 +1 @@\n+}}}} ((( it(")])
    assert row["tw_test_files_parsed"] == 1
