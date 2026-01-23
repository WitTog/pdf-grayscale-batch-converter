import fitz  # PyMuPDF
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from concurrent.futures import ThreadPoolExecutor, Future
import threading
import time
import re
import gc
from datetime import datetime
import psutil
from queue import Queue, Empty
from typing import Dict, List, Tuple, Optional, Callable, Any

# ======================== 常量定义 ========================
APP_TITLE = "PDF 批量灰度转换工具（兼容完整版）"
DEFAULT_DPI = 150
DEFAULT_WORKERS = min(4, max(1, os.cpu_count() or 4))
DEFAULT_PREFIX = "gray_"
MIN_DPI = 72
MAX_DPI = 600
MIN_WORKERS = 1
MAX_WORKERS = 16
ILLEGAL_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

# 性能常量
BATCH_SIZE = 20
LOG_UPDATE_INTERVAL = 50  # ms
GC_INTERVAL_PAGES = 8

# ======================== 数据结构 ========================
class FileInfo:
    """文件信息结构"""
    def __init__(self, input_path: str, output_path: str):
        self.input_path = input_path
        self.output_path = output_path
        self.file_size = os.path.getsize(input_path) if os.path.exists(input_path) else 0
        self.status = "pending"  # pending/success/failed/skipped
        self.error_message = ""
        self.processing_time = 0.0

class ConversionStats:
    """转换统计数据"""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = 0.0
        self.current_file = ""
    
    def reset(self):
        """重置统计"""
        self.__init__()
        self.start_time = time.time()
    
    def progress(self) -> float:
        """计算进度百分比"""
        if self.total == 0:
            return 0.0
        return ((self.success + self.failed + self.skipped) / self.total) * 100

# ======================== 内存管理器（简化版） ========================
class SimpleMemoryManager:
    """简易内存管理器"""
    def __init__(self):
        self.last_gc_time = 0
    
    def optimize(self):
        """智能垃圾回收"""
        current_time = time.time()
        if current_time - self.last_gc_time > 2.0:
            gc.collect()
            self.last_gc_time = current_time
    
    def get_memory_info(self) -> str:
        """获取内存信息"""
        try:
            mem = psutil.virtual_memory()
            used_mb = mem.used // 1024 // 1024
            total_mb = mem.total // 1024 // 1024
            return f"内存: {mem.percent}% ({used_mb}MB/{total_mb}MB)"
        except:
            return "内存: N/A"

