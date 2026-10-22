import pytest
from features.file_tagger import RawFile, load_tagger, tag_files, tags_for

from shared.contracts import FileTag

T = FileTag

TABLE: list[tuple[str, set[FileTag]]] = [
    ("src/a.test.ts", {T.TEST}),
    ("src/a.spec.js", {T.TEST}),
    ("src/__tests__/a.ts", {T.TEST}),
    ("test/helper.js", {T.TEST}),
    ("tests/x.py", {T.TEST}),
    ("e2e/login.ts", {T.TEST}),
    ("jest.config.js", {T.TEST_CONFIG, T.SOURCE}),
    ("packages/a/vitest.config.ts", {T.TEST_CONFIG, T.SOURCE}),
    (".mocharc.json", {T.TEST_CONFIG}),
    ("playwright.config.ts", {T.TEST_CONFIG, T.SOURCE}),
    ("src/index.ts", {T.SOURCE}),
    ("src/a.jsx", {T.SOURCE}),
    ("src/a.tsx", {T.SOURCE}),
    ("src/a.mjs", {T.SOURCE}),
    ("src/a.cjs", {T.SOURCE}),
    ("package.json", {T.PACKAGE_MANIFEST}),
    ("pkg/package.json", {T.PACKAGE_MANIFEST}),
    ("package-lock.json", {T.LOCKFILE}),
    ("yarn.lock", {T.LOCKFILE}),
    ("pnpm-lock.yaml", {T.LOCKFILE}),
    ("tsconfig.json", {T.BUILD_CONFIG}),
    ("tsconfig.build.json", {T.BUILD_CONFIG}),
    ("babel.config.js", {T.BUILD_CONFIG, T.SOURCE}),
    (".babelrc", {T.BUILD_CONFIG}),
    ("eslint.config.js", {T.LINT_CONFIG, T.SOURCE}),
    (".eslintrc.json", {T.LINT_CONFIG}),
    (".prettierrc", {T.LINT_CONFIG}),
    ("webpack.config.js", {T.BUNDLER_CONFIG, T.SOURCE}),
    ("vite.config.ts", {T.BUNDLER_CONFIG, T.SOURCE}),
    ("rollup.config.mjs", {T.BUNDLER_CONFIG, T.SOURCE}),
    ("esbuild.js", {T.BUNDLER_CONFIG, T.SOURCE}),
    (".github/workflows/ci.yml", {T.WORKFLOW}),
    (".github/workflows/ci.yaml", {T.WORKFLOW}),
    ("Dockerfile", {T.DOCKERFILE}),
    ("app/Dockerfile.dev", {T.DOCKERFILE}),
    ("docker-compose.yml", {T.DOCKERFILE}),
    (".dockerignore", {T.DOCKERFILE}),
    ("vercel.json", {T.DEPLOY_CONFIG}),
    ("netlify.toml", {T.DEPLOY_CONFIG}),
    ("fly.toml", {T.DEPLOY_CONFIG}),
    ("k8s/deploy.yaml", {T.DEPLOY_CONFIG}),
    ("helm/chart/values.yaml", {T.DEPLOY_CONFIG}),
    ("README.md", {T.DOCS}),
    ("docs/guide.txt", {T.DOCS}),
    ("src/notes.md", {T.DOCS}),
    ("image.png", {T.OTHER}),
    ("LICENSE", {T.OTHER}),
    # multi-tag and exclusion cases
    ("docs/a.test.ts", {T.TEST, T.DOCS}),
    ("test/a.md", {T.TEST, T.DOCS}),
    ("tests/vite.config.ts", {T.TEST, T.BUNDLER_CONFIG}),
]


@pytest.mark.parametrize(("path", "expected"), TABLE)
def test_tags(path: str, expected: set[FileTag]) -> None:
    tags = tags_for(path)
    assert set(tags) == expected
    assert len(tags) == len(set(tags))  # no duplicates


def test_every_tag_is_covered() -> None:
    covered = {t for _, tags in TABLE for t in tags}
    assert covered == set(FileTag)


def test_test_file_is_not_source() -> None:
    assert T.SOURCE not in tags_for("src/a.test.ts")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("export default defineConfig({\n  test: {\n    globals: true,\n  },\n})", True),
        ("export default defineConfig({ plugins: [] })", False),
        ("const latest = 1; // contest: no", False),
        (None, False),
    ],
)
def test_vite_config_test_block(content: str | None, expected: bool) -> None:
    tags = tags_for("vite.config.ts", content)
    assert (T.TEST_CONFIG in tags) is expected
    assert T.BUNDLER_CONFIG in tags


def test_non_vite_content_never_adds_test_config() -> None:
    assert T.TEST_CONFIG not in tags_for("src/app.ts", "test: {}")


def _raw(path: str, patch: str | None, content: str | None = None) -> RawFile:
    return RawFile(
        path=path, status="modified", additions=1, deletions=0, patch=patch, content=content
    )


def test_small_patch_kept_and_multi_tag() -> None:
    res = tag_files([_raw("docs/a.test.ts", "@@ -1 +1 @@")])
    assert not res.truncated
    f = res.files[0]
    assert f.patch == "@@ -1 +1 @@"
    assert set(f.tags) == {T.TEST, T.DOCS}


def test_oversize_patch_dropped_and_truncated() -> None:
    limit = load_tagger().max_patch_bytes
    res = tag_files([_raw("src/a.ts", "x" * (limit + 1)), _raw("src/b.ts", "ok")])
    assert res.truncated
    assert res.files[0].patch is None
    assert res.files[1].patch == "ok"


def test_patch_at_limit_kept() -> None:
    limit = load_tagger().max_patch_bytes
    res = tag_files([_raw("src/a.ts", "x" * limit)])
    assert not res.truncated
    assert res.files[0].patch is not None


def test_limit_counts_bytes_not_characters() -> None:
    limit = load_tagger().max_patch_bytes
    res = tag_files([_raw("src/a.ts", "é" * (limit // 2 + 1))])
    assert res.truncated and res.files[0].patch is None


def test_lockfile_patch_by_reference_not_truncated() -> None:
    res = tag_files([_raw("package-lock.json", "@@ small @@")])
    assert res.files[0].patch is None
    assert not res.truncated
    assert res.files[0].tags == [T.LOCKFILE]


def test_binary_patch_none_stays_none() -> None:
    res = tag_files([_raw("logo.png", None)])
    assert res.files[0].patch is None and not res.truncated
    assert res.files[0].tags == [T.OTHER]


def test_vite_content_flows_through_tag_files() -> None:
    res = tag_files([_raw("vite.config.ts", "p", content="export default { test: {} }")])
    assert set(res.files[0].tags) == {T.BUNDLER_CONFIG, T.TEST_CONFIG, T.SOURCE}
