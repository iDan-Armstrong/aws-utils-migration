"""A self-contained HTML report a reviewer can open without any tools."""
from __future__ import annotations

import html


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _size(n) -> str:
    if n is None:
        return ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def html_report(doc: dict) -> str:
    items = doc["items"]
    ok = sum(1 for i in items if (i.get("verification") or {}).get("status") == "verified")
    bad = sum(1 for i in items if (i.get("verification") or {}).get("status") == "mismatch" or i["status"] == "failed")
    hand = sum(1 for i in items if i["status"] == "handed-off")
    rows = []
    for i in items:
        v = i.get("verification") or {}
        checks = "".join(f"<li class='{'ok' if c['ok'] else 'bad'}'>{'Pass' if c['ok'] else 'Fail'}: {_e(c['name'])}"
                         f"{' <span>' + _e(c['note']) + '</span>' if c.get('note') else ''}</li>" for c in v.get("checks", []))
        flags = "".join(f"<li>{_e(f)}</li>" for f in i.get("flags") or [])
        rows.append(f"""<tr><td><b>{_e(i['key'])}</b><br><span class='m'>{_e(i['source_type'])} · {_e(i['kind'])}{' · ' + _e(i['geometry_type']) + ' EPSG:' + _e(i['srid']) if i.get('geometry_type') else ''}</span>
          {('<ul class=flags>' + flags + '</ul>') if flags else ''}</td>
          <td class=n>{_e(i['rows'])}</td><td class=n>{_size(i['bytes'])}</td>
          <td>{_e(i['route']['method'])}<br><span class='m'>{_e('; '.join(i['route']['reasons']))}</span></td>
          <td>{_e(i['status'])}{('<br><span class=bad>' + _e(i['error']) + '</span>') if i.get('error') else ''}<br><span class='m'>{_e(', '.join(i.get('destinations') or []))}</span></td>
          <td>{('<b>' + _e(v.get('status')) + '</b><ul class=checks>' + checks + '</ul>') if v else '<span class=m>not verified</span>'}</td></tr>""")
    aws = doc["aws"]
    post = aws.get("bucket_posture") or {}
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>awsmig report: {_e(doc['job'])}</title><style>
body{{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:24px;color:#1d2320;background:#fff}}h1{{font-size:22px;margin:0 0 4px}}
.m{{color:#5c6661;font-size:12.5px}}table{{border-collapse:collapse;width:100%;margin-top:16px}}td,th{{border-bottom:1px solid #d6dbd5;padding:6px 8px;vertical-align:top;text-align:left}}
th{{font-size:12px;color:#5c6661}}.n{{text-align:right;font-variant-numeric:tabular-nums}}.ok{{color:#2e7a4c}}.bad{{color:#b0352b}}
ul{{margin:4px 0 0;padding-left:16px}}.flags li{{color:#a8701a}}.tiles{{display:flex;gap:24px;flex-wrap:wrap;margin:12px 0}}.tiles b{{font-size:24px;display:block}}
.banner{{background:#f1f3f0;padding:8px 12px;border-left:4px solid #28579e;margin:8px 0}}
</style></head><body>
<h1>Migration report: {_e(doc['job'])}</h1>
<div class=m>Generated {_e(doc['generated'])} by {_e(doc['operator'])} · awsmig {_e(doc['awsmig_version'])} · marking <b>{_e(doc['marking'])}</b></div>
{"<div class=banner>Run against a simulated AWS endpoint. No real AWS account was used.</div>" if aws.get('simulated') else ''}
<div class=tiles><div><b>{len(items)}</b>items</div><div><b class=ok>{ok}</b>verified</div><div><b class=bad>{bad}</b>failed or mismatched</div><div><b>{hand}</b>handed to an AWS service</div></div>
<div class=m>Target: s3://{_e(aws['bucket'])} in {_e(aws['partition'])} {_e(aws['region'])}{' (FIPS endpoints)' if aws.get('fips') else ''} · KMS key {_e(aws.get('kms_key') or 'SSE-S3')} ·
bucket encryption {_e(post.get('default_encryption'))}, versioning {_e(post.get('versioning'))}, public access blocked {_e(post.get('public_access_blocked'))}</div>
<table><thead><tr><th>Item</th><th class=n>Rows / files</th><th class=n>Size</th><th>Route</th><th>Status</th><th>Verification</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""
