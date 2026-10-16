"""Generate local-only random secrets into .local/dev.env (gitignored). Never use these outside a disposable dev box."""
import base64, os, secrets, sys
from pathlib import Path
out = Path(sys.argv[1] if len(sys.argv) > 1 else '.local/dev.env')
if out.exists():
    sys.exit(f'{out} exists; refusing to overwrite')
tok = lambda: secrets.token_urlsafe(32)
key = lambda: base64.b64encode(os.urandom(32)).decode()
vals = {
 'TELCO_API_READER_PASSWORD': tok(), 'TELCO_API_WRITER_PASSWORD': tok(), 'TELCO_API_IDENTITY_PASSWORD': tok(), 'TELCO_OPS_PASSWORD': tok(),
 'TELCO_AGENT_MEMORY_PASSWORD': tok(),
 'JWT_SECRET': tok(), 'N8N_SERVICE_TOKEN': tok(), 'MCP_SERVICE_TOKEN': tok(), 'WA_APP_SECRET': tok(), 'WA_VERIFY_TOKEN': tok(),
 'WA_ACCESS_TOKEN': 'mock-' + tok(), 'IDENTITY_ENC_KEY': key(), 'IDENTITY_HMAC_KEY': key(), 'CHART_SIGNING_KEY': tok(),
 'N8N_DISPATCH_SECRET': tok(), 'HERMES_API_KEY': tok(), 'ROUTER_API_KEY': 'mock-' + tok(), 'N8N_ENCRYPTION_KEY': tok(),
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(''.join(f'{k}={v}\n' for k, v in vals.items()), encoding='utf-8')
print('wrote', out, 'keys:', len(vals))
