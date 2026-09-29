"""Blog charts for the harness search post. Data comes from results.json only."""
import json, os
from collections import defaultdict
from xml.sax.saxutils import escape

R = json.load(open(os.path.expanduser('~/orbi-bench/results.json')))
OUT = os.path.expanduser('~/Projects/orbi/website/public/img/diagrams')
REPOS = ['pyinfra', 'chainloop', 'fedify']
c = defaultdict(lambda: [0, 0])
for x in R:
    for k in (x['variant'], (x['variant'], x['repo'])):
        c[k][1] += 1; c[k][0] += bool(x['core'])

INK, SOFT, LINE, BG = '#10292c', '#526566', '#b8c5c1', '#f7f8f5'
GOOD, MID, BAD, ACC = '#0a6b52', '#8a5b00', '#c2413a', '#0a6b52'
FONT = "Instrument Sans, DejaVu Sans, PingFang SC, Noto Sans CJK SC, sans-serif"
MONO = "IBM Plex Mono, DejaVu Sans Mono, monospace"

L = {
 'en': dict(
   trend_title='Same model, harness versions: pass rate', trend_sub='deepseek-flash implements and reviews · passes / runs across three repos',
   grid_title='Harness × model', grid_sub='passes / runs across three repos · blank = not run · * chainloop only',
   repo_title='Per repository: original harness vs v13–v15', repo_sub='deepseek-flash · passes / runs',
   orig='original harness', final='v13–v15', impl='implementer / reviewer', few='pale bars: fewer than 5 runs, weak evidence',
   names={'v0': 'original', 'v1': 'compare before/after', 'v4': 'v1 as skill', 'v5': 'generated inputs', 'v10': '+ contribution skill',
          'v11': 'independent oracle', 'v13': 'stripped chars as data', 'v14': 'reference cross-check', 'v15': 'collision checks', 'v17': 'producer table'}),
 'zh': dict(
   trend_title='同一个模型，不同 harness 版本的通过率', trend_sub='实现和评审都用 deepseek-flash · 三个仓库合计「通过 / 运行」',
   grid_title='harness 版本 × 模型组合', grid_sub='三个仓库合计「通过 / 运行」· 空白 = 没跑 · * 只跑了 chainloop',
   repo_title='分仓库：原版 harness 对比 v13–v15', repo_sub='deepseek-flash · 通过 / 运行',
   orig='原版 harness', final='v13–v15', impl='实现 / 评审', few='浅色柱：不到 5 次，证据弱',
   names={'v0': '原版', 'v1': '对比修复前后', 'v4': 'v1 做成 skill', 'v5': '程序生成输入', 'v10': '+ 贡献 skill',
          'v11': '独立判据', 'v13': '剥掉的字符也是数据', 'v14': '参考实现交叉比对', 'v15': '碰撞检查', 'v17': '产出方清单'}),
}


def t(x, y, s, size=14, fill=INK, anchor='start', weight=400, font=FONT):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-family="{font}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{escape(s)}</text>'


def svg(w, h, body, label):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{escape(label)}">'
            f'<rect width="{w}" height="{h}" fill="{BG}"/>' + ''.join(body) + '</svg>\n')


def color(p, n):
    r = p / n
    return GOOD if r == 1 else (MID if r >= .5 else BAD)


