#!/usr/bin/env python3
"""Prepare local evidence candidates; never infer personality or access WeChat."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import xml.etree.ElementTree as ET

TOPICS = {
    '目标与动力': r'喜欢|想要|希望|目标|方向|长期|创作|研究|意义|自由',
    '工作与约束': r'成本|收入|利润|报价|时间|团队|负责|排期|交付|修改|拒绝|承诺',
    '生活与关系': r'陪|家人|朋友|休息|睡|累|压力|开心|担心|生气|难受',
}


def moment(value):
    d = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('时间必须带时区，例如 +08:00')
    return d


def ownership(value):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ('true', 'false', '0', '1'):
        return value.lower() in ('true', '1')
    return None


def body(row):
    kind = row.get('kind_name', '')
    s = str(row.get('text') or row.get('content') or '')
    quoted = kind == 'quote'
    if quoted:
        try:
            s = ET.fromstring(s).findtext('.//appmsg/title') or '[引用回复不可解析]'
        except ET.ParseError:
            s = '[引用回复不可解析]'
    elif kind not in ('text', ''):
        s = '[' + str(kind) + '：附件或非纯文本未读取]'
    elif s.lstrip().startswith('<'):
        s = '[结构化内容未展开]'
    s = re.sub(r'https?://\S+', '[链接省略]', s)
    s = re.sub(r'(?im)^.*(?:验证码|密码|密钥|aeskey|税号|收件人|开户行|卡号|收款账户|手机号码).*$', '[敏感字段省略]', s)
    s = re.sub(r'\b\d{7,}\b', '[长号码省略]', s)
    return s[:2000], len(s) > 2000, quoted


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--since', required=True)
    ap.add_argument('--until', required=True)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--per-chat', type=int, default=2)
    ap.add_argument('--sample-limit', type=int, default=160)
    ap.add_argument('--topic-limit', type=int, default=45)
    args = ap.parse_args()
    start, end = moment(args.since), moment(args.until)
    if start >= end or min(args.per_chat, args.sample_limit, args.topic_limit) < 1:
        raise ValueError('窗口或样本上限无效')
    if args.output.exists():
        raise ValueError('输出目录已存在，请选新目录，避免覆盖旧证据')
    rows, skipped, source_digest = [], collections.Counter(), hashlib.sha256()
    with args.input.open('rb') as f:
        for line_no, line in enumerate(f, 1):
            source_digest.update(line)
            if not line.strip():
                skipped['空行'] += 1
                continue
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError(f'第 {line_no} 行不是消息对象')
            timestamp = r.get('create_time')
            if timestamp is None:
                timestamp = moment(r['time']).timestamp()
            timestamp = float(timestamp)
            if not start.timestamp() <= timestamp < end.timestamp():
                skipped['窗口外'] += 1
                continue
            if not r.get('talker'):
                raise ValueError(f'第 {line_no} 行缺少会话标识')
            r['_line'], r['_ts'], r['_own'] = line_no, timestamp, ownership(r.get('from_me'))
            rows.append(r)
    rows.sort(key=lambda r: (r['_ts'], r['_line']))
    chats = sorted({r['talker'] for r in rows})
    labels = {c: '会话%04d' % (i + 1) for i, c in enumerate(chats)}
    grouped = collections.defaultdict(list)
    for r in rows:
        grouped[r['talker']].append(r)
    own = [r for r in rows if r['_own'] is True]
    eligible = [r for r in own if r.get('kind_name') in ('text', 'quote') and len(body(r)[0]) >= 12]
    candidates, rng = {}, random.Random(args.seed)
    for c in chats:
        available = [r for r in eligible if r['talker'] == c]
        selected = rng.sample(available, min(args.per_chat, len(available)))
        for r in selected:
            candidates[r['_line']] = {'row': r, 'reasons': ['跨会话抽样']}
    if len(candidates) > args.sample_limit:
        candidates = dict(rng.sample(list(candidates.items()), args.sample_limit))
    for topic, pattern in TOPICS.items():
        hits = [r for r in eligible if re.search(pattern, body(r)[0])]
        rng.shuffle(hits)
        per_chat = collections.Counter()
        selected_count = 0
        for r in hits:
            if per_chat[r['talker']] >= 3:
                continue
            item = candidates.setdefault(r['_line'], {'row': r, 'reasons': []})
            item['reasons'].append(topic)
            per_chat[r['talker']] += 1
            selected_count += 1
            if selected_count >= args.topic_limit:
                break
    positions = {r['_line']: i for rs in grouped.values() for i, r in enumerate(rs)}

    def render(r):
        text, truncated, quoted = body(r)
        return {'源行号': r['_line'], '时间': dt.datetime.fromtimestamp(r['_ts'], start.tzinfo).isoformat(),
                '会话': labels[r['talker']], '发言人': '本人' if r['_own'] is True else ('他人' if r['_own'] is False else '未知'),
                '类型': r.get('kind_name'), '正文或占位': text, '截断': truncated, '仅引用新回复': quoted}

    packet = []
    for n, item in sorted(candidates.items(), key=lambda kv: (kv[1]['row']['_ts'], kv[0])):
        r = item['row']
        i, rs = positions[n], grouped[r['talker']]
        packet.append({'候选': render(r), '筛选原因': item['reasons'],
                       '上下文': [render(x) for x in rs[max(0, i - 3):i + 4]]})
    summary = {'开始包含': start.isoformat(), '截止不包含': end.isoformat(), '窗口消息数': len(rows),
               '本人消息数': len(own), '本人参与会话数': len({r['talker'] for r in own}),
               '本人私聊消息数': sum(not r['talker'].endswith('@chatroom') for r in own),
               '本人标识未知消息数': sum(r['_own'] is None for r in rows),
               '本人各类型': dict(collections.Counter(r.get('kind_name', '未知') for r in own)),
               '本人发言时段': dict(sorted(collections.Counter(dt.datetime.fromtimestamp(r['_ts'], start.tzinfo).hour for r in own).items())),
               '过滤统计': dict(skipped), '候选数': len(packet), '输入sha256': source_digest.hexdigest(),
               '限制': '仅结构统计与词表/抽样候选，不是人格结论；未读取附件和语音。姓名去除不等于正文完全匿名，仍须本地保存；转发文本仍需人工辨认。'}
    os.umask(0o077)
    args.output.mkdir(parents=True)
    for filename, value in [('统计.json', summary), ('上下文候选.json', packet)]:
        (args.output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'完成': True, '窗口消息数': len(rows), '本人消息数': len(own), '候选数': len(packet)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
