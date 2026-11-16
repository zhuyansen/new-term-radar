"""The radar's flow as an animated SVG and a GIF: dots travel the connectors.

  python docs/make_diagram.py <oss-zh|oss-en> <out-basename> [--plain]

What 0.6.0 added (the keyword table's new readings, the new sites' first words, AI answers on ⑦,
three more payment platforms and the builders on ⑧, ⑨ just listed, ⑩ money, the side evidence) is drawn
in purple, with a NEW tag; --plain draws it like the rest.
rsvg-convert renders each frame (it does not run SVG animation) and ffmpeg joins them.
"""
import math, subprocess, sys, tempfile
from pathlib import Path

W, H = 760, 1066
FONT = "Hiragino Sans GB, PingFang SC, Noto Sans CJK SC, sans-serif"
AMB = dict(fill="#FAECE7", stroke="#993C1D", title="#712B13", sub="#993C1D")
GRY = dict(fill="#F1EFE8", stroke="#5F5E5A", title="#444441", sub="#5F5E5A")
GRN = dict(fill="#E1F5EE", stroke="#0F6E56", title="#085041", sub="#0F6E56")
BLU = dict(fill="#E6F1FB", stroke="#185FA5", title="#0C447C", sub="#185FA5")
NEW = dict(fill="#EEEDFE", stroke="#534AB7", title="#3C3489", sub="#534AB7")
NEW_INK = "#534AB7"
CREDIT_TABLE = "子木 @CoderJeffLee"
CREDIT_SOURCE = "by 饭特稀 | Fentix @fantexi997"