# ======================== 核心转换引擎（兼容版） ========================
class PDFConversionEngine:
    """PDF转换引擎（极致兼容：无save参数）"""
    def __init__(self, log_callback: Optional[Callable] = None):
        self.log_callback = log_callback
        self.cancel_flag = threading.Event()
        self.memory_manager = SimpleMemoryManager()
    
    def _log(self, message: str, level: str = "INFO"):
        """安全日志"""
        if self.log_callback:
            try:
                self.log_callback(message, level)
            except:
                pass
    
    def convert_file(self, file_info: FileInfo, dpi: int, overwrite: bool = False) -> Tuple[bool, str]:
        """
        转换单个文件（极致兼容版）
        """
        start_time = time.time()
        
        # 检查取消
        if self.cancel_flag.is_set():
            return False, "已取消"
        
        # 检查输出文件
        if os.path.exists(file_info.output_path) and not overwrite:
            return False, "跳过（文件已存在）"
        
        try:
            # 内存优化
            self.memory_manager.optimize()
            
            # 打开PDF（兼容加密/损坏文件）
            try:
                doc = fitz.open(file_info.input_path)
            except fitz.FileDataError:
                return False, "文件损坏或不是有效的PDF"
            except fitz.PermissionError:
                return False, "文件已加密或无访问权限"
            except Exception as e:
                return False, f"打开失败: {str(e)[:30]}"
            
            try:
                # 计算缩放矩阵
                zoom = dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                page_count = len(doc)
                
                # 分批处理页面（优化内存）
                for start in range(0, page_count, GC_INTERVAL_PAGES):
                    if self.cancel_flag.is_set():
                        raise KeyboardInterrupt()
                    
                    end = min(start + GC_INTERVAL_PAGES, page_count)
                    for page_num in range(start, end):
                        page = doc[page_num]
                        
                        # 生成灰度像素图（兼容所有版本）
                        try:
                            # 旧版本兼容（带dpi参数）
                            pix = page.get_pixmap(
                                matrix=mat,
                                colorspace=fitz.csGRAY,
                                alpha=False,
                                dpi=dpi
                            )
                        except TypeError:
                            # 新版本（无dpi参数）
                            pix = page.get_pixmap(
                                matrix=mat,
                                colorspace=fitz.csGRAY,
                                alpha=False
                            )
                        
                        # 替换页面内容（兼容set_pixmap/insert_image）
                        try:
                            # 新版本：insert_image
                            page.insert_image(page.rect, pixmap=pix)
                            page.clean_contents()
                        except AttributeError:
                            # 旧版本：set_pixmap
                            page.set_pixmap(pix)
                        
                        # 立即释放
                        pix = None
                    
                    # 批次GC
                    gc.collect()
                
                # ========== 核心兼容点：无任何关键字参数 ==========
                doc.save(file_info.output_path)
                doc.close()
                
                # 计算耗时
                file_info.processing_time = time.time() - start_time
                file_info.status = "success"
                
                return True, f"转换成功（{file_info.processing_time:.1f}s）"
                
            except KeyboardInterrupt:
                doc.close()
                return False, "已取消"
            except Exception as e:
                doc.close()
                error_msg = f"转换失败: {type(e).__name__} - {str(e)[:30]}"
                file_info.error_message = error_msg
                file_info.status = "failed"
                return False, error_msg
                
        except Exception as e:
            error_msg = f"预处理失败: {str(e)[:30]}"
            file_info.error_message = error_msg
            file_info.status = "failed"
            return False, error_msg
    
    def start(self):
        """启动引擎"""
        self.cancel_flag.clear()
    
    def stop(self):
        """停止引擎"""
        self.cancel_flag.set()

