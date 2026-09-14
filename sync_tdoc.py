"""
腾讯文档数据同步脚本
功能：每日收盘后自动将关键 JSON 数据推送到腾讯文档
用法：python sync_tdoc.py
定时：Windows 任务计划程序，交易日 16:00 执行
"""
import os, json, sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))

# 腾讯文档 MCP 调用（openclaw 环境）
try:
    from openclaw_tools import qclaw_tdoc_mcp_call
    HAS_TDOC = True
except ImportError:
    HAS_TDOC = False

# 要同步的文件（相对于 BASE）
SYNC_FILES = [
    'day1to2.json',
    'dragon_back.json',
    'signals.json',
    'market_sentiment.json',
]

def load_json(name):
    path = os.path.join(BASE, name)
    if not os.path.exists(path):
        return None, None
    mtime = os.path.getmtime(path)
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data, mtime

def build_mdx_summary(name, data, mtime):
    ts = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
    lines = [f"# {name}", "", f"**更新时间：{ts}**", ""]

    if name == 'day1to2.json' and isinstance(data, dict):
        samples = data.get('samples', [])
        total = data.get('total', 0)
        updated = data.get('updated', '')
        by_date = {}
        for s in samples:
            d = s.get('sig_date', 'unknown')
            by_date.setdefault(d, []).append(s)
        lines += ["", f"**样本总数：{total}**  |  **更新时间：{updated}**", ""]
        for sig_date in sorted(by_date.keys(), reverse=True):
            items = by_date[sig_date]
            success = [x for x in items if x.get('success')]
            lines += ["", f"## {sig_date}  首板日",
                       f"共 {len(items)} 只  |  晋级成功 {len(success)} 只", ""]
            for s in sorted(items, key=lambda x: x.get('next_pct') or -999, reverse=True):
                pct = s.get('next_pct') or 0
                status = 'SUCCESS' if s.get('success') else ('POS' if pct > 0 else 'NEG')
                lines.append(f"- {status}  {s.get('code')} {s.get('name','--')}  晋级:{pct:+.2f}%  板块:{s.get('industry','--')}")

    elif name == 'signals.json' and isinstance(data, list):
        lines += ["", f"**关注信号总数：{len(data)}**", ""]
        for s in data:
            starred = '[STAR]' if s.get('starred') else '      '
            lines.append(f"- {starred} {s.get('code')} {s.get('name','--')}  信号日:{s.get('date')}  状态:{s.get('status','观察中')}")
            if s.get('note'):
                lines.append(f"  备注: {s.get('note')}")

    elif name == 'dragon_back.json' and isinstance(data, list):
        lines += ["", f"**龙回头股票：{len(data)} 只**", ""]
        for s in data:
            pinned = '[PIN]' if s.get('pinned') else '     '
            lines.append(f"- {pinned} {s.get('code')} {s.get('name','--')}")

    elif name == 'market_sentiment.json':
        if isinstance(data, dict):
            latest = data.get('latest', {})
            updated = data.get('updated', '')
            lines += ["", f"**市场情绪**  更新:{updated}", ""]
            if latest:
                for k, v in latest.items():
                    if v is not None and str(v) not in ('nan', 'None', ''):
                        lines.append(f"- **{k}**：{v}")

    else:
        snippet = json.dumps(data, ensure_ascii=False, indent=2)[:3000]
        lines += ["", f"```json", snippet, "```"]

    return '\n'.join(lines)


def sync_to_tdoc(name, mdx_content):
    if not HAS_TDOC:
        print(f"[WARN] 腾讯文档 MCP 不可用: {name}")
        return False
    doc_title = f"打板-{name.replace('.json','')}-{datetime.now().strftime('%m%d')}"
    if len(doc_title) > 36:
        doc_title = doc_title[:36]
    try:
        qclaw_tdoc_mcp_call(
            tool_name='create_smartcanvas_by_mdx',
            arguments={'title': doc_title, 'mdx': mdx_content, 'content_format': 'mdx'}
        )
        print(f"  [OK] {name} -> {doc_title}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return False


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 腾讯文档同步开始")
    success = 0
    for fname in SYNC_FILES:
        data, mtime = load_json(fname)
        if data is None:
            print(f"  [SKIP] {fname} 不存在")
            continue
        print(f"处理 {fname}...")
        mdx = build_mdx_summary(fname, data, mtime)
        if HAS_TDOC:
            if sync_to_tdoc(fname, mdx):
                success += 1
        else:
            print(f"  [PREVIEW] {fname} (MCP 不可用，本地预览):")
            print(mdx[:800])
            success += 1
    print(f"\n完成！成功 {success}/{len(SYNC_FILES)}")
    return success


if __name__ == '__main__':
    main()
