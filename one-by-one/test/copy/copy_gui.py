from __future__ import annotations

import concurrent.futures
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "TIF 批量复制工具"
NAMES_NAME = "name.txt"
PATHS_NAME = "path.txt"
TARGET_SUFFIX = "_2025.tif"
PERCENT_BYTES_PATTERN = re.compile(rb"(\d+(?:\.\d+)?)%")


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def related_image_files(tif_path: Path) -> list[Path]:
    """Return the TIF and every same-basename sidecar in its directory."""
    name_prefix = f"{tif_path.stem}.".casefold()
    related: list[Path] = []
    with os.scandir(tif_path.parent) as entries:
        for entry in entries:
            if entry.is_file() and entry.name.casefold().startswith(name_prefix):
                related.append(Path(entry.path))
    return sorted(
        related,
        key=lambda path: (
            path.name.casefold() != tif_path.name.casefold(),
            path.name.casefold(),
        ),
    )


@dataclass(frozen=True)
class CopyItem:
    number: int
    name: str
    source: Path
    destination: Path
    work_file: Path
    size: int
    is_folder: bool = False


class CopyApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.base_dir = application_dir()
        self.names_path = self.base_dir / NAMES_NAME
        self.paths_path = self.base_dir / PATHS_NAME

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.active_processes: set[subprocess.Popen[bytes]] = set()
        self.running = False
        self.completed = 0
        self.copy_total = 0

        self.root.title(APP_TITLE)
        self.root.geometry("1020x700")
        self.root.minsize(840, 580)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.root_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.jobs_var = tk.StringVar(value="4")
        self.copy_folders_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.names_var = tk.StringVar(value="name.txt：正在读取")
        self.progress_var = tk.DoubleVar(value=0)

        self.configure_style()
        self.build_ui()
        self.load_configuration()
        self.root.after(150, self.poll_events)

    def configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5c6670")
        style.configure("Status.TLabel", foreground="#245c8a")
        style.configure("Run.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text="使用 Windows Robocopy 多线程复制 TIF 及其同名配套文件，实时显示复制进度。",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 14))

        settings = ttk.LabelFrame(outer, text="复制设置", padding=12)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="输入根目录").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=5
        )
        self.root_entry = ttk.Entry(settings, textvariable=self.root_var)
        self.root_entry.grid(row=0, column=1, sticky="ew", pady=5)
        self.root_browse = ttk.Button(
            settings, text="浏览…", command=self.browse_root, width=9
        )
        self.root_browse.grid(row=0, column=2, padx=(8, 0), pady=5)

        ttk.Label(settings, text="输出目录").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=5
        )
        self.output_entry = ttk.Entry(settings, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", pady=5)
        self.output_browse = ttk.Button(
            settings, text="浏览…", command=self.browse_output, width=9
        )
        self.output_browse.grid(row=1, column=2, padx=(8, 0), pady=5)

        ttk.Label(settings, text="并发任务数").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=5
        )
        self.jobs_box = ttk.Combobox(
            settings,
            textvariable=self.jobs_var,
            values=tuple(str(value) for value in range(1, 17)),
            width=8,
            state="readonly",
        )
        self.jobs_box.grid(row=2, column=1, sticky="w", pady=5)

        self.copy_folders_check = ttk.Checkbutton(
            settings,
            text="复制整个输入文件夹（不读取 name.txt）",
            variable=self.copy_folders_var,
            command=self.refresh_name_count,
        )
        self.copy_folders_check.grid(
            row=3, column=1, columnspan=2, sticky="w", pady=5
        )

        actions = ttk.Frame(settings)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        self.run_button = ttk.Button(
            actions, text="开始复制", command=self.start_copy, style="Run.TButton"
        )
        self.run_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            actions, text="停止", command=self.stop_copy, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="编辑 name.txt", command=self.open_names).pack(
            side=tk.LEFT, padx=(18, 0)
        )
        ttk.Button(actions, text="打开输出目录", command=self.open_output).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Label(actions, textvariable=self.names_var).pack(side=tk.RIGHT)

        table_frame = ttk.LabelFrame(outer, text="复制进度", padding=7)
        table_frame.grid(row=3, column=0, sticky="nsew", pady=(13, 0))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("number", "name", "status", "percent", "speed", "eta")
        self.task_table = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.task_table.heading("number", text="序号")
        self.task_table.heading("name", text="文件名")
        self.task_table.heading("status", text="状态")
        self.task_table.heading("percent", text="进度")
        self.task_table.heading("speed", text="速度")
        self.task_table.heading("eta", text="剩余时间")
        self.task_table.column("number", width=65, anchor="center", stretch=False)
        self.task_table.column("name", width=230, anchor="w")
        self.task_table.column("status", width=100, anchor="center", stretch=False)
        self.task_table.column("percent", width=90, anchor="e", stretch=False)
        self.task_table.column("speed", width=115, anchor="e", stretch=False)
        self.task_table.column("eta", width=105, anchor="center", stretch=False)
        self.task_table.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(
            table_frame, orient=tk.VERTICAL, command=self.task_table.yview
        )
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.task_table.configure(yscrollcommand=y_scroll.set)

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(11, 0))
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            footer, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=1, padx=(12, 0)
        )

    def read_non_comment_lines(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        lines: list[str] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                value = raw_line.strip().strip('"')
                if value and not value.startswith("#"):
                    lines.append(value)
        return lines

    def normalized_names(self, *, strip_target_suffix: bool = True) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_name in self.read_non_comment_lines(self.names_path):
            name = raw_name
            if strip_target_suffix and name.casefold().endswith(TARGET_SUFFIX.casefold()):
                name = name[: -len(TARGET_SUFFIX)]
            key = name.casefold()
            if key and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def load_configuration(self) -> None:
        path_lines = self.read_non_comment_lines(self.paths_path)
        if len(path_lines) >= 2:
            self.root_var.set(path_lines[0])
            self.output_var.set(path_lines[1])
        self.refresh_name_count()
        self.status_var.set("就绪")

    def refresh_name_count(self) -> int:
        folder_mode = self.copy_folders_var.get()
        if folder_mode:
            self.names_var.set("文件夹模式：不读取 name.txt")
            return 0
        count = len(self.normalized_names())
        self.names_var.set(f"name.txt：{count} 个名称")
        return count

    def browse_root(self) -> None:
        selected = filedialog.askdirectory(
            title="选择输入根目录", initialdir=self.initial_directory(self.root_var.get())
        )
        if selected:
            self.root_var.set(selected)

    def browse_output(self) -> None:
        selected = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.initial_directory(self.output_var.get()),
        )
        if selected:
            self.output_var.set(selected)

    @staticmethod
    def initial_directory(value: str) -> str | None:
        value = value.strip()
        return value if value and Path(value).exists() else None

    def save_paths(self, root_path: str, output_path: str) -> None:
        content = (
            "# 第1个非空行：输入根目录\n"
            "# 第2个非空行：输出目录\n"
            f"{root_path}\n{output_path}\n"
        )
        self.paths_path.write_text(content, encoding="utf-8", newline="\n")

    def validate_inputs(self) -> tuple[Path, Path, int, list[str]] | None:
        root_text = self.root_var.get().strip().strip('"')
        output_text = self.output_var.get().strip().strip('"')
        folder_mode = self.copy_folders_var.get()
        names = self.normalized_names()

        if not folder_mode and shutil.which("robocopy") is None:
            messagebox.showerror(APP_TITLE, "系统中找不到 robocopy.exe。")
            return None
        if not folder_mode and (not self.names_path.is_file() or not names):
            messagebox.showerror(APP_TITLE, "name.txt 不存在或没有可用名称。")
            return None
        if not root_text or not Path(root_text).is_dir():
            messagebox.showerror(APP_TITLE, "输入根目录不存在，请重新选择。")
            return None
        if not output_text:
            messagebox.showerror(APP_TITLE, "请填写输出目录。")
            return None
        if folder_mode:
            source = Path(root_text).resolve()
            output = Path(output_text).resolve()
            try:
                output.relative_to(source)
            except ValueError:
                pass
            else:
                messagebox.showerror(
                    APP_TITLE, "复制整个文件夹时，输出目录不能位于输入文件夹里面。"
                )
                return None
            if output / source.name == source:
                messagebox.showerror(
                    APP_TITLE, "输出目录不能是输入文件夹的上一级目录。"
                )
                return None
        try:
            jobs = int(self.jobs_var.get())
        except ValueError:
            messagebox.showerror(APP_TITLE, "并发任务数无效。")
            return None
        if not 1 <= jobs <= 32:
            messagebox.showerror(APP_TITLE, "并发任务数必须在 1 到 32 之间。")
            return None
        return Path(root_text), Path(output_text), jobs, names

    def start_copy(self) -> None:
        if self.running:
            return
        validated = self.validate_inputs()
        if validated is None:
            return
        source_root, output_dir, jobs, names = validated
        copy_folders = self.copy_folders_var.get()

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            self.save_paths(str(source_root), str(output_dir))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法准备目录：\n{exc}")
            return

        self.clear_table()
        self.stop_event.clear()
        self.completed = 0
        self.copy_total = 0
        self.progress_var.set(0)
        self.status_var.set(
            "正在统计输入文件夹…" if copy_folders else "正在扫描源目录…"
        )
        self.set_running_state(True)

        coordinator = threading.Thread(
            target=self.copy_pipeline,
            args=(source_root, output_dir, jobs, names, copy_folders),
            name="copy-coordinator",
            daemon=True,
        )
        coordinator.start()

    def copy_pipeline(
        self,
        source_root: Path,
        output_dir: Path,
        jobs: int,
        names: list[str],
        copy_folders: bool,
    ) -> None:
        try:
            missing: list[str] = []
            existing = 0
            items: list[CopyItem] = []
            total_bytes = 0
            work_dir = output_dir / ".copy_work"
            work_dir.mkdir(parents=True, exist_ok=True)

            if copy_folders:
                source = source_root
                destination = output_dir / source.name
                if destination.exists():
                    existing = 1
                else:
                    size = self.folder_size(source)
                    item = CopyItem(
                        number=1,
                        name=source.name,
                        source=source,
                        destination=destination,
                        work_file=work_dir / source.name,
                        size=size,
                        is_folder=True,
                    )
                    items.append(item)
                    total_bytes += size
                found_count = 1
            else:
                wanted = {f"{name}{TARGET_SUFFIX}".casefold(): name for name in names}
                found: dict[str, Path] = {}
                scanned = 0

                def on_walk_error(error: OSError) -> None:
                    self.events.put(("notice", f"无法读取目录：{error.filename}"))

                for directory, _, files in os.walk(
                    source_root, onerror=on_walk_error
                ):
                    if self.stop_event.is_set():
                        self.events.put(("cancelled", None))
                        return
                    for filename in files:
                        scanned += 1
                        key = filename.casefold()
                        if key in wanted and key not in found:
                            found[key] = Path(directory) / filename
                    if scanned and scanned % 10000 == 0:
                        self.events.put(
                            (
                                "status",
                                f"正在扫描：已检查 {scanned:,} 个文件，找到 {len(found)} 个",
                            )
                        )
                    if len(found) == len(wanted):
                        break

                for name in names:
                    filename = f"{name}{TARGET_SUFFIX}"
                    source = found.get(filename.casefold())
                    if source is None:
                        missing.append(filename)
                        continue
                    try:
                        related_files = related_image_files(source)
                    except OSError as exc:
                        self.events.put(
                            ("notice", f"无法读取配套文件：{source.parent}（{exc}）")
                        )
                        related_files = [source]

                    for related_source in related_files:
                        destination = output_dir / related_source.name
                        if destination.exists():
                            existing += 1
                            continue
                        try:
                            size = related_source.stat().st_size
                        except OSError:
                            self.events.put(("notice", f"无法读取文件：{related_source}"))
                            continue
                        item = CopyItem(
                            number=len(items) + 1,
                            name=related_source.name,
                            source=related_source,
                            destination=destination,
                            work_file=work_dir / related_source.name,
                            size=size,
                        )
                        items.append(item)
                        total_bytes += size
                found_count = len(found)

            if missing:
                (output_dir / "missing_2025_tifs.txt").write_text(
                    "\n".join(sorted(missing)) + "\n", encoding="utf-8"
                )

            self.events.put(
                (
                    "prepared",
                    {
                        "items": items,
                        "found": found_count,
                        "missing": len(missing),
                        "existing": existing,
                        "bytes": total_bytes,
                        "folder_mode": copy_folders,
                    },
                )
            )
            if not items:
                self.events.put(("done", {"failed": 0, "cancelled": False}))
                return

            failed = 0
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=jobs, thread_name_prefix="robocopy"
            ) as executor:
                futures = [executor.submit(self.copy_one, item) for item in items]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        success = future.result()
                    except Exception as exc:
                        success = False
                        self.events.put(("notice", f"复制线程异常：{exc}"))
                    if not success and not self.stop_event.is_set():
                        failed += 1

            self.events.put(
                ("done", {"failed": failed, "cancelled": self.stop_event.is_set()})
            )
        except Exception as exc:
            self.events.put(("fatal", str(exc)))

    def copy_one(self, item: CopyItem) -> bool:
        if item.is_folder:
            return self.copy_one_folder(item)
        if self.stop_event.is_set():
            return False
        if item.destination.exists():
            self.events.put(("task_done", (item.number, True, "已存在，跳过")))
            return True

        self.events.put(("task_start", item.number))
        # 上次中断留下的临时文件不能直接当作完整文件使用，重新复制更安全。
        item.work_file.unlink(missing_ok=True)
        command = [
            "robocopy",
            str(item.source.parent),
            str(item.work_file.parent),
            item.source.name,
            "/J",
            "/R:2",
            "/W:2",
            "/COPY:DAT",
            "/DCOPY:T",
            "/NJH",
            "/NJS",
            "/NDL",
            "/ETA",
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process: subprocess.Popen[bytes] | None = None
        last_percent = 0.0
        last_bytes = 0.0
        last_time = time.monotonic()
        smoothed_speed = 0.0
        recent = bytearray()

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                creationflags=creation_flags,
            )
            with self.process_lock:
                self.active_processes.add(process)

            assert process.stdout is not None
            while True:
                if self.stop_event.is_set() and process.poll() is None:
                    self.terminate_process_tree(process)
                chunk = process.stdout.read(1)
                if not chunk:
                    if process.poll() is not None:
                        break
                    continue

                recent.extend(chunk)
                if len(recent) > 256:
                    del recent[:-256]
                if chunk != b"%":
                    continue

                matches = list(PERCENT_BYTES_PATTERN.finditer(recent))
                if not matches:
                    continue
                percent = min(100.0, float(matches[-1].group(1)))
                now = time.monotonic()
                transferred = item.size * percent / 100.0
                elapsed = now - last_time
                if elapsed >= 0.15 and transferred >= last_bytes:
                    current_speed = (transferred - last_bytes) / elapsed
                    smoothed_speed = (
                        current_speed
                        if smoothed_speed <= 0
                        else smoothed_speed * 0.65 + current_speed * 0.35
                    )
                    remaining_seconds = (
                        (item.size - transferred) / smoothed_speed
                        if smoothed_speed > 0
                        else 0
                    )
                    self.events.put(
                        (
                            "progress",
                            (
                                item.number,
                                percent,
                                smoothed_speed,
                                remaining_seconds,
                            ),
                        )
                    )
                    last_percent = percent
                    last_bytes = transferred
                    last_time = now

            return_code = process.wait()
            if self.stop_event.is_set():
                self.events.put(("task_done", (item.number, False, "已停止")))
                return False
            if return_code >= 8 or not item.work_file.exists():
                self.events.put(
                    ("task_done", (item.number, False, f"失败，代码 {return_code}"))
                )
                return False

            if item.destination.exists():
                item.work_file.unlink(missing_ok=True)
                self.events.put(("task_done", (item.number, True, "已存在，跳过")))
                return True

            os.replace(item.work_file, item.destination)
            self.events.put(
                (
                    "progress",
                    (item.number, 100.0, smoothed_speed, 0.0),
                )
            )
            self.events.put(("task_done", (item.number, True, "完成")))
            return True
        except Exception as exc:
            self.events.put(("task_done", (item.number, False, f"失败：{exc}")))
            return False
        finally:
            if process is not None:
                with self.process_lock:
                    self.active_processes.discard(process)

    @staticmethod
    def folder_size(folder: Path) -> int:
        total = 0
        for directory, _, files in os.walk(folder):
            for filename in files:
                total += (Path(directory) / filename).stat().st_size
        return total

    def copy_one_folder(self, item: CopyItem) -> bool:
        if self.stop_event.is_set():
            return False
        if item.destination.exists():
            self.events.put(("task_done", (item.number, True, "已存在，跳过")))
            return True

        self.events.put(("task_start", item.number))
        copied = 0
        last_bytes = 0
        last_time = time.monotonic()
        smoothed_speed = 0.0
        directory_metadata: list[tuple[Path, Path]] = []

        try:
            if item.work_file.exists():
                shutil.rmtree(item.work_file)
            item.work_file.mkdir(parents=True, exist_ok=True)

            for directory, _, files in os.walk(item.source):
                if self.stop_event.is_set():
                    self.events.put(("task_done", (item.number, False, "已停止")))
                    return False

                source_dir = Path(directory)
                relative = source_dir.relative_to(item.source)
                target_dir = item.work_file / relative
                target_dir.mkdir(parents=True, exist_ok=True)
                directory_metadata.append((source_dir, target_dir))

                for filename in files:
                    source_file = source_dir / filename
                    target_file = target_dir / filename
                    with source_file.open("rb") as source_handle, target_file.open(
                        "wb"
                    ) as target_handle:
                        while True:
                            if self.stop_event.is_set():
                                self.events.put(
                                    ("task_done", (item.number, False, "已停止"))
                                )
                                return False
                            chunk = source_handle.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            target_handle.write(chunk)
                            copied += len(chunk)

                            now = time.monotonic()
                            elapsed = now - last_time
                            if elapsed >= 0.2:
                                current_speed = (copied - last_bytes) / elapsed
                                smoothed_speed = (
                                    current_speed
                                    if smoothed_speed <= 0
                                    else smoothed_speed * 0.65 + current_speed * 0.35
                                )
                                percent = (
                                    min(100.0, copied * 100.0 / item.size)
                                    if item.size > 0
                                    else 100.0
                                )
                                remaining_seconds = (
                                    (item.size - copied) / smoothed_speed
                                    if smoothed_speed > 0
                                    else 0.0
                                )
                                self.events.put(
                                    (
                                        "progress",
                                        (
                                            item.number,
                                            percent,
                                            smoothed_speed,
                                            remaining_seconds,
                                        ),
                                    )
                                )
                                last_bytes = copied
                                last_time = now
                    shutil.copystat(source_file, target_file)

            for source_dir, target_dir in reversed(directory_metadata):
                shutil.copystat(source_dir, target_dir)

            if item.destination.exists():
                shutil.rmtree(item.work_file)
                self.events.put(("task_done", (item.number, True, "已存在，跳过")))
                return True
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(item.work_file, item.destination)
            self.events.put(
                ("progress", (item.number, 100.0, smoothed_speed, 0.0))
            )
            self.events.put(("task_done", (item.number, True, "完成")))
            return True
        except Exception as exc:
            self.events.put(("task_done", (item.number, False, f"失败：{exc}")))
            return False

    def poll_events(self) -> None:
        try:
            for _ in range(1000):
                event_type, payload = self.events.get_nowait()
                if event_type == "status" or event_type == "notice":
                    self.status_var.set(str(payload))
                elif event_type == "prepared":
                    self.on_prepared(payload)  # type: ignore[arg-type]
                elif event_type == "task_start":
                    self.update_row(int(payload), status="复制中")
                elif event_type == "progress":
                    number, percent, speed, eta = payload  # type: ignore[misc]
                    self.update_row(
                        int(number),
                        status="复制中",
                        percent=f"{float(percent):.1f}%",
                        speed=f"{float(speed) / 1_000_000:.2f} MB/s",
                        eta=self.format_duration(float(eta)),
                    )
                elif event_type == "task_done":
                    number, success, status = payload  # type: ignore[misc]
                    self.completed += 1
                    self.update_row(
                        int(number),
                        status=str(status),
                        percent="100.0%" if success else "—",
                        eta="00:00:00" if success else "—",
                    )
                    if self.copy_total:
                        self.progress_var.set(self.completed * 100 / self.copy_total)
                        self.status_var.set(
                            f"总体进度：{self.completed}/{self.copy_total}"
                        )
                elif event_type == "done":
                    self.finish_run(payload)  # type: ignore[arg-type]
                elif event_type == "cancelled":
                    self.finish_run({"failed": 0, "cancelled": True})
                elif event_type == "fatal":
                    self.status_var.set(f"错误：{payload}")
                    self.set_running_state(False)
                    messagebox.showerror(APP_TITLE, f"运行失败：\n{payload}")
        except queue.Empty:
            pass
        self.root.after(150, self.poll_events)

    def on_prepared(self, data: dict[str, object]) -> None:
        items: list[CopyItem] = data["items"]  # type: ignore[assignment]
        folder_mode = bool(data.get("folder_mode", False))
        self.task_table.heading("name", text="文件夹" if folder_mode else "文件名")
        self.copy_total = len(items)
        total_gib = int(data["bytes"]) / 1024**3
        self.status_var.set(
            f"找到 {data['found']}，跳过 {data['existing']}，"
            f"缺失 {data['missing']}；待复制 {len(items)} 个 / {total_gib:.2f} GiB"
        )
        for item in items:
            self.task_table.insert(
                "",
                tk.END,
                iid=str(item.number),
                values=(
                    f"{item.number}/{len(items)}",
                    item.name,
                    "等待",
                    "0.0%",
                    "0.00 MB/s",
                    "—",
                ),
            )

    def update_row(
        self,
        number: int,
        *,
        status: str | None = None,
        percent: str | None = None,
        speed: str | None = None,
        eta: str | None = None,
    ) -> None:
        iid = str(number)
        if not self.task_table.exists(iid):
            return
        values = list(self.task_table.item(iid, "values"))
        if status is not None:
            values[2] = status
        if percent is not None:
            values[3] = percent
        if speed is not None:
            values[4] = speed
        if eta is not None:
            values[5] = eta
        self.task_table.item(iid, values=values)
        if status == "复制中":
            self.task_table.see(iid)

    def finish_run(self, result: dict[str, object]) -> None:
        self.set_running_state(False)
        failed = int(result.get("failed", 0))
        cancelled = bool(result.get("cancelled", False))
        if cancelled:
            self.status_var.set("任务已停止")
            messagebox.showwarning(APP_TITLE, "复制任务已停止。")
        elif failed:
            self.status_var.set(f"复制结束，失败 {failed} 个")
            messagebox.showerror(APP_TITLE, f"复制结束，有 {failed} 个文件失败。")
        else:
            self.progress_var.set(100)
            self.status_var.set("复制完成")
            messagebox.showinfo(APP_TITLE, "复制任务已完成。")

    def stop_copy(self) -> None:
        if not self.running:
            return
        if not messagebox.askyesno(APP_TITLE, "确定停止当前复制任务吗？"):
            return
        self.stop_event.set()
        self.status_var.set("正在停止…")
        with self.process_lock:
            processes = list(self.active_processes)
        for process in processes:
            self.terminate_process_tree(process)

    @staticmethod
    def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=flags,
        )

    def set_running_state(self, running: bool) -> None:
        self.running = running
        entry_state = tk.DISABLED if running else tk.NORMAL
        self.root_entry.configure(state=entry_state)
        self.output_entry.configure(state=entry_state)
        self.root_browse.configure(state=entry_state)
        self.output_browse.configure(state=entry_state)
        self.jobs_box.configure(state=tk.DISABLED if running else "readonly")
        self.copy_folders_check.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.run_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)

    def clear_table(self) -> None:
        for item in self.task_table.get_children():
            self.task_table.delete(item)

    def open_names(self) -> None:
        try:
            os.startfile(self.names_path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法打开 name.txt：\n{exc}")

    def open_output(self) -> None:
        value = self.output_var.get().strip().strip('"')
        if not value or not Path(value).exists():
            messagebox.showwarning(APP_TITLE, "输出目录尚不存在。")
            return
        try:
            os.startfile(value)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法打开输出目录：\n{exc}")

    @staticmethod
    def format_duration(seconds: float) -> str:
        if seconds <= 0 or seconds == float("inf"):
            return "—"
        seconds = int(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def on_close(self) -> None:
        if self.running:
            messagebox.showwarning(APP_TITLE, "复制任务正在运行，请先停止任务。")
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    CopyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
