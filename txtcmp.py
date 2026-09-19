# -*- coding: utf-8 -*-
"""
纯文本差异比较器（PyQt5）—— 锚点优先的字符串匹配 + 移动检测
原则：字符串匹配先确定"未修改"锚点；差异分类/替换验证/移动检测都在
      "锚点处文字未改动"的前提下进行；只有证据充分时才个别否决锚点。
- 删除：红字 + 淡红高亮 + 删除线
- 新增：深色字 + 淡绿高亮 + 加粗
- 替换：深黄字 + 淡黄高亮 + 下划线
- 移动：蓝字 + 淡蓝高亮 + 波浪下划线
- 当前差异：大红字 #ff0000 + 亮黄背景 #ffff00
依赖：pip install PyQt5
"""
import re
import html
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QEvent
from PyQt5.QtGui import (QColor, QCursor, QFont, QFontDatabase,
                         QTextCharFormat, QTextCursor)
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame,
                             QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                             QPushButton, QSpinBox, QSplitter, QTextEdit,
                             QVBoxLayout, QWidget)


# ================= 分词 =================
WORD_RE = re.compile(r'\w+|\s+|[^\w\s]', re.UNICODE)
CJK_RE = re.compile('[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]')

NL = '\n'
SENT_END_TOKENS = frozenset({'。', '！', '？', '!', '?'})

INLINE_LIMIT = 1_000_000      # 词级精细匹配的规模上限；超限走唯一词分治（永不整块放弃）
ALIVE_RATIO = 0.60            # “存活句”残余覆盖率阈值
ANCHOR_MARGIN = 1.0           # 锚点冲突否决的宽容带：质量差超过它才否决

MOVE_MIN_LEN = 4
MOVE_SIM_RATIO = 0.85
MOVE_SIM_LIMIT = 400

STOPWORDS = frozenset({
    'the', 'a', 'an', 'of', 'to', 'in', 'on', 'at', 'by', 'for', 'with', 'as',
    'is', 'are', 'was', 'were', 'be', 'been', 'being', 'am', 'and', 'or', 'but',
    'if', 'then', 'so', 'than', 'that', 'this', 'these', 'those', 'it', 'its',
    'we', 'our', 'you', 'your', 'he', 'she', 'they', 'their', 'his', 'her', 'i',
    'not', 'no', 'do', 'does', 'did', 'have', 'has', 'had', 'will', 'would',
    'can', 'could', 'may', 'might', 'shall', 'should', 'must', 'from', 'into',
    'over', 'under', 'between', 'about', 'through', 'during', 'before', 'after',
    'up', 'down', 'out', 'off', 'again', 'once', 'here', 'there', 'when',
    'where', 'why', 'how', 'all', 'any', 'both', 'each', 'few', 'more', 'most',
    'other', 'some', 'such', 'only', 'own', 'same', 'too', 'very', 'just',
})


def tokenize(text):
    """拉丁词整体保留；CJK 逐字；标点单独成 token；连续空白一个 token。"""
    tokens = []
    for m in WORD_RE.finditer(text):
        tok = m.group()
        if not tok.strip() or not CJK_RE.search(tok):
            tokens.append(tok)
            continue
        buf = []
        for ch in tok:
            if CJK_RE.match(ch):
                if buf:
                    tokens.append(''.join(buf))
                    buf = []
                tokens.append(ch)
            else:
                buf.append(ch)
        if buf:
            tokens.append(''.join(buf))
    return tokens


def build_keys(tokens, ignore_case=False, keep_whitespace=False):
    keys, idx_map = [], []
    for i, t in enumerate(tokens):
        if not t.strip():
            if keep_whitespace:
                keys.append(t.lower() if ignore_case else t)
                idx_map.append(i)
            elif NL in t:
                keys.append(NL)
                idx_map.append(i)
            continue
        keys.append(t.lower() if ignore_case else t)
        idx_map.append(i)
    return keys, idx_map


def _is_nl_key(k):
    return k == NL or (not k.strip() and NL in k)


def split_sentences(keys):
    """句末标点与换行断句；换行归前一句；英文 '.' 后接数字/小写词不断句。"""
    bounds, start = [], 0
    n = len(keys)
    idx = 0
    while idx < n:
        k = keys[idx]
        end = False
        if _is_nl_key(k) or k in SENT_END_TOKENS:
            end = True
        elif k == '.':
            nxt = keys[idx + 1] if idx + 1 < n else None
            if nxt is None or not (nxt[0].isdigit()
                                   or (nxt[0].isalpha() and nxt[0].islower())):
                end = True
        if end:
            j = idx + 1
            while j < n and _is_nl_key(keys[j]):
                j += 1
            bounds.append((start, j))
            start = j
            idx = j
        else:
            idx += 1
    if start < n:
        bounds.append((start, n))
    return bounds


def _span(sents, total, i1, i2):
    if i2 > i1:
        return sents[i1][0], sents[i2 - 1][1]
    pos = sents[i1][0] if i1 < len(sents) else total
    return pos, pos


def _sid_array(sents, total):
    sid = [0] * total
    for k, (s, e) in enumerate(sents):
        for p in range(s, e):
            sid[p] = k
    return sid


def key_span_to_token_span(idx_map, total_tokens, i1, i2):
    if i2 <= i1:
        pos = idx_map[i1] if i1 < len(idx_map) else total_tokens
        return pos, pos
    return idx_map[i1], idx_map[i2 - 1] + 1


def find_subseq(haystack, needle, start=0):
    """KMP 子序列查找。"""
    n, m = len(haystack), len(needle)
    if m == 0:
        return start if start <= n else -1
    if n - start < m:
        return -1
    lps = [0] * m
    k = 0
    for i in range(1, m):
        while k and needle[i] != needle[k]:
            k = lps[k - 1]
        if needle[i] == needle[k]:
            k += 1
        lps[i] = k
    k = 0
    for i in range(start, n):
        while k and haystack[i] != needle[k]:
            k = lps[k - 1]
        if haystack[i] == needle[k]:
            k += 1
        if k == m:
            return i - m + 1
    return -1


