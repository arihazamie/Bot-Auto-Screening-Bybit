import requests, json, os, pytz, pandas as pd
import time
from datetime import datetime
from modules.config_loader import CONFIG
from modules.database import insert_trade, get_trades_open, get_state, set_state

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_now():
    return datetime.now(pytz.timezone(CONFIG['system']['timezone']))

def format_price(v):
    f = float(v)
    if f == 0:
        return "0"
    if f < 0.001:
        return f"{f:.8f}".rstrip('0').rstrip('.')
    if f < 1:
        return f"{f:.6f}".rstrip('0').rstrip('.')
    if f < 100:
        return f"{f:.4f}".rstrip('0').rstrip('.')
    return f"{f:.2f}"

def _pct(a, b):
    """Return % change from a→b as a formatted string like +1.27%"""
    if float(a) == 0:
        return ""
    p = ((float(b) - float(a)) / float(a)) * 100
    return f"{p:+.2f}%"

def _bar(score, max_score=10, width=8):
    """Build a unicode progress bar. e.g. ■■■■░░░░"""
    if max_score <= 0:
        return '░' * width
    filled = min(round((score / max_score) * width), width)
    return '■' * filled + '░' * (width - filled)

def _rvol_label(rvol):
    if rvol > 5.0: return "Nuclear ⚡⚡"
    if rvol > 3.0: return "Explosive ⚡"
    if rvol > 2.0: return "Strong 🔥"
    return "Normal 🌊"

TG_BASE = "https://api.telegram.org/bot{token}/{method}"

# ─── Telegram API ─────────────────────────────────────────────────────────────

def normalize_chat_id(chat_id) -> str:
    chat_id_str = str(chat_id).strip()
    if chat_id_str.startswith('-') and not chat_id_str.startswith('-100'):
        numeric_part = chat_id_str.lstrip('-')
        if len(numeric_part) >= 10:
            print(f"⚠️  [TELEGRAM] chat_id '{chat_id_str}' kemungkinan Channel/Supergroup.")
            print(f"⚠️  Format yang benar: '-100{numeric_part}'")
    return chat_id_str


def _tg(method, token, _retry=3, **kwargs):
    url = TG_BASE.format(token=token, method=method)
    for attempt in range(1, _retry + 1):
        try:
            r    = requests.post(url, timeout=30, **kwargs)
            data = r.json()
            if not data.get('ok'):
                desc = data.get('description', '')
                if 'not modified' not in desc.lower():
                    print(f"❌ [Telegram/{method}] Error {data.get('error_code','?')}: {desc}")
            return data
        except requests.exceptions.Timeout:
            if attempt < _retry:
                print(f"⏳ [Telegram/{method}] Timeout, retry {attempt}/{_retry}...")
                time.sleep(2 * attempt)
            else:
                print(f"❌ [Telegram/{method}] Timeout setelah {_retry}x retry")
                return None
        except Exception as e:
            print(f"❌ Telegram API Error [{method}]: {e}")
            return None


# ─── SIGNAL ALERT ─────────────────────────────────────────────────────────────

