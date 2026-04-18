import requests, json, os, pytz, pandas as pd, numpy as np
import mplfinance as mpf
from scipy.signal import argrelextrema
from datetime import datetime
from modules.config_loader import CONFIG
from modules.database import insert_trade, get_trades_open, get_state, set_state

def get_now(): return datetime.now(pytz.timezone(CONFIG['system']['timezone']))
def format_price(value): return "{:.8f}".format(float(value)).rstrip('0').rstrip('.') if float(value) < 1 else "{:.2f}".format(float(value))

def generate_chart(df, symbol, pattern, timeframe):
    filename = f"chart_{symbol.replace('/','_')}_{timeframe}.png"
    try:
        plot_df = df.iloc[-100:].copy()
        if 'timestamp' in plot_df.columns: plot_df.set_index('timestamp', inplace=True)
        plot_df.index = pd.to_datetime(plot_df.index)

        n = 3
        min_idx = argrelextrema(plot_df['low'].values, np.less_equal, order=n)[0]
        max_idx = argrelextrema(plot_df['high'].values, np.greater_equal, order=n)[0]

        peak_dates, peak_vals   = plot_df.index[max_idx], plot_df['high'].iloc[max_idx].values
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


def send_alert(data):
    webhook = CONFIG['api']['discord_webhook']
    if not webhook: return False

    symbol     = data['Symbol']
    image_path = None

    # 1. Generate Chart
    try:
        image_path = generate_chart(data['df'], symbol, data['Pattern'], data['Timeframe'])
    except Exception as e:
        print(f"❌ Chart Error: {e}")

    try:
        is_long = data['Side'] == 'Long'
        color   = 0x00ff00 if is_long else 0xff0000
        emoji   = "🚀" if is_long else "🔻"

        rvol      = data['df']['RVOL'].iloc[-1]
        rvol_txt  = "⚡ Explosive" if rvol > 3.0 else ("🔥 Strong" if rvol > 2.0 else "🌊 Normal")
        obi_val   = data.get('OBI', 0.0)
        obi_icon  = "🟢" if obi_val > 0 else ("🔴" if obi_val < 0 else "⚪")

        quant_block = (
            f"**RVOL:** `{rvol:.1f}x` ({rvol_txt})\n"
            f"**Z-Score:** `{data.get('Z_Score', 0):.2f}σ`\n"
            f"**ζ-Field:** `{data.get('Zeta_Score', 0):.1f}` / 100\n"
            f"**OBI:** `{obi_val:.2f}` {obi_icon}"
        )

        fund_rate = data['df'].get('funding', pd.Series([0])).iloc[-1]
        if isinstance(fund_rate, pd.Series): fund_rate = fund_rate.iloc[-1]
        fund_pct  = fund_rate * 100
        fund_icon = "🔴" if fund_pct > 0.01 else "🟢"
        fund_txt  = "Hot" if fund_pct > 0.01 else "Cool"
        basis_pct = data.get('Basis', 0) * 100

        deriv_block = (
            f"**Funding:** `{fund_pct:.4f}%` {fund_icon} ({fund_txt})\n"
            f"**Basis:** `{basis_pct:.4f}%`\n"
            f"**Bias:** {data.get('Deriv_Reasons', 'Neutral')}"
        )

        smc_reasons_str = str(data.get('SMC_Reasons', ''))
        smc_txt = "None"
        if "Order Block" in smc_reasons_str:
            smc_txt = "🟢 Demand Zone" if "Bullish" in smc_reasons_str else "🔴 Supply Zone"
        elif "Structure" in smc_reasons_str:
            smc_txt = "📈 Higher Low" if "Higher Low" in smc_reasons_str else "📉 Lower High"
        elif data['SMC_Score'] > 0:
            smc_txt = "✅ Confluence Found"

        scores_txt = (
            f"Tech: `{data['Tech_Score']}` | "
            f"SMC: `{data['SMC_Score']}` | "
            f"Quant: `{data['Quant_Score']}` | "
            f"Deriv: `{data['Deriv_Score']}`"
        )

        analysis_txt = (
            f"**Tech:** {data.get('Tech_Reasons', '-')}\n"
            f"**SMC:** {smc_reasons_str if smc_reasons_str else '-'}\n"
            f"**Quant:** {data.get('Quant_Reasons', '-')}"
        )

        legend_txt = (
            "• **Z-Score:** `>3.0`=Nuclear | **ζ-Field:** `>70`=High Prob\n"
            "• **OBI:** `>0.3`=Bullish Book | **Funding:** `>0.01%`=Expensive"
        )

        embed = {
            "title": f"{emoji} SIGNAL: {symbol} ({data['Pattern']})",
            "description": f"**{data['Side']}** | **{data['Timeframe']}**",
            "color": color,
            "fields": [
                {"name": "🎯 Entry",  "value": f"`{format_price(data['Entry'])}`", "inline": True},
                {"name": "🛑 Stop",   "value": f"`{format_price(data['SL'])}`",    "inline": True},
                {"name": "💰 Rewards","value": f"RR: **1:{data.get('RR', 0.0)}**", "inline": True},

                {"name": "🏁 Targets", "value": f"TP1: `{format_price(data['TP1'])}`\nTP2: `{format_price(data['TP2'])}`\nTP3: `{format_price(data['TP3'])}`", "inline": False},
                {"name": "📊 Technicals", "value": f"**Pattern:** {data['Pattern']}\n**Trend:** {emoji} {data['Side']}\n**SMC:** {smc_txt}", "inline": False},

                {"name": "🧮 Quant Models", "value": quant_block,  "inline": True},
                {"name": "⛽ Derivatives",  "value": deriv_block,  "inline": True},

                {"name": "🏆 Scores",           "value": scores_txt,   "inline": False},
                {"name": "📝 Detailed Analysis","value": analysis_txt,  "inline": False},
                {"name": "ℹ️ Metrics Guide",     "value": legend_txt,   "inline": False},
                {"name": "🧠 Context",           "value": f"Bias: **{data['BTC_Bias']}**", "inline": False},
            ],
            "footer": {"text": f"V8 Bot | {get_now().strftime('%Y-%m-%d %H:%M:%S')}"}
        }

        payload = {"content": "", "embeds": [embed]}

        if image_path:
            with open(image_path, 'rb') as f:
                r = requests.post(webhook, data={'payload_json': json.dumps(payload)}, files={'file': f}, params={"wait": "true"})
        else:
            r = requests.post(webhook, json=payload, params={"wait": "true"})

        # 2. Save to JSON
        if r.status_code in [200, 201]:
            resp_json = r.json()
            insert_trade({
                "symbol":       symbol,
                "side":         data['Side'],
                "timeframe":    data['Timeframe'],
                "pattern":      data['Pattern'],
                "entry_price":  data['Entry'],
                "sl_price":     data['SL'],
                "tp1":          data['TP1'],
                "tp2":          data['TP2'],
                "tp3":          data['TP3'],
                "rr":           data['RR'],
                "reason":       data['Reason'],
                "tech_score":   data['Tech_Score'],
                "quant_score":  data['Quant_Score'],
                "deriv_score":  data['Deriv_Score'],
                "smc_score":    data['SMC_Score'],
                "basis":        data['Basis'],
                "btc_bias":     data['BTC_Bias'],
                "z_score":      data['Z_Score'],
                "zeta_score":   data['Zeta_Score'],
                "obi":          data['OBI'],
                "tech_reasons": data.get('Tech_Reasons', ''),
                "quant_reasons":data.get('Quant_Reasons', ''),
                "deriv_reasons":data.get('Deriv_Reasons', ''),
                "smc_reasons":  smc_reasons_str,
                "message_id":   resp_json.get('id'),
                "channel_id":   resp_json.get('channel_id'),
            })
            return True

    except Exception as e:
        print(f"Alert Error: {e}")
        return False
    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)


