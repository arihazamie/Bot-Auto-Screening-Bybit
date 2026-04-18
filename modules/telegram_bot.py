import requests, json, os, pytz, pandas as pd, numpy as np
import mplfinance as mpf
from scipy.signal import argrelextrema
from datetime import datetime
from modules.config_loader import CONFIG
from modules.database import insert_trade, get_trades_open, get_state, set_state

def get_now(): return datetime.now(pytz.timezone(CONFIG['system']['timezone']))
def format_price(value): return "{:.8f}".format(float(value)).rstrip('0').rstrip('.') if float(value) < 1 else "{:.2f}".format(float(value))

TG_BASE = "https://api.telegram.org/bot{token}/{method}"

def _tg(method, token, **kwargs):
    url = TG_BASE.format(token=token, method=method)
    try:
        r = requests.post(url, **kwargs)
        data = r.json()
        # Return response apa pun (ok atau tidak) — caller yang handle
        return data
    except Exception as e:
        print(f"Telegram API Error [{method}]: {e}")
        return None


def generate_chart(df, symbol, pattern, timeframe):
    filename = f"chart_{symbol.replace('/','_')}_{timeframe}.png"
    try:
        plot_df = df.iloc[-100:].copy()
        if 'timestamp' in plot_df.columns: plot_df.set_index('timestamp', inplace=True)
        plot_df.index = pd.to_datetime(plot_df.index)

        n = 3
        min_idx = argrelextrema(plot_df['low'].values, np.less_equal, order=n)[0]
        max_idx = argrelextrema(plot_df['high'].values, np.greater_equal, order=n)[0]

        peak_dates, peak_vals     = plot_df.index[max_idx], plot_df['high'].iloc[max_idx].values
        valley_dates, valley_vals = plot_df.index[min_idx], plot_df['low'].iloc[min_idx].values

        lines, colors = [], []
        def add_line(dates, vals, color):
            if len(dates) >= 2:
                lines.append([(str(dates[-2]), float(vals[-2])), (str(dates[-1]), float(vals[-1]))])
                colors.append(color)

        if pattern in ['ascending_triangle', 'bullish_rectangle', 'double_top', 'bear_flag', 'descending_triangle']:
            add_line(peak_dates, peak_vals, 'red')
        if pattern in ['descending_triangle', 'bullish_rectangle', 'double_bottom', 'bull_flag', 'ascending_triangle']:
            add_line(valley_dates, valley_vals, 'green')

        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc)
        apds = []
        if 'EMA_Fast' in plot_df.columns:
            apds.append(mpf.make_addplot(plot_df['EMA_Fast'], color='cyan', width=1))

        ratios, vol_panel = (3, 1), 1
        if 'MACD_h' in plot_df.columns:
            cols = ['#2ebd85' if v >= 0 else '#f6465d' for v in plot_df['MACD_h']]
            apds.append(mpf.make_addplot(plot_df['MACD_h'], type='bar', panel=1, color=cols, ylabel='MACD'))
            ratios, vol_panel = (3, 1, 1), 2

        kwargs = dict(
            type='candle', style=s, addplot=apds,
            title=f"\n{symbol} ({timeframe}) - {pattern}",
            figsize=(12, 8), panel_ratios=ratios,
            volume=True, volume_panel=vol_panel,
            savefig=dict(fname=filename, dpi=100, bbox_inches='tight')
        )
        if lines:
            kwargs['alines'] = dict(alines=lines, colors=colors, linewidths=1.5, alpha=0.7)
        mpf.plot(plot_df, **kwargs)
        return filename
    except Exception as e:
        print(f"Chart Error: {e}")
        return None


