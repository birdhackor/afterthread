# PyPI 發布手冊

本文件是 maintainer 用的發布 runbook。一般使用者的安裝方式見根目錄 README 與
公開文件站。

## 發布契約

- PyPI 與 TestPyPI 上的 wheel／sdist 都是公開檔案；GitHub repo 的可見性不因發布
  流程而改變。
- 版本遵循 Semantic Versioning，Git tag 必須是 `v<project.version>`，例如
  `backend/pyproject.toml` 為 `0.1.0` 時只能由 `v0.1.0` 發布。
- 一次 build 產生的同一組 wheel／sdist 會先送 TestPyPI。workflow 從 TestPyPI
  下載 wheel、與原 artifact 做 byte-for-byte `cmp`、實際安裝並確認 CLI 版本後，
  才允許 PyPI publish job 取得 OIDC 權限。
- 不使用長效 PyPI API token。TestPyPI 與 PyPI 都只信任
  `.github/workflows/release.yml` 的 GitHub OIDC identity。
- release workflow 只處理非 prerelease 的 GitHub Release；draft 與 prerelease
  不會發布。

## 一次性平台設定

### 1. PyPI 與 TestPyPI 帳號

PyPI 與 TestPyPI 是兩套獨立帳號／專案資料庫。兩邊都要建立並保護 maintainer
帳號、驗證 email，並設定可用的復原方式。

公開搜尋不到名稱不等於名稱已保留；pending publisher 要等第一次成功上傳才會建立
並占用 `afterthread` 專案名稱。因此一次性設定完成後，不要長時間擱置首次發布。

### 2. GitHub environments

在 repo 的 Settings → Environments 建立：

- `testpypi`：不需人工核准。
- `pypi`：以 `birdhackor` 為 required reviewer；允許 self-review，讓建立 Release 的
  maintainer 能在檢查 TestPyPI 結果後批准正式上傳。

兩個 environment 都使用 selected deployment branches and tags，且只建立 `v*` 的
**tag** 規則（不是同名 branch 規則）。這讓發布 job 只能由版本 tag 進入。`pypi`
的 required reviewer 讓正式上傳在 TestPyPI 驗證成功後仍需一次明確人工批准。

### 3. Pending Trusted Publishers

分別在 TestPyPI 與 PyPI 的 account Publishing 頁新增 GitHub pending publisher：

| 欄位 | TestPyPI | PyPI |
| --- | --- | --- |
| PyPI project name | `afterthread` | `afterthread` |
| GitHub owner | `birdhackor` | `birdhackor` |
| Repository | `afterthread` | `afterthread` |
| Workflow | `release.yml` | `release.yml` |
| Environment | `testpypi` | `pypi` |

欄位必須與 workflow 完全相同；不需也不應建立 `PYPI_TOKEN` GitHub secret。

## 每次發布

1. 在一般 PR 中更新 `backend/pyproject.toml` 的版本、`backend/uv.lock` 與
   `CHANGELOG.md`。建議使用 `cd backend && uv version <X.Y.Z>`，避免 lockfile 的
   editable project version 漂移。
2. 確認 PR CI 全綠，合併到預設分支。不要直接從未合併的 feature branch 發布。
3. 在乾淨 checkout 執行本機 release gates：

   ```bash
   cd backend
   uv run ruff format --check .
   uv run ruff check .
   uv run ty check
   uv run pytest

   cd ../frontend
   pnpm install --frozen-lockfile
   pnpm lint
   pnpm test
   pnpm build

   cd ..
   bash e2e/smoke.sh
   RELEASE_TAG=v<X.Y.Z> bash e2e/wheel_smoke.sh
   uvx --from "twine==6.2.0" twine check --strict \
     backend/dist/afterthread-*.whl \
     backend/dist/afterthread-*.tar.gz
   uvx --from "pip-audit==2.10.1" pip-audit backend
   ```

4. 從 GitHub Releases 建立 tag `v<X.Y.Z>`，先存成 draft 並完成 release notes；確認
   target 是已通過 CI 的預設分支 commit。
5. Publish GitHub Release。`Release to PyPI` workflow 會依序執行 build → TestPyPI
   publish → TestPyPI artifact 驗證 → `pypi` environment approval → PyPI publish。
6. 發布後從正式 PyPI 驗證：

   ```bash
   uvx --refresh afterthread --version
   ```

   並確認 <https://pypi.org/project/afterthread/> 的 README、Python／OS classifiers、
   MIT license、檔案 hashes 與 Trusted Publishing 標記。

## 失敗處理

- build、測試、metadata 或 dependency audit 失敗時，不會取得任何 publish job 的
  OIDC 權限；修正後用新 commit 重新走 PR／Release。
- TestPyPI publish 使用 `skip-existing` 讓同一個 GitHub Release workflow 可安全
  rerun；隨後的 byte comparison 會阻止同版本但內容不同的舊檔流入正式 PyPI。
- 如果 TestPyPI 已存在同版本、但下載內容與本次 build 不同，停止發布並 bump 新
  版本；不可略過 `cmp`。
- PyPI 接受某個 distribution filename 後不可用不同內容覆寫。正式上傳後若發現
  問題，應 yank 該版本並發布新的 patch version，不要刪除後嘗試重傳同版號。
