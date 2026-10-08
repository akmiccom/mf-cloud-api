# MFクラウド会計API ローカルバックアップツール

このリポジトリは、MFクラウド会計から必要データを完全・再現可能な形で取得し、ローカルへバックアップする取得専用ツールです。総務AIが主担当です。会社共通の承認・Git運用は `../akmic-company/AGENTS.md`、固有指示は `AGENTS.md` を参照してください。

取得したページ単位RAW JSONをバックアップ正本とし、全ページ結合JSON、manifest、確認用CSVを保存します。ここでいう正本はAPI取得内容の元データです。法定の会計原本や正式な長期保管先の指定とは区別します。`output/` はGit管理外のローカル領域です。Google Driveへの転送は行いません。

Supabase連携、Storageアップロード、DB登録・正規化、試算表／PL／BSの自前再計算、総勘定元帳生成、Google Sheets、UI、FastAPIは責務外です。会計APIにはGETのみを使用します。OAuthトークンの交換・更新にはPOSTを使用します。

## 初期設定

Python 3.10以上を使用します。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

設定する環境変数名は `MF_CLIENT_ID`、`MF_CLIENT_SECRET`、`MF_REDIRECT_URI` です。値はGitに保存しません。初回取得時はブラウザOAuth認証を行い、`token.json` にトークンを保存します。有効期限の60秒前からrefresh対象となります。

必要な参照スコープ：

```text
mfc/admin/tenant.read
mfc/accounting/offices.read
mfc/accounting/accounts.read
mfc/accounting/journal.read
mfc/accounting/departments.read
mfc/accounting/taxes.read
mfc/accounting/trade_partners.read
mfc/accounting/report.read
```

補助科目は `accounts.read`、会計年度設定は `offices.read` を使用します。以前のトークンに追加スコープがない場合は、アプリポータル側の参照権限を確認して再認証してください。必要に応じて既存 `token.json` をGit管理外の安全な場所へ退避し、初回認証をやり直します。ツールは既存トークンを自動削除しません。APIキー認証は今回追加していません。

## 年度単位の一括バックアップ

開始日・終了日はMFの会計年度設定と完全に一致させてください。開始日の西暦から会計年度を推測せず、取得した `term_settings` の `fiscal_year` を使います。

```powershell
python backup_fiscal_year.py `
  --start-date 2024-11-01 `
  --end-date 2025-10-31
```

取得順序は `term_settings` → `accounts` → `sub_accounts` → `taxes` → `departments` → `trade_partners` → `journals` → `trial_balance_bs` → `trial_balance_pl` です。年度バックアップでは次の区分を使います。

| 区分 | endpoint | 意味 |
|---|---|---|
| 必須 (`required`) | term_settings, accounts, journals | 全体の完了に必要。失敗時は `complete=false`、`status=failed`、終了コード1。 |
| 推奨 (`recommended`) | sub_accounts, trial_balance_bs, trial_balance_pl | 補助科目と検算用試算表を取得できる場合に保存。失敗しても続行。accounts内の補助科目情報は保持するが、代替RAWを捏造しない。 |
| 任意 (`optional`) | taxes, departments, trade_partners | 取得できる場合に保存。403を含む失敗でも後続を続行。 |

推奨・任意だけが失敗した場合、必須がすべて取得・検証・保存まで完了すれば `complete=true`、`status=complete_with_warnings`、終了コード0です。`warnings` に失敗endpoint、区分、安全なエラー、HTTP statusを記録します。各endpointは `failed` のままとし、403を成功扱いにしません。必須失敗時も独立した後続取得を続行します。対象年度をterm_settingsから確定できない場合だけ、年度に依存する帳票を `skipped_dependency` として取得せず、日付指定のjournalsは継続します。認証クライアント初期化や失敗記録の保存自体に失敗した場合は停止します。マスター単独・帳票単独コマンドでは、要求したendpointをすべて必須として扱います。

バックアップ正本は `journals/pages/` のjournals RAW JSONです。`trial_balance_bs`／`trial_balance_pl` は検算用の推奨データであり、試算表API取得が403等で失敗しても必須データが揃えばバックアップは成立します。`complete_with_warnings` は必須範囲が完了し、推奨・任意に取得失敗がある状態です。manifestの既存 `requirement` フィールドに `required`／`recommended`／`optional` を記録します。試算表の自前再計算は本ツールでは実装していません。

```text
output/
  2024-11-01_2025-10-31/
    manifest.json
    master/
      term_settings.json
      accounts.json
      sub_accounts.json
      taxes.json
      departments.json
      trade_partners.json
    journals/
      pages/
        page_0001.json
        page_0002.json
        ...
      all.json
      flat.csv
      expanded.csv
    reports/
      trial_balance_bs.json
      trial_balance_pl.json
```