def send_alert(data, auto_trade: bool = False):
    token   = CONFIG['api'].get('telegram_bot_token')
    chat_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not chat_id:
        return False

    symbol     = data['Symbol']
    image_path = None

    # 1. Generate Chart
    try:
        image_path = generate_chart(data['df'], symbol, data['Pattern'], data['Timeframe'])
    except Exception as e:
        print(f"❌ Chart Error: {e}")

    try:
        is_long = data['Side'] == 'Long'
        emoji   = "🚀" if is_long else "🔻"

        rvol     = data['df']['RVOL'].iloc[-1]
        rvol_txt = "⚡ Explosive" if rvol > 3.0 else ("🔥 Strong" if rvol > 2.0 else "🌊 Normal")
        obi_val  = data.get('OBI', 0.0)
        obi_icon = "🟢" if obi_val > 0 else ("🔴" if obi_val < 0 else "⚪")

        fund_rate = data['df'].get('funding', pd.Series([0])).iloc[-1]
        if isinstance(fund_rate, pd.Series): fund_rate = fund_rate.iloc[-1]
        fund_pct  = fund_rate * 100
        fund_icon = "🔴" if fund_pct > 0.01 else "🟢"
        fund_txt  = "Hot" if fund_pct > 0.01 else "Cool"
        basis_pct = data.get('Basis', 0) * 100

        smc_reasons_str = str(data.get('SMC_Reasons', ''))
        smc_txt = "None"
        if "Order Block" in smc_reasons_str:
            smc_txt = "🟢 Demand Zone" if "Bullish" in smc_reasons_str else "🔴 Supply Zone"
        elif "Structure" in smc_reasons_str:
            smc_txt = "📈 Higher Low" if "Higher Low" in smc_reasons_str else "📉 Lower High"
        elif data['SMC_Score'] > 0:
            smc_txt = "✅ Confluence Found"

        caption = (
            f"{emoji} <b>SIGNAL: {symbol} ({data['Pattern']})</b>\n"
            f"<b>{data['Side']}</b>  |  <b>{data['Timeframe']}</b>\n\n"
            f"🎯 <b>Entry:</b> <code>{format_price(data['Entry'])}</code>\n"
            f"🛑 <b>Stop:</b>  <code>{format_price(data['SL'])}</code>\n"
            f"💰 <b>RR:</b>    1:{data.get('RR', 0.0)}\n\n"
            f"🏁 <b>Targets</b>\n"
            f"  TP1: <code>{format_price(data['TP1'])}</code>\n"
            f"  TP2: <code>{format_price(data['TP2'])}</code>\n"
            f"  TP3: <code>{format_price(data['TP3'])}</code>\n\n"
            f"📊 <b>Technicals</b>\n"
            f"  Pattern: {data['Pattern']}\n"
            f"  SMC: {smc_txt}\n\n"
            f"🧮 <b>Quant Models</b>\n"
            f"  RVOL: <code>{rvol:.1f}x</code> ({rvol_txt})\n"
            f"  Z-Score: <code>{data.get('Z_Score', 0):.2f}σ</code>\n"
            f"  ζ-Field: <code>{data.get('Zeta_Score', 0):.1f}</code> / 100\n"
            f"  OBI: <code>{obi_val:.2f}</code> {obi_icon}\n\n"
            f"⛽ <b>Derivatives</b>\n"
            f"  Funding: <code>{fund_pct:.4f}%</code> {fund_icon} ({fund_txt})\n"
            f"  Basis: <code>{basis_pct:.4f}%</code>\n"
            f"  Bias: {data.get('Deriv_Reasons', 'Neutral')}\n\n"
            f"🏆 <b>Scores</b>\n"
            f"  Tech: <code>{data['Tech_Score']}</code> | SMC: <code>{data['SMC_Score']}</code> | "
            f"Quant: <code>{data['Quant_Score']}</code> | Deriv: <code>{data['Deriv_Score']}</code>\n\n"
            f"📝 <b>Analysis</b>\n"
            f"  Tech: {data.get('Tech_Reasons', '-')}\n"
            f"  SMC: {smc_reasons_str if smc_reasons_str else '-'}\n"
            f"  Quant: {data.get('Quant_Reasons', '-')}\n\n"
            f"🧠 <b>Context</b>  Bias: <b>{data['BTC_Bias']}</b>\n"
            f"<i>V8 Bot | {get_now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )

        resp = None
        if image_path:
            with open(image_path, 'rb') as f:
                resp = _tg('sendPhoto', token,
                           data={'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'},
                           files={'photo': f})
        else:
            resp = _tg('sendMessage', token,
                       json={'chat_id': chat_id, 'text': caption, 'parse_mode': 'HTML'})

        if resp and resp.get('ok'):
            msg = resp.get('result', {})
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
                "smc_reasons":   smc_reasons_str,
                "message_id":    str(msg.get('message_id', '')),
                "channel_id":    str(chat_id),
            })
            return True

    except Exception as e:
        print(f"Alert Error: {e}")
        return False
    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)


def update_status_dashboard():
    token   = CONFIG['api'].get('telegram_bot_token')
    chat_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not chat_id:
        return

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

        msg_id = get_state('dashboard_msg_id')

        if msg_id:
            resp = _tg('editMessageText', token,
                       json={'chat_id': chat_id, 'message_id': int(msg_id),
                             'text': content, 'parse_mode': 'Markdown'})
            # Jika edit gagal (pesan dihapus / expired / konten sama) → reset & kirim baru
            if resp is None or not resp.get('ok'):
                err = (resp or {}).get('description', '')
                # "message is not modified" = konten sama, tidak perlu kirim baru
                if 'not modified' not in err.lower():
                    set_state('dashboard_msg_id', '')
                    _send_new_dashboard(token, chat_id, content)
        else:
            _send_new_dashboard(token, chat_id, content)

    except Exception:
        pass


def _send_new_dashboard(token: str, chat_id: str, content: str):
    """Kirim pesan dashboard baru dan simpan message_id-nya."""
    resp = _tg('sendMessage', token,
               json={'chat_id': chat_id, 'text': content, 'parse_mode': 'Markdown'})
    if resp and resp.get('ok'):
        set_state('dashboard_msg_id', str(resp['result']['message_id']))


def run_fast_update():
    update_status_dashboard()


def send_scan_completion(count, duration, bias, auto_trade: bool = False):
    token   = CONFIG['api'].get('telegram_bot_token')
    chat_id = CONFIG['api'].get('telegram_chat_id')
    if not token or not chat_id:
        return
    bias_emoji = "📈" if "Bullish" in bias else ("📉" if "Bearish" in bias else "↔️")
    text = (
        f"🔭 *Scan Cycle Complete*\n"
        f"⏱️ Duration: `{duration:.2f}s`\n"
        f"📶 Signals:  `{count}`\n"
        f"{bias_emoji} Bias:     *{bias}*"
    )
    try:
        _tg('sendMessage', token,
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'})
    except:
        pass