# A line that starts with "+" is new in 0.6.0: drawn in purple, without the "+".
ZH = {
    "s1": ("① X 与 HF", "每 8 小时", "HF Spaces 热门 demo"), "s2": ("② GitHub", "爆发仓库 · 新黑马免截断"),
    "s3": ("③ 竞品与新站", "sitemap 新页 · 每周新站", "8 个 AI 工具导航站", "+新站最先排上的词"), "s4": ("④ 搜索控制台", "首次曝光的词 · 可选"),
    "s6": ("⑥ 有人付钱的产品", "TrustMRR · 每周", "增长最快 · 从 $0 到有收入"),
    "s7": ("⑦ Reddit 求助", "12 种问法 · 每周", "模型筛出要做的事", "+ChatGPT 首选与多轮分布"),
    "s8": ("⑧ 刚开始收钱的站", "收款链接 · 每周", "+8 家收款 · 建站作品"),
    "judge": ("名字判断：是具体名字吗", "得分 ≥ 0.5 才留下", "格式：视频>语音>图片 · ⚡ 一键机会"),
    "known": ("剔除你已经覆盖的词",),
    "readings": ("词龄 · 多市场 · 首页形态 · 12 个月", "首次被搜的那周 · 美德法日搜索量与难度 · 最佳市场",
                 "前十形态 · 讨论帖与 AI 概览 · 精确匹配的 .com", "你的排名 → 更新、改进或新建页面"),
    "sources": ("源头报告（每 8 小时）", "有新名字才出", "附推文原文"),
    "table": ("新词表：名字 + 它背后的通用词", "模型提词 → 相关性筛选 → 搜索量 → Trends"),
    "page": ("首页竞争", "前十弱站 · KD · KGR · 广告 · 已有页面 · 在投广告", "首页挤满的「优先」降为「观察 ↓」"),
    "evidence": ("取证（AIsa）", "高赞推文 · README", "付费产品的自述"),
    "goany": ("GoAnyAPI（可选）", "相关查询 · 美国日走势", CREDIT_SOURCE),
    "daily": ("日报（每天一次）", "新词表在最前 · 线索存入 radar.db", "⑥ 收入 · ⑦ 求助 · ⑧ 新卖家 · ⚡ 教程观众在要什么",
              "+⑨ 新上架 · ⑩ 付费信号 · 新名字的旁证"),
    "review": ("14 天后 DataForSEO Trends 复核", "名字和通用词都复核 · 不计未结算的最后两天"),
    "credit": f"建议：{CREDIT_TABLE}",
    "use_h": "四种用法，同一套引擎",
    "use": [("命令行 + 定时任务", "daily · sources", "keywords · leads"), ("3 个 Agent Skill", "一站式", "只收集 · 只做关键词"),
            ("MCP 服务器", "查线索 · 出新词表", "跑一次"), ("pains 痛点命令", "Chrome / App Store 低星", "Reddit · 教程评论")],
    "tag": "新增",
    "legend": ("信号与取证", "报告产出", "新词表", "0.6.0 新增", "命中率写回日报"),
}
EN = {
    "s1": ("① X and HF", "every 8 hours", "trending HF Spaces"), "s2": ("② GitHub", "breakouts · new kept"),
    "s3": ("③ Sitemaps", "new pages · new sites", "8 AI tool directories", "+new sites' top-20 words"), "s4": ("④ Search Console", "optional"),
    "s6": ("⑥ What people pay for", "TrustMRR · weekly", "fastest growing · from $0"),
    "s7": ("⑦ Reddit asks", "12 phrasings · weekly", "the jobs people want done", "+ChatGPT pick · multi-run"),
    "s8": ("⑧ Started selling", "payment links · weekly", "+8 platforms · builders"),
    "judge": ("Name judge: a name, or a word?", "a score of 0.5 or more stays", "format: video>voice>image · ⚡ one-click"),
    "known": ("Drop the terms you already cover",),
    "readings": ("Word age · Markets · Page-one shape · 12 months", "first searched · US DE FR JP volume + KD · best market",
                 "top-10 shape · threads and AI overview · exact-match .com", "your rank → update, improve or new page"),
    "sources": ("Sources report (every 8 h)", "only on a new name", "with the posts"),
    "table": ("Keyword table: names + generic terms", "model phrases → screen → volume → Trends"),
    "page": ("Page one", "weak sites · KD · KGR · ads · pages built · advertisers", "a crowded \"prioritize\" becomes \"watch ↓\""),
    "evidence": ("Evidence (AIsa)", "top posts · README", "a paying product's own words"),
    "goany": ("GoAnyAPI (optional)", "related queries · US curve", CREDIT_SOURCE),
    "daily": ("Daily report (once a day)", "keyword table first · leads in radar.db", "⑥ revenue · ⑦ asks · ⑧ sellers · ⚡ viewers ask",
              "+⑨ just listed · ⑩ money · side evidence"),
    "review": ("DataForSEO Trends check, 14 days later", "names and generic terms · last two days left out"),
    "credit": f"Idea: {CREDIT_TABLE}",
    "use_h": "Four ways in, one engine",
    "use": [("Command line", "daily · sources", "keywords · leads"), ("3 Agent Skills", "one run,", "or its two halves"),
            ("MCP server", "leads · keyword table", "run"), ("pains command", "Chrome / App Store", "Reddit · tutorials")],
    "tag": "NEW",
    "legend": ("signals", "reports", "keyword table", "new in 0.6.0", "hit rate fed back"),
}
VARIANTS = {"oss-zh": ZH, "oss-en": EN}


def width(text, size):   # a CJK glyph is about a square, a Latin one about half
    return sum(size if ord(ch) > 0x2E7F else size * 0.6 for ch in text)


def tag(x, y, label):
    w = width(label, 10) + 10
    return (f'<rect x="{x - w:.0f}" y="{y - 8}" width="{w:.0f}" height="15" rx="7.5" fill="{NEW_INK}"/>'
            f'<text x="{x - w / 2:.0f}" y="{y + 3}" font-size="10" fill="#FFFFFF" font-weight="600">{label}</text>')


def box(x, y, w, h, c, lines, marks, dashed=False, new=False, title_dy=0):
    """A box; `new` draws it purple with a tag, a line starting with "+" is a new line inside an old box."""
    if new and marks:
        c = NEW
    title, *subs = lines
    cx = x + w / 2
    dash = ' stroke-dasharray="5 3"' if dashed else ""
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{c["fill"]}" stroke="{c["stroke"]}" '
           f'stroke-width="{1.2 if new and marks else 1 if dashed else 0.5}"{dash}/>']
    ty = y + h / 2 + 5 - 9 * len(subs) + title_dy
    out.append(f'<text x="{cx}" y="{ty}" fill="{c["title"]}" font-weight="500">{title}</text>')
    for i, s in enumerate(subs):
        fresh = s.startswith("+")
        ink = NEW_INK if fresh and marks else c["sub"]
        text = s[1:] if fresh else s
        weight = ' font-weight="600"' if fresh and marks else ""
        out.append(f'<text x="{cx}" y="{ty + 19 + 16.5 * i}" fill="{ink}" font-size="{11 if "@" in s else 12}"{weight}>{text}</text>')
    if marks and (new or any(s.startswith("+") for s in subs)):
        out.append(tag(x + w - 4, y + 2, T_TAG[0]))
    return "\n".join(out)


