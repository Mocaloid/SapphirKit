#!/usr/bin/env python3
"""
Replace numeric citations like [1], [2] with ~\\citep{<bibkey>}
by parsing a body file and a BibTeX references file.
Usage:
    Set the three variables BODY_FILE, BIB_FILE, OUTPUT_FILE below,
    then run the script.
"""
import sys
import re

def read_file(filepath: str) -> str:
    """读取文件内容，以 GBK 编码打开。"""
    try:
        with open(filepath, 'r', encoding='GBK') as f:
            return f.read()
    except UnicodeDecodeError:
        print("find byte can't be decoded by GBK.")
def write_file(filepath: str, content: str) -> None:
    """将字符串写入文件，UTF‑8 编码。"""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
def skip_brace_block(lines: list, start: int) -> int:
    """
    跳过一个大括号块（如 @comment, @preamble, @string），
    返回块结束后的下一行索引。
    """
    full = "\n".join(lines[start:])
    brace_start = full.find('{')
    if brace_start == -1:
        return start + 1
    level = 1
    idx = brace_start + 1
    while idx < len(full) and level > 0:
        ch = full[idx]
        if ch == '{':
            level += 1
        elif ch == '}':
            level -= 1
        idx += 1
    end_pos = idx - 1          # 闭合的 '}'
    new_i = start + full[:end_pos+1].count('\n')
    return new_i
def extract_key_and_skip(lines: list, start: int) -> tuple:
    """
    从 @article{key, ... 开始提取标识符 key，并跳过整个条目。
    返回 (key, 下一个处理的起始行索引)。
    """
    full = "\n".join(lines[start:])
    brace_start = full.find('{')
    if brace_start == -1:
        return None, start + 1
    level = 1
    idx = brace_start + 1
    key_end = None
    while idx < len(full) and level > 0:
        ch = full[idx]
        if ch == '{':
            level += 1
        elif ch == '}':
            level -= 1
            if level == 0:
                if key_end is None:
                    key_end = idx
                break
        elif ch == ',' and level == 1:
            if key_end is None:
                key_end = idx
        idx += 1
    if key_end is None:
        key_end = idx
    key = full[brace_start+1 : key_end].strip()
    end_pos = idx - 1  # '}' 的位置
    new_i = start + full[:end_pos+1].count('\n')
    return key, new_i


def parse_bib_file(filepath: str) -> dict:
    """
    解析 .bib 或 .txt 引用文件，返回一个字典：
        { 引用序号 : 文献标识符 }
    """
    text = read_file(filepath)
    lines = text.split('\n')
    n_lines = len(lines)
    i = 0
    entries = []          # 元素：{'key': ..., 'num': 指定序号 或 None}
    pending_num = None    # 最近一个注释中给出的序号
    while i < n_lines:
        stripped = lines[i].strip()
        if not stripped:                     # 空行，不改变 pending_num
            i += 1
            continue
        if stripped.startswith('%'):         # 注释行
            m = re.search(r'\d+', stripped)
            if m:
                pending_num = int(m.group())
            else:
                pending_num = None
            i += 1
            continue
        # 判断是否为文献条目开始
        m = re.match(r'@(\w+)\s*\{', stripped)
        if m:
            entry_type = m.group(1).lower()
            # 跳过 comment / preamble / string 等非文献条目
            if entry_type in ('comment', 'preamble', 'string'):
                i = skip_brace_block(lines, i)
                continue
            key, next_i = extract_key_and_skip(lines, i)
            num = pending_num
            entries.append({'key': key, 'num': num})
            pending_num = None        # 消耗掉注释
            i = next_i
            continue
        # 其他行（通常不会出现，保守处理：忽略，不改变 pending_num）
        i += 1
    # ----- 构建最终映射 -----
    specified = {}
    for e in entries:
        if e['num'] is not None:
            if e['num'] in specified:
                print(f"Warning: duplicate specified number {e['num']} "
                      f"for key '{e['key']}', overwriting '{specified[e['num']]}'.", file=sys.stderr)
            specified[e['num']] = e['key']
    used = set(specified.keys())
    next_auto = 1
    ref_map = {}
    for e in entries:
        if e['num'] is not None:
            ref_map[e['num']] = e['key']
        else:
            while next_auto in used:
                next_auto += 1
            ref_map[next_auto] = e['key']
            used.add(next_auto)
            next_auto += 1
    return ref_map


def replace_citations (body: str, ref_map: dict) -> str:
    """
    将正文中所有形如 [数字] 的引用替换为 ~\citep{标识符}。
    若找不到对应的文献，保留原样并输出警告。

    num = int(ref_map.group(1))
    key = ref_map.get(num)
    if key:
        return re.sub(r'\[(\d+)\]', f'~\\citep{{{key}}}', body)
    else:
        print(f"Warning: citation [{num}] not found in reference file.", file=sys.stderr)
        return ""
"""

    def repl(match):
        num = int(match.group(1))
        key = ref_map.get(num)
        if key:
            return f'~\\citep{{{key}}}'
        else:
            print(f"Warning: citation [{num}] not found in reference file.", file=sys.stderr)
            return match.group(0)
    return re.sub(r'\[(\d+)\]', repl, body)

def process_files(body_path, bib_path, output_path):
    """主处理流程：读取、解析、替换、写入。"""
    body_text = read_file(body_path)
    ref_map = parse_bib_file(bib_path)
    new_body = replace_citations(body_text, ref_map)
    write_file(output_path, new_body)
    print(f"Modified file written to {output_path}")


if __name__ == '__main__':
    # ===== 在这里修改文件路径 =====
    BODY_FILE = "body.tex"  # 包含 [1]、[2] 等引用的正文文件
    BIB_FILE = "ref.bib"  # BibTeX 格式的参考文献文件
    OUTPUT_FILE = "output.tex"  # 输出文件
    # ============================
    process_files(BODY_FILE, BIB_FILE, OUTPUT_FILE)