def trend(lang):
    s = L[lang]; vs = ['v0', 'v1', 'v4', 'v5', 'v10', 'v11', 'v13', 'v14', 'v15']
    W, H, left, top, bw, gap = 720, 470, 56, 92, 52, 19
    ch = 220; base = top + ch
    b = [t(24, 36, s['trend_title'], 20, weight=700), t(24, 60, s['trend_sub'], 13, SOFT)]
    for pct in (0, 50, 100):
        y = base - ch * pct / 100
        b.append(f'<line x1="{left}" x2="{W-20}" y1="{y}" y2="{y}" stroke="{LINE}" stroke-width="1"/>')
        b.append(t(left - 8, y + 4, f'{pct}%', 12, SOFT, 'end', font=MONO))
    for i, v in enumerate(vs):
        p, n = c[v]; x = left + 14 + i * (bw + gap); h = ch * p / n
        op = 1 if n >= 5 else .45
        b.append(f'<rect x="{x}" y="{base-h:.1f}" width="{bw}" height="{h:.1f}" fill="{color(p, n)}" opacity="{op}" rx="2"/>')
        b.append(t(x + bw / 2, base - h - 8, f'{p}/{n}', 14, INK, 'middle', 600, MONO))
        b.append(t(x + bw / 2, base + 20, v, 14, INK, 'middle', 600, MONO))
        words = s['names'][v]
        lim = 10 if lang == 'en' else 5
        lines = [words] if len(words) <= lim else _wrap(words, lim)
        for k, ln in enumerate(lines[:3]):
            b.append(t(x + bw / 2, base + 40 + k * 16, ln, 12, SOFT, 'middle'))
    b.append(f'<rect x="{left}" y="{H-30}" width="14" height="14" fill="{MID}" opacity=".45" rx="2"/>')
    b.append(t(left + 20, H - 18, s['few'], 13, SOFT))
    return svg(W, H, b, s['trend_title'])


def _wrap(text, n):
    if ' ' not in text:
        return [text[i:i + n] for i in range(0, len(text), n)]
    if any(ord(ch) > 0x2e80 for ch in text):
        return text.split(' ', 1) if len(text.split(' ',1)[0]) <= n else [text[i:i + n] for i in range(0, len(text), n)]
    out, cur = [], ''
    for w in text.split():
        if len(cur) + len(w) + 1 > n and cur:
            out.append(cur); cur = w
        else:
            cur = (cur + ' ' + w).strip()
    return out + [cur]


GRID = [('v0', {'ds/ds': 'v0', 'luna/sol': 'v2'}), ('v1', {'ds/ds': 'v1', 'Opus': 'v7'}), ('v4', {'ds/ds': 'v4'}),
        ('v5', {'ds/ds': 'v5', 'sol/sol': 'v9', 'Opus': 'v8'}), ('v10', {'ds/ds': 'v10'}), ('v11', {'ds/ds': 'v11', 'Opus': 'v12'}),
        ('v13', {'ds/ds': 'v13'}), ('v14', {'ds/ds': 'v14', 'GLM': 'v16'}), ('v15', {'ds/ds': 'v15', 'ds/sol': 'v18'}), ('v17', {'ds/ds': 'v17'})]
COLS = ['ds/ds', 'luna/sol', 'sol/sol', 'Opus', 'GLM', 'ds/sol']
ONLY_CL = {'v17', 'v18'}


def grid(lang):
    s = L[lang]; W, rowh, left, cw, top = 720, 30, 210, 80, 104
    H = top + rowh * len(GRID) + 24
    b = [t(24, 36, s['grid_title'], 20, weight=700), t(24, 60, s['grid_sub'], 13, SOFT), t(left, 88, s['impl'], 11, SOFT)]
    for j, col in enumerate(COLS):
        b.append(t(left + j * cw + cw / 2, top - 2, col, 12, INK, 'middle', 600, MONO))
    for i, (row, cells) in enumerate(GRID):
        y = top + 8 + i * rowh
        b.append(t(24, y + 20, row, 12, INK, 'start', 600, MONO))
        b.append(t(62, y + 20, s['names'][row], 12, SOFT))
        for j, col in enumerate(COLS):
            x = left + j * cw
            b.append(f'<rect x="{x+3}" y="{y+2}" width="{cw-6}" height="{rowh-4}" fill="none" stroke="{LINE}" stroke-width="1" rx="3"/>')
            v = cells.get(col)
            if not v:
                continue
            p, n = c[v]
            b.append(f'<rect x="{x+3}" y="{y+2}" width="{cw-6}" height="{rowh-4}" fill="{color(p, n)}" opacity="{0.25 + 0.75 * p / n:.2f}" rx="3"/>')
            b.append(t(x + cw / 2, y + 20, f'{p}/{n}' + ('*' if v in ONLY_CL else ''), 13, '#ffffff' if p / n >= .5 else INK, 'middle', 600, MONO))
    return svg(W, H, b, s['grid_title'])