`--output-dir` はバックアップ単位のディレクトリを直接指定します。`--per-page` は1～10000、既定100です。帳票は補助科目を含み、通常仕訳・決算整理仕訳の両方を対象とします。帳票の `include_tax` は既定false、`--include-tax` 指定時はtrueです。取得条件をmanifestに記録します。

## 個別の取得CLI

マスター6種類だけを保存する場合：

```powershell
python get_master_data.py --output-dir output/master_snapshot_2026-10-07
```

日付指定は任意です。指定すると、対象期間が1つの会計年度内に含まれることを検証し、manifestに会計期間を記録します。マスター自体を過去年度の状態へ切り替える指定ではありません。

BS／PL試算表だけを取得する場合：

```powershell
python get_trial_balance.py `
  --start-date 2024-11-01 `
  --end-date 2025-10-31
```

既定出力先は `output/2024-11-01_2025-10-31_reports/` です。会計年度を特定するため `term_settings` も保存します。年度内の部分期間も指定できます。返却された帳票の開始日・終了日が指定期間と一致することを検証します。

既存の仕訳CLIも使用できます。

```powershell
python get_journals.py `
  --start-date 2024-11-01 `
  --end-date 2025-10-31 `
  --all-pages `
  --output-dir output/journals_2024_backup
```

このCLIでは従来のファイル名を維持します。

- `journals_2024-11-01_2025-10-31_all.json`
- `journals_2024-11-01_2025-10-31_all.csv`
- `journals_2024-11-01_2025-10-31_expanded.csv`
- 追加：`journals/pages/page_0001.json` などと `manifest.json`

`--all-pages` を省略すると単一ページ（既定1）を取得します。`--page N` と `--all-pages` の併用はできません。単一ページでは `_pageN` のファイル名となり、manifestの `complete` はfalseです。指定ページの取得成功を年度全体の完全取得と混同しないでください。

仕訳CLIの日付を両方省略すると、officesから今日を含む会計期間を選びます。該当なしの場合は終了日が最新の会計期間を使用します。このCLIの既定出力先は従来どおり `output/` ですが、既存出力のある環境では別の `--output-dir` を指定してください。

`python check_connection.py` と `python get_accounts.py` は従来の診断用CLIです。取得内容を標準出力へ表示し、バックアップ一式は作成しません。バックアップ用途には上記の保存CLIを使います。

## RAW JSONと完全性の検証

各ページの `response.json()` をフィールド削除・独自metadata追加なしで保存します。未知のトップレベル／仕訳／branch／借方・貸方フィールドも保持します。HTTP本文のバイト列そのものではなく、UTF-8 JSONとして再シリアライズした保存です。HTTPヘッダーは保存しません。HTTP statusはmanifestに記録します。

結合JSONは次の2要素だけで構成します。

```text
journals: 全ページの仕訳配列（各仕訳を変更しない）
backup_metadata:
  retrieved_at, start_date, end_date,
  retrieved_pages, retrieved_journal_count,
  api_total_count, total_count_verified, complete
```

元のpagination等はRAWページで確認します。結合JSONに第1ページのpaginationを流用しません。以前の結合JSONトップレベルの `retrieved_pages`／`retrieved_journal_count` は `backup_metadata` へ移動しました。

仕訳は1ページ目から順次取得し、最大1000ページです。公式仕様の `metadata` と従来の `pagination`／`paging`／`meta`／トップレベルを認識します。ページ情報がない場合は短いページまたは空ページで終了します。

`total_count` が存在すれば最終件数と照合します。不一致、取得途中のtotal変化、矛盾・不正なtotal、重複仕訳ID、不正な仕訳配列は失敗です。RAWを残してstderr・manifestに理由を記録し、非0を返します。重複仕訳を黙って削除しません。totalがない場合は `api_total_count=null`、`total_count_verified=false` とし、終了条件による取得完了のみを示します。

`journal_type=adjusting_entry`、`entered_by=JOURNAL_TYPE_OPENING` を含め、取得された分類値をそのまま保持します。分類ロジックや帳票再計算は行いません。

## manifestとSHA-256

`manifest.json` はバックアップの取得条件・結果・保存ファイルを検証するための台帳です。

- 形式バージョン、UTCの作成・完了時刻、対象期間、取得した会計期間、仕訳で観測したterm_period。
- エンドポイントごとのGETパス、公開リクエストパラメータ、HTTP status、取得件数、ページ数、API total、検証結果。
- `not_started`／`incomplete`／`success`／`failed`、全体の `complete`、失敗理由。
- 当該実行で保存した全JSON／CSVの `relative_path`、`sha256`、`size_bytes`。manifest自身は自己ハッシュの対象外です。