def _coverage(keys, cnt):
    """keys 在 Counter 多重集中的覆盖率；空序列视为 1（整句移动自动通过）。"""
    if not keys:
        return 1.0
    need = Counter(keys)
    return sum((need & cnt).values()) / len(keys)


def _merge_adjacent(ops):
    merged = []
    for tag, i1, i2, j1, j2 in ops:
        if i1 == i2 and j1 == j2:
            continue
        if merged:
            ptag, pi1, pi2, pj1, pj2 = merged[-1]
            if ptag == tag and pi2 == i1 and pj2 == j1:
                merged[-1] = (ptag, pi1, i2, pj1, j2)
                continue
        merged.append((tag, i1, i2, j1, j2))
    return merged


class DiffResult:
    __slots__ = ('old_tokens', 'new_tokens', 'old_spans', 'new_spans', 'stats')

    def __init__(self, old_tokens, new_tokens, old_spans, new_spans, stats):
        self.old_tokens = old_tokens
        self.new_tokens = new_tokens
        self.old_spans = old_spans
        self.new_spans = new_spans
        self.stats = stats


# ================= 比对引擎 =================
class SmartDiffer:
    def __init__(self, ignore_case=False, ignore_ws=True,
                 smart_replace=True, look_ahead=150):
        self.ignore_case = ignore_case
        self.ignore_ws = ignore_ws
        self.smart_replace = smart_replace
        self.look_ahead = max(0, int(look_ahead))

    # ---------- 对外入口 ----------
    def diff_texts(self, old_text, new_text):
        old_tokens = tokenize(old_text)
        new_tokens = tokenize(new_text)
        o_keys, o_map = build_keys(old_tokens, self.ignore_case, not self.ignore_ws)
        n_keys, n_map = build_keys(new_tokens, self.ignore_case, not self.ignore_ws)
        self._setup_ctx(o_keys, n_keys)

        ops = self.diff_structured(o_keys, n_keys)   # 句级硬锚点 + 块内字符串匹配（锚点优先）
        ops = self._refine_all_replaces(ops)         # 替换段验证（存活句条件）
        self._mark_moves(ops)                        # 移动检测（句级 + 片段级）
        ops = _merge_adjacent(ops)

        old_spans, new_spans = [], []
        st = dict(delete_blocks=0, delete_words=0, insert_blocks=0, insert_words=0,
                  replace_blocks=0, replace_old_words=0, replace_new_words=0,
                  move_blocks=0, move_words=0, equal_words=0)
        for tag, i1, i2, j1, j2 in ops:
            old_spans.append((*key_span_to_token_span(o_map, len(old_tokens), i1, i2), tag))
            new_spans.append((*key_span_to_token_span(n_map, len(new_tokens), j1, j2), tag))
            if tag == 'equal':
                st['equal_words'] += i2 - i1
            elif tag == 'delete':
                st['delete_blocks'] += 1
                st['delete_words'] += i2 - i1
            elif tag == 'insert':
                st['insert_blocks'] += 1
                st['insert_words'] += j2 - j1
            elif tag == 'move':
                if i2 > i1:
                    st['move_blocks'] += 1
                    st['move_words'] += i2 - i1
                    st['equal_words'] += i2 - i1
            else:
                st['replace_blocks'] += 1
                st['replace_old_words'] += i2 - i1
                st['replace_new_words'] += j2 - j1
        total = len(o_keys) + len(n_keys)
        st['similarity'] = (2.0 * st['equal_words'] / total) if total else 1.0
        return DiffResult(old_tokens, new_tokens, old_spans, new_spans, st)

    def _setup_ctx(self, a, b):
        self._a, self._b = a, b
        self._a_sents = split_sentences(a)
        self._b_sents = split_sentences(b)
        self._a_sid = _sid_array(self._a_sents, len(a))
        self._b_sid = _sid_array(self._b_sents, len(b))
        self._cnt_a = Counter(a)
        self._cnt_b = Counter(b)

    # ---------- 句级精确 diff（整句硬锚点）；replace 块进入词级匹配 ----------
    def diff_structured(self, a, b):
        if not a and not b:
            return []
        a_sents, b_sents = self._a_sents, self._b_sents
        a_units = [tuple(a[s:e]) for s, e in a_sents]
        b_units = [tuple(b[s:e]) for s, e in b_sents]
        ops = []
        for tag, i1, i2, j1, j2 in SequenceMatcher(
                None, a_units, b_units, autojunk=False).get_opcodes():
            ka1, ka2 = _span(a_sents, len(a), i1, i2)
            kb1, kb2 = _span(b_sents, len(b), j1, j2)
            if tag == 'equal':
                ops.append(('equal', ka1, ka2, kb1, kb2))
            elif tag == 'delete':
                ops.append(('delete', ka1, ka2, kb1, kb1))
            elif tag == 'insert':
                ops.append(('insert', ka1, ka1, kb1, kb2))
            else:
                ops.extend(self._word_diff_block(ka1, ka2, kb1, kb2))
        return _merge_adjacent(ops)

    # ---------- 块内词级匹配：小规模精细匹配；大规模唯一词分治 ----------
    def _word_diff_block(self, ka1, ka2, kb1, kb2):
        if ka1 >= ka2 and kb1 >= kb2:
            return []
        if ka1 >= ka2:
            return [('insert', ka1, ka1, kb1, kb2)]
        if kb1 >= kb2:
            return [('delete', ka1, ka2, kb1, kb1)]
        if (ka2 - ka1) * (kb2 - kb1) <= INLINE_LIMIT:
            return self._word_diff_small(ka1, ka2, kb1, kb2)
        return self._word_diff_divide(ka1, ka2, kb1, kb2)

    def _word_diff_divide(self, ka1, ka2, kb1, kb2):
        """超大块分治：选"块内两侧都只出现一次"的唯一词中最居中者作为锚点
        （唯一词是无可辩驳的最强锚点：however、术语、数字往往都是），
        切成左右两块递归；无唯一词才退回整块 replace（病态防御）。"""
        a, b = self._a, self._b
        ca = Counter(a[ka1:ka2])
        cb = Counter(b[kb1:kb2])
        pos_in_b = {}
        for p in range(kb1, kb2):
            k = b[p]
            if cb[k] == 1:
                pos_in_b[k] = p
        mid = (ka1 + ka2) / 2.0
        best = None
        for p in range(ka1, ka2):
            k = a[p]
            if ca[k] == 1 and k in pos_in_b:
                d = abs(p - mid)
                if best is None or d < best[0]:
                    best = (d, p, pos_in_b[k])
        if best is None:
            return [('replace', ka1, ka2, kb1, kb2)]
        _, pa, pb = best
        left = self._word_diff_block(ka1, pa, kb1, pb)
        right = self._word_diff_block(pa + 1, ka2, pb + 1, kb2)
        return left + [('equal', pa, pa + 1, pb, pb + 1)] + right

    # ---------- 小规模词级匹配 + 锚点焊接审查（证据充分才否决）----------
    def _word_diff_small(self, ka1, ka2, kb1, kb2):
        a, b = self._a, self._b
        raw = SequenceMatcher(None, a[ka1:ka2], b[kb1:kb2], autojunk=False).get_opcodes()
        anchors = []
        for idx, (tag, i1, i2, j1, j2) in enumerate(raw):
            if tag == 'equal' and i2 > i1:
                so, sn = self._a_sid[ka1 + i1], self._b_sid[kb1 + j1]
                anchors.append((idx, so, sn,
                                self._anchor_quality(ka1 + i1, ka1 + i2,
                                                     kb1 + j1, kb1 + j2)))
        bad = self._find_bad_anchors(anchors)
        if not bad:
            return [(tag, ka1 + i1, ka1 + i2, kb1 + j1, kb1 + j2)
                    for tag, i1, i2, j1, j2 in raw]

        host_entry = (self._a_sid[ka1], self._b_sid[kb1])
        host_exit = None
        if ka2 < len(a) and kb2 < len(b):
            host_exit = (self._a_sid[ka2], self._b_sid[kb2])
        out, reg = [], None
        last_host = host_entry
        for idx, (tag, i1, i2, j1, j2) in enumerate(raw):
            gi1, gi2, gj1, gj2 = ka1 + i1, ka1 + i2, kb1 + j1, kb1 + j2
            if tag == 'equal' and i2 > i1 and idx not in bad:
                if reg:
                    self._flush_region(out, reg, last_host,
                                       (self._a_sid[gi1], self._b_sid[gj1]))
                    reg = None
                out.append(('equal', gi1, gi2, gj1, gj2))
                last_host = (self._a_sid[gi1], self._b_sid[gj1])
            else:
                if reg is None:
                    reg = [gi1, gi2, gj1, gj2]
                else:
                    reg[1], reg[3] = gi2, gj2
        if reg:
            self._flush_region(out, reg, last_host, host_exit)
        return out

    def _anchor_quality(self, gi1, gi2, gj1, gj2):
        """句首-句首 / 句尾-句尾对齐是强信号（句子的“身份”由边界确立）。"""
        q = min(gi2 - gi1, 10) * 0.1
        if (gi1 == self._a_sents[self._a_sid[gi1]][0]
                and gj1 == self._b_sents[self._b_sid[gj1]][0]):
            q += 2.0
        if (gi2 == self._a_sents[self._a_sid[gi2 - 1]][1]
                and gj2 == self._b_sents[self._b_sid[gj2 - 1]][1]):
            q += 2.0
        return q

    @staticmethod
    def _find_bad_anchors(anchors):
        """否决规则（严格 + 宽容）：
        ① 仅当同一旧句被锚到多个新句（或反之）才存在冲突；
        ② 按"组内最高质量、平局取最早"选出该句的对应句；
        ③ 指向其他句的锚点，仅当质量比最优组低超过 ANCHOR_MARGIN 才否决——
           平局、弱差一律保留（锚点默认不可推翻）。"""
        bad = set()
        for swap in (False, True):
            by_host = defaultdict(list)
            for order, (idx, so, sn, q) in enumerate(anchors):
                host, target = (sn, so) if swap else (so, sn)
                by_host[host].append((order, idx, target, q))
            for members in by_host.values():
                targets = {m[2] for m in members}
                if len(targets) <= 1:
                    continue
                best_t, best_k = None, None
                for t in targets:
                    ms = [m for m in members if m[2] == t]
                    k = (max(m[3] for m in ms), -min(m[0] for m in ms))
                    if best_k is None or k > best_k:
                        best_k, best_t = k, t
                best_q = best_k[0]
                for order, idx, t, q in members:
                    if t != best_t and q < best_q - ANCHOR_MARGIN:
                        bad.add(idx)
        return bad

    # ---------- 重对齐区：按最近保留锚点确立的“宿主句对应”处理 ----------
    @staticmethod
    def _sid_range_in(sid_arr, lo, hi, sid):
        s = e = None
        for p in range(lo, hi):
            if sid_arr[p] == sid:
                if s is None:
                    s = p
                e = p + 1
        return (s, e) if s is not None else None

    def _flush_region(self, out, reg, host_L, host_R):
        oa1, oa2, ob1, ob2 = reg
        pairs = []
        for host in (host_L, host_R):
            if not host:
                continue
            so, sn = host
            ro = self._sid_range_in(self._a_sid, oa1, oa2, so)
            rn = self._sid_range_in(self._b_sid, ob1, ob2, sn)
            if ro and rn:
                pairs.append((ro, rn))
        pairs.sort()
        cur_o, cur_n = oa1, ob1
        for (os_, oe), (ns, ne) in pairs:
            if os_ < cur_o or ns < cur_n:
                continue
            self._flush_residual(out, cur_o, os_, cur_n, ns)
            for stag, x1, x2, y1, y2 in SequenceMatcher(
                    None, self._a[os_:oe], self._b[ns:ne],
                    autojunk=False).get_opcodes():
                out.append((stag, os_ + x1, os_ + x2, ns + y1, ns + y2))
            cur_o, cur_n = oe, ne
        self._flush_residual(out, cur_o, oa2, cur_n, ob2)

    def _flush_residual(self, out, o1, o2, n1, n2):
        """无宿主对应的残余：双侧非空则词级匹配（锚点照常保留，不再过滤短锚点）。"""
        if o2 > o1 and n2 > n1:
            out.extend(self._word_diff_block(o1, o2, n1, n2))
        else:
            if o2 > o1:
                out.append(('delete', o1, o2, n1, n1))
            if n2 > n1:
                out.append(('insert', o2, o2, n1, n2))

    # ---------- 替换段验证（存活句条件）----------
    def _refine_all_replaces(self, ops):
        if not self.smart_replace:
            return _merge_adjacent(ops)
        out = []
        for tag, i1, i2, j1, j2 in ops:
            if tag == 'replace' and not self._is_true_replace(i1, i2, j1, j2):
                out.append(('delete', i1, i2, j1, j1))
                out.append(('insert', i2, i2, j1, j2))
            else:
                out.append((tag, i1, i2, j1, j2))
        return _merge_adjacent(out)

    def _is_true_replace(self, i1, i2, j1, j2):
        a, b = self._a, self._b
        old_seg, new_seg = a[i1:i2], b[j1:j2]
        if max(len(old_seg), len(new_seg)) <= 2:
            return True
        sim = SequenceMatcher(None, old_seg, new_seg, autojunk=False).ratio()
        if sim >= 0.60:
            return True
        la = self.look_ahead
        if la > 0:
            if len(old_seg) >= 3 and self._reproduced_alive(
                    old_seg, b, j2, la, self._b_sid, self._b_sents, self._cnt_a):
                return False
            if len(new_seg) >= 3 and self._reproduced_alive(
                    new_seg, a, i2, la, self._a_sid, self._a_sents, self._cnt_b):
                return False
            if len(old_seg) >= 6:
                h = find_subseq(b, old_seg[:3], j2)
                if j2 <= h < j2 + la and find_subseq(b, old_seg[-3:], h + 3) >= 0:
                    return False
            if sim < 0.30 and len(old_seg) >= 3:
                pos = find_subseq(b, old_seg, max(0, j1 - la))
                if 0 <= pos < j1:
                    sid = self._b_sid[pos]
                    s, e = self._b_sents[sid]
                    residue = b[s:pos] + b[pos + len(old_seg):e]
                    if _coverage(residue, self._cnt_a) >= ALIVE_RATIO:
                        return False
        return True

    @staticmethod
    def _reproduced_alive(seg, text, from_pos, window, text_sid, text_sents, other_cnt):
        """seg 在后文重现，且重现句去掉 seg 后的残余在另一侧文本中有对应
        （重现处不是全新句子，重现才算“移动”，否则是巧合重用）。"""
        end = min(len(text), from_pos + window)
        pos = find_subseq(text, seg, from_pos)
        checked = 0
        while 0 <= pos < end and checked < 5:
            sid = text_sid[pos]
            s, e = text_sents[sid]
            residue = text[s:pos] + text[pos + len(seg):e]
            if _coverage(residue, other_cnt) >= ALIVE_RATIO:
                return True
            pos = find_subseq(text, seg, pos + 1)
            checked += 1
        return False

    # ---------- 移动检测 ----------
    def _mark_moves(self, ops):
        a, b = self._a, self._b
        dels = [k for k, op in enumerate(ops) if op[0] == 'delete']
        inss = [k for k, op in enumerate(ops) if op[0] == 'insert']
        if not dels or not inss:
            return
        dmap, imap = defaultdict(list), defaultdict(list)
        for k in dels:
            _, i1, i2, _, _ = ops[k]
            t = tuple(a[i1:i2])
            if self._moveable_content(t):
                dmap[t].append(k)
        for k in inss:
            _, _, _, j1, j2 = ops[k]
            t = tuple(b[j1:j2])
            if self._moveable_content(t):
                imap[t].append(k)
        paired = set()
        for t, dks in dmap.items():
            iks = imap.get(t)
            if not iks:
                continue
            for dk, ik in zip(dks, iks):
                if dk in paired or ik in paired:
                    continue
                if self._move_pair_alive(ops[dk], ops[ik]):
                    ops[dk] = ('move',) + tuple(ops[dk][1:])
                    ops[ik] = ('move',) + tuple(ops[ik][1:])
                    paired.add(dk)
                    paired.add(ik)
        rd = [k for k in dels if k not in paired
              and self._is_full_sentence(ops[k], self._a_sents)]
        ri = [k for k in inss if k not in paired
              and self._is_full_sentence(ops[k], self._b_sents)]
        if rd and ri and len(rd) * len(ri) <= MOVE_SIM_LIMIT:
            best_d, best_i = {}, {}
            for dk in rd:
                ta = a[ops[dk][1]:ops[dk][2]]
                if len(ta) < MOVE_MIN_LEN:
                    continue
                for ik in ri:
                    tb = b[ops[ik][3]:ops[ik][4]]
                    if len(tb) < MOVE_MIN_LEN:
                        continue
                    r = SequenceMatcher(None, ta, tb, autojunk=False).ratio()
                    if r >= MOVE_SIM_RATIO:
                        if r > best_d.get(dk, (0.0,))[0]:
                            best_d[dk] = (r, ik)
                        if r > best_i.get(ik, (0.0,))[0]:
                            best_i[ik] = (r, dk)
            for dk, (r, ik) in best_d.items():
                if best_i.get(ik, (0.0, None))[1] == dk:
                    ops[dk] = ('move',) + tuple(ops[dk][1:])
                    ops[ik] = ('move',) + tuple(ops[ik][1:])

    @staticmethod
    def _is_content_word(tok):
        return any(c.isalnum() for c in tok) and tok.lower() not in STOPWORDS

    def _moveable_content(self, t):
        if len(t) >= 3:
            return True
        if len(t) == 2:
            return any(self._is_content_word(x) for x in t)
        return len(t) == 1 and self._is_content_word(t[0])

    @staticmethod
    def _is_full_sentence(op, sents):
        _, i1, i2, j1, j2 = op
        p1, p2 = (i1, i2) if i2 > i1 else (j1, j2)
        starts = {s for s, _ in sents}
        ends = {e for _, e in sents}
        return p1 in starts and p2 in ends

    def _move_pair_alive(self, dop, iop):
        a, b = self._a, self._b
        _, i1, i2, _, _ = dop
        _, _, _, j1, j2 = iop
        res_a = self._sentence_residue(self._a_sents, self._a_sid, a, i1, i2)
        res_b = self._sentence_residue(self._b_sents, self._b_sid, b, j1, j2)
        return (_coverage(res_a, self._cnt_b) >= ALIVE_RATIO
                and _coverage(res_b, self._cnt_a) >= ALIVE_RATIO)

    @staticmethod
    def _sentence_residue(sents, sid_arr, keys, p1, p2):
        s = sents[sid_arr[p1]][0]
        e = sents[sid_arr[p2 - 1]][1]
        return keys[s:p1] + keys[p2:e]


