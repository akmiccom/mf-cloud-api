# MFクラウドAPI 接続プロジェクト

akmic合同会社のバックオフィス用API連携リポジトリです。総務AIが主担当となり、当面は領収書とMFクラウド仕訳を照合できる状態までを実証範囲とします。会社共通の承認境界と担当定義は `../akmic-company/AGENTS.md`、固有指示は `AGENTS.md` を参照してください。

## ファイル構成

```text
mf-cloud-api/
├─ .env                    # 認証情報。Gitへ保存しない
├─ .env_local              # 機密情報を含まないローカル設定例
├─ .gitignore
├─ AGENTS.md               # リポジトリ固有の作業指示
├─ requirements.txt
├─ mf_auth.py              # OAuth認証・トークン保存・自動更新
├─ mf_client.py            # API通信の共通処理
├─ mf_endpoints.py         # API URLの定義
├─ check_connection.py     # 事業者情報の接続確認
├─ get_accounts.py         # 勘定科目の取得
├─ get_journals.py         # 仕訳の取得
└─ output/                 # Git管理外のローカル一時作業領域
```

`token.json`は初回認証後に自動生成されます。

## 初期設定

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env_local .env
```

`.env`へClient IDとClient Secretを設定してください。

## 接続確認

```powershell
python check_connection.py
```

## 勘定科目取得

```powershell
python get_accounts.py
```

## 仕訳取得

```powershell
python get_journals.py --start-date 2025-11-01 --end-date 2026-10-31
```

ページと件数も指定できます。

```powershell
python get_journals.py `
  --start-date 2025-11-01 `
  --end-date 2026-10-31 `
  --page 1 `
  --per-page 100
```

## 認証スコープ

```python
mfc/admin/tenant.read
mfc/accounting/offices.read
mfc/accounting/accounts.read
mfc/accounting/journal.read
```

スコープを変更した場合は、既存の`token.json`を削除して再認証してください。

## Gitへ保存しないもの

- `.env`
- `token.json`
- `.venv/`
- `output/`

仕訳、金額、摘要、取引先その他の会計情報をGitへ登録しません。`output/` は正式な保存場所ではなく、当面の実証に限って使用するローカル一時領域です。Google Drive保存は照合自動化段階で再検討します。

## 当面の実証範囲

- MFクラウド仕訳の取得と検証
- 領収書JSONと仕訳の照合候補作成
- 一致、候補あり、要確認、未照合の区分
- 秘書AIによる結果確認と社長報告に必要な、機密情報を含まない集計

銀行、カード、証券への範囲拡大、仕訳の確定、会計・税務判断の自動化は対象外です。`mf-cloud-api` のブランチと権限の流れは未決定であり、社長決定後に会社共通運用指示へ追加します。