# ======================== 主应用类（还原完整设计） ========================
class PDFGrayConverter:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x800")
        self.root.minsize(800, 600)
        
        # 核心组件
        self.engine = PDFConversionEngine(log_callback=self._engine_log)
        self.stats = ConversionStats()
        self.file_queue: List[FileInfo] = []
        self.selected_single_files: List[str] = []  # 手动选择的文件
        self.task_executor: Optional[ThreadPoolExecutor] = None
        
        # UI变量
        self.input_folder = tk.StringVar(value="")
        self.output_folder = tk.StringVar(value="")
        self.dpi = tk.IntVar(value=DEFAULT_DPI)
        self.max_workers = tk.IntVar(value=DEFAULT_WORKERS)
        self.output_prefix = tk.StringVar(value=DEFAULT_PREFIX)
        self.overwrite = tk.BooleanVar(value=False)
        
        # 队列（UI更新）
        self.log_queue = Queue(maxsize=500)
        self.progress_queue = Queue(maxsize=100)
        
        # 创建UI（还原完整设计）
        self._create_ui()
        
        # 启动UI更新线程
        self._start_ui_updater()
    
    def _create_ui(self):
        """创建完整UI布局（还原较早版本设计）"""
        # 主框架
        main_frame = ttk.Frame(self.root, padding="8")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 1. 顶部控制面板
        self._create_top_panel(main_frame)
        
        # 2. 中心区域（参数+进度+日志）
        center_frame = ttk.Frame(main_frame)
        center_frame.pack(fill=tk.BOTH, expand=True)
        
        # 左侧：参数+进度面板
        left_frame = ttk.Frame(center_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        
        self._create_params_panel(left_frame)
        self._create_progress_panel(left_frame)
        
        # 右侧：日志面板
        self._create_log_panel(center_frame)
        
        # 3. 底部状态栏
        self._create_status_bar(main_frame)
    
    def _create_top_panel(self, parent):
        """顶部控制面板（还原文件夹+文件选择）"""
        top_frame = ttk.Frame(parent)
        top_frame.pack(fill=tk.X, pady=(0, 8))
        
        # 操作按钮
        btn_frame = ttk.Frame(top_frame)
        btn_frame.pack(side=tk.LEFT, fill=tk.Y)
        
        self.start_btn = ttk.Button(btn_frame, text="🚀 开始转换", command=self.start_conversion, width=10)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        
        self.stop_btn = ttk.Button(btn_frame, text="⏹️ 停止", command=self.stop_conversion, state=tk.DISABLED, width=8)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        
        self.file_btn = ttk.Button(btn_frame, text="📄 选择文件", command=self.browse_single_file, width=8)
        self.file_btn.pack(side=tk.LEFT, padx=2)
        
        # 文件夹选择
        folder_frame = ttk.Frame(top_frame)
        folder_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        
        # 输入文件夹
        input_row = ttk.Frame(folder_frame)
        input_row.pack(fill=tk.X, pady=2)
        ttk.Label(input_row, text="输入文件夹:", width=10).pack(side=tk.LEFT)
        ttk.Entry(input_row, textvariable=self.input_folder).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(input_row, text="浏览", command=self.browse_input, width=6).pack(side=tk.LEFT)
        
        # 输出文件夹
        output_row = ttk.Frame(folder_frame)
        output_row.pack(fill=tk.X, pady=2)
        ttk.Label(output_row, text="输出文件夹:", width=10).pack(side=tk.LEFT)
        ttk.Entry(output_row, textvariable=self.output_folder).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(output_row, text="浏览", command=self.browse_output, width=6).pack(side=tk.LEFT)
    
    def _create_params_panel(self, parent):
        """参数面板（还原DPI/线程/前缀等配置）"""
        param_frame = ttk.LabelFrame(parent, text="转换参数", padding="10")
        param_frame.pack(fill=tk.X, pady=(0, 8))
        
        # DPI设置
        dpi_row = ttk.Frame(param_frame)
        dpi_row.pack(fill=tk.X, pady=3)
        ttk.Label(dpi_row, text="分辨率(DPI):").pack(side=tk.LEFT)
        dpi_combo = ttk.Combobox(dpi_row, textvariable=self.dpi, values=[72, 100, 150, 200, 300], width=8, state="readonly")
        dpi_combo.pack(side=tk.LEFT, padx=5)
        dpi_combo.current(2)
        
        # 线程数
        thread_row = ttk.Frame(param_frame)
        thread_row.pack(fill=tk.X, pady=3)
        ttk.Label(thread_row, text="线程数:").pack(side=tk.LEFT)
        ttk.Spinbox(thread_row, from_=1, to=16, textvariable=self.max_workers, width=6).pack(side=tk.LEFT, padx=5)
        
        # 输出前缀
        prefix_row = ttk.Frame(param_frame)
        prefix_row.pack(fill=tk.X, pady=3)
        ttk.Label(prefix_row, text="输出前缀:").pack(side=tk.LEFT)
        ttk.Entry(prefix_row, textvariable=self.output_prefix, width=10).pack(side=tk.LEFT, padx=5)
        
        # 覆盖选项
        opt_row = ttk.Frame(param_frame)
        opt_row.pack(fill=tk.X, pady=3)
        ttk.Checkbutton(opt_row, text="覆盖已存在文件", variable=self.overwrite).pack(side=tk.LEFT)
    
    def _create_progress_panel(self, parent):
        """进度统计面板（还原进度条+统计信息）"""
        progress_frame = ttk.LabelFrame(parent, text="转换进度", padding="10")
        progress_frame.pack(fill=tk.X, pady=(0, 8))
        
        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X)
        
        # 进度标签
        self.progress_label = ttk.Label(progress_frame, text="就绪")
        self.progress_label.pack()
        
        # 统计信息
        stats_frame = ttk.LabelFrame(parent, text="统计信息", padding="10")
        stats_frame.pack(fill=tk.X)
        
        # 统计标签
        self.stats_labels = {
            "total": ttk.Label(stats_frame, text="总数: 0"),
            "success": ttk.Label(stats_frame, text="成功: 0"),
            "failed": ttk.Label(stats_frame, text="失败: 0"),
            "skipped": ttk.Label(stats_frame, text="跳过: 0")
        }
        
        row = 0
        col = 0
        for key, label in self.stats_labels.items():
            label.grid(row=row, column=col, padx=5, pady=2, sticky="w")
            col += 1
            if col >= 2:
                col = 0
                row += 1
    
    def _create_log_panel(self, parent):
        """日志面板（还原滚动日志+颜色）"""
        log_frame = ttk.LabelFrame(parent, text="处理日志", padding="10")
        log_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Consolas", 9),
            bg="#1e1e1e", fg="#ffffff"
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 日志标签样式
        self.log_text.tag_config("INFO", foreground="#ffffff")
        self.log_text.tag_config("SUCCESS", foreground="#4CAF50")
        self.log_text.tag_config("WARNING", foreground="#FFC107")
        self.log_text.tag_config("ERROR", foreground="#F44336")
        self.log_text.config(state=tk.DISABLED)
        
        # 日志操作按钮
        btn_frame = ttk.Frame(log_frame)
        btn_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(btn_frame, text="清空日志", command=self.clear_log).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="复制日志", command=self.copy_log).pack(side=tk.LEFT, padx=5)
    
    def _create_status_bar(self, parent):
        """底部状态栏（还原内存+状态显示）"""
        status_frame = ttk.Frame(parent, relief=tk.SUNKEN, borderwidth=1)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))
        
        self.status_label = ttk.Label(status_frame, text="就绪", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        self.memory_label = ttk.Label(status_frame, text="内存: N/A", width=25)
        self.memory_label.pack(side=tk.RIGHT, padx=5)
    
    # ======================== UI操作方法 ========================
    def browse_single_file(self):
        """选择单个/多个文件（还原功能）"""
        files = filedialog.askopenfilenames(
            title="选择PDF文件",
            filetypes=[("PDF文件", "*.pdf *.PDF"), ("所有文件", "*.*")],
            initialdir=self.input_folder.get() or os.path.expanduser("~")
        )
        if files:
            self.selected_single_files = list(files)
            # 自动设置输入/输出文件夹
            first_file = files[0]
            self.input_folder.set(os.path.dirname(first_file))
            if not self.output_folder.get():
                output_dir = os.path.join(os.path.dirname(first_file), "PDF灰度输出")
                self.output_folder.set(output_dir)
            self._engine_log(f"已选择 {len(files)} 个PDF文件", "INFO")
    
    def browse_input(self):
        """浏览输入文件夹"""
        folder = filedialog.askdirectory(title="选择输入文件夹", initialdir=self.input_folder.get())
        if folder:
            self.input_folder.set(folder)
            if not self.output_folder.get():
                self.output_folder.set(os.path.join(folder, "PDF灰度输出"))
    
    def browse_output(self):
        """浏览输出文件夹"""
        folder = filedialog.askdirectory(title="选择输出文件夹", initialdir=self.output_folder.get())
        if folder:
            self.output_folder.set(folder)
    
    def clear_log(self):
        """清空日志"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.progress_var.set(0)
        self.progress_label.config(text="就绪")
    
    def copy_log(self):
        """复制日志"""
        try:
            content = self.log_text.get(1.0, tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            messagebox.showinfo("成功", "日志已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("错误", f"复制失败: {str(e)}")
    
    # ======================== 转换核心逻辑 ========================
    def start_conversion(self):
        """开始转换（还原批量处理逻辑）"""
        # 验证输入
        if not self._validate_input():
            return
        
        # 重置状态
        self.stats.reset()
        self.file_queue.clear()
        self.clear_log()
        
        # 更新按钮状态
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        
        # 扫描文件
        self._engine_log("开始扫描文件...", "INFO")
        threading.Thread(target=self._scan_files, daemon=True).start()
    
    def _validate_input(self) -> bool:
        """验证输入（还原权限检查）"""
        errors = []
        
        # 检查输出文件夹
        if not self.output_folder.get():
            errors.append("请选择输出文件夹")
        else:
            # 检查写入权限
            try:
                os.makedirs(self.output_folder.get(), exist_ok=True)
                test_file = os.path.join(self.output_folder.get(), "test.tmp")
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
            except:
                errors.append("输出文件夹无写入权限，请选择其他文件夹（推荐桌面）")
        
        # 检查参数
        if not (MIN_DPI <= self.dpi.get() <= MAX_DPI):
            errors.append(f"DPI必须在 {MIN_DPI}-{MAX_DPI} 之间")
        
        if not (MIN_WORKERS <= self.max_workers.get() <= MAX_WORKERS):
            errors.append(f"线程数必须在 {MIN_WORKERS}-{MAX_WORKERS} 之间")
        
        # 检查文件
        if not self.selected_single_files and not self.input_folder.get():
            errors.append("请选择输入文件夹或直接选择PDF文件")
        
        if errors:
            messagebox.showerror("输入错误", "\n".join(errors))
            return False
        
        return True
    
    def _scan_files(self):
        """扫描文件（还原文件夹+单个文件逻辑）"""
        try:
            # 优先处理手动选择的文件
            if self.selected_single_files:
                for input_path in self.selected_single_files:
                    self._add_file_to_queue(input_path)
            else:
                # 扫描文件夹
                input_dir = self.input_folder.get()
                for root, _, files in os.walk(input_dir):
                    for file in files:
                        if file.lower().endswith(".pdf") and not file.startswith("~$"):
                            input_path = os.path.join(root, file)
                            self._add_file_to_queue(input_path)
            
            # 更新总数
            self.stats.total = len(self.file_queue)
            self.progress_queue.put(("total", self.stats.total))
            
            if self.stats.total == 0:
                self._engine_log("未找到PDF文件", "WARNING")
                self._finish_conversion()
                return
            
            self._engine_log(f"共找到 {self.stats.total} 个PDF文件，开始转换...", "INFO")
            
            # 启动转换线程
            threading.Thread(target=self._process_files, daemon=True).start()
            
        except Exception as e:
            self._engine_log(f"扫描文件失败: {str(e)}", "ERROR")
            self._finish_conversion()
    
    def _add_file_to_queue(self, input_path: str):
        """添加文件到队列"""
        try:
            # 生成输出路径
            filename = os.path.basename(input_path)
            output_name = f"{self.output_prefix.get()}{filename}"
            output_name = re.sub(ILLEGAL_CHARS, "_", output_name)
            
            # 保持目录结构
            rel_path = os.path.relpath(os.path.dirname(input_path), self.input_folder.get())
            if rel_path == ".":
                output_path = os.path.join(self.output_folder.get(), output_name)
            else:
                output_dir = os.path.join(self.output_folder.get(), rel_path)
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, output_name)
            
            # 添加到队列
            self.file_queue.append(FileInfo(input_path, output_path))
        
        except Exception as e:
            self._engine_log(f"跳过文件 {input_path}: {str(e)}", "WARNING")
    
    def _process_files(self):
        """处理文件（还原多线程批量处理）"""
        self.engine.start()
        self.task_executor = ThreadPoolExecutor(max_workers=self.max_workers.get(), thread_name_prefix="PDFWorker")
        
        futures = []
        for file_info in self.file_queue:
            if self.engine.cancel_flag.is_set():
                break
            
            # 提交任务
            future = self.task_executor.submit(
                self.engine.convert_file,
                file_info,
                self.dpi.get(),
                self.overwrite.get()
            )
            future.file_info = file_info
            futures.append(future)
        
        # 处理结果
        for future in futures:
            if self.engine.cancel_flag.is_set():
                break
            
            try:
                success, message = future.result()
                filename = os.path.basename(future.file_info.input_path)
                
                # 更新统计
                if "跳过" in message:
                    self.stats.skipped += 1
                    self._engine_log(f"⏭️ {filename}: {message}", "INFO")
                elif success:
                    self.stats.success += 1
                    self._engine_log(f"✅ {filename}: {message}", "SUCCESS")
                else:
                    self.stats.failed += 1
                    self._engine_log(f"❌ {filename}: {message}", "ERROR")
                
                # 更新进度
                self.progress_queue.put(("progress", self.stats.progress()))
                
            except Exception as e:
                self.stats.failed += 1
                self._engine_log(f"❌ 处理失败: {str(e)}", "ERROR")
        
        # 完成转换
        self._finish_conversion()
    
    def stop_conversion(self):
        """停止转换"""
        self.engine.stop()
        if self.task_executor:
            self.task_executor.shutdown(wait=False)
        self._engine_log("转换已停止", "WARNING")
        self._finish_conversion()
    
    def _finish_conversion(self):
        """完成转换"""
        # 恢复按钮状态
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        
        # 输出统计
        elapsed = time.time() - self.stats.start_time
        self._engine_log("="*60, "INFO")
        self._engine_log(f"转换完成 | 耗时: {elapsed:.1f}秒", "SUCCESS")
        self._engine_log(f"统计: 总数 {self.stats.total} | 成功 {self.stats.success} | 失败 {self.stats.failed} | 跳过 {self.stats.skipped}", "INFO")
        
        # 提示打开输出文件夹
        if self.stats.success > 0:
            self._engine_log(f"输出目录: {self.output_folder.get()}", "INFO")
            if messagebox.askyesno("完成", f"成功转换 {self.stats.success} 个文件，是否打开输出目录？"):
                try:
                    os.startfile(self.output_folder.get())
                except:
                    pass
    
    # ======================== UI更新 ========================
    def _start_ui_updater(self):
        """启动UI更新线程"""
        def updater():
            while True:
                try:
                    # 更新日志
                    while not self.log_queue.empty():
                        level, msg = self.log_queue.get_nowait()
                        self._update_log(msg, level)
                    
                    # 更新进度
                    while not self.progress_queue.empty():
                        type_, value = self.progress_queue.get_nowait()
                        if type_ == "progress":
                            self.progress_var.set(value)
                            self.progress_label.config(text=f"进度: {value:.1f}% ({self.stats.success+self.stats.failed+self.stats.skipped}/{self.stats.total})")
                        elif type_ == "total":
                            self.stats_labels["total"].config(text=f"总数: {value}")
                    
                    # 更新统计
                    self.stats_labels["success"].config(text=f"成功: {self.stats.success}")
                    self.stats_labels["failed"].config(text=f"失败: {self.stats.failed}")
                    self.stats_labels["skipped"].config(text=f"跳过: {self.stats.skipped}")
                    
                    # 更新内存信息
                    self.memory_label.config(text=self.engine.memory_manager.get_memory_info())
                    
                    time.sleep(0.05)
                except:
                    time.sleep(0.1)
        
        threading.Thread(target=updater, daemon=True).start()
    
    def _engine_log(self, message: str, level: str = "INFO"):
        """引擎日志回调"""
        try:
            self.log_queue.put((level, message))
            self.status_label.config(text=message[:60])
        except:
            pass
    
    def _update_log(self, message: str, level: str):
        """更新日志到UI"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] {message}\n"
        
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, log_msg, level)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

# ======================== 主函数 ========================
def main():
    root = tk.Tk()
    # Windows DPI适配
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(2)
            root.tk.call('tk', 'scaling', 1.5)
        except:
            pass
    # 设置字体
    root.option_add("*Font", ("Microsoft YaHei", 9))
    # 创建应用
    app = PDFGrayConverter(root)
    # 关闭事件
    def on_closing():
        app.engine.stop()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_closing)
    # 运行
    root.mainloop()

if __name__ == "__main__":
    main()