# ================= 后台线程 =================
class DiffWorker(QThread):
    done = pyqtSignal(object, object)

    def __init__(self, opts, old_text, new_text, parent=None):
        super().__init__(parent)
        self.opts, self.old_text, self.new_text = opts, old_text, new_text

    def run(self):
        try:
            self.done.emit(SmartDiffer(**self.opts).diff_texts(self.old_text,
                                                               self.new_text), None)
        except Exception as exc:
            self.done.emit(None, exc)


# ================= 渲染与工具函数 =================
def make_formats():
    fmt = {'equal': QTextCharFormat()}
    d = QTextCharFormat()
    d.setForeground(QColor('#c62828'))
    d.setBackground(QColor('#fde1e1'))
    d.setFontStrikeOut(True)
    fmt['delete'] = d
    i = QTextCharFormat()
    i.setForeground(QColor('#1b5e20'))
    i.setBackground(QColor('#e2f1e4'))
    i.setFontWeight(QFont.Bold)
    fmt['insert'] = i
    r = QTextCharFormat()
    r.setForeground(QColor('#8a6d00'))
    r.setBackground(QColor('#fff7cc'))
    r.setFontUnderline(True)
    r.setUnderlineColor(QColor('#8a6d00'))
    fmt['replace'] = r
    m = QTextCharFormat()
    m.setForeground(QColor('#1565c0'))
    m.setBackground(QColor('#e3f2fd'))
    m.setUnderlineStyle(QTextCharFormat.WaveUnderline)
    m.setUnderlineColor(QColor('#1565c0'))
    fmt['move'] = m
    return fmt


