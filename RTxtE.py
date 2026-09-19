# -*- coding: utf-8 -*-
"""
auto_color_editor.py  (PyQt5)  v2
  * 基本格式：字号 / 字体颜色 / 高亮 / 加粗 / 斜体 / 下划线 / 删除线
  * Ctrl + 鼠标滚轮：0.5x~3.0x 整体缩放（显示层缩放，对所有文字生效）
  * 特色1：未手动设置高亮的文本，按句子（英文句号 . ）自动分配极淡高亮色
  * 特色2：未手动设置字体颜色的文本，句内按逗号 , 切分分句，用黑/深灰循环着色
  * 特色3：自动匹配 [] ，括号及其内容显示 红字 + 黄色高亮
  * 换行符不参与任何自动着色；默认字体 Times New Roman
"""
import sys
import random
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor, QFont, QKeySequence, QTextCharFormat, QTextCursor, QTextFormat,
)
from PyQt5.QtWidgets import (
    QAction, QApplication, QComboBox, QColorDialog, QFrame,
    QGraphicsScene, QGraphicsView, QLabel, QMainWindow, QTextEdit, QToolBar,
)
# ============================ 可调参数 ============================
DEFAULT_FONT_FAMILY = "Times New Roman"
DEFAULT_FONT_SIZE = 11
ZOOM_MIN_FACTOR = 0.5        # Ctrl+滚轮缩放范围
ZOOM_MAX_FACTOR = 3.0
ZOOM_STEP = 1.1              # 每格滚轮的缩放倍率
SENTENCE_ENDS = {'.'}        # 句子结束符（想支持中文可加 '。'）
CLAUSE_SEPS   = {','}        # 分句分隔符（想支持中文可加 '，'）
BRACKET_OPEN, BRACKET_CLOSE = '[', ']'
LINE_SEPARATORS = frozenset('\n\r  ')   # 换行/段落分隔符，不着色
# 特色1：句子高亮色（HSV 空间）—— 更淡：更低饱和、更高亮度
SENT_SAT_MIN, SENT_SAT_MAX = 0.08, 0.16
SENT_VAL_MIN, SENT_VAL_MAX = 0.96, 1.00
HUE_STEP = 0.38196601125     # 黄金比例步进 => 相邻句色相差约137.5°
# 特色2：分句灰色。最浅不超过 0x63(=99)，gray ∈ [0, 99]
CLAUSE_MAX_GRAY   = 0x63
CLAUSE_GRAY_RATIO = (0.0, 1.0, 0.42, 0.78, 0.20, 0.58)
# 特色3：方括号配色
BRACKET_FG = QColor(198, 40, 40)     # 红字
BRACKET_BG = QColor(255, 235, 59)    # 黄色高亮
DECORATE_DELAY_MS = 200
DEMO_TEXT = (
    "This editor colors every sentence automatically, as long as you did not "
    "set a highlight yourself. Inside a sentence, clauses separated by commas, "
    "like this one, and this one, get different shades of gray. Matched "
    "brackets [like these] are always red on yellow, even nested [outer "
    "[inner] done]. Select text and use the toolbar for size, color, "
    "highlight, bold, italic, underline and strike-through. "
    "Hold Ctrl and scroll the wheel to zoom everything on screen."
)
# ============================ 编辑器 ============================
class RichTextEditor(QTextEdit):
    """负责发出缩放/格式快捷键请求，具体处理交给外层"""
    zoomRequested = pyqtSignal(int)     # +1 放大 / -1 缩小
    formatShortcut = pyqtSignal(str)    # 'bold' / 'italic' / 'underline' / 'strike'
    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont(DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE)
        self.setFont(font)                       # 控件字体
        self.document().setDefaultFont(font)     # 文档默认字体（真正起作用）
    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoomRequested.emit(1 if delta > 0 else -1)
            event.accept()
        else:
            super().wheelEvent(event)
    def keyPressEvent(self, event):
        # 快捷键兜底：编辑器被嵌入 QGraphicsProxyWidget 后，
        # QAction 的窗口级快捷键可能收不到，这里直接处理保证可用
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            key = event.key()
            name = None
            if key == Qt.Key_B:
                name = 'bold'
            elif key == Qt.Key_I:
                name = 'italic'
            elif key == Qt.Key_U:
                name = 'underline'
            elif key == Qt.Key_X and (mods & Qt.ShiftModifier):
                name = 'strike'
            if name:
                self.formatShortcut.emit(name)
                event.accept()
                return
        super().keyPressEvent(event)