T_TAG = ["NEW"]

# id, points, colour, dashed, new, dot duration (s), dot begin (s)
PATHS = [
    ("p1", [(110, 116), (330, 140)], "#D85A30", False, False, 2.4, 0.0),
    ("p2", [(290, 116), (360, 140)], "#D85A30", False, False, 2.4, 0.6),
    ("p3", [(470, 116), (400, 140)], "#D85A30", False, False, 2.4, 1.2),
    ("p4", [(650, 116), (430, 140)], "#D85A30", False, False, 2.4, 1.8),
    ("p6", [(220, 179), (240, 179)], "#D85A30", False, False, 1.2, 0.2),
    ("p7", [(540, 179), (520, 179)], "#D85A30", False, False, 1.2, 0.8),
    ("p8b", [(220, 262), (231, 262), (231, 200), (240, 200)], "#D85A30", False, False, 1.6, 0.5),
    ("p7b", [(730, 179), (748, 179), (748, 432), (730, 432)], "#D85A30", False, False, 2.8, 0.4),
    ("p5", [(380, 218), (380, 246)], "#888780", False, False, 1.2, 0.3),
    ("p8", [(300, 290), (130, 318)], "#1D9E75", False, False, 1.6, 0.0),
    ("p9", [(440, 290), (495, 318)], "#378ADD", False, False, 1.6, 0.4),
    ("p15", [(495, 462), (495, 480)], "#7F77DD", False, True, 1.0, 0.3),
    ("p10", [(370, 616), (370, 584)], "#D85A30", False, False, 1.4, 0.8),
    ("p11", [(615, 616), (615, 584)], "#378ADD", True, False, 1.4, 1.2),
    ("p12", [(130, 378), (130, 756), (170, 756)], "#1D9E75", False, False, 2.8, 0.2),
    ("p13", [(260, 446), (245, 446), (245, 720)], "#378ADD", False, False, 2.2, 0.6),
    ("p14", [(590, 848), (650, 848), (650, 756), (590, 756)], "#888780", True, False, 4.8, 0.0),
]
DASH = ' stroke-dasharray="4 3"'


