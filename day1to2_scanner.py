# -*- coding: utf-8 -*-
"""
1进2晋级扫描器 v2：
- 拉近 N 个交易日每天的涨停池（akshare stock_zt_pool_em）——相邻日池子判定晋级（准确）
- K线/量比特征走腾讯实时接口（web.ifzq.gtimg.cn fqkline，不受本机 .day 滞后影响）
- 每只"首板票"：1进2（次日进2板）、2进3（第3天进3板）、累计最高肉（10日内）
- 落盘 day1to2.json
"""
import os, sys, json, time, datetime, struct
import pandas as pd
import akshare as ak
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, 'day1to2.json')

# akshare 涨停池约3周数据，但判定需要 +2 天
FETCH_DAYS = 9       # 拉池天数（含判定用次日）
KLINE_DAYS = 6       # 最近 N 天拉 K 线入列表（用户关注最近）

def get_trade_dates(n):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=n + 20)
    try:
        df = ak.tool_trade_date_hist_sina()
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
        recent = df[(df['trade_date'] >= start) & (df['trade_date'] <= end)].sort_values('trade_date', ascending=False)
        return [d.strftime('%Y%m%d') for d in recent['trade_date'].head(n)]
    except Exception as e:
        print('trade_date err:', e, flush=True)
        return []

def fetch_zt_pool(date_str):
    """东方财富涨停池"""
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        if df is None or df.empty:
            return []
        rows = []
        for _, r in df.iterrows():
            rows.append({
                'code': str(r.get('代码','')).zfill(6),
                'name': str(r.get('名称','')),
                'industry': str(r.get('所属行业','')),
                'amount': float(r.get('成交额', 0) or 0) / 1e8,  # 元 → 亿
                'mktcap': float(r.get('流通市值', 0) or 0) / 1e8,  # 元 → 亿
                'turnover': float(r.get('换手率', 0) or 0),
                'fb_money': float(r.get('封板资金', 0) or 0) / 1e8,  # 元 → 亿
                'first_time': str(r.get('首次封板时间','')).strip(),
                'zb_count': int(r.get('炸板次数', 0) or 0),
                'boards': int(r.get('连板数', 1) or 1),
                'pct': float(r.get('涨跌幅', 0) or 0),
            })
        return rows
    except Exception as e:
        print(f'zt_pool {date_str} err:', e, flush=True)
        return []

_last_kline_req = [0.0]  # 全局限速（腾讯备胎）

VIP = r'C:/new_tdx/vipdoc'

def code_to_path(code):
    if code.startswith('8') or code.startswith('4'):
        mkt = 'bj'
    elif code.startswith('6'):
        mkt = 'sh'
    else:
        mkt = 'sz'
    return os.path.join(VIP, mkt, 'lday', f'{mkt}{code}.day')

def load_day_kline(code, n=200):
    """本地通达信 .day：返回 [{date,open,close,high,low,vol}] 或 None"""
    p = code_to_path(code)
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'rb') as f:
            data = f.read()
        rec = 32
        total = len(data) // rec
        if total == 0:
            return None
        bars = []
        start = max(0, total - n)
        for i in range(start, total):
            d, o, h, l, c, amount, vol, _ = struct.unpack('<iiiiifii', data[i*rec:(i+1)*rec])
            bars.append({'date': d, 'open': o/100.0, 'high': h/100.0, 'low': l/100.0, 'close': c/100.0, 'vol': float(vol)})
        return bars
    except Exception as e:
        print(f'day_kline {code} err: {e}', flush=True)
        return None

