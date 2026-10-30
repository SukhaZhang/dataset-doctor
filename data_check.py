#!/usr/bin/env python3
"""数据体检师核心检查脚本。

仅用 Python 标准库（csv/re/statistics/datetime/json/argparse），
便于在任何受限环境（含扣子技能运行时）直接运行。

用法:
    python data_check.py input.csv                 # 输出Markdown报告到stdout
    python data_check.py input.csv -o report.md    # 写入文件
"""
import argparse
import csv
import io
import json
import os
import re
import statistics
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATE_PATTERNS = [
    (r"^\d{4}-\d{1,2}-\d{1,2}$", "YYYY-MM-DD"),
    (r"^\d{4}/\d{1,2}/\d{1,2}$", "YYYY/MM/DD"),
    (r"^\d{4}年\d{1,2}月\d{1,2}日$", "中文日期"),
    (r"^\d{1,2}-\d{1,2}-\d{4}$", "DD-MM-YYYY 或 MM-DD-YYYY"),
    (r"^\d{8}$", "YYYYMMDD"),
    (r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}(:\d{2})?$", "日期时间"),
]
NUM_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$|^-?\d+(\.\d+)?$")


def is_number(v):
    return bool(NUM_RE.match(v.strip()))


def to_number(v):
    try:
        return float(v.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def date_kind(v):
    v = v.strip()
    for pat, name in DATE_PATTERNS:
        if re.match(pat, v):
            return name
    return None


def parse_date(v):
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return __import__("datetime").datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def classify_column(values):
    """返回列类型: number/date/text/empty，以及解析失败的下标。"""
    non_empty = [(i, v) for i, v in enumerate(values) if v]
    if not non_empty:
        return "empty", []
    num_ok = sum(1 for _, v in non_empty if is_number(v))
    date_kinds = [date_kind(v) for _, v in non_empty]
    date_ok = sum(1 for k in date_kinds if k)
    n = len(non_empty)
    if num_ok / n >= 0.9:
        return "number", [i for i, v in non_empty if not is_number(v)]
    if date_ok / n >= 0.9:
        return "date", [i for (i, _), k in zip(non_empty, date_kinds) if not k]
    return "text", []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    raw = open(args.input, "rb").read()
    issues, info = [], []

    try:
        text = raw.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
            encoding = "gb18030（建议另存为CSV UTF-8）"
        except UnicodeDecodeError:
            print(json.dumps({"error": "无法识别文件编码（非UTF-8/GBK）"}, ensure_ascii=False))
            sys.exit(1)

    if not text.strip():
        print(json.dumps({"error": "文件为空"}, ensure_ascii=False))
        sys.exit(1)

    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        print(json.dumps({"error": "只有表头或没有数据行"}, ensure_ascii=False))
        sys.exit(1)

    header = [h.strip() for h in rows[0]]
    all_data = rows[1:]
    ncols = len(header)

    # 行级：空行（先剥离，避免污染列统计）
    data, blank_rows = [], 0
    for r in all_data:
        if any(c.strip() for c in r):
            data.append(r)
        else:
            blank_rows += 1
    nrows = len(data)
    info.append(f"共 {nrows} 行数据、{ncols} 列（编码：{encoding}）")

    if blank_rows:
        issues.append(("🟡", "空行", f"发现 {blank_rows} 个完全空白的行", "筛选删除空行即可"))

    # 行级：重复记录（仅忽略名称明确的标识符列，如工号/订单号/SKU）
    id_re = re.compile(r"(工号|编号|订单号|单号|序号|学号|卡号|条码|sku|_id|^id$)", re.I)
    id_cols = {c for c in range(ncols) if id_re.search(header[c] if c < len(header) else "")}
    id_names = [header[c] for c in sorted(id_cols)] if id_cols else []
    seen, dup_idx = {}, []
    for i, r in enumerate(data):
        key = tuple(v.strip() if c not in id_cols else "" for c, v in enumerate(r))
        if key in seen:
            dup_idx.append((i, seen[key]))
        else:
            seen[key] = i
    if dup_idx:
        pairs = "、".join(f"第{b+2}行=第{a+2}行" for a, b in dup_idx[:4])
        idn = f"（已忽略{'、'.join(id_names)}列）" if id_names else ""
        issues.append(("🔴", "重复记录", f"发现 {len(dup_idx)} 组疑似重复记录{idn}：{pairs}",
                       "重复记录会让统计结果翻倍失真。核对是否为重复录入或系统重复推送，确认后删除多余行"))

    # 列级检查（基于非空行）
    col_types = {}
    for c in range(ncols):
        vals = [r[c].strip() if c < len(r) else "" for r in data]
        name = header[c] if c < len(header) else f"第{c+1}列"
        ctype, bad = classify_column(vals)
        col_types[name] = ctype
        non_empty = [v for v in vals if v]
        miss = nrows - len(non_empty)
        miss_ratio = miss / nrows if nrows else 0
        if ctype == "empty":
            issues.append(("🟡", "全空列", f"列「{name}」没有任何数据", "确认是否为废弃列，建议删除"))
        elif miss_ratio >= 0.3:
            ex_rows = [i + 2 for i, v in enumerate(vals) if not v][:5]
            if ctype == "date":
                issues.append(("🟡", "高缺失日期列", f"列「{name}」缺失 {miss} 个值（{miss_ratio:.0%}）",
                               "日期列大面积为空通常是业务正常现象（如'离职日期'仅在离职后填写），确认后可忽略本条"))
            else:
                issues.append(("🔴", "高缺失列", f"列「{name}」缺失 {miss} 个值（{miss_ratio:.0%}），示例行号：{ex_rows}",
                               "缺失比例较高。若该列参与统计（如电话、金额），需补录或剔除对应记录"))
        elif miss_ratio >= 0.05:
            issues.append(("🟡", "缺失值", f"列「{name}」缺失 {miss} 个值（{miss_ratio:.0%}）",
                           "少量缺失可填默认值或留空，视分析目的决定"))
        if ctype == "number" and bad:
            ex = [vals[i] for i in bad[:3]]
            issues.append(("🔴", "数字列混入文本", f"列「{name}」有 {len(bad)} 个值不是合法数字（如：{ex}）",
                           "会导致求和/平均值计算出错或报错。检查是否混入了单位、备注或录入错误"))
        if ctype == "date":
            kinds = {}
            for v in non_empty:
                k = date_kind(v)
                if k:
                    kinds[k] = kinds.get(k, 0) + 1
            if len(kinds) >= 2:
                detail = "、".join(f"{k}×{n}" for k, n in kinds.items())
                issues.append(("🟡", "日期格式不统一", f"列「{name}」混用了多种格式：{detail}",
                               "统一为一种格式（推荐 YYYY-MM-DD），否则排序筛选会错乱"))
            if bad:
                ex = [vals[i] for i in bad[:3]]
                issues.append(("🟡", "日期异常值", f"列「{name}」有 {len(bad)} 个值不是合法日期（如：{ex}）", "人工核对修正"))

        if ctype == "number":
            pairs = [(i + 2, to_number(v)) for i, v in enumerate(vals) if v]
            nums_c = [v for _, v in pairs if v is not None]
            if len(nums_c) >= 8:
                qs = statistics.quantiles(nums_c, n=4)
                lo, hi = qs[0] - 1.5 * (qs[2] - qs[0]), qs[2] + 1.5 * (qs[2] - qs[0])
                out_vals = [(r, v) for r, v in pairs if v is not None and (v < lo or v > hi)]
                if 0 < len(out_vals) <= 20:
                    ex = [f"第{r}行={v:g}" for r, v in out_vals[:3]]
                    issues.append(("🟡", "疑似异常值", f"列「{name}」有 {len(out_vals)} 个离群值（如：{ex}）",
                                   "可能是录入错误（多打一个0），也可能是真实极端值——结合业务确认，不要直接删除"))

    # 日期逻辑矛盾
    date_cols = [h for h, t in col_types.items() if t == "date"]
    if len(date_cols) >= 2:
        import itertools
        for a, b in itertools.combinations(date_cols, 2):
            ia, ib = header.index(a), header.index(b)
            contradictions = [i + 2 for i, r in enumerate(data)
                              if len(r) > max(ia, ib)
                              and (da := parse_date(r[ia])) and (db := parse_date(r[ib]))
                              and db < da]
            if contradictions:
                issues.append(("🔴", "日期逻辑矛盾", f"「{b}」早于「{a}」的记录 {len(contradictions)} 条（行号：{contradictions[:5]}）",
                               "如'发货早于下单'通常是录入错误，需人工核对"))

    weight = {"🔴": 12, "🟡": 5}
    score = max(5, 100 - sum(weight.get(tag, 0) for tag, *_ in issues))
    grade = "🟢 健康" if score >= 85 else "🟡 有隐患" if score >= 60 else "🔴 问题较多"

    lines = [
        "# 📋 数据体检报告",
        f"**文件**：{os.path.basename(args.input)}",
        "",
        f"## 总评：{score}/100 {grade}",
        f"{info[0]}",
        "",
    ]
    if not issues:
        lines.append("🎉 未发现明显问题，数据质量良好。")
    else:
        for tag, name, detail, fix in issues:
            lines.append(f"### {tag} {name}")
            lines.append(f"- {detail}")
            if fix:
                lines.append(f"- 💡 建议：{fix}")
            lines.append("")
    report = "\n".join(lines)

    if args.output:
        open(args.output, "w", encoding="utf-8").write(report)
        print(f"report -> {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