def per_repo(lang):
    s = L[lang]; W, H, left, top = 720, 340, 56, 92
    ch = 170; base = top + ch; gw = 200; bw = 64
    b = [t(24, 36, s['repo_title'], 20, weight=700), t(24, 60, s['repo_sub'], 13, SOFT)]
    for pct in (0, 50, 100):
        y = base - ch * pct / 100
        b.append(f'<line x1="{left}" x2="{W-20}" y1="{y}" y2="{y}" stroke="{LINE}"/>')
        b.append(t(left - 8, y + 4, f'{pct}%', 12, SOFT, 'end', font=MONO))
    for i, repo in enumerate(REPOS):
        gx = left + 30 + i * gw
        for k, (vs, lab, fill) in enumerate([(['v0'], s['orig'], '#9aa8a4'), (['v13', 'v14', 'v15'], s['final'], GOOD)]):
            p = sum(c[(v, repo)][0] for v in vs); n = sum(c[(v, repo)][1] for v in vs)
            x = gx + k * (bw + 8); h = ch * p / n
            b.append(f'<rect x="{x}" y="{base-h:.1f}" width="{bw}" height="{h:.1f}" fill="{fill}" rx="2"/>')
            b.append(t(x + bw / 2, base - h - 8, f'{p}/{n}', 13, INK, 'middle', 600, MONO))
        b.append(t(gx + bw + 4, base + 22, repo, 14, INK, 'middle', 600, MONO))
    lx = left + 30
    for k, (lab, fill) in enumerate([(s['orig'], '#9aa8a4'), (s['final'], GOOD)]):
        b.append(f'<rect x="{lx + k*170}" y="{H-34}" width="14" height="14" fill="{fill}" rx="2"/>')
        b.append(t(lx + k * 170 + 20, H - 22, lab, 13, INK))
    return svg(W, H, b, s['repo_title'])


os.makedirs(OUT, exist_ok=True)
for lang in ('en', 'zh'):
    sfx = '' if lang == 'en' else '-zh'
    for name, fn in (('harness-trend', trend), ('harness-grid', grid), ('harness-per-repo', per_repo)):
        open(f'{OUT}/{name}{sfx}.svg', 'w').write(fn(lang))
print('ok', {k: c[k] for k in ['v0', 'v15', 'v18']})


FLOW = {
 'en': dict(title='How each run is scored', boxes=[('Private snapshot', 'repo at the commit', 'Orbi started from'), ('Orbi runner', 'claim → implement → PR', '→ review → merge'), ('Hidden grader', 'never shown to Orbi', '+ repo checks + full suite')],
            calib='Grader calibrated first:', checks=[('original code', 'fail', BAD), ("Orbi's merged fork PR", 'fail', BAD), ('hand-verified fix', 'pass', GOOD)]),
 'zh': dict(title='每次运行怎么打分', boxes=[('私有快照仓库', 'Orbi 当初起步的', '那个 commit'), ('Orbi runner', '认领 → 实现 → 开 PR', '→ 评审 → 合并'), ('隐藏评分器', 'Orbi 看不到', '+ 仓库检查 + 全量测试')],
            calib='评分器先校准：', checks=[('原始代码', '判失败', BAD), ('Orbi 当初合并的 fork PR', '判失败', BAD), ('手工核实的正确修复', '判通过', GOOD)]),
}