def send_alert(data, auto_trade: bool = False):
    """
    Kirim signal alert ke Telegram.
    Returns message_id (int) on success, None on failure.
    """
    token  = CONFIG['api'].get('telegram_bot_token')
    raw_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not raw_id:
        print("❌ [send_alert] telegram_bot_token atau telegram_chat_id belum diisi!")
        return None

    chat_id = normalize_chat_id(raw_id)
    symbol  = data['Symbol']

    try:
        is_long  = data['Side'] == 'Long'
        side_ico = "🚀 LONG" if is_long else "🔻 SHORT"
        side_dir = "📈" if is_long else "📉"

        # ── Prices ──────────────────────────────────────────────────────
        entry = data['Entry']
        sl    = data['SL']
        tp1, tp2, tp3 = data['TP1'], data['TP2'], data['TP3']
        rr    = data.get('RR', 0.0)

        sl_pct  = _pct(entry, sl)
        tp1_pct = _pct(entry, tp1)
        tp2_pct = _pct(entry, tp2)
        tp3_pct = _pct(entry, tp3)

        # ── Scores ──────────────────────────────────────────────────────
        t_sc = data['Tech_Score']
        s_sc = data['SMC_Score']
        q_sc = data['Quant_Score']
        d_sc = data['Deriv_Score']
        total = t_sc + s_sc + q_sc + d_sc

        # ── Market data ─────────────────────────────────────────────────
        df        = data['df']
        rvol      = df['RVOL'].iloc[-1]
        rvol_lbl  = _rvol_label(rvol)
        obi_val   = data.get('OBI', 0.0)
        obi_ico   = "🟢" if obi_val > 0.05 else ("🔴" if obi_val < -0.05 else "⚪")
        z_score   = data.get('Z_Score', 0.0)
        zeta      = data.get('Zeta_Score', 0.0)

        fund_raw  = data['df'].get('funding', pd.Series([0])).iloc[-1]
        if isinstance(fund_raw, pd.Series): fund_raw = fund_raw.iloc[-1]
        fund_pct  = float(fund_raw) * 100
        fund_ico  = "🔴 Hot" if fund_pct > 0.01 else "🟢 Cool"
        basis_pct = data.get('Basis', 0) * 100

        # ── Reasons (compact) ────────────────────────────────────────────
        reasons = []
        for r in [data.get('Tech_Reasons',''), data.get('SMC_Reasons',''), data.get('Deriv_Reasons','')]:
            for part in str(r).split(','):
                part = part.strip()
                if part and part not in reasons:
                    reasons.append(part)
        reason_line = "  ·  ".join(reasons[:5]) if reasons else "—"

        # ── Timestamp ────────────────────────────────────────────────────
        ts = get_now().strftime('%Y-%m-%d %H:%M:%S')

        # ── Build message ────────────────────────────────────────────────
        SEP  = "━━━━━━━━━━━━━━━━━━━━━━━"
        SEP2 = "─────────────────────────"

        text = (
            f"{SEP}\n"
            f"<b>{side_ico}</b>  ·  <b>{symbol}</b>\n"
            f"<i>{data['Pattern'].replace('_',' ').title()}  ·  {data['Timeframe']}  ·  BTC {side_dir} {data['BTC_Bias']}</i>\n"
            f"{SEP}\n\n"

            f"📍 <b>Entry</b>   <code>{format_price(entry)}</code>\n"
            f"🛑 <b>Stop</b>    <code>{format_price(sl)}</code>   <i>({sl_pct})</i>\n\n"

            f"🎯 <b>Targets</b>\n"
            f"   TP1  <code>{format_price(tp1)}</code>   <i>{tp1_pct}</i>\n"
            f"   TP2  <code>{format_price(tp2)}</code>   <i>{tp2_pct}</i>\n"
            f"   TP3  <code>{format_price(tp3)}</code>   <i>{tp3_pct}</i>\n"
            f"   ⚖️ R:R  <code>1 : {rr}</code>\n\n"

            f"{SEP2}\n"
            f"📊 <b>Score  {total} pts</b>\n"
            f"<pre>"
            f"Tech  {_bar(t_sc)}  {t_sc:2}    Quant {_bar(q_sc)}  {q_sc:2}\n"
            f"SMC   {_bar(s_sc)}  {s_sc:2}    Deriv {_bar(d_sc)}  {d_sc:2}"
            f"</pre>\n"

            f"{SEP2}\n"
            f"📡 <b>Market</b>\n"
            f"   RVOL    <code>{rvol:.2f}x</code>  {rvol_lbl}\n"
            f"   OBI     <code>{obi_val:+.4f}</code>  {obi_ico}\n"
            f"   Z-Score <code>{z_score:.2f}σ</code>  ζ <code>{zeta:.1f}/100</code>\n"
            f"   Funding <code>{fund_pct:.4f}%</code>  {fund_ico}\n"
            f"   Basis   <code>{basis_pct:+.4f}%</code>\n\n"

            f"💡 <i>{reason_line}</i>\n\n"

            f"{SEP2}\n"
            f"<i>V8 Bot  ·  {ts}</i>"
        )

        resp = _tg('sendMessage', token,
                   json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'})

        if resp and resp.get('ok'):
            msg       = resp.get('result', {})
            tg_msg_id = msg.get('message_id')
            insert_trade({
                "symbol":        symbol,
                "side":          data['Side'],
                "timeframe":     data['Timeframe'],
                "pattern":       data['Pattern'],
                "entry_price":   data['Entry'],
                "sl_price":      data['SL'],
                "tp1":           data['TP1'],
                "tp2":           data['TP2'],
                "tp3":           data['TP3'],
                "rr":            data['RR'],
                "reason":        data['Reason'],
                "tech_score":    data['Tech_Score'],
                "quant_score":   data['Quant_Score'],
                "deriv_score":   data['Deriv_Score'],
                "smc_score":     data['SMC_Score'],
                "basis":         data['Basis'],
                "btc_bias":      data['BTC_Bias'],
                "z_score":       data['Z_Score'],
                "zeta_score":    data['Zeta_Score'],
                "obi":           data['OBI'],
                "tech_reasons":  data.get('Tech_Reasons', ''),
                "quant_reasons": data.get('Quant_Reasons', ''),
                "deriv_reasons": data.get('Deriv_Reasons', ''),
                "smc_reasons":   str(data.get('SMC_Reasons', '')),
                "message_id":    str(tg_msg_id),
                "channel_id":    str(chat_id),
            })
            return tg_msg_id
        else:
            print(f"❌ [send_alert] Gagal kirim. Resp: {resp}")
            return None

    except Exception as e:
        print(f"❌ Alert Error: {e}")
        return None


# ─── LIVE DASHBOARD ───────────────────────────────────────────────────────────

def update_status_dashboard():
    token  = CONFIG['api'].get('telegram_bot_token')
    raw_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not raw_id:
        return

    chat_id = normalize_chat_id(raw_id)

    try:
        trades = get_trades_open()
        ts     = get_now().strftime('%H:%M:%S')
        SEP    = "━━━━━━━━━━━━━━━━━━━━━━━"
        SEP2   = "─────────────────────────"

        if not trades:
            content = (
                f"{SEP}\n"
                f"📊 <b>LIVE DASHBOARD</b>\n"
                f"{SEP}\n\n"
                f"😴  Tidak ada posisi terbuka saat ini.\n\n"
                f"{SEP2}\n"
                f"<i>🕐 {ts}</i>"
            )
        else:
            # Build rows
            rows = []
            for t in sorted(trades, key=lambda x: x.get('created_at', ''), reverse=True):
                sym   = t['symbol'].replace('/USDT:USDT', '').replace('/USDT', '')
                side  = t['side']
                side_ico = "🟢" if side == "Long" else "🔴"
                status = t.get('status', '—')

                # Status icon
                if 'OPEN_TPS' in status:
                    st_ico = "🎯"
                elif 'OPEN' in status:
                    st_ico = "⚡"
                else:
                    st_ico = "⏳"

                # Time since opened
                ts_raw = t.get('entry_hit_at') or t.get('created_at', '')
                try:
                    dt_open = datetime.fromisoformat(str(ts_raw))
                    mins    = int((datetime.now() - dt_open.replace(tzinfo=None)).total_seconds() / 60)
                    dur     = f"{mins}m" if mins < 60 else f"{mins//60}h{mins%60:02d}m"
                except:
                    dur = "—"

                entry_str = format_price(t.get('entry_price', 0))
                rows.append(f"{st_ico} {side_ico} <b>{sym}</b>  @<code>{entry_str}</code>  <i>{dur}</i>")

            body = "\n".join(rows)

            content = (
                f"{SEP}\n"
                f"📊 <b>LIVE DASHBOARD</b>\n"
                f"{SEP}\n\n"
                f"🔢 Open Positions: <b>{len(trades)}</b>\n\n"
                f"{body}\n\n"
                f"{SEP2}\n"
                f"<i>🕐 Updated: {ts}</i>"
            )

        msg_id = get_state('dashboard_msg_id')
        if msg_id:
            resp = _tg('editMessageText', token,
                       json={'chat_id': chat_id, 'message_id': int(msg_id),
                             'text': content, 'parse_mode': 'HTML'})
            if resp is None or not resp.get('ok'):
                err = (resp or {}).get('description', '')
                if 'not modified' not in err.lower():
                    set_state('dashboard_msg_id', '')
                    _send_new_dashboard(token, chat_id, content)
        else:
            _send_new_dashboard(token, chat_id, content)

    except Exception as e:
        print(f"❌ [update_status_dashboard] Error: {e}")


def _send_new_dashboard(token: str, chat_id: str, content: str):
    resp = _tg('sendMessage', token,
               json={'chat_id': chat_id, 'text': content, 'parse_mode': 'HTML'})
    if resp and resp.get('ok'):
        set_state('dashboard_msg_id', str(resp['result']['message_id']))


def run_fast_update():
    update_status_dashboard()


# ─── SCAN COMPLETION ──────────────────────────────────────────────────────────

def send_scan_completion(count, duration, bias, auto_trade: bool = False):
    token  = CONFIG['api'].get('telegram_bot_token')
    raw_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not raw_id:
        return

    chat_id    = normalize_chat_id(raw_id)
    bias_ico   = "📈" if "Bullish" in bias else ("📉" if "Bearish" in bias else "↔️")
    sig_ico    = "📶" if count > 0 else "📭"
    ts         = get_now().strftime('%H:%M:%S')
    SEP        = "━━━━━━━━━━━━━━━━━━━━━━━"

    text = (
        f"{SEP}\n"
        f"🔭 <b>Scan Complete</b>\n"
        f"{SEP}\n\n"
        f"⏱  Duration   <code>{duration:.1f}s</code>\n"
        f"{sig_ico}  Signals    <code>{count}</code>\n"
        f"{bias_ico}  BTC Bias   <b>{bias}</b>\n\n"
        f"<i>🕐 {ts}</i>"
    )

    try:
        resp = _tg('sendMessage', token,
                   json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'})
        if resp and not resp.get('ok'):
            print(f"❌ [send_scan_completion] Gagal: {resp.get('description')}")
    except Exception as e:
        print(f"❌ [send_scan_completion] Exception: {e}")