def update_status_dashboard():
    webhook = CONFIG['api']['discord_dashboard_webhook']
    if not webhook: return

    try:
        trades = get_trades_open()
        lines  = []
        for t in sorted(trades, key=lambda x: x.get('created_at', ''), reverse=True):
            ts  = t.get('entry_hit_at') or t.get('created_at', '')
            try:    ts_str = datetime.fromisoformat(str(ts)).strftime('%H:%M')
            except: ts_str = "??:??"
            icon = '🟢' if 'Active' in t.get('status', '') else '⏳'
            lines.append(f"`{ts_str}` {icon} **{t['symbol']}** ({t['side']}): {t.get('status','')}")

        content = "**📊 LIVE DASHBOARD**\n" + ("\n".join(lines) if lines else "No active trades.")

        msg_id = get_state('dashboard_msg_id')

        if msg_id:
            requests.patch(f"{webhook}/messages/{msg_id}", json={"content": content})
        else:
            r = requests.post(webhook, json={"content": content}, params={"wait": "true"})
            if r.status_code in [200, 201]:
                set_state('dashboard_msg_id', r.json().get('id'))

    except Exception:
        pass


def run_fast_update():
    update_status_dashboard()


def send_scan_completion(count, duration, bias):
    webhook = CONFIG['api']['discord_webhook']
    if not webhook: return
    color = 0x00ff00 if "Bullish" in bias else (0xff0000 if "Bearish" in bias else 0x808080)
    embed = {
        "title": "🔭 Scan Cycle Complete",
        "color": color,
        "fields": [
            {"name": "⏱️ Duration", "value": f"`{duration:.2f}s`", "inline": True},
            {"name": "📶 Signals",  "value": f"`{count}`",         "inline": True},
            {"name": "📊 Bias",     "value": f"**{bias}**",        "inline": True},
        ]
    }
    try: requests.post(webhook, json={"embeds": [embed]})
    except: pass
