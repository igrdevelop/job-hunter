"""commands/debug_url.py — /debug_url diagnostic command handler."""

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_debug_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic: show step-by-step expired detection for a single URL.

    Usage: /debug_url <url>
    """
    args = (context.args or [])
    if not args:
        await update.message.reply_text(
            "Usage: /debug_url &lt;url&gt;\n"
            "Example: /debug_url https://www.pracuj.pl/praca/x,oferta,123",
            parse_mode=ParseMode.HTML,
        )
        return

    url = args[0].strip()
    msg = await update.message.reply_text(
        f"🔍 Diagnosing: <code>{url[:80]}</code>…", parse_mode=ParseMode.HTML
    )

    lines = [f"🔍 <b>debug_url</b>: <code>{url[:80]}</code>\n"]

    try:
        from urllib.parse import urlparse
        from hunter.sources import fetch_job_text
        from hunter.sources.html_fallback import clean_url
        from hunter.expired_check import is_job_expired, is_expired_by_html
        from hunter.expired_marker import _quick_html_expired, _is_cloudflare_challenge

        domain = urlparse(url).hostname or ""
        clean = clean_url(url)
        lines.append(f"<b>Domain:</b> {domain}")
        lines.append(f"<b>Clean URL:</b> <code>{clean[:80]}</code>")

        # 1. Is it in unsent rows?
        from hunter.tracker import (
            iter_unsent_rows,
            ATS_COL_INDEX, SENT_COL_INDEX, ID_COL_INDEX,
            URL_COL_INDEX, COMPANY_COL_INDEX, TITLE_COL_INDEX,
        )
        from hunter.config import TRACKER_PATH
        import openpyxl as _openpyxl

        offer_id = url.split(",oferta,")[-1].split("?")[0] if ",oferta," in url else ""

        def _url_matches(row_url: str) -> bool:
            if offer_id and offer_id in row_url:
                return True
            return row_url == clean or row_url == url

        rows = await asyncio.to_thread(iter_unsent_rows)
        matching = [r for r in rows if _url_matches(r.get("url", ""))]
        if matching:
            r = matching[0]
            lines.append(f"\n✅ <b>In unsent tracker rows:</b> {r['company']} — {r['title']}")
            lines.append(f"   ATS={r['ats']} | Sent={repr(r['sent'])} | ID={r['id'][:8]}")
        else:
            lines.append("\n⚠️ <b>NOT in unsent tracker rows</b>")

            # Scan ALL rows (including excluded ones) to explain why
            def _find_row_in_tracker():
                if not TRACKER_PATH.exists():
                    return None
                wb = _openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
                ws = wb.active
                try:
                    for row in ws.iter_rows(min_row=2, values_only=True):
                        if not row:
                            continue
                        row_url = str(row[URL_COL_INDEX - 1] or "").strip()
                        if _url_matches(row_url):
                            return {
                                "company": str(row[COMPANY_COL_INDEX - 1] or "").strip(),
                                "title": str(row[TITLE_COL_INDEX - 1] or "").strip(),
                                "ats": str(row[ATS_COL_INDEX - 1] or "").strip(),
                                "sent": str(row[SENT_COL_INDEX - 1] or "").strip(),
                                "id": str(row[ID_COL_INDEX - 1] or "").strip(),
                                "url": row_url,
                            }
                finally:
                    wb.close()
                return None

            found_row = await asyncio.to_thread(_find_row_in_tracker)
            if found_row:
                ats = found_row["ats"]
                sent = found_row["sent"]
                row_id = found_row["id"]
                lines.append(f"   Found in all rows: {found_row['company']} — {found_row['title']}")
                lines.append(f"   ATS={repr(ats)} | Sent={repr(sent)} | ID={repr(row_id[:8] if row_id else '')}")
                reasons = []
                if ats == "SKIP":
                    reasons.append("ATS=SKIP")
                if sent:
                    reasons.append(f"Sent={repr(sent)} (non-empty → excluded from /check_expired)")
                if not row_id:
                    reasons.append("no ID")
                if reasons:
                    lines.append(f"   ❌ Excluded because: {', '.join(reasons)}")
                else:
                    lines.append("   ⚠️ No obvious exclusion reason — URL matching may have missed it")
            else:
                lines.append("   ❌ Not found in tracker at all (not applied, or URL mismatch)")

        # 2. Quick HTML check — use _fetch_quick_html so we don't double-fetch
        from hunter.expired_marker import _fetch_quick_html, _check_html_expired
        from hunter.expired_check import HTML_EXPIRED_MARKERS
        lines.append("\n<b>Step 1 — quick HTML check:</b>")
        try:
            fetch_html, fetch_status = await asyncio.to_thread(_fetch_quick_html, url)
            lines.append(f"  fetch → HTTP {fetch_status}, {len(fetch_html)} bytes")
            cf_challenge = _is_cloudflare_challenge(fetch_html)
            html_expired = is_expired_by_html(fetch_html, domain)
            lines.append(f"  is_cloudflare_challenge: {cf_challenge}")
            lines.append(f"  is_expired_by_html: {html_expired}")
            # show which marker matched
            for key, markers in HTML_EXPIRED_MARKERS.items():
                if key in domain:
                    for m in markers:
                        if m.lower() in fetch_html.lower():
                            lines.append(f"  ✅ HTML marker hit: <code>{m[:50]}</code>")
                            break
            # reuse the same HTML — avoids a second request that may get throttled
            check_result = _check_html_expired(fetch_html, domain, url=url)
            lines.append(f"  _check_html_expired → <b>{check_result}</b>")
        except Exception as e:
            lines.append(f"  ERROR: {str(e)[:100]}")

        # 3. Full fetch
        lines.append("\n<b>Step 2 — full fetch_job_text:</b>")
        try:
            text = await asyncio.to_thread(fetch_job_text, url)
            lines.append(f"  length: {len(text)} chars")
            expired = is_job_expired(text)
            lines.append(f"  is_job_expired: <b>{expired}</b>")
            # show last 300 chars (where archived notice appears)
            tail = text[-300:].replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"  tail:\n<pre>{tail}</pre>")
        except Exception as e:
            lines.append(f"  ERROR: {str(e)[:150]}")

    except Exception as e:
        lines.append(f"\n❌ Diagnostic failed: {e}")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