実行開始時から `incomplete` として書き、ファイル保存ごとに更新します。すべて成功した場合は `success`／`complete=true`、年度バックアップの推奨・任意のみ失敗した場合は `complete_with_warnings`／`complete=true` です。完了は必須範囲の完了を意味し、全endpointの成功は個別結果で確認してください。必須失敗時は `failed`／`complete=false` となり、取得済みRAWとハッシュ記録を残します。強制終了で `incomplete` が残る場合も、成功したバックアップとして扱いません。

manifestにはaccess token、refresh token、client secret、Authorization header、生のエラーレスポンスを記録しません。HTTPエラーの本文はターミナルにも表示しません。

マスターの `retrieved_count=0` は認識済みの空配列を意味します。未取得・未知構造では件数は `null` です。`response_structure_recognized` に認識結果を記録し、未知構造はHTTP 200でも失敗にします。RAW保存のcheckpointにも認識済み件数を記録します。

manifest更新には固有名の `.manifest-*.json.tmp` を使用し、置換時の一時的なPermissionErrorを最大3回試行します。置換が完了しない場合は対象endpointの保存失敗として扱い、一時ファイルを診断用に残します。失敗の記録も保存できない場合は処理を停止します。既存の `manifest.json.tmp` は再利用・削除しません。残った一時ファイルだけを根拠にバックアップ成功とは判断しないでください。

## 上書き防止と再実行

デフォルトでは出力先ディレクトリに既存ファイルがあれば、認証・API取得を開始する前にエラーにします。新しい出力先を指定して再実行する方法を推奨します。

明示的な `--overwrite` の場合だけ既存出力先を再利用し、今回の出力とmanifestを上書きします。無関係なファイルや前回だけに存在した余分なページを削除しません。そのため再利用先には古いファイルが残る可能性があります。今回のバックアップの構成はmanifestの保存ファイル一覧を正とし、ディレクトリ内の全ファイルを無条件に取り込まないでください。並行して同じ出力先へ実行しないでください。

## retry・timeout

GETの429／500／502／503／504、Timeout、ConnectionErrorは最大3回再試行します（初回を含め最大4回）。待機は1／2／4秒の指数バックオフで、429の `Retry-After` があれば秒数またはHTTP-dateを優先します。HTTP timeoutは30秒です。400／403等は無条件retryしません。

401は保存済みrefresh tokenがある場合に更新し、1回だけ再送します。再度401、refresh失敗、refresh tokenなしでは失敗します。認証に使うPOSTや汎用POSTは自動retryしません。

## 公式仕様と取得範囲の限界

実装時に確認した公式仕様（2026-10-07）：

- [MFクラウド会計API V3](https://developers.api-accounting.moneyforward.com/)
- [公式OpenAPI YAML](https://developers.api-accounting.moneyforward.com/v3/openapi.yaml)

マスター6種類には公式仕様上page／per_page／年度指定がありません。`available` フィルタを付けず、補助科目も任意の `account_id` フィルタを付けずに取得します。過去年度ごとのマスター状態をAPIで復元できる保証はありません。manifestにも取得時点のスナップショットであることを記録します。将来、マスター応答に次ページありのmetadataが追加された場合は、未確認パラメータを推測して送らず失敗させます。

試算表APIは未実現仕訳を含まず、全部門合計で、全金額が0の科目等を返しません。RAW JSONはAPIが返した内容を保持しますが、MFが返さない情報まで復元するものではありません。複数APIを取得する間のデータ更新を固定するスナップショット機能もありません。取得中の会計データ編集は避けてください。

connected_accounts、transactions、transition_bs／pl、証憑ファイルの取得・登録・削除は今回の対象外です。`voucher_file_ids` は保持しますが、証憑ファイル本体をバックアップする機能はありません。

## テスト

```powershell
python -B -m unittest discover -v
```

FakeClient／HTTP mockと一時ディレクトリを使います。実API、`.env`の実値、保存済みトークン、実会計データは使用しません。

## Gitに保存しないもの

`.env`、`token.json`、トークン、client secret、実会計データ、`output/` 配下の実バックアップはcommitしません。`.gitignore` に認証ファイル、一時トークン、仮想環境、キャッシュ、`output/` を登録しています。`--output-dir` を変更する場合も、保存先がGit管理外であることを確認してください。

主要実装：`mf_auth.py`、`mf_client.py`、`mf_endpoints.py`、`backup_support.py`、`mf_backup.py`、`get_journals.py`。実行入口：`backup_fiscal_year.py`、`get_master_data.py`、`get_trial_balance.py`、`get_journals.py`。
