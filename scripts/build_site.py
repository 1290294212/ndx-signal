"""Publish a public, non-account-specific NDX signal with fail-closed freshness checks."""
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parent.parent
site = root / 'site'
site.mkdir(exist_ok=True)
out = site / 'signal.json'
ran = datetime.now(timezone.utc).isoformat(timespec='seconds')
res = subprocess.run([sys.executable, str(root/'scripts'/'ndx_signal.py'), '--output', str(out)], capture_output=True, text=True, timeout=600)
if out.exists():
    payload = json.loads(out.read_text(encoding='utf-8'))
else:
    payload = {
       'series': 'NASDAQ100', 'trade_status': 'DATA_UNAVAILABLE_NO_ACTION',
       'error': (res.stderr or res.stdout)[-900:], 'last_observation_date': None
    }
# A fatal run or invalid/missing data cannot be portrayed as an actionable signal.
if res.returncode not in (0, 2):
    payload['trade_status'] = 'DATA_UNAVAILABLE_NO_ACTION'
    payload['error'] = (res.stderr or res.stdout)[-900:]
if not payload.get('last_weekly_decision_validated', False):
    payload['trade_status'] = 'WEEKLY_CALENDAR_UNVERIFIED_NO_ACTION'
if payload.get('stale'):
    payload['trade_status'] = 'DATA_STALE_NO_ACTION'
payload['generated_at_utc'] = ran
payload['public_data_only'] = True
payload['not_financial_advice'] = True
# No personal funds, credentials, or portfolio details published.
for private in ('base_capital_rmb','weekly_target_amount_rmb'):
    payload.pop(private, None)
out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
(site/'index.html').write_text('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>NDX Signal</title><body><h1>NDX F-V1.1 research signal</h1><p>Public, research-only market indicators. Not trading instructions.</p><a href="signal.json">View JSON</a></body></html>''',encoding='utf-8')
print('Generated',str(out),'status',payload['trade_status'])