def highlight_format(base_fmt):
    f = QTextCharFormat(base_fmt)
    f.setForeground(QColor('#ff0000'))
    f.setBackground(QColor('#ffff00'))
    if f.underlineStyle() != QTextCharFormat.NoUnderline:
        f.setUnderlineColor(QColor('#ff0000'))
    return f


def render_diff(edit, tokens, spans, fmts):
    cursor = QTextCursor(edit.document())
    cursor.beginEditBlock()
    cursor.select(QTextCursor.Document)
    cursor.removeSelectedText()
    nav, pos = [], 0
    for s, e, tag in spans:
        if s < pos:
            s = pos
        if s > pos:
            cursor.insertText(''.join(tokens[pos:s]), fmts['equal'])
        start = cursor.position()
        if e > s:
            cursor.insertText(''.join(tokens[s:e]), fmts[tag])
        if tag != 'equal':
            nav.append((start, cursor.position() - start, tag))
        pos = max(pos, e)
    if pos < len(tokens):
        cursor.insertText(''.join(tokens[pos:]), fmts['equal'])
    cursor.endEditBlock()
    edit.moveCursor(QTextCursor.Start)
    return nav


def read_text_auto(path):
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-8-sig', 'gb18030', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')
    
# ================= 导出（HTML / Markdown） =================
_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>文本差异比较结果</title>
<style>
html,body{height:100%;margin:0}
body{display:flex;flex-direction:column;font-family:"Segoe UI","Microsoft YaHei",sans-serif}
#bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 12px;background:#f5f5f5;border-bottom:1px solid #ddd;font-size:13px}
#bar button{padding:4px 12px;cursor:pointer;font-size:13px}
#nav{font-weight:bold}
#stats{margin-left:auto;color:#555}
#panes{flex:1;display:flex;min-height:0}
.pane{flex:1;overflow-y:auto;padding:12px 16px}
.pane+.pane{border-left:1px solid #ddd}
.pane h3{margin:0 0 8px;font-size:13px;color:#888}
pre{white-space:pre-wrap;word-break:break-word;font-family:Consolas,"Courier New",monospace;font-size:14px;line-height:1.55;margin:0}
.delete{color:#c62828;background:#fde1e1;text-decoration:line-through}
.insert{color:#1b5e20;background:#e2f1e4;font-weight:bold}
.replace{color:#8a6d00;background:#fff7cc;text-decoration:underline}
.move{color:#1565c0;background:#e3f2fd;text-decoration:underline wavy}
.cur{background:#ffff00!important;color:#ff0000!important}
.lg{padding:0 3px;border-radius:2px}
</style>
</head>
<body>
<div id="bar">
<button onclick="go(-1)">&#9664; 上一个差异</button>
<button onclick="go(1)">下一个差异 &#9654;</button>
<span id="nav">差异 0/__NDIFF__</span>
<span id="legend">
<span class="lg" style="background:#fde1e1;color:#c62828">删除</span>
<span class="lg" style="background:#e2f1e4;color:#1b5e20"><b>新增</b></span>
<span class="lg" style="background:#fff7cc;color:#8a6d00"><u>替换</u></span>
<span class="lg" style="background:#e3f2fd;color:#1565c0">移动</span>
<span class="lg" style="background:#ffff00;color:#ff0000">当前差异</span>
</span>
<span id="stats">__STATS__</span>
</div>
<div id="panes">
<div class="pane"><h3>旧文本</h3><pre>__LEFT__</pre></div>
<div class="pane"><h3>新文本</h3><pre>__RIGHT__</pre></div>
</div>
<script>
var N=__NDIFF__,cur=-1;
function go(step){
  if(N===0)return;
  cur=(cur+step+N)%N;
  var olds=document.querySelectorAll('.cur'),i;
  for(i=0;i<olds.length;i++)olds[i].classList.remove('cur');
  var els=document.querySelectorAll('[data-d="'+cur+'"]');
  for(i=0;i<els.length;i++){
    els[i].classList.add('cur');
    els[i].scrollIntoView({block:'center',behavior:'smooth'});
  }
  document.getElementById('nav').textContent='差异 '+(cur+1)+'/'+N;
}
</script>
</body>
</html>
'''


def build_html(result):
    """DiffResult → 独立 HTML：双栏对照 + 内嵌 JS 差异导航。
    左右两侧第 k 个差异带相同 data-d="k"（spans 与 ops 一一对应，空区间不生成
    元素但序号照增，故两侧天然配对）；导航高亮 = 黄底 #ffff00 + 红字 #ff0000，
    下划线/删除线/波浪线颜色跟随 color 自动变红，效果与桌面版一致。"""

    def side(tokens, spans):
        out, pos, idx = [], 0, 0
        for s, e, tag in spans:
            if s < pos:
                s = pos
            if s > pos:
                out.append(html.escape(''.join(tokens[pos:s])))
            if e > s:
                txt = html.escape(''.join(tokens[s:e]))
                if tag == 'equal':
                    out.append(txt)
                else:
                    out.append(f'<span class="{tag}" data-d="{idx}">{txt}</span>')
            if tag != 'equal':
                idx += 1
            pos = max(pos, e)
        if pos < len(tokens):
            out.append(html.escape(''.join(tokens[pos:])))
        return ''.join(out), idx

    left_body, n_diff = side(result.old_tokens, result.old_spans)
    right_body, _ = side(result.new_tokens, result.new_spans)
    st = result.stats
    stats = (f'相似度 {st["similarity"]:.1%} ｜ '
             f'删除 {st["delete_blocks"]} 处/{st["delete_words"]} 词 ｜ '
             f'新增 {st["insert_blocks"]} 处/{st["insert_words"]} 词 ｜ '
             f'替换 {st["replace_blocks"]} 处（{st["replace_old_words"]} → '
             f'{st["replace_new_words"]} 词）｜ '
             f'移动 {st["move_blocks"]} 处/{st["move_words"]} 词')
    return (_HTML_TEMPLATE
            .replace('__STATS__', html.escape(stats))
            .replace('__LEFT__', left_body)
            .replace('__RIGHT__', right_body)
            .replace('__NDIFF__', str(n_diff)))


_MD_CHAR_ESCAPE = re.compile(r'([\\*~`_[\]<>])')
_MD_LINE_ESCAPE = re.compile(r'(?m)^(\s*)(#{1,6}|>|[-+]|\d+[.)])(?=\s|$)')


def _md_escape(s):
    """转义 Markdown 标记字符 + 行首标题/引用/列表符号（防原文被当成排版）。"""
    s = _MD_CHAR_ESCAPE.sub(r'\\\1', s)
    return _MD_LINE_ESCAPE.sub(lambda m: m.group(1) + '\\' + m.group(2), s)


def build_markdown(result):
    """DiffResult → Markdown 合并视图（单流文档，无双栏/导航）：
    ~~删除~~ ｜ **新增** ｜ ~~旧~~**新**（替换）｜ ==移动==（Obsidian/Typora 支持）"""
    parts = []
    for (s1, e1, tag), (s2, e2, _t) in zip(result.old_spans, result.new_spans):
        old_seg = _md_escape(''.join(result.old_tokens[s1:e1]))
        new_seg = _md_escape(''.join(result.new_tokens[s2:e2]))
        if tag == 'equal':
            parts.append(old_seg)
        elif tag == 'delete':
            parts.append('~~' + old_seg + '~~')
        elif tag == 'insert':
            parts.append('**' + new_seg + '**')
        elif tag == 'replace':
            parts.append('~~' + old_seg + '~~**' + new_seg + '**')
        elif tag == 'move':
            parts.append('==' + (old_seg if e1 > s1 else new_seg) + '==')
    st = result.stats
    header = (
        '# 文本差异比较结果\n\n'
        '> **图例**：~~删除~~ ｜ **新增** ｜ ~~旧~~**新**（替换）｜ ==移动==\n>\n'
        f'> 相似度 **{st["similarity"]:.1%}** ｜ '
        f'删除 {st["delete_blocks"]} 处/{st["delete_words"]} 词 ｜ '
        f'新增 {st["insert_blocks"]} 处/{st["insert_words"]} 词 ｜ '
        f'替换 {st["replace_blocks"]} 处（{st["replace_old_words"]} → '
        f'{st["replace_new_words"]} 词）｜ '
        f'移动 {st["move_blocks"]} 处/{st["move_words"]} 词\n\n---\n\n')
    return header + ''.join(parts) + '\n'


# ================= 主窗口 =================
class MainWindow(QMainWindow):
    TAG_NAME = {'delete': '删除', 'insert': '新增', 'replace': '替换', 'move': '移动'}

    def __init__(self):
        super().__init__()
        self.setWindowTitle('纯文本差异比较器（锚点优先 · 移动检测）')
        self.resize(1280, 820)
        self._syncing = False
        self._in_result = False
        self._zoom_accum = 0
        self.worker = None
        self._result = None
        self.nav, self.nav_index = [], -1
        self._current = None
        self.fmts = make_formats()
        self._build_ui()
        self._bind()
        self.set_result_mode(False)
        self._update_count()

    def _build_ui(self):
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 2)
        root.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(5)
        self.btn_load_l = QPushButton('导入左')
        self.btn_load_r = QPushButton('导入右')
        self.btn_clear = QPushButton('清空')
        self.btn_swap = QPushButton('⇄ 交换')
        self.btn_compare = QPushButton('▶ 开始比较')
        self.btn_compare.setStyleSheet('font-weight:bold;')
        self.btn_edit = QPushButton('✎ 返回编辑')
        self.btn_export = QPushButton('⤓ 导出')
        self.btn_export.setToolTip('将比较结果导出为 HTML（带差异导航）或 Markdown')
        self.chk_case = QCheckBox('忽略大小写')
        self.chk_ws = QCheckBox('忽略空白')
        self.chk_ws.setChecked(True)
        self.chk_smart = QCheckBox('智能替换')
        self.chk_smart.setChecked(True)
        self.spin_look = QSpinBox()
        self.spin_look.setRange(0, 1000)
        self.spin_look.setValue(150)
        self.spin_look.setSingleStep(10)
        self.spin_look.setPrefix('后视 ')
        self.spin_look.setSuffix(' 词')
        self.spin_look.setToolTip('向后看多少个词，验证“替换”是否其实是移动/增删；0 关闭验证')
        self.btn_prev = QPushButton('◀')
        self.btn_next = QPushButton('▶')
        self.btn_prev.setFixedWidth(32)
        self.btn_next.setFixedWidth(32)
        self.lbl_nav = QLabel('差异 0/0')

        for b in (self.btn_load_l, self.btn_load_r, self.btn_clear, self.btn_swap,
                  self.btn_compare, self.btn_edit, self.btn_export, self.btn_prev, self.btn_next):
            b.setFixedHeight(26)

        for w in (self.btn_load_l, self.btn_load_r, self.btn_clear, self.btn_swap):
            bar.addWidget(w)
        bar.addWidget(self._vsep())
        bar.addWidget(self.btn_compare)
        bar.addWidget(self.btn_edit)
        bar.addWidget(self.btn_export)
        bar.addWidget(self._vsep())
        for w in (self.chk_case, self.chk_ws, self.chk_smart, self.spin_look):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.lbl_nav)
        root.addLayout(bar)

        self.splitter = QSplitter(Qt.Horizontal)
        self.left_edit = self._make_edit('旧文本：在此输入 / 粘贴，或点击左上角“导入左”…')
        self.right_edit = self._make_edit('新文本：在此输入 / 粘贴，或点击左上角“导入右”…')
        self.splitter.addWidget(self.left_edit)
        self.splitter.addWidget(self.right_edit)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        self.left_edit.viewport().installEventFilter(self)
        self.right_edit.viewport().installEventFilter(self)

        self.lbl_stats = QLabel('就绪：输入文本后点击“开始比较”（Ctrl+滚轮同步缩放）')
        self.statusBar().addWidget(self.lbl_stats, 1)
        self.lbl_count = QLabel('')
        self.statusBar().addPermanentWidget(self.lbl_count)
        self.statusBar().addPermanentWidget(QLabel(
            '<span style="background:#fde1e1;color:#c62828;"> 删除 </span> '
            '<span style="background:#e2f1e4;color:#1b5e20;"><b> 新增 </b></span> '
            '<span style="background:#fff7cc;color:#8a6d00;"><u> 替换 </u></span> '
            '<span style="background:#e3f2fd;color:#1565c0;"> 移动 </span> '
            '<span style="background:#ffff00;color:#ff0000;"> 当前差异 </span>'))

    @staticmethod
    def _vsep():
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    @staticmethod
    def _fixed_font():
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        f.setPointSize(10)
        return f

    def _make_edit(self, placeholder):
        e = QTextEdit()
        e.setAcceptRichText(False)
        e.setLineWrapMode(QTextEdit.WidgetWidth)
        e.setPlaceholderText(placeholder)
        e.setFont(self._fixed_font())
        return e

    def _bind(self):
        self.btn_compare.clicked.connect(self.on_compare)
        self.btn_edit.clicked.connect(self.back_to_edit)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_load_l.clicked.connect(lambda: self.import_file(self.left_edit))
        self.btn_load_r.clicked.connect(lambda: self.import_file(self.right_edit))
        self.btn_clear.clicked.connect(self.on_clear)
        self.btn_swap.clicked.connect(self.on_swap)
        self.btn_prev.clicked.connect(lambda: self.goto_diff(-1))
        self.btn_next.clicked.connect(lambda: self.goto_diff(1))
        self.left_edit.verticalScrollBar().valueChanged.connect(
            lambda _v: self._sync_scroll(self.left_edit, self.right_edit))
        self.right_edit.verticalScrollBar().valueChanged.connect(
            lambda _v: self._sync_scroll(self.right_edit, self.left_edit))
        self.left_edit.textChanged.connect(self._update_count)
        self.right_edit.textChanged.connect(self._update_count)
        
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and obj in (self.left_edit.viewport(),
                                                    self.right_edit.viewport()):
            if event.modifiers() & Qt.ControlModifier:
                self._zoom_accum += event.angleDelta().y()
                steps = int(self._zoom_accum / 120)
                if steps:
                    self._zoom_accum -= steps * 120
                    for e in (self.left_edit, self.right_edit):
                        if steps > 0:
                            e.zoomIn(steps)
                        else:
                            e.zoomOut(-steps)
                    src = self.left_edit if obj is self.left_edit.viewport() \
                        else self.right_edit
                    self._sync_scroll(src, self.right_edit if src is self.left_edit
                                      else self.left_edit)
                return True
            self._zoom_accum = 0
        return super().eventFilter(obj, event)

    def set_result_mode(self, on):
        self._in_result = on
        self.left_edit.setReadOnly(on)
        self.right_edit.setReadOnly(on)
        self.btn_compare.setEnabled(not on)
        self.btn_edit.setEnabled(on)
        self.btn_export.setEnabled(on)

    def back_to_edit(self):
        if not self._in_result:
            return
        self._current = None
        for e in (self.left_edit, self.right_edit):
            v = e.verticalScrollBar().value()
            c = QTextCursor(e.document())
            c.select(QTextCursor.Document)
            c.setCharFormat(QTextCharFormat())
            c.clearSelection()
            c.movePosition(QTextCursor.Start)
            e.setTextCursor(c)
            e.verticalScrollBar().setValue(v)
        self.set_result_mode(False)
        self.nav, self.nav_index = [], -1
        self.lbl_nav.setText('差异 0/0')
        self.lbl_stats.setText('编辑中：修改文本后点击“开始比较”')

    def import_file(self, edit):
        path, _ = QFileDialog.getOpenFileName(
            self, '导入文本文件', '', '文本文件 (*.txt *.md *.log *.csv);;所有文件 (*)')
        if not path:
            return
        try:
            text = read_text_auto(path)
        except Exception as e:
            QMessageBox.warning(self, '读取失败', str(e))
            return
        self.back_to_edit()
        edit.setPlainText(text)

    def on_clear(self):
        self.back_to_edit()
        self.left_edit.clear()
        self.right_edit.clear()
        self.lbl_stats.setText('就绪')

    def on_swap(self):
        was_result = self._in_result
        self.back_to_edit()
        t = self.left_edit.toPlainText()
        self.left_edit.setPlainText(self.right_edit.toPlainText())
        self.right_edit.setPlainText(t)
        if was_result:
            self.on_compare()

    def on_compare(self):
        old = self.left_edit.toPlainText()
        new = self.right_edit.toPlainText()
        if not old.strip() and not new.strip():
            QMessageBox.information(self, '提示', '请先输入或导入需要比较的文本。')
            return
        opts = dict(ignore_case=self.chk_case.isChecked(),
                    ignore_ws=self.chk_ws.isChecked(),
                    smart_replace=self.chk_smart.isChecked(),
                    look_ahead=self.spin_look.value())
        self.btn_compare.setEnabled(False)
        self.lbl_stats.setText('正在比较，请稍候…')
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        self.worker = DiffWorker(opts, old, new, self)
        self.worker.done.connect(self.on_diff_done)
        self.worker.start()
    
    def on_export(self):
        """导出比较结果（仅比较模式可用）：HTML 含 JS 差异导航；Markdown 为合并视图。"""
        print(f'[DEBUG] on_export 被调用, _result is None: {self._result is None}')
        if self._result is None:
            QMessageBox.information(self, '无法导出',
                                    '当前没有可导出的比较结果。\n（若已在比较模式仍看到此提示，'
                                    '说明 on_diff_done 中缺少 self._result = result）')
            return
        path, sel = QFileDialog.getSaveFileName(
            self, '导出比较结果', 'diff_result.html',
            'HTML 文件 (*.html);;Markdown 文件 (*.md)')
        if not path:
            return
        try:
            if 'Markdown' in sel or path.lower().endswith('.md'):
                if not path.lower().endswith('.md'):
                    path += '.md'
                content = build_markdown(self._result)
            else:
                if not path.lower().endswith(('.html', '.htm')):
                    path += '.html'
                content = build_html(self._result)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            QMessageBox.warning(self, '导出失败', f'{type(e).__name__}: {e}')
            return
        self.lbl_stats.setText(f'已导出：{path}')

    def on_diff_done(self, result, err):
        QApplication.restoreOverrideCursor()
        if err is not None:
            self.btn_compare.setEnabled(True)
            self.lbl_stats.setText('比较失败')
            QMessageBox.critical(self, '比较失败', f'{type(err).__name__}: {err}')
            return
        self._result = result            # ★ 必须在这里：err 检查之后、render 之前
        old_nav = render_diff(self.left_edit, result.old_tokens, result.old_spans, self.fmts)
        new_nav = render_diff(self.right_edit, result.new_tokens, result.new_spans, self.fmts)
        self.set_result_mode(True)
        self.nav = list(zip(old_nav, new_nav))
        self.nav_index = -1
        self._current = None
        st = result.stats
        n_diff = len(self.nav)
        self.lbl_nav.setText(f'差异 0/{n_diff}')
        if n_diff == 0:
            self.lbl_stats.setText('两段文本内容一致（空白差异已忽略）')
        else:
            self.lbl_stats.setText(
                f'相似度 {st["similarity"]:.1%} ｜ '
                f'删除 {st["delete_blocks"]} 处/{st["delete_words"]} 词 ｜ '
                f'新增 {st["insert_blocks"]} 处/{st["insert_words"]} 词 ｜ '
                f'替换 {st["replace_blocks"]} 处（{st["replace_old_words"]} → '
                f'{st["replace_new_words"]} 词）｜ '
                f'移动 {st["move_blocks"]} 处/{st["move_words"]} 词')
    
    @staticmethod
    def _set_span_format(edit, span, fmt):
        pos, length, _ = span
        if length <= 0:
            return
        c = edit.textCursor()
        c.setPosition(pos)
        c.setPosition(pos + length, QTextCursor.KeepAnchor)
        c.mergeCharFormat(fmt)

    def _clear_current_highlight(self):
        if not self._current:
            return
        o, n = self._current
        self._set_span_format(self.left_edit, o, self.fmts[o[2]])
        self._set_span_format(self.right_edit, n, self.fmts[n[2]])
        self._current = None

    def goto_diff(self, step):
        if not self.nav:
            return
        self._clear_current_highlight()
        self.nav_index = (self.nav_index + step) % len(self.nav)
        o, n = self.nav[self.nav_index]
        for edit, span in ((self.left_edit, o), (self.right_edit, n)):
            self._set_span_format(edit, span, highlight_format(self.fmts[span[2]]))
            c = edit.textCursor()
            c.setPosition(span[0])
            edit.setTextCursor(c)
            edit.ensureCursorVisible()
        self._current = (o, n)
        self.lbl_nav.setText(
            f'差异 {self.nav_index + 1}/{len(self.nav)}（{self.TAG_NAME[o[2]]}）')

    def _sync_scroll(self, src, dst):
        if self._syncing:
            return
        self._syncing = True
        try:
            s, d = src.verticalScrollBar(), dst.verticalScrollBar()
            d.setValue(int(round(s.value() / max(1, s.maximum()) * d.maximum())))
        finally:
            self._syncing = False

    def _update_count(self):
        l = len(self.left_edit.toPlainText())
        r = len(self.right_edit.toPlainText())
        self.lbl_count.setText(f'左 {l} 字符 ｜ 右 {r} 字符　')

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(1500)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('SmartTextDiff')
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()