def scene(T, marks):
    T_TAG[0] = T["tag"]
    uses = [box(30 + 180 * i, 942, 160, 72, c, T["use"][i], marks) for i, c in enumerate((GRY, BLU, BLU, GRY))]
    boxes = "\n".join([
        box(30, 36, 160, 80, AMB, T["s1"], marks), box(210, 36, 160, 80, AMB, T["s2"], marks),
        box(390, 36, 160, 80, AMB, T["s3"], marks), box(570, 36, 160, 80, AMB, T["s4"], marks),
        box(30, 140, 190, 78, AMB, T["s6"], marks), box(240, 140, 280, 78, GRY, T["judge"], marks),
        box(540, 140, 190, 78, AMB, T["s7"], marks), box(30, 230, 190, 64, AMB, T["s8"], marks),
        box(240, 246, 280, 44, GRY, T["known"], marks),
        box(30, 318, 200, 60, GRN, T["sources"], marks),
        box(260, 318, 470, 266, BLU, T["table"], marks, title_dy=-103),
        box(276, 388, 438, 74, BLU, T["page"], marks),
        box(276, 480, 438, 92, BLU, T["readings"], marks, new=True),
        box(260, 616, 220, 72, AMB, T["evidence"], marks), box(500, 616, 230, 72, BLU, T["goany"], marks, dashed=True),
        box(170, 720, 420, 84, GRN, T["daily"], marks), box(170, 820, 420, 56, GRY, T["review"], marks),
        *uses,
    ])
    lines = "\n".join(f'<path id="{i}" d="M{" L".join(f"{x} {y}" for x, y in p)}" fill="none" stroke="{c if marks or not new else "#888780"}" '
                      f'stroke-width="{1.4 if new and marks else 1}"{DASH if dash else ""} marker-end="url(#arrow)"/>'
                      for i, p, c, dash, new, *_ in PATHS)
    use = (f'<path d="M30 918 H730" stroke="#D3D1C7" stroke-width="0.5" fill="none"/>'
           f'<text x="30" y="934" font-size="12" fill="#5F5E5A" font-weight="500">{T["use_h"]}</text>')
    notes = use + f'<text x="730" y="311" text-anchor="end" font-size="11" fill="#185FA5">{T["credit"]}</text>'
    legend_y = 1034
    swatches = [f'fill="{AMB["fill"]}" stroke="{AMB["stroke"]}" stroke-width="0.5"', f'fill="{GRN["fill"]}" stroke="{GRN["stroke"]}" stroke-width="0.5"',
                f'fill="{BLU["fill"]}" stroke="{BLU["stroke"]}" stroke-width="0.5"', f'fill="{NEW["fill"]}" stroke="{NEW["stroke"]}" stroke-width="1.2"']
    labels = list(T["legend"]) if marks else [s for i, s in enumerate(T["legend"]) if i != 3]
    kinds = ["sw0", "sw1", "sw2", "sw3", "line"] if marks else ["sw0", "sw1", "sw2", "line"]
    widths = [22 + width(s, 12) if k != "line" else 38 + width(s, 12) for s, k in zip(labels, kinds)]
    gap = (W - 60 - sum(widths)) / (len(labels) - 1)
    x, legend = 30, ""
    for label, kind, w in zip(labels, kinds, widths):
        if kind == "line":
            legend += (f'<path d="M{x:.0f} {legend_y + 7} H{x + 30:.0f}" stroke="#888780" stroke-width="1" stroke-dasharray="4 3" fill="none"/>'
                       f'<text x="{x + 38:.0f}" y="{legend_y + 12}" font-size="12" fill="#444441">{label}</text>')
        else:
            legend += (f'<rect x="{x:.0f}" y="{legend_y}" width="14" height="14" rx="3" {swatches[int(kind[2])]}/>'
                       f'<text x="{x + 22:.0f}" y="{legend_y + 12}" font-size="12" fill="#444441">{label}</text>')
        x += w + gap
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * 2}" height="{H * 2}" viewBox="0 0 {W} {H}" font-family="{FONT}">\n'
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            '<path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>\n'
            f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>\n')
    return head, f'{lines}\n<g font-size="14" text-anchor="middle">\n{boxes}\n</g>\n{notes}\n{legend}\n'


def at(points, f):
    seg = [math.dist(a, b) for a, b in zip(points, points[1:])]
    left = f * sum(seg)
    for (a, b), s in zip(zip(points, points[1:]), seg):
        if left <= s:
            k = left / s if s else 0
            return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        left -= s
    return points[-1]


def main(variant, base, marks=True):
    head, body = scene(VARIANTS[variant], marks)
    colour = lambda c, new: c if marks or not new else "#888780"  # noqa: E731
    smil = "\n".join(f'<circle r="{4.5 if new and marks else 4}" fill="{colour(c, new)}"><animateMotion dur="{dur}s" repeatCount="indefinite" '
                     f'begin="{b}s"><mpath href="#{i}"/></animateMotion></circle>' for i, _, c, _, new, dur, b in PATHS)
    Path(base + ".svg").write_text(head + body + smil + "\n</svg>\n")
    fps, seconds = 15, 4.8
    with tempfile.TemporaryDirectory() as tmp:
        for n in range(int(fps * seconds)):
            tm = n / fps
            dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{4.5 if new and marks else 4}" fill="{colour(c, new)}"/>'
                           for _, p, c, _, new, dur, b in PATHS for x, y in [at(p, ((tm - b) % dur) / dur)])
            f = Path(tmp) / f"f{n:03d}.svg"
            f.write_text(head.replace("context-stroke", "#888780") + body + dots + "\n</svg>\n")
            subprocess.run(["rsvg-convert", "-w", str(W * 2), "-b", "white", str(f), "-o", str(f.with_suffix(".png"))], check=True)
        frames = ["-y", "-loglevel", "error", "-framerate", str(fps), "-i", f"{tmp}/f%03d.png"]
        subprocess.run(["ffmpeg", *frames, "-vf", "scale=1140:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=none",
                        base + ".gif"], check=True)
        subprocess.run(["cp", f"{tmp}/f030.png", base + ".png"], check=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], marks="--plain" not in sys.argv)
