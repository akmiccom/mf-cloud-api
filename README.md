# MFクラウドAPI 接続プロジェクト

## ファイル構成

```text
mf-cloud-api/
├─ .env                    # 認証情報。Gitへ保存しない
├─ .env.example            # .envのひな型
├─ .gitignore
├─ requirements.txt
├─ mf_auth.py              # OAuth認証・トークン保存・自動更新
├─ mf_client.py            # API通信の共通処理
├─ mf_endpoints.py         # API URLの定義
├─ check_connection.py     # 事業者情報の接続確認
├─ get_accounts.py         # 勘定科目の取得
└─ get_journals.py         # 仕訳の取得
```

`token.json`は初回認証後に自動生成されます。

## 初期設定

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
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