def fetch_tx_kline(code, count=160):
    """腾讯前复权日线：返回 [{date,open,close,high,low,vol}] 或 None（带限速+重试）"""
    qcode = ('sh' if code.startswith('6') else 'bj' if (code.startswith('8') or code.startswith('4')) else 'sz') + code
    url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qcode},day,,,{count},qfq'
    for attempt in range(3):
        # 限速：每次请求间隔 >= 0.35s
        gap = time.time() - _last_kline_req[0]
        if gap < 0.35:
            time.sleep(0.35 - gap)
        _last_kline_req[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                j = json.loads(resp.read().decode('utf-8'))
            node = j['data'][qcode]
            bars_raw = node.get('qfqday') or node.get('day')
            if not bars_raw:
                return None
            bars = []
            for b in bars_raw:
                # row = [date, open, close, high, low, vol]
                bars.append({
                    'date': int(str(b[0]).replace('-', '').replace('/', '')),
                    'open': float(b[1]),
                    'close': float(b[2]),
                    'high': float(b[3]),
                    'low': float(b[4]),
                    'vol': float(b[5]) if len(b) > 5 else 0,
                })
            return bars
        except Exception as e:
            if attempt < 2:
                time.sleep(0.8 + attempt * 0.8)  # 退避
            else:
                print(f'tx_kline {code} err: {e}', flush=True)
    return None

def calc_features(code, sig_idx, bars):
    """根据腾讯日线 bars 计算首板日特征"""
    if not bars or sig_idx is None or sig_idx < 5:
        return None
    cur = bars[sig_idx]
    vol_today = cur['vol']
    vol_prev5 = sum(b['vol'] for b in bars[sig_idx-5:sig_idx]) / 5
    vol_ratio = vol_today / vol_prev5 if vol_prev5 > 0 else 0
    # 近 60 日涨停次数（收盘涨幅 >9.5%，粗略）
    zt60 = 0
    last_zt_days = -1
    for i in range(sig_idx - 1, -1, -1):
        if bars[i]['close'] > 0 and bars[i]['open'] > 0 and (bars[i]['close'] - bars[i]['open']) / bars[i]['open'] > 0.095:
            zt60 += 1
            if last_zt_days == -1:
                last_zt_days = sig_idx - i
    return {
        'vol_ratio': round(vol_ratio, 2),
        'zt_count_60d': zt60,
        'days_since_last_zt': last_zt_days,
        'sig_close': cur['close'],
    }

def main():
    print(f'[scan] start at {datetime.datetime.now()}', flush=True)
    dates = get_trade_dates(FETCH_DAYS)
    print(f'[scan] trade dates: {dates}', flush=True)

    # 1. 每天涨停池 → 缓存 dict
    pool = {}
    for d in dates:
        pool[d] = fetch_zt_pool(d)
        print(f'[scan] {d} 涨停 {len(pool[d])} 只', flush=True)
        time.sleep(1.0)

    # 2. 对每天首板，判定 1进2 / 2进3（K 线仅最近 KLINE_DAYS 拉）
    samples = []
    recent_cut = dates[:KLINE_DAYS]  # 这些天入列表
    for i in range(1, len(recent_cut)):  # i=0 是最新交易日（无次日，跳过）
        d = recent_cut[i]
        rows = pool.get(d, [])
        first_boards = [r for r in rows if r['boards'] == 1]
        next_d = dates[i-1]  # 降序：前一个元素 = 更新的交易日
        next_pool = pool.get(next_d, [])
        next_codes_2plus = {r['code'] for r in next_pool if r['boards'] >= 2}  # 次日已 2 板+
        next_pct_map = {r['code']: r.get('pct') for r in next_pool}
        third_d = dates[i-2] if i - 2 >= 0 else None
        third_codes_3plus = set()
        if third_d:
            third_codes_3plus = {r['code'] for r in pool.get(third_d, []) if r['boards'] >= 3}

        for r in first_boards:
            code = r['code']
            # 本地 .day 优先（最新到 9/8）；缺失或 sig_date 比 .day 新则跳过（等数据更新）
            bars = load_day_kline(code, n=200)
            if not bars:
                continue
            sig_idx = next((idx for idx, b in enumerate(bars) if b['date'] == int(d)), None)
            if sig_idx is None:
                # 本地 .day 还没覆盖首板日（最新样本），尝试腾讯单票补（只补最新一天的）
                if int(d) > bars[-1]['date'] and dates.index(d) <= 2:
                    bars = fetch_tx_kline(code, count=200)
                    if not bars:
                        continue
                    sig_idx = next((idx for idx, b in enumerate(bars) if b['date'] == int(d)), None)
                if sig_idx is None:
                    continue
            feats = calc_features(code, sig_idx, bars)
            if not feats:
                continue

            success = code in next_codes_2plus
            success2 = success and code in third_codes_3plus

            # 次日涨跌幅（腾讯K线保证有次日；没有则从次日涨停池涨跌幅兜底）
            next_pct = None
            if sig_idx + 1 < len(bars):
                nxt = bars[sig_idx + 1]
                next_pct = round((nxt['close'] / feats['sig_close'] - 1) * 100, 2)
            if next_pct is None:
                next_pct = next_pct_map.get(code)

            # 累计最高肉：首板收盘 → 10日内最高收盘（.day 有后续数据才算）
            max_pct = 0.0
            if sig_idx + 1 < len(bars):
                for j in range(sig_idx + 1, min(sig_idx + 11, len(bars))):
                    p = (bars[j]['close'] / feats['sig_close'] - 1) * 100
                    if p > max_pct:
                        max_pct = p
            # 最新样本（.day 无后续）但已晋级：用次日涨停涨幅兜底，避免虚 0
            if max_pct == 0.0 and success and next_pct:
                max_pct = next_pct
            max_pct = round(max_pct, 2)

            samples.append({
                'code': code,
                'name': r['name'],
                'sig_date': d,
                'next_date': next_d,
                'industry': r['industry'],
                'first_time': r['first_time'],
                'zb_count': r['zb_count'],
                'fb_money': r['fb_money'],  # 亿
                'amount': r['amount'],  # 亿
                'mktcap': r['mktcap'],  # 亿
                'turnover': r['turnover'],
                'vol_ratio': feats['vol_ratio'],
                'zt_count_60d': feats['zt_count_60d'],
                'days_since_last_zt': feats['days_since_last_zt'],
                'sig_close': feats['sig_close'],
                'success': success,
                'success2': success2,
                'next_pct': next_pct,
                'max_pct': max_pct,
            })

        succ_n = sum(1 for s in samples if s['sig_date'] == d and s['success'])
        print(f'[scan] {d} 首板 {len(first_boards)} 只 → 晋级 {succ_n} 只', flush=True)

    # 3. 落盘
    out = {
        'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total': len(samples),
        'success_count': sum(1 for s in samples if s['success']),
        'history_days': KLINE_DAYS,
        'samples': samples,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))

    # 统计
    succ = [s for s in samples if s['success']]
    fail = [s for s in samples if not s['success']]
    succ2 = [s for s in succ if s['success2']]
    print('[scan] === DONE ===', flush=True)
    print(f'[scan] 总样本 {len(samples)} 成功 {len(succ)} 失败 {len(fail)} 成功率 {len(succ)/len(samples)*100 if samples else 0:.1f}%', flush=True)
    if succ:
        avg_max = sum(s['max_pct'] for s in succ) / len(succ)
        med_max = sorted(s['max_pct'] for s in succ)[len(succ)//2]
        print(f'[scan] 1进2成功 {len(succ)} 只 → 2进3成功 {len(succ2)} 只（吃肉概率 {len(succ2)/len(succ)*100:.1f}%）', flush=True)
        print(f'[scan] 1进2成功后：累计最高涨幅均值 {avg_max:.1f}% / 中位数 {med_max:.1f}%', flush=True)
        big15 = sum(1 for s in succ if s['max_pct'] >= 15)
        big30 = sum(1 for s in succ if s['max_pct'] >= 30)
        print(f'[scan] 其中肉>=15% 的 {big15} 只（{big15/len(succ)*100:.0f}%），肉>=30% 的 {big30} 只', flush=True)
    # 特征对比
    if succ and fail:
        for k in ['vol_ratio', 'mktcap', 'fb_money', 'turnover', 'zt_count_60d']:
            sm = sum(s[k] for s in succ) / len(succ)
            fm = sum(s[k] for s in fail) / len(fail)
            print(f'  {k}: 成功 {sm:.2f} 失败 {fm:.2f}', flush=True)

if __name__ == '__main__':
    main()
