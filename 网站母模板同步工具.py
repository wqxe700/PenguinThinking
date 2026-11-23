# -*- coding: utf-8 -*-
"""
PenguinThinking 站点「母模板 → 子网页」同步工具  (Windows / Python 3.8+)

设计前提（依据实际站点结构）：
  * 每个 *-container.html 都是一份**完整页面**，骨架重复：
      两块 <style>、#sidebar-bg、#task-panel(.task-list 菜单)、底部企鹅/联系图标/版权、
      以及一个 #slot > iframe 内容槽。
  * 页面是刻意的复古写法：gb2312 编码、<head> 里夹着 <body>、两个 <body> 标签、
    大量乱码注释与固定缩进。因此**绝不用 DOM 解析器重序列化**，只做「文本级整页替换」，
    保证字节结构、注释、空行、CRLF 全部原样保留。
  * 子页真正私有的东西只有：<title> 文本、iframe 的 src（contact.html / nothing.html ...）。
    个别子页还有「底部企鹅被 <a href="home-container.html"> 包裹」这种个性化，默认按
    「整页以母模板为准」覆盖，界面里可勾选额外保留。

同步策略（用户选定）：
  整页以母模板为准重建 → 回填子页原有的 <title> 与 iframe src → 其余
  （菜单项增删、CSS 增删、底部元素增删、任务栏提示文字改动）全部自动跟随母模板。
  原地覆盖，覆盖前生成带时间戳的 .bak；按各文件原编码写回，不改 meta charset。

文件选择：
  通过 PowerShell 子进程调用 .NET OpenFileDialog（与 Windows 资源管理器同一套
  原生 Shell 对话框），单选母模板 / 多选子网页；异常时自动回退 tkinter 原生对话框。
  另有「在资源管理器中显示」按钮，直接起 explorer.exe 子进程。
"""

import os
import re
import sys
import shutil
import difflib
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path

# tkinter 延迟到实例化 GUI 时才真正使用，方便在无界面的环境下导入本模块做逻辑自测
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    tk = None
    ttk = filedialog = messagebox = scrolledtext = None


# --------------------------------------------------------------------------
# 一、编码：保持 gb2312 / gbk 原样，绝不把全站中文搞成乱码
# --------------------------------------------------------------------------