def flow(lang):
    s = FLOW[lang]; W, H = 720, 300; bw, bh, y = 200, 96, 64
    b = [t(24, 36, s['title'], 20, weight=700)]
    for i, (h, l1, l2) in enumerate(s['boxes']):
        x = 24 + i * (bw + 36)
        b.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="#ffffff" stroke="{INK if i==1 else LINE}" stroke-width="{2 if i==1 else 1.5}" rx="6"/>')
        b.append(t(x + bw / 2, y + 32, h, 16, INK, 'middle', 700))
        b.append(t(x + bw / 2, y + 56, l1, 13, SOFT, 'middle'))
        b.append(t(x + bw / 2, y + 74, l2, 13, SOFT, 'middle'))
        if i < 2:
            ax = x + bw + 6
            b.append(f'<path d="M{ax} {y+bh/2} h22 m-7 -6 l7 6 l-7 6" fill="none" stroke="{INK}" stroke-width="2"/>')
    cy = 196
    b.append(t(24, cy, s['calib'], 14, INK, weight=600))
    for k, (what, verdict, col) in enumerate(s['checks']):
        yy = cy + 24 + k * 24
        b.append(f'<circle cx="34" cy="{yy-5}" r="5" fill="{col}"/>')
        b.append(t(48, yy, what, 14, INK))
        b.append(t(300, yy, verdict, 14, col, weight=700, font=MONO))
    return svg(W, H, b, s['title'])


for lang in ('en', 'zh'):
    open(f"{OUT}/harness-flow{'' if lang == 'en' else '-zh'}.svg", 'w').write(flow(lang))


# ---- held-out + cost ----
HO = json.load(open(os.path.expanduser('~/orbi-bench/results_ho.json')))
ST = json.load(open(os.path.expanduser('~/orbi-bench/stats.json')))
HO_TASKS = [('gojq-match-empty-global', 'gojq', 'Go'), ('urfave-cli-bare-dash', 'urfave/cli', 'Go'), ('pflag-wrap-unbreakable-word', 'pflag', 'Go'),
            ('magic-string-replace-dollar', 'magic-string', 'TS'), ('recast-nullish-parens', 'recast', 'TS'), ('cron-parser-step-range-stringify', 'cron-parser', 'TS'),
            ('dotenv-setkey-backslash', 'python-dotenv', 'Py'), ('mdit-blockquote-table-eof', 'markdown-it-py', 'Py'), ('pyjwt-base64url-padding', 'pyjwt', 'Py')]
HV = [('v0', {'en': 'original', 'zh': '原版'}), ('v15', {'en': 'v15', 'zh': 'v15'}), ('v18', {'en': 'v15 + sol review', 'zh': 'v15 + sol 评审'})]
HT = {'en': ('Nine unseen issues: pass / runs', "hidden grader = the maintainer's own tests · deepseek-flash implements", 'not run'),
      'zh': ('九个没见过的 issue：通过 / 运行', '隐藏评分器 = 上游维护者自己的测试 · 实现都用 deepseek-flash', '没跑')}


