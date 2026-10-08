"""GET paths verified against the official V3 OpenAPI on 2026-10-07.

https://developers.api-accounting.moneyforward.com/v3/openapi.yaml
Master endpoints have no pagination or fiscal-year parameters in this spec.
"""

TENANT_URL = "https://api.biz.moneyforward.com/v2/tenant"

ACCOUNTING_BASE_URL = "https://api-accounting.moneyforward.com/api/v3"
OFFICES_URL = f"{ACCOUNTING_BASE_URL}/offices"
ACCOUNTS_URL = f"{ACCOUNTING_BASE_URL}/accounts"
JOURNALS_URL = f"{ACCOUNTING_BASE_URL}/journals"
TERM_SETTINGS_URL = f"{ACCOUNTING_BASE_URL}/term_settings"
SUB_ACCOUNTS_URL = f"{ACCOUNTING_BASE_URL}/sub_accounts"
TAXES_URL = f"{ACCOUNTING_BASE_URL}/taxes"
DEPARTMENTS_URL = f"{ACCOUNTING_BASE_URL}/departments"
TRADE_PARTNERS_URL = f"{ACCOUNTING_BASE_URL}/trade_partners"
TRIAL_BALANCE_BS_URL = f"{ACCOUNTING_BASE_URL}/reports/trial_balance_bs"
TRIAL_BALANCE_PL_URL = f"{ACCOUNTING_BASE_URL}/reports/trial_balance_pl"

MASTER_ENDPOINTS = {
    "term_settings": TERM_SETTINGS_URL, "accounts": ACCOUNTS_URL,
    "sub_accounts": SUB_ACCOUNTS_URL, "taxes": TAXES_URL,
    "departments": DEPARTMENTS_URL, "trade_partners": TRADE_PARTNERS_URL,
}
REPORT_ENDPOINTS = {
    "trial_balance_bs": TRIAL_BALANCE_BS_URL,
    "trial_balance_pl": TRIAL_BALANCE_PL_URL,
}