# ============================ 缩放视图 ============================
class ZoomView(QGraphicsView):
    """把编辑器嵌入 QGraphicsScene，通过对代理项 setScale 实现纯显示层缩放：
       - 所有文字（包括单独设置过字号的片段）一起缩放；
       - 不修改文档内容，不影响撤销栈和逻辑字号。"""
    def __init__(self, child, parent=None):
        super().__init__(parent)
        self._child = child
        self._scene = QGraphicsScene(self)
        self._proxy = self._scene.addWidget(child)
        self._proxy.setPos(0, 0)
        self.setScene(self._scene)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._zoom = 1.0
    def zoom_by(self, direction):
        factor = self._zoom * ZOOM_STEP if direction > 0 else self._zoom / ZOOM_STEP
        factor = max(ZOOM_MIN_FACTOR, min(ZOOM_MAX_FACTOR, factor))
        if abs(factor - self._zoom) < 1e-9:
            return
        self._zoom = factor
        self._proxy.setScale(factor)
        self._sync_geometry()
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_geometry()
    def _sync_geometry(self):
        # 让编辑器缩放后恰好填满可视区域（文字按缩放后的宽度重新折行）
        vs = self.viewport().size()
        z = self._zoom or 1.0
        self._child.resize(max(1, int(vs.width() / z)),
                           max(1, int(vs.height() / z)))
        self._scene.setSceneRect(self._proxy.sceneBoundingRect())
# ============================ 主窗口 ============================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("自动着色富文本编辑器")
        self.resize(960, 640)
        self.editor = RichTextEditor()
        self.zoom_view = ZoomView(self.editor, self)
        self.setCentralWidget(self.zoom_view)
        self.editor.zoomRequested.connect(self.zoom_view.zoom_by)
        self.editor.formatShortcut.connect(self._on_format_shortcut)
        self._base_hue = random.random()   # 本次运行的句子色相起点（随机）
        self._build_toolbar()
        self._timer = QTimer(self, singleShot=True, interval=DECORATE_DELAY_MS)
        self._timer.timeout.connect(self.update_decorations)
        self.editor.textChanged.connect(self._timer.start)
        self.editor.currentCharFormatChanged.connect(self._sync_format_actions)
        self.editor.setPlainText(DEMO_TEXT)
        self.update_decorations()
    # -------------------- 工具栏 --------------------
    def _add_action(self, tb, text, slot, checkable=False,
                    shortcut=None, tooltip=None):
        act = QAction(text, self)
        if checkable:
            act.setCheckable(True)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        if tooltip:
            act.setToolTip(tooltip)
        act.triggered.connect(slot)
        tb.addAction(act)
        return act
    def _build_toolbar(self):
        tb = QToolBar("格式", self)
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" 字号 "))
        self.size_combo = QComboBox(self)
        self.size_combo.setEditable(True)
        self.size_combo.addItems([str(s) for s in
                                  (8, 9, 10, 11, 12, 14, 16, 18, 20,
                                   24, 28, 32, 36, 48, 72)])
        self.size_combo.setCurrentText(str(DEFAULT_FONT_SIZE))
        self.size_combo.activated[str].connect(self._set_font_size)
        self.size_combo.lineEdit().returnPressed.connect(
            lambda: self._set_font_size(self.size_combo.currentText()))
        tb.addWidget(self.size_combo)
        tb.addSeparator()
        self.act_bold = self._add_action(
            tb, "加粗", self._toggle_bold, checkable=True, shortcut="Ctrl+B")
        self.act_italic = self._add_action(
            tb, "斜体", self._toggle_italic, checkable=True, shortcut="Ctrl+I")
        self.act_underline = self._add_action(
            tb, "下划线", self._toggle_underline, checkable=True, shortcut="Ctrl+U")
        self.act_strike = self._add_action(
            tb, "删除线", self._toggle_strike, checkable=True,
            shortcut="Ctrl+Shift+X")
        tb.addSeparator()
        self._add_action(tb, "字体颜色…", self._pick_fg,
                         tooltip="手动设置后，该片段不再参与自动分句着色")
        self._add_action(tb, "高亮颜色…", self._pick_bg,
                         tooltip="手动设置后，该片段不再参与自动句子高亮")
        self._add_action(tb, "恢复自动着色", self._clear_manual_color,
                         tooltip="移除选中文本手动设置的颜色/高亮，恢复自动着色"
                                 "（保留加粗、字号等其他格式）")
    # -------------------- 手动格式 --------------------
    def _merge_format(self, fmt):
        self.editor.mergeCurrentCharFormat(fmt)
        self.editor.setFocus()
    def _on_format_shortcut(self, name):
        handler = {
            'bold': self._toggle_bold,
            'italic': self._toggle_italic,
            'underline': self._toggle_underline,
            'strike': self._toggle_strike,
        }.get(name)
        if handler:
            handler()
    def _set_font_size(self, text):
        try:
            size = float(text)
        except ValueError:
            return
        if 1 <= size <= 200:
            fmt = QTextCharFormat()
            fmt.setFontPointSize(size)
            self._merge_format(fmt)
    # 切换类操作不依赖按钮的 checked，而是以光标处当前格式为准取反，
    # 这样按钮点击和快捷键两条路径行为一致
    def _toggle_bold(self, _checked=False):
        cur = self.editor.currentCharFormat()
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Normal if cur.fontWeight() >= QFont.Bold
                          else QFont.Bold)
        self._merge_format(fmt)
    def _toggle_italic(self, _checked=False):
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.editor.currentCharFormat().fontItalic())
        self._merge_format(fmt)
    def _toggle_underline(self, _checked=False):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not self.editor.currentCharFormat().fontUnderline())
        self._merge_format(fmt)
    def _toggle_strike(self, _checked=False):
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(not self.editor.currentCharFormat().fontStrikeOut())
        self._merge_format(fmt)
    def _pick_fg(self, _checked=False):
        cur = self.editor.currentCharFormat()
        initial = (cur.foreground().color()
                   if cur.hasProperty(QTextFormat.ForegroundBrush)
                   else QColor("black"))
        color = QColorDialog.getColor(initial, self, "选择字体颜色")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._merge_format(fmt)
    def _pick_bg(self, _checked=False):
        cur = self.editor.currentCharFormat()
        initial = (cur.background().color()
                   if cur.hasProperty(QTextFormat.BackgroundBrush)
                   else QColor("white"))
        color = QColorDialog.getColor(initial, self, "选择高亮颜色")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            self._merge_format(fmt)
    def _clear_manual_color(self, _checked=False):
        """移除选区内的 前景色/背景色 属性（保留粗体、字号等），恢复自动着色"""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            return
        doc = self.editor.document()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        jobs = []
        block = doc.findBlock(start)
        while block.isValid() and block.position() < end:
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.length() > 0:
                    fs, fe = frag.position(), frag.position() + frag.length()
                    s, e = max(fs, start), min(fe, end)
                    if s < e:
                        fmt = frag.charFormat()
                        if (fmt.hasProperty(QTextFormat.ForegroundBrush) or
                                fmt.hasProperty(QTextFormat.BackgroundBrush)):
                            new_fmt = QTextCharFormat()
                            for pid, val in fmt.properties().items():
                                if pid not in (QTextFormat.ForegroundBrush,
                                               QTextFormat.BackgroundBrush):
                                    new_fmt.setProperty(pid, val)
                            jobs.append((s, e, new_fmt))
                it += 1
            block = block.next()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        for s, e, f in jobs:
            cur.setPosition(s)
            cur.setPosition(e, QTextCursor.KeepAnchor)
            cur.setCharFormat(f)
        cur.endEditBlock()
    def _sync_format_actions(self, fmt):
        self.act_bold.setChecked(fmt.fontWeight() >= QFont.Bold)
        self.act_italic.setChecked(fmt.fontItalic())
        self.act_underline.setChecked(fmt.fontUnderline())
        self.act_strike.setChecked(fmt.fontStrikeOut())
        if fmt.fontPointSize() > 0:
            self.size_combo.setCurrentText(str(int(fmt.fontPointSize())))
    # -------------------- 自动着色 --------------------
    def _sentence_color(self, index):
        """第 index 个句子的颜色：极淡（低饱和/高亮）、相邻色相差大、
        同一序号颜色恒定（刷新不闪烁）"""
        hue = (self._base_hue + index * HUE_STEP) % 1.0
        rng = random.Random(index * 2654435761)
        sat = SENT_SAT_MIN + rng.random() * (SENT_SAT_MAX - SENT_SAT_MIN)
        val = SENT_VAL_MIN + rng.random() * (SENT_VAL_MAX - SENT_VAL_MIN)
        return QColor.fromHsvF(hue, sat, val)
    @staticmethod
    def _clause_color(index):
        ratio = CLAUSE_GRAY_RATIO[index % len(CLAUSE_GRAY_RATIO)]
        g = int(round(CLAUSE_MAX_GRAY * ratio))
        return QColor(g, g, g)
    def _decide_clause(self, fg_decision, has_fg, s, e, clause_idx):
        if s >= e:
            return
        color = self._clause_color(clause_idx)
        for p in range(s, e):
            if not has_fg[p]:
                fg_decision[p] = color
    def update_decorations(self):
        doc = self.editor.document()
        text = doc.toPlainText()
        n = len(text)
        if n == 0:
            self.editor.setExtraSelections([])
            return
        # 1) 标记被用户手动设置了 前景/背景 色的字符
        has_fg = bytearray(n)
        has_bg = bytearray(n)
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.length() > 0:
                    fmt = frag.charFormat()
                    fg_set = 1 if fmt.hasProperty(QTextFormat.ForegroundBrush) else 0
                    bg_set = 1 if fmt.hasProperty(QTextFormat.BackgroundBrush) else 0
                    if fg_set or bg_set:
                        for p in range(frag.position(),
                                       min(frag.position() + frag.length(), n)):
                            has_fg[p] = fg_set
                            has_bg[p] = bg_set
                it += 1
            block = block.next()
        # 2) 按句子结束符切分句子
        sentences = []
        start = 0
        for i, ch in enumerate(text):
            if ch in SENTENCE_ENDS:
                sentences.append((start, i + 1))
                start = i + 1
        if start < n:
            sentences.append((start, n))
        fg_decision = [None] * n
        bg_decision = [None] * n
        # 3) 特色1：句子级高亮（跳过手动设置过高亮的字符）
        for si, (s, e) in enumerate(sentences):
            color = self._sentence_color(si)
            for p in range(s, e):
                if not has_bg[p]:
                    bg_decision[p] = color
        # 4) 特色2：句内按逗号切分句，黑/深灰循环（跳过手动设过颜色的字符）
        clause_idx = 0
        for s, e in sentences:
            cstart = s
            for p in range(s, e):
                if text[p] in CLAUSE_SEPS:
                    self._decide_clause(fg_decision, has_fg,
                                        cstart, p + 1, clause_idx)
                    clause_idx += 1
                    cstart = p + 1
            self._decide_clause(fg_decision, has_fg, cstart, e, clause_idx)
            clause_idx += 1
        # 5) 特色3：栈匹配方括号（支持嵌套），匹配到的整体红字黄底
        stack = []
        for i, ch in enumerate(text):
            if ch == BRACKET_OPEN:
                stack.append(i)
            elif ch == BRACKET_CLOSE and stack:
                s = stack.pop()
                for p in range(s, i + 1):
                    fg_decision[p] = BRACKET_FG
                    bg_decision[p] = BRACKET_BG
        # 6) 换行/段落分隔符一律不着色（否则行尾会出现多余色块）
        for p, ch in enumerate(text):
            if ch in LINE_SEPARATORS:
                fg_decision[p] = None
                bg_decision[p] = None
        # 7) 合并相邻相同决策，生成叠加选区
        selections = []
        p = 0
        while p < n:
            f, b = fg_decision[p], bg_decision[p]
            if f is None and b is None:
                p += 1
                continue
            q = p + 1
            while q < n and fg_decision[q] == f and bg_decision[q] == b:
                q += 1
            sel = QTextEdit.ExtraSelection()
            cursor = QTextCursor(doc)
            cursor.setPosition(p)
            cursor.setPosition(q, QTextCursor.KeepAnchor)
            sel.cursor = cursor
            fmt = QTextCharFormat()
            if f is not None:
                fmt.setForeground(f)
            if b is not None:
                fmt.setBackground(b)
            sel.format = fmt
            selections.append(sel)
            p = q
        self.editor.setExtraSelections(selections)
# ============================ 入口 ============================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())