def heldout(lang):
    hc = defaultdict(lambda: [0, 0])
    for x in HO:
        hc[(x['task'], x['variant'])][1] += 1; hc[(x['task'], x['variant'])][0] += x['pass']
    title, sub, na = HT[lang]
    W, top, rowh, left, cw = 720, 110, 30, 230, 150
    H = top + rowh * len(HO_TASKS) + 60
    b = [t(24, 36, title, 20, weight=700), t(24, 60, sub, 13, SOFT)]
    for j, (v, lab) in enumerate(HV):
        b.append(t(left + j * cw + cw / 2, top - 8, lab[lang], 13, INK, 'middle', 600, MONO))
    for i, (task, name, lg) in enumerate(HO_TASKS):
        y = top + i * rowh
        b.append(t(24, y + 20, lg, 12, SOFT, font=MONO)); b.append(t(60, y + 20, name, 14, INK))
        for j, (v, _) in enumerate(HV):
            p, n = hc[(task, v)]; x = left + j * cw
            if n:
                b.append(f'<rect x="{x+6}" y="{y+3}" width="{cw-12}" height="{rowh-6}" fill="{color(p, n)}" rx="3"/>')
                b.append(t(x + cw / 2, y + 20, f'{p}/{n}', 13, '#ffffff', 'middle', 600, MONO))
            else:
                b.append(f'<rect x="{x+6}" y="{y+3}" width="{cw-12}" height="{rowh-6}" fill="none" stroke="{LINE}" rx="3"/>')
                b.append(t(x + cw / 2, y + 20, na, 12, SOFT, 'middle'))
    tot = [(sum(hc[(tk, v)][0] for tk, _, _ in HO_TASKS), sum(hc[(tk, v)][1] for tk, _, _ in HO_TASKS)) for v, _ in HV]
    yy = top + rowh * len(HO_TASKS) + 30
    b.append(f'<line x1="24" x2="{W-24}" y1="{yy-22}" y2="{yy-22}" stroke="{LINE}"/>')
    b.append(t(24, yy, 'total' if lang == 'en' else '合计', 14, INK, weight=700))
    for j, (p, n) in enumerate(tot):
        b.append(t(left + j * cw + cw / 2, yy, f'{p}/{n}', 15, INK, 'middle', 700, MONO))
    return svg(W, H, b, title), H, tot


def med(a):
    a = sorted(a); return a[len(a) // 2] if a else 0


CT = {'en': ('What the stronger harness costs', 'median per run · deepseek-flash in both roles', 'million tokens', 'minutes', 'three training issues', 'nine held-out issues', 'original', 'v15'),
      'zh': ('更强的 harness 要付出多少', '每次运行的中位数 · 实现和评审都用 deepseek-flash', '百万 token', '分钟', '三个训练 issue', '九个留出 issue', '原版', 'v15')}


def cost(lang):
    s = CT[lang]; W, H = 720, 360
    b = [t(24, 36, s[0], 20, weight=700), t(24, 60, s[1], 13, SOFT)]
    groups = [(False, s[4]), (True, s[5])]
    panels = [('tok', s[2], 20), ('min', s[3], 30)]
    for pi, (kind, unit, vmax) in enumerate(panels):
        px = 40 + pi * 340; base = 290; ch = 170
        b.append(t(px, 92, unit, 14, INK, weight=600))
        b.append(f'<line x1="{px}" x2="{px+300}" y1="{base}" y2="{base}" stroke="{LINE}"/>')
        for gi, (ho, glab) in enumerate(groups):
            gx = px + 10 + gi * 150
            for k, v in enumerate(['v0', 'v15']):
                xs = [x for x in ST if x['heldout'] == ho and x['variant'] == v]
                val = med([x['tokens']['total'] for x in xs if x['tokens']['total']]) / 1e6 if kind == 'tok' else med([x['minutes'] for x in xs if x['minutes']])
                h = ch * val / vmax; x = gx + k * 62
                b.append(f'<rect x="{x}" y="{base-h:.1f}" width="54" height="{h:.1f}" fill="{"#9aa8a4" if v == "v0" else GOOD}" rx="2"/>')
                b.append(t(x + 27, base - h - 7, f'{val:.1f}' if kind == 'tok' else f'{val}', 13, INK, 'middle', 600, MONO))
            b.append(t(gx + 58, base + 20, glab, 12, SOFT, 'middle'))
    for k, (lab, fill) in enumerate([(s[6], '#9aa8a4'), (s[7], GOOD)]):
        b.append(f'<rect x="{40 + k*120}" y="{H-30}" width="14" height="14" fill="{fill}" rx="2"/>')
        b.append(t(60 + k * 120, H - 18, lab, 13, INK))
    return svg(W, H, b, s[0])


for lang in ('en', 'zh'):
    sfx = '' if lang == 'en' else '-zh'
    body, hh, tot = heldout(lang)
    open(f'{OUT}/harness-heldout{sfx}.svg', 'w').write(body)
    open(f'{OUT}/harness-cost{sfx}.svg', 'w').write(cost(lang))
print('heldout H', hh, tot)
