import requests, json, os, pytz, pandas as pd
import time
from datetime import datetime
from modules.config_loader import CONFIG
from modules.database import insert_trade, get_trades_open, get_state, set_state

def get_now(): return datetime.now(pytz.timezone(CONFIG['system']['timezone']))
def format_price(v): return "{:.8f}".format(float(v)).rstrip('0').rstrip('.') if float(v) < 1 else "{:.2f}".format(float(v))

TG_BASE = "https://api.telegram.org/bot{token}/{method}"


def normalize_chat_id(chat_id) -> str:
    chat_id_str = str(chat_id).strip()
    if chat_id_str.startswith('-') and not chat_id_str.startswith('-100'):
        numeric_part = chat_id_str.lstrip('-')
        if len(numeric_part) >= 10:
            print(f"⚠️  [TELEGRAM] chat_id '{chat_id_str}' kemungkinan Channel/Supergroup.")
            print(f"⚠️  Format yang benar: '-100{numeric_part}'")
    return chat_id_str


def _tg(method, token, _retry=3, **kwargs):
    """Kirim request ke Telegram API dengan retry otomatis jika timeout."""
    url = TG_BASE.format(token=token, method=method)
    for attempt in range(1, _retry + 1):
        try:
            r = requests.post(url, timeout=30, **kwargs)
            data = r.json()
            if not data.get('ok'):
                desc = data.get('description', '')
                # ✅ FIX: Suppress benign "not modified" error — content unchanged, not a real error
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


def send_alert(data, auto_trade: bool = False):
    """
    Send signal alert to Telegram.
    Returns message_id (int) on success, None on failure.
    (Previously returned True/False — now returns int so callers can use it for replies.)
    """
    token  = CONFIG['api'].get('telegram_bot_token')
    raw_id = CONFIG['api'].get('telegram_chat_id')

    if not token or not raw_id:
        print("❌ [send_alert] telegram_bot_token atau telegram_chat_id belum diisi di config.json!")
        return None

    chat_id = normalize_chat_id(raw_id)
    symbol  = data['Symbol']

    try:
        is_long   = data['Side'] == 'Long'
        emoji     = "🚀" if is_long else "🔻"
        rvol      = data['df']['RVOL'].iloc[-1]
        rvol_txt  = "⚡ Explosive" if rvol > 3.0 else ("🔥 Strong" if rvol > 2.0 else "🌊 Normal")
        obi_val   = data.get('OBI', 0.0)
        obi_icon  = "🟢" if obi_val > 0 else ("🔴" if obi_val < 0 else "⚪")

        fund_rate = data['df'].get('funding', pd.Series([0])).iloc[-1]
        if isinstance(fund_rate, pd.Series): fund_rate = fund_rate.iloc[-1]
        fund_pct  = fund_rate * 100
        fund_icon = "🔴" if fund_pct > 0.01 else "🟢"
        fund_txt  = "Hot" if fund_pct > 0.01 else "Cool"
        basis_pct = data.get('Basis', 0) * 100
        smc_str   = str(data.get('SMC_Reasons', ''))
        total     = data['Tech_Score'] + data['SMC_Score'] + data['Quant_Score'] + data['Deriv_Score']

        text = (
            f"{emoji} <b>SIGNAL: {symbol} ({data['Pattern']})</b>\n"
            f"<b>{data['Side']}</b>  |  <b>{data['Timeframe']}</b>\n\n"
            f"🎯 <b>Entry:</b> <code>{format_price(data['Entry'])}</code>\n"
            f"🛑 <b>Stop:</b>  <code>{format_price(data['SL'])}</code>\n"
            f"💰 <b>RR:</b>    1:{data.get('RR', 0.0)}\n\n"
            f"🏁 <b>Targets</b>\n"
            f"  TP1: <code>{format_price(data['TP1'])}</code>\n"
            f"  TP2: <code>{format_price(data['TP2'])}</code>\n"
            f"  TP3: <code>{format_price(data['TP3'])}</code>\n\n"
            f"🧮 <b>Quant</b>\n"
            f"  RVOL: <code>{rvol:.1f}x</code> ({rvol_txt})  OBI: <code>{obi_val:.2f}</code> {obi_icon}\n"
            f"  Z-Score: <code>{data.get('Z_Score', 0):.2f}σ</code>  ζ: <code>{data.get('Zeta_Score', 0):.1f}</code>/100\n\n"
            f"⛽ <b>Derivatives</b>\n"
            f"  Funding: <code>{fund_pct:.4f}%</code> {fund_icon} ({fund_txt})  "
            f"Basis: <code>{basis_pct:.4f}%</code>\n\n"
            f"🏆 <b>Score: {total}</b>  "
            f"(tech={data['Tech_Score']} smc={data['SMC_Score']} quant={data['Quant_Score']} deriv={data['Deriv_Score']})\n"
            f"🧠 Bias: <b>{data['BTC_Bias']}</b>\n"
            f"<i>V8 Bot | {get_now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
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
                "smc_reasons":   smc_str,
                "message_id":    str(tg_msg_id),
                "channel_id":    str(chat_id),
            })
            # ✅ Return int message_id so callers can pass it to save_signal_to_db for replies
            return tg_msg_id
        else:
            print(f"❌ [send_alert] Gagal kirim ke {chat_id}. Resp: {resp}")
            return None

    except Exception as e:
        print(f"❌ Alert Error: {e}")
        return None


def update_status_dashboard():
    token  = CONFIG['api'].get('telegram_bot_token')
    raw_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not raw_id:
        return

    chat_id = normalize_chat_id(raw_id)

    try:
        trades = get_trades_open()
        lines  = []
        for t in sorted(trades, key=lambda x: x.get('created_at', ''), reverse=True):
            ts = t.get('entry_hit_at') or t.get('created_at', '')
            try:    ts_str = datetime.fromisoformat(str(ts)).strftime('%H:%M')
            except: ts_str = "??:??"
            icon = '🟢' if 'Active' in t.get('status', '') else '⏳'
            lines.append(f"`{ts_str}` {icon} *{t['symbol']}* ({t['side']}): {t.get('status','')}")

        content = "📊 *LIVE DASHBOARD*\n" + ("\n".join(lines) if lines else "No active trades.")
        msg_id  = get_state('dashboard_msg_id')

        if msg_id:
            resp = _tg('editMessageText', token,
                       json={'chat_id': chat_id, 'message_id': int(msg_id),
                             'text': content, 'parse_mode': 'Markdown'})
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
               json={'chat_id': chat_id, 'text': content, 'parse_mode': 'Markdown'})
    if resp and resp.get('ok'):
        set_state('dashboard_msg_id', str(resp['result']['message_id']))


def run_fast_update():
    update_status_dashboard()


def send_scan_completion(count, duration, bias, auto_trade: bool = False):
    token  = CONFIG['api'].get('telegram_bot_token')
    raw_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not raw_id:
        return

    chat_id    = normalize_chat_id(raw_id)
    bias_emoji = "📈" if "Bullish" in bias else ("📉" if "Bearish" in bias else "↔️")
    text = (
        f"🔭 *Scan Cycle Complete*\n"
        f"⏱️ Duration: `{duration:.2f}s`\n"
        f"📶 Signals:  `{count}`\n"
        f"{bias_emoji} Bias:     *{bias}*"
    )
    try:
        resp = _tg('sendMessage', token,
                   json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'})
        if resp and not resp.get('ok'):
            print(f"❌ [send_scan_completion] Gagal: {resp.get('description')}")
    except Exception as e:
        print(f"❌ [send_scan_completion] Exception: {e}")