CHARSET_RE = re.compile(rb'<meta[^>]*charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)


def declared_charset(raw: bytes):
    m = CHARSET_RE.search(raw[:4096])
    return m.group(1).decode('ascii', 'ignore').lower() if m else None


def choose_codec(raw: bytes, declared):
    """挑一个能无损往返的 codec；gb2312 统一按 gbk 处理（字节向下兼容）。"""
    if declared in ('utf-8', 'utf8'):
        return 'utf-8'
    if declared in ('gb2312', 'gb-2312', 'gb_2312', 'gbk', 'gb18030'):
        try:
            raw.decode('gbk')
            return 'gbk'
        except UnicodeDecodeError:
            return 'gb18030'
    for cand in ('utf-8-sig', 'gb18030'):
        try:
            raw.decode(cand)
            return cand
        except UnicodeDecodeError:
            continue
    return 'gb18030'


def read_html(path):
    """返回 (文本, codec, meta 声明, 原始字节)。按 bytes 读写，CRLF 不变。"""
    raw = Path(path).read_bytes()
    declared = declared_charset(raw)
    codec = choose_codec(raw, declared)
    return raw.decode(codec), codec, declared, raw


def encode_html(text, codec):
    try:
        return text.encode(codec), codec
    except UnicodeEncodeError:
        return text.encode('gb18030'), 'gb18030'   # 仅极端情况，GUI 里会给出警告


# --------------------------------------------------------------------------
# 二、文本级抽取 / 回填（不碰 DOM）
# --------------------------------------------------------------------------

TITLE_RE = re.compile(r'(<title\b[^>]*>)(.*?)(</title\s*>)', re.I | re.S)
IFRAME_SRC_RE = re.compile(r'(<iframe\b[^>]*?\bsrc\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
BARE_PENGUIN_RE = re.compile(r'<img\b[^>]*class\s*=\s*(["\'])footer-penguin\1[^>]*>', re.I | re.S)
PENGUIN_ANCHOR_RE = re.compile(
    r'(<a\b[^>]*>\s*<img\b[^>]*class\s*=\s*(["\'])footer-penguin\2[^>]*>\s*</a>)',
    re.I | re.S,
)


def get_title(text):
    m = TITLE_RE.search(text)
    return m.group(2) if m else None


def set_title(text, inner):
    if inner is None or not TITLE_RE.search(text):
        return text, False
    return TITLE_RE.sub(lambda m: m.group(1) + inner + m.group(3), text, count=1), True


def get_iframe_src(text):
    m = IFRAME_SRC_RE.search(text)
    return m.group(3) if m else None


def set_iframe_src(text, src):
    if src is None or not IFRAME_SRC_RE.search(text):
        return text, False
    return IFRAME_SRC_RE.sub(
        lambda m: m.group(1) + m.group(2) + src + m.group(2), text, count=1
    ), True


def get_penguin_anchor(text):
    m = PENGUIN_ANCHOR_RE.search(text)
    return m.group(1) if m else None


def diff_stat(mother_text, child_text):
    """统计母模板 / 子页的独有行，并返回几行子页独有内容做覆盖预警。"""
    a, b = mother_text.splitlines(), child_text.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    mother_only = child_only = 0
    samples = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('delete', 'replace'):
            mother_only += i2 - i1
        if tag in ('insert', 'replace'):
            child_only += j2 - j1
            for line in b[j1:j2]:
                s = line.strip()
                if s and len(samples) < 5:
                    samples.append(s[:110])
    return mother_only, child_only, samples


def build_child(mother_text, child_text, keep_penguin_anchor=False):
    """
    整页以母模板为准，仅回填子页的 <title> 与 iframe src。
    返回 (新文本, 报告字典)。
    """
    m_title, m_src = get_title(mother_text), get_iframe_src(mother_text)
    c_title, c_src = get_title(child_text), get_iframe_src(child_text)

    out, title_done = set_title(mother_text, c_title if c_title is not None else m_title)
    out, src_done = set_iframe_src(out, c_src if c_src is not None else m_src)

    anchor = get_penguin_anchor(child_text) if keep_penguin_anchor else None
    anchor_done = False
    if anchor and BARE_PENGUIN_RE.search(out):
        out = BARE_PENGUIN_RE.sub(lambda m: anchor, out, count=1)
        anchor_done = True

    mother_only, child_only, samples = diff_stat(mother_text, child_text)
    return out, {
        'title': c_title if c_title is not None else m_title,
        'title_done': title_done,
        'iframe_src': c_src if c_src is not None else m_src,
        'iframe_done': src_done,
        'penguin_anchor': anchor_done,
        'mother_only': mother_only,
        'child_only': child_only,
        'samples': samples,
        'warnings': ([] if c_title is not None else ['子页无 <title>，沿用母模板标题']) +
                    ([] if c_src is not None else ['子页无 iframe，沿用母模板 src（请人工核对！）']),
    }


def backup_path(path, stamp):
    p = Path(str(path) + '.' + stamp + '.bak')
    i = 1
    while p.exists():
        p = Path(str(path) + '.' + stamp + '_' + str(i) + '.bak')
        i += 1
    return p


# --------------------------------------------------------------------------
# 三、GUI
# --------------------------------------------------------------------------

class TemplateSyncApp:
    APP_TITLE = 'PenguinThinking · 母模板同步工具'

    def __init__(self):
        if tk is None:
            raise RuntimeError('未安装 tkinter，请使用 Windows 官方 Python 安装包（含 tkinter）。')
        self.root = tk.Tk()
        self.root.title(self.APP_TITLE)
        self.root.geometry('900x720')
        self.root.minsize(760, 600)
        try:
            self.root.iconbitmap(default='')  # 无 ico 时静默跳过
        except Exception:
            pass

        self.mother_var = tk.StringVar()
        self.dryrun_var = tk.BooleanVar(value=False)
        self.penguin_var = tk.BooleanVar(value=False)
        self.ps_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value='就绪')

        self.children = []
        self._build_ui()

    # ---------- 界面 ----------
    def _build_ui(self):
        root = self.root
        font = ('Microsoft YaHei UI', 9)
        mono = ('Consolas', 9)

        top = ttk.Frame(root, padding=(12, 10, 12, 0))
        top.pack(fill='x')

        # 母模板
        f1 = ttk.LabelFrame(top, text='① 母模板（总模板，唯一基准）', padding=8)
        f1.pack(fill='x', pady=(0, 8))
        ttk.Entry(f1, textvariable=self.mother_var, font=font).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        ttk.Button(f1, text='浏览…', command=self.pick_mother).pack(side='left', padx=(0, 6))
        ttk.Button(f1, text='在资源管理器中显示', command=lambda: self.reveal(self.mother_var.get())).pack(side='left')

        # 子网页
        f2 = ttk.LabelFrame(top, text='② 子网页（可多选，同步时逐个重建）', padding=8)
        f2.pack(fill='both', expand=True, pady=(0, 8))
        left = ttk.Frame(f2)
        left.pack(side='left', fill='both', expand=True)
        self.lst = tk.Listbox(left, selectmode=tk.EXTENDED, font=mono, height=7,
                              activestyle='dotbox', bg='#ffffff')
        sb = ttk.Scrollbar(left, orient='vertical', command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        self.lst.bind('<Delete>', lambda e: self.remove_selected())

        right = ttk.Frame(f2, padding=(10, 0, 0, 0))
        right.pack(side='left', fill='y')
        ttk.Button(right, text='添加网页…', command=self.pick_children, width=16).pack(pady=(0, 6))
        ttk.Button(right, text='移除选中', command=self.remove_selected, width=16).pack(pady=(0, 6))
        ttk.Button(right, text='清空列表', command=self.clear_children, width=16).pack(pady=(0, 6))
        ttk.Button(right, text='在资源管理器中显示', command=self.reveal_selected, width=16).pack(pady=(0, 6))

        # 选项
        f3 = ttk.LabelFrame(top, text='③ 同步选项', padding=8)
        f3.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(f3, text='额外保留：子页底部企鹅的 <a href="home-container.html"> 外链包裹',
                        variable=self.penguin_var).pack(anchor='w')
        ttk.Checkbutton(f3, text='仅预览，不写回文件（演练模式）',
                        variable=self.dryrun_var).pack(anchor='w', pady=(2, 0))
        ttk.Checkbutton(f3, text='用 PowerShell 子进程调原生资源管理器对话框（失败自动回退）',
                        variable=self.ps_var).pack(anchor='w', pady=(2, 0))
        ttk.Label(f3, text='编码：自动识别并保持各文件原有 gb2312/gbk 声明，覆盖前自动生成 .bak',
                  foreground='#666666', font=font).pack(anchor='w', pady=(4, 0))

        # 按钮
        f4 = ttk.Frame(top)
        f4.pack(fill='x', pady=(0, 8))
        ttk.Button(f4, text='对比预览', command=self.preview, width=14).pack(side='left')
        ttk.Button(f4, text='开始同步', command=self.sync, width=14).pack(side='left', padx=(8, 0))
        ttk.Label(f4, textvariable=self.status_var, foreground='#0a6ab5', font=font).pack(side='left', padx=(14, 0))

        # 日志
        f5 = ttk.LabelFrame(root, text='差异 / 执行日志', padding=(8, 6))
        f5.pack(fill='both', expand=True, padx=12, pady=(4, 6))
        self.log = scrolledtext.ScrolledText(f5, font=mono, wrap='word', height=16,
                                             bg='#fbfbfb', fg='#222222')
        self.log.pack(fill='both', expand=True)

        ttk.Label(root, textvariable=self.status_var, anchor='w', padding=(12, 2),
                  font=font).pack(fill='x', side='bottom')

        self.log_line('提示：先选母模板，再一次性多选所有 *-container.html；建议先「对比预览」再「开始同步」。')
        self.log_line('同步方式＝整页以母模板重建，仅回填每页自己的 <title> 与 iframe src。')

    # ---------- 日志 ----------
    def log_line(self, s=''):
        self.log.insert('end', s + '\n')
        self.log.see('end')
        self.root.update_idletasks()

    def set_status(self, s):
        self.status_var.set(s)
        self.root.update_idletasks()

    # ---------- 原生文件对话框（PowerShell 子进程） ----------
    @staticmethod
    def _ps_quote(s):
        return "'" + str(s).replace("'", "''") + "'"

    def _pick_via_powershell(self, multiple, title, init_dir):
        """起 powershell -STA 子进程，用 .NET OpenFileDialog 选文件，结果经临时文件回传。"""
        if sys.platform != 'win32':
            raise RuntimeError('非 Windows 环境')
        fd, out_path = tempfile.mkstemp(prefix='mb_pick_', suffix='.txt')
        os.close(fd)
        fd2, ps1 = tempfile.mkstemp(prefix='mb_pick_', suffix='.ps1')
        os.close(fd2)
        script = f"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = {self._ps_quote(title)}
$d.Filter = 'HTML 网页 (*.html;*.htm)|*.html;*.htm|所有文件 (*.*)|*.*'
$d.Multiselect = ${'true' if multiple else 'false'}
$d.CheckFileExists = $true
$d.InitialDirectory = {self._ps_quote(init_dir)}
[void]$d.ShowDialog()
[System.IO.File]::WriteAllLines({self._ps_quote(out_path)}, [string[]]$d.FileNames, (New-Object System.Text.UTF8Encoding($false)))
"""
        # UTF-8 BOM：Windows PowerShell 5.1 靠它正确识别中文脚本
        Path(ps1).write_bytes(b'\xef\xbb\xbf' + script.encode('utf-8'))
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            r = subprocess.run(
                ['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', ps1],
                timeout=300, creationflags=flags,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if r.returncode != 0:
                raise RuntimeError((r.stderr or b'').decode('utf-8', 'ignore')[:300])
            lines = Path(out_path).read_text(encoding='utf-8').splitlines()
            return [x.strip() for x in lines if x.strip()]
        finally:
            for p in (out_path, ps1):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _pick_via_tk(self, multiple, title, init_dir):
        kw = dict(title=title, initialdir=init_dir or None,
                  filetypes=[('HTML 网页', ('*.html', '*.htm')), ('所有文件', '*.*')])
        if multiple:
            return list(filedialog.askopenfilenames(**kw))
        p = filedialog.askopenfilename(**kw)
        return [p] if p else []

    def _pick(self, multiple, title):
        base = self.mother_var.get().strip()
        init_dir = str(Path(base).parent) if base and Path(base).is_file() else str(Path.cwd())
        if self.ps_var.get():
            try:
                return self._pick_via_powershell(multiple, title, init_dir)
            except Exception as e:
                self.log_line(f'[回退] 原生对话框子进程不可用（{type(e).__name__}），改用 tkinter 原生对话框。')
        return self._pick_via_tk(multiple, title, init_dir)

    # ---------- 选择动作 ----------
    def pick_mother(self):
        got = self._pick(False, '选择母模板（总模板 HTML）')
        if got:
            self.mother_var.set(got[0])
            self.log_line('母模板：' + got[0])

    def pick_children(self):
        got = self._pick(True, '选择一个或多个子网页（可 Ctrl / Shift 多选）')
        added = 0
        for p in got:
            if p not in self.children:
                self.children.append(p)
                self.lst.insert('end', p)
                added += 1
        if added:
            self.log_line(f'已加入 {added} 个子网页。')
        self.set_status(f'子网页 {len(self.children)} 个')

    def remove_selected(self):
        for i in reversed(self.lst.curselection()):
            self.lst.delete(i)
            del self.children[i]
        self.set_status(f'子网页 {len(self.children)} 个')

    def clear_children(self):
        self.lst.delete(0, 'end')
        self.children.clear()
        self.set_status('子网页 0 个')

    # ---------- 资源管理器子进程 ----------
    @staticmethod
    def reveal(path):
        if not path or not Path(path).exists():
            return
        if sys.platform == 'win32':
            subprocess.Popen(['explorer.exe', '/select,', os.path.normpath(path)])
        else:
            subprocess.Popen(['xdg-open', str(Path(path).parent)])

    def reveal_selected(self):
        sel = self.lst.curselection()
        if sel:
            self.reveal(self.children[sel[0]])
        else:
            self.reveal(self.mother_var.get())

    # ---------- 校验 + 计算 ----------
    def _validate(self):
        mother = self.mother_var.get().strip().strip('"')
        self.mother_var.set(mother)
        if not mother or not Path(mother).is_file():
            messagebox.showerror('缺少母模板', '请先选择一个有效的母模板 HTML 文件。')
            return None
        kids = [p for p in self.children if Path(p).is_file()]
        m_abs = os.path.normcase(os.path.abspath(mother))
        kids = [p for p in kids if os.path.normcase(os.path.abspath(p)) != m_abs]
        if not kids:
            messagebox.showerror('缺少子网页', '请至少添加一个子网页（母模板自身会被自动跳过）。')
            return None
        return mother, kids

    def _compute(self):
        v = self._validate()
        if not v:
            return None
        mother, kids = v
        try:
            m_text, m_codec, m_decl, _ = read_html(mother)
        except Exception as e:
            messagebox.showerror('读取失败', f'母模板读取失败：{e}')
            return None
        if get_iframe_src(m_text) is None:
            self.log_line('[警告] 母模板里没找到 <iframe src=...>，同步后子页 iframe 将保持原样。')
        return mother, kids, m_text, m_codec, m_decl

    # ---------- 预览 ----------
    def preview(self):
        c = self._compute()
        if not c:
            return
        mother, kids, m_text, m_codec, m_decl = c
        self.log_line('')
        self.log_line('=' * 78)
        self.log_line(f'对比预览  母模板：{Path(mother).name}  '
                      f'编码={m_codec}(声明 {m_decl or "无"})  子网页 {len(kids)} 个')
        self.log_line('=' * 78)
        for p in kids:
            try:
                c_text, c_codec, c_decl, _ = read_html(p)
                new_text, rep = build_child(m_text, c_text, self.penguin_var.get())
            except Exception as e:
                self.log_line(f'  [跳过] {Path(p).name} → 读取/解析失败：{e}')
                continue
            self.log_line(f'  ▸ {Path(p).name}  (编码 {c_codec})')
            self.log_line(f'      保留 title      : {rep["title"]!r}')
            self.log_line(f'      保留 iframe src : {rep["iframe_src"]!r}')
            self.log_line(f'      母模板新增行将补入 {rep["mother_only"]} 行；'
                          f'子页独有个性化内容将被覆盖 {rep["child_only"]} 行')
            if rep['penguin_anchor']:
                self.log_line('      已额外保留底部企鹅 <a> 外链包裹')
            for w in rep['warnings']:
                self.log_line('      [注意] ' + w)
            for s in rep['samples']:
                self.log_line('      将被覆盖示例: ' + s)
            if c_codec != m_codec:
                self.log_line(f'      [注意] 编码与母模板不一致（{c_codec} ≠ {m_codec}），'
                              f'写回时会按母模板编码并对齐 meta charset')
        self.log_line('=' * 78)
        self.log_line('预览结束。确认无误后点「开始同步」；不放心可先勾「仅预览，不写回文件」。')
        self.set_status('预览完成')

    # ---------- 同步 ----------
    def sync(self):
        c = self._compute()
        if not c:
            return
        mother, kids, m_text, m_codec, m_decl = c
        dry = self.dryrun_var.get()
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if not dry:
            ok = messagebox.askyesno(
                '确认写回',
                f'将以母模板整页重建 {len(kids)} 个子网页并原地覆盖。\n\n'
                '· 只保留每页的 <title> 与 iframe src\n'
                '· 菜单 / CSS / 底部元素的增删全部跟随母模板\n'
                '· 覆盖前自动生成 .bak 备份\n\n继续吗？',
            )
            if not ok:
                self.set_status('已取消')
                return

        self.log_line('')
        self.log_line('=' * 78)
        self.log_line(f'{"[演练] " if dry else ""}开始同步  {datetime.now():%Y-%m-%d %H:%M:%S}')
        self.log_line('=' * 78)
        done = skipped = 0
        for p in kids:
            name = Path(p).name
            try:
                c_text, c_codec, c_decl, _ = read_html(p)
                new_text, rep = build_child(m_text, c_text, self.penguin_var.get())

                # 编码不一致时以母模板为准，避免 meta 声明与实际字节错位
                use_codec = c_codec
                if c_codec != m_codec:
                    use_codec = m_codec
                    new_text = _align_charset(new_text, m_decl)
                    self.log_line(f'  [编码] {name} 原为 {c_codec}，已按母模板 {m_codec} 写回并同步 meta charset')

                payload, real_codec = encode_html(new_text, use_codec)
                if real_codec != use_codec:
                    self.log_line(f'  [编码] {name} 含 {use_codec} 无法表示的字符，已回退 {real_codec}')

                if dry:
                    self.log_line(f'  [演练] {name} → 待写 {len(payload)} 字节  '
                                  f'(title={rep["title"]!r}, src={rep["iframe_src"]!r})')
                    done += 1
                    continue

                bak = backup_path(p, stamp)
                shutil.copy2(p, bak)          # 字节级备份，最可靠
                Path(p).write_bytes(payload)
                self.log_line(f'  [完成] {name} ← 母模板  备份 {bak.name}  '
                              f'title={rep["title"]!r}, src={rep["iframe_src"]!r}')
                done += 1
                for w in rep['warnings']:
                    self.log_line('          [注意] ' + w)
            except Exception as e:
                skipped += 1
                self.log_line(f'  [失败] {name} → {type(e).__name__}: {e}')

        self.log_line('-' * 78)
        self.log_line(f'结果：成功 {done} 个，跳过/失败 {skipped} 个'
                      + ('（演练模式，未改动任何文件）' if dry else ''))
        self.set_status(f'同步完成 {done}/{len(kids)}')
        messagebox.showinfo('同步完成',
                            f'成功 {done} 个，失败 {skipped} 个。'
                            + ('\n演练模式，文件未改动。' if dry else '\n原文件已备份为 .bak。'))


def _align_charset(text, declared):
    """把 meta 里的 charset 声明改成母模板的声明值。"""
    if not declared:
        return text
    pat = re.compile(r'(<meta[^>]*charset\s*=\s*["\']?)([A-Za-z0-9_\-]+)', re.I)
    return pat.sub(lambda m: m.group(1) + declared, text, count=1)


def enable_dpi_awareness():
    if sys.platform != 'win32':
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    enable_dpi_awareness()
    app = TemplateSyncApp()
    app.root.mainloop()


if __name__ == '__main__':
    main()
