import csv
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import codecs
import unicodedata
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk


APP_TITLE = "MarkerCut Corporate Exporter"
DEFAULT_PREFIX = "parte_"
DEFAULT_EXT = "mp4"
LOG_FILE = "export_log.txt"
DEVELOPER_NAME = "Pendragon503"
DEVELOPER_GITHUB = "https://github.com/Pendragon503"
PROJECT_REPOSITORY = "https://github.com/Pendragon503/exportar_marcadores_premiere"
APP_COPYRIGHT = "© Pendragon503"


# ============================================================
# UTILIDADES
# ============================================================

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def safe_filename(text: str, max_len: int = 90) -> str:
    text = (text or "").strip()
    text = unicodedata.normalize("NFKC", text)

    bad_chars = '<>:"/\\|?*\n\r\t'
    for ch in bad_chars:
        text = text.replace(ch, "_")

    text = " ".join(text.split())
    text = text.strip(" ._")

    if not text:
        text = "sin_nombre"

    return text[:max_len].strip(" ._") or "sin_nombre"


def strip_accents_lower(text: str) -> str:
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()


def parse_fraction(value: str) -> float:
    value = (value or "").strip()
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return float(num) / float(den)
        except Exception:
            return 0.0

    try:
        return float(value)
    except Exception:
        return 0.0


def normalize_fps(fps: float) -> float:
    typical = {
        23.976: 0.01,
        24.0: 0.01,
        25.0: 0.01,
        29.97: 0.01,
        30.0: 0.01,
        50.0: 0.02,
        59.94: 0.02,
        60.0: 0.02,
    }

    for target, tolerance in typical.items():
        if abs(fps - target) <= tolerance:
            return target

    return float(f"{fps:.3f}")


def seconds_to_hms(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def format_duration_compact(seconds: float) -> str:
    """Formato legible para la cuenta regresiva de la interfaz."""
    seconds = max(0, int(round(float(seconds))))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


def parse_time_any(raw: str, fps: float) -> float:
    """
    Soporta:
    - HH:MM:SS:FF
    - HH:MM:SS.mmm
    - HH:MM:SS
    - MM:SS
    - segundos
    """
    text = (raw or "").strip().replace(";", ":").replace(",", ".")
    if not text:
        raise ValueError("Tiempo vacío")

    parts = text.split(":")

    if len(parts) == 4:
        hh, mm, ss, ff = parts
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + (int(ff) / float(fps))

    if len(parts) == 3:
        hh = int(parts[0])
        mm = int(parts[1])
        sec = float(parts[2])
        return hh * 3600 + mm * 60 + sec

    if len(parts) == 2:
        mm = int(parts[0])
        sec = float(parts[1])
        return mm * 60 + sec

    if len(parts) == 1:
        return float(parts[0])

    raise ValueError(f"Formato de tiempo no reconocido: {raw}")


def sniff_encoding(csv_path: str) -> str:
    with open(csv_path, "rb") as file:
        head = file.read(4)

    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"

    return "utf-8-sig"


def sniff_delimiter(sample_text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t")
        return dialect.delimiter
    except Exception:
        if "\t" in sample_text:
            return "\t"
        if ";" in sample_text:
            return ";"
        return ","


def find_header(fieldnames: list[str], candidates: list[str]) -> str | None:
    norm_map = {strip_accents_lower(f): f for f in fieldnames if f}

    for candidate in candidates:
        key = strip_accents_lower(candidate)
        if key in norm_map:
            return norm_map[key]

    return None


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


# ============================================================
# FFPROBE / FFMPEG
# ============================================================

def detect_fps(input_video: str) -> float:
    if not ffprobe_available():
        return 0.0

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-of", "default=nw=1:nk=1",
        input_video,
    ]

    try:
        out = run_quiet(cmd).stdout.splitlines()
        values = [parse_fraction(x) for x in out if x.strip()]
        values = [v for v in values if v > 0]
        if not values:
            return 0.0

        return normalize_fps(values[0])
    except Exception:
        return 0.0


def detect_duration(input_video: str) -> float:
    if not ffprobe_available():
        return 0.0

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        input_video,
    ]

    try:
        out = run_quiet(cmd).stdout.strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0


def nvenc_video_opts(fps: float, quality: int) -> list[str]:
    gop = max(2, int(round(fps * 2)))
    return [
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-tune", "hq",
        "-rc", "vbr",
        "-cq", str(quality),
        "-b:v", "0",
        "-spatial_aq", "1",
        "-temporal_aq", "1",
        "-aq-strength", "8",
        "-bf", "3",
        "-g", str(gop),
        "-pix_fmt", "yuv420p",
    ]


def qsv_video_opts(fps: float, quality: int) -> list[str]:
    gop = max(2, int(round(fps * 2)))
    return [
        "-c:v", "h264_qsv",
        "-global_quality", str(quality),
        "-look_ahead", "1",
        "-bf", "3",
        "-g", str(gop),
        "-pix_fmt", "yuv420p",
    ]


def x264_video_opts(fps: float, quality: int, threads: int = 0) -> list[str]:
    """
    x264 se usa como apoyo de CPU.
    - threads=0: x264 decide y puede usar toda la CPU. Útil si solo usas backend x264.
    - threads=2/3/4: mejor para modo ultra, porque permite varios procesos sin pelearse toda la CPU.
    """
    gop = max(2, int(round(fps * 2)))
    thread_value = "0" if threads <= 0 else str(max(1, int(threads)))
    return [
        "-c:v", "libx264",
        "-crf", str(quality),
        "-preset", "veryfast",
        "-threads", thread_value,
        "-g", str(gop),
        "-pix_fmt", "yuv420p",
    ]


def video_opts_for_backend(
    backend: str,
    fps: float,
    quality: int,
    x264_threads: int = 0,
) -> list[str]:
    if backend == "nvenc":
        return nvenc_video_opts(fps, quality)
    if backend == "qsv":
        return qsv_video_opts(fps, quality)
    return x264_video_opts(fps, quality, threads=x264_threads)


def test_backend(backend: str, input_video: str, fps: float, quality: int) -> bool:
    if backend not in {"nvenc", "qsv", "x264"}:
        return False

    with tempfile.TemporaryDirectory() as temp_dir:
        out_file = Path(temp_dir) / "backend_test.mp4"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", "0",
            "-t", "0.5",
            "-i", input_video,
            "-map", "0:v:0",
            "-map", "0:a?",
            *video_opts_for_backend(backend, fps, quality, x264_threads=0),
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_file),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return out_file.exists() and out_file.stat().st_size > 0
        except Exception:
            return False


def choose_backend(requested: str, input_video: str, fps: float, quality: int) -> str:
    requested = requested.lower().strip()

    if requested in {"nvenc", "qsv", "x264"}:
        if test_backend(requested, input_video, fps, quality):
            return requested

        if requested != "x264":
            return "x264"

    for backend in ("nvenc", "qsv", "x264"):
        if test_backend(backend, input_video, fps, quality):
            return backend

    return "x264"


def input_hwaccel_opts(backend: str) -> list[str]:
    """
    Intenta acelerar la decodificación del video fuente.
    No garantiza más velocidad en todos los equipos, pero ayuda cuando el cuello
    de botella está en leer/decodificar H.264/H.265.
    """
    if backend in {"nvenc", "qsv"}:
        return ["-hwaccel", "auto"]
    return []


def choose_backend_sequence(requested: str, input_video: str, fps: float, quality: int) -> list[str]:
    """
    Devuelve una lista de backends válidos.
    - auto: usa el primer backend disponible en orden nvenc -> qsv -> x264.
    - nvenc/qsv/x264: fuerza uno solo si funciona.
    - hibrido: reparte segmentos entre todos los backends disponibles para usar
      NVIDIA, Intel Quick Sync y CPU al mismo tiempo.
    """
    requested = requested.lower().strip()
    order = ["nvenc", "qsv", "x264"]
    available: list[str] = []

    for backend in order:
        if test_backend(backend, input_video, fps, quality):
            available.append(backend)

    if requested in {"hibrido", "híbrido", "hybrid", "mixto", "all"}:
        return available or ["x264"]

    if requested in set(order):
        if requested in available:
            return [requested]
        if "x264" in available:
            return ["x264"]
        return available[:1] or ["x264"]

    # auto: preferir NVIDIA; si no está disponible, Intel QSV; último recurso CPU.
    return available[:1] or ["x264"]



def is_multi_backend_mode(requested: str) -> bool:
    requested = requested.lower().strip()
    return requested in {"ultra", "hibrido", "híbrido", "hybrid", "mixto", "all"}


def make_worker_backends(requested: str, available: list[str], parallel_jobs: int) -> list[str]:
    """
    Crea un pool de trabajadores atados a backend.
    Cada trabajador toma el siguiente segmento libre cuando termina el suyo.

    Modo ultra/híbrido:
    - NVIDIA NVENC: hasta 2 trabajadores.
    - Intel QSV: hasta 1 trabajador por defecto.
    - CPU x264: usa los cupos restantes.

    No conviene lanzar 10 NVENC/QSV a ciegas: se satura el decoder/disco y no gana velocidad real.
    """
    requested = requested.lower().strip()
    parallel_jobs = max(1, min(16, int(parallel_jobs or 1)))

    if not available:
        return ["x264"]

    if not is_multi_backend_mode(requested):
        backend = available[0]
        return [backend for _ in range(parallel_jobs)]

    workers: list[str] = []

    if "nvenc" in available and len(workers) < parallel_jobs:
        nvenc_slots = 2 if parallel_jobs >= 4 else 1
        workers.extend(["nvenc"] * min(nvenc_slots, parallel_jobs - len(workers)))

    if "qsv" in available and len(workers) < parallel_jobs:
        qsv_slots = 1 if parallel_jobs < 7 else 2
        workers.extend(["qsv"] * min(qsv_slots, parallel_jobs - len(workers)))

    if "x264" in available and len(workers) < parallel_jobs:
        workers.extend(["x264"] * (parallel_jobs - len(workers)))

    # Si por alguna razón faltó llenar cupos, repite el backend más rápido disponible.
    preferred = "nvenc" if "nvenc" in available else ("qsv" if "qsv" in available else available[0])
    while len(workers) < parallel_jobs:
        workers.append(preferred)

    return workers[:parallel_jobs]


def summarize_worker_pool(worker_backends: list[str]) -> str:
    counts: dict[str, int] = {}
    for backend in worker_backends:
        counts[backend] = counts.get(backend, 0) + 1

    order = ["nvenc", "qsv", "x264"]
    parts = [f"{backend} x{counts[backend]}" for backend in order if counts.get(backend, 0)]
    return ", ".join(parts) if parts else "sin trabajadores"


def recommended_x264_threads(worker_backends: list[str]) -> int:
    """
    En modo ultra, cada proceso x264 usa pocos hilos para no bloquear al resto.
    Si solo hay un x264, puede usar todos los hilos con 0.
    """
    x264_workers = sum(1 for backend in worker_backends if backend == "x264")
    if x264_workers <= 0:
        return 0
    if len(set(worker_backends)) == 1 and worker_backends[0] == "x264":
        return 0

    cpu_count = os.cpu_count() or 4
    return max(2, min(4, cpu_count // max(1, x264_workers)))


def build_ffmpeg_cmd(
    input_video: str,
    start_s: float,
    duration: float,
    out_file: Path,
    backend: str,
    fps: float,
    quality: int,
    use_double_seek: bool,
    x264_threads: int = 0,
) -> list[str]:
    """
    Doble seek real:
    - Primer -ss antes del input: rápido.
    - Segundo -ss después del input: precisión fina.
    """
    duration = max(0.001, duration)

    if use_double_seek:
        pre_seek = max(0.0, start_s - 3.0)
        post_seek = start_s - pre_seek

        seek_part = [
            "-ss", f"{pre_seek:.3f}",
            *input_hwaccel_opts(backend),
            "-i", input_video,
            "-ss", f"{post_seek:.3f}",
        ]
    else:
        seek_part = [
            "-ss", f"{start_s:.3f}",
            *input_hwaccel_opts(backend),
            "-i", input_video,
        ]

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-y",
        *seek_part,
        "-t", f"{duration:.3f}",
        "-map", "0:v:0",
        "-map", "0:a?",
        *video_opts_for_backend(backend, fps, quality, x264_threads=x264_threads),
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(out_file),
    ]


# ============================================================
# MARCADORES
# ============================================================

@dataclass(frozen=True)
class Marker:
    time_s: float
    name: str


@dataclass(frozen=True)
class Segment:
    index: int
    start_s: float
    end_s: float
    name: str
    out_file: Path
    input_video: str

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s


def load_markers(csv_path: str, fps: float) -> list[Marker]:
    enc = sniff_encoding(csv_path)

    with open(csv_path, "r", encoding=enc, errors="strict") as file:
        sample = file.read(4096)

    delimiter = sniff_delimiter(sample)

    with open(csv_path, "r", encoding=enc, newline="") as file:
        reader = csv.DictReader(file, delimiter=delimiter)
        fieldnames = [fn for fn in (reader.fieldnames or []) if fn]

        if not fieldnames:
            raise ValueError("No se pudieron leer cabeceras del CSV.")

        start_candidates = [
            "Start", "In", "Time", "Inicio", "Entrada",
            "Start Time", "StartTime",
            "Punto de entrada", "Tiempo de inicio",
        ]

        name_candidates = [
            "Name", "Marker Name", "Nombre", "Nombre del marcador",
            "Comment", "Comentario", "Description", "Descripción",
        ]

        start_col = find_header(fieldnames, start_candidates)
        name_col = find_header(fieldnames, name_candidates)

        if not start_col:
            raise ValueError(
                "No encuentro columna de tiempo. "
                f"Cabeceras detectadas: {fieldnames}"
            )

        markers: list[Marker] = []

        for row in reader:
            raw_time = (row.get(start_col) or "").strip()
            if not raw_time:
                continue

            try:
                time_s = parse_time_any(raw_time, fps)
            except Exception:
                continue

            name = (row.get(name_col) or "").strip() if name_col else ""
            markers.append(Marker(time_s=time_s, name=name))

    markers.sort(key=lambda item: item.time_s)

    # Elimina duplicados exactos de tiempo para evitar segmentos de duración 0.
    unique: list[Marker] = []
    seen: set[float] = set()

    for marker in markers:
        key = round(marker.time_s, 3)
        if key in seen:
            continue
        seen.add(key)
        unique.append(marker)

    return unique


def make_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix

    for n in range(2, 1000):
        candidate = path.with_name(f"{stem}_{n:02d}{suffix}")
        if not candidate.exists():
            return candidate

    raise FileExistsError(f"No se pudo crear un nombre único para: {path}")


def build_segments(
    markers: list[Marker],
    input_video: str,
    output_dir: str,
    prefix: str,
    ext: str,
    video_duration: float = 0.0,
) -> list[Segment]:
    if len(markers) < 2:
        raise ValueError("Se requieren al menos 2 marcadores: inicio y fin.")

    output_path = Path(output_dir)
    segments: list[Segment] = []

    for idx in range(len(markers) - 1):
        start_marker = markers[idx]
        end_marker = markers[idx + 1]

        if end_marker.time_s <= start_marker.time_s:
            continue

        if video_duration > 0 and start_marker.time_s >= video_duration:
            continue

        end_s = end_marker.time_s
        if video_duration > 0:
            end_s = min(end_s, video_duration)

        if end_s <= start_marker.time_s:
            continue

        base = f"{prefix}{idx + 1:03d}"
        if start_marker.name:
            base += f"_{safe_filename(start_marker.name)}"

        out_file = make_unique_path(output_path / f"{base}.{ext.lstrip('.')}")

        segments.append(
            Segment(
                index=idx,
                start_s=start_marker.time_s,
                end_s=end_s,
                name=start_marker.name,
                out_file=out_file,
                input_video=input_video,
            )
        )

    if not segments:
        raise ValueError("No se generó ningún segmento válido. Revisa los tiempos de los marcadores.")

    return segments


# ============================================================
# EXPORTACIÓN
# ============================================================

class ExportCancelled(Exception):
    pass


def append_log(log_path: Path, text: str, lock: threading.Lock) -> None:
    with lock:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(text)


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=4)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def export_one_segment(
    segment: Segment,
    backend: str,
    fps: float,
    quality: int,
    use_double_seek: bool,
    progress_queue: queue.Queue,
    stop_event: threading.Event,
    log_path: Path,
    log_lock: threading.Lock,
    processes: dict[int, subprocess.Popen],
    processes_lock: threading.Lock,
    x264_threads: int = 0,
) -> Path:
    if stop_event.is_set():
        raise ExportCancelled()

    cmd = build_ffmpeg_cmd(
        input_video=segment.input_video,
        start_s=segment.start_s,
        duration=segment.duration,
        out_file=segment.out_file,
        backend=backend,
        fps=fps,
        quality=quality,
        use_double_seek=use_double_seek,
        x264_threads=x264_threads,
    )

    append_log(
        log_path,
        (
            f"[{segment.index + 1:03d}] {segment.out_file.name}\n"
            f"  Backend : {backend}\n"
            f"  Hilos CPU x264: {x264_threads if backend == 'x264' and x264_threads > 0 else 'auto'}\n"
            f"  Inicio  : {seconds_to_hms(segment.start_s)}\n"
            f"  Fin     : {seconds_to_hms(segment.end_s)}\n"
            f"  Duración: {seconds_to_hms(segment.duration)}\n"
            f"  CMD     : {' '.join(cmd)}\n"
        ),
        log_lock,
    )

    # Se usa un solo pipe para stdout/stderr.
    # Motivo: FFmpeg puede bloquearse si stderr se llena mientras solo se lee stdout;
    # eso deja la barra en 100% pero impide emitir el evento final.
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    with processes_lock:
        processes[segment.index] = process

    output_tail: list[str] = []

    try:
        if not process.stdout:
            raise RuntimeError("No se pudo leer el progreso de ffmpeg.")

        for raw_line in process.stdout:
            if stop_event.is_set():
                terminate_process(process)
                raise ExportCancelled()

            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("out_time_ms="):
                try:
                    out_time_ms = int(line.split("=", 1)[1])
                    current = out_time_ms / 1_000_000
                    fraction = min(1.0, max(0.0, current / max(0.001, segment.duration)))
                    progress_queue.put(("segment_progress", segment.index, fraction))
                except Exception:
                    pass
            else:
                output_tail.append(line)
                if len(output_tail) > 80:
                    output_tail = output_tail[-80:]

        return_code = process.wait()
        ffmpeg_output = "\n".join(output_tail)

        if return_code != 0:
            append_log(log_path, f"  ERROR   : {ffmpeg_output[-3000:]}\n\n", log_lock)
            raise subprocess.CalledProcessError(return_code, cmd, stderr=ffmpeg_output)

        progress_queue.put(("segment_progress", segment.index, 1.0))
        append_log(log_path, "  Estado  : OK\n\n", log_lock)

        return segment.out_file

    finally:
        with processes_lock:
            processes.pop(segment.index, None)


def export_all_segments(
    csv_path: str,
    input_video: str,
    output_dir: str,
    requested_backend: str,
    fps: float,
    quality: int,
    parallel_jobs: int,
    prefix: str,
    ext: str,
    use_double_seek: bool,
    progress_queue: queue.Queue,
    stop_event: threading.Event,
    processes: dict[int, subprocess.Popen],
    processes_lock: threading.Lock,
) -> None:
    log_lock = threading.Lock()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    log_path = output_path / LOG_FILE
    video_duration = detect_duration(input_video)

    progress_queue.put(("log", "Leyendo marcadores..."))
    markers = load_markers(csv_path, fps)
    progress_queue.put(("log", f"Marcadores detectados: {len(markers)}"))

    segments = build_segments(
        markers=markers,
        input_video=input_video,
        output_dir=output_dir,
        prefix=prefix,
        ext=ext,
        video_duration=video_duration,
    )

    progress_queue.put(("segments_ready", len(segments)))
    progress_queue.put(("log", f"Segmentos válidos: {len(segments)}"))

    available_backends = choose_backend_sequence("hibrido", input_video, fps, quality)
    requested = requested_backend.lower().strip()

    if requested in {"auto", ""}:
        selected_backends = choose_backend_sequence("auto", input_video, fps, quality)
    elif requested in {"nvenc", "qsv", "x264"}:
        selected_backends = choose_backend_sequence(requested, input_video, fps, quality)
    else:
        selected_backends = available_backends

    worker_backends = make_worker_backends(requested_backend, selected_backends, parallel_jobs)
    pool_label = summarize_worker_pool(worker_backends)
    x264_threads = recommended_x264_threads(worker_backends)

    progress_queue.put(("backend", pool_label))
    progress_queue.put(("log", f"Backends disponibles: {', '.join(available_backends)}"))
    progress_queue.put(("log", f"Pool de trabajo: {pool_label}"))
    if x264_threads > 0:
        progress_queue.put(("log", f"Hilos por proceso x264 en modo mixto: {x264_threads}"))

    append_log(
        log_path,
        (
            "\n" + "=" * 90 + "\n"
            f"Fecha       : {datetime.now().isoformat(timespec='seconds')}\n"
            f"CSV         : {Path(csv_path).resolve()}\n"
            f"Video       : {Path(input_video).resolve()}\n"
            f"Duración    : {seconds_to_hms(video_duration) if video_duration else 'No detectada'}\n"
            f"FPS         : {fps}\n"
            f"Backends disp.: {', '.join(available_backends)}\n"
            f"Pool        : {pool_label}\n"
            f"Calidad     : {quality}\n"
            f"Paralelos   : {parallel_jobs}\n"
            f"x264 threads: {x264_threads if x264_threads > 0 else 'auto'}\n"
            f"Doble seek  : {use_double_seek}\n"
            "-" * 90 + "\n"
        ),
        log_lock,
    )

    completed = 0
    errors = 0
    completed_lock = threading.Lock()
    work_queue: queue.Queue[Segment] = queue.Queue()

    for segment in segments:
        work_queue.put(segment)

    def worker_loop(worker_id: int, backend: str) -> None:
        nonlocal completed, errors

        while not stop_event.is_set():
            try:
                segment = work_queue.get_nowait()
            except queue.Empty:
                break

            try:
                progress_queue.put(("log", f"Worker {worker_id:02d} [{backend}] -> {segment.out_file.name}"))
                out_file = export_one_segment(
                    segment,
                    backend,
                    fps,
                    quality,
                    use_double_seek,
                    progress_queue,
                    stop_event,
                    log_path,
                    log_lock,
                    processes,
                    processes_lock,
                    x264_threads=x264_threads if backend == "x264" else 0,
                )

                with completed_lock:
                    completed += 1
                    done_count = completed

                progress_queue.put(("segment_done", segment.index, out_file.name, done_count))
                progress_queue.put(("log", f"OK [{backend}]: {out_file.name}"))

            except ExportCancelled:
                progress_queue.put(("log", f"Worker {worker_id:02d} cancelado."))
                break
            except subprocess.CalledProcessError as exc:
                with completed_lock:
                    errors += 1
                msg = f"Falló [{backend}] {segment.out_file.name}. Código ffmpeg: {exc.returncode}"
                progress_queue.put(("segment_error", segment.index, msg))
                progress_queue.put(("log", msg))
            except Exception as exc:
                with completed_lock:
                    errors += 1
                msg = f"Error [{backend}] en {segment.out_file.name}: {exc}"
                progress_queue.put(("segment_error", segment.index, msg))
                progress_queue.put(("log", msg))
            finally:
                work_queue.task_done()

    threads: list[threading.Thread] = []
    for worker_id, backend in enumerate(worker_backends, start=1):
        thread = threading.Thread(target=worker_loop, args=(worker_id, backend), daemon=True)
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    if stop_event.is_set():
        progress_queue.put(("cancelled", completed, errors))
        append_log(log_path, f"Estado final: CANCELADO | OK: {completed} | Errores: {errors}\n", log_lock)
        return

    progress_queue.put(("finished", completed, errors, str(output_path)))
    append_log(log_path, f"Estado final: TERMINADO | OK: {completed} | Errores: {errors}\n", log_lock)


# ============================================================
# INTERFAZ
# ============================================================

CORPORATE_COLORS = {
    "bg": "#F3F6FA",
    "surface": "#FFFFFF",
    "surface_alt": "#EAF0F6",
    "border": "#D7DEE8",
    "primary": "#0F3D5E",
    "primary_dark": "#092A42",
    "accent": "#2A7FBA",
    "success": "#1E7D4F",
    "danger": "#A63A3A",
    "text": "#1F2933",
    "muted": "#667085",
    "disabled": "#AAB4C0",
}


class MarkerExporterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("940x720")
        self.minsize(860, 640)
        self.configure(bg=CORPORATE_COLORS["bg"])

        self.progress_queue: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        self.processes: dict[int, subprocess.Popen] = {}
        self.processes_lock = threading.Lock()

        self.segment_progress: dict[int, float] = {}
        self.total_segments = 0
        self.completed_segments = 0
        self.error_segments = 0
        self.final_state_received = False
        self.last_output_folder = ""
        self.export_started_at: float | None = None
        self.last_percent = 0.0

        self.csv_var = tk.StringVar()
        self.video_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.fps_var = tk.StringVar(value="auto")
        self.backend_var = tk.StringVar(value="ultra")
        self.quality_var = tk.IntVar(value=20)
        self.jobs_var = tk.IntVar(value=min(6, max(1, os.cpu_count() or 1)))
        self.prefix_var = tk.StringVar(value=DEFAULT_PREFIX)
        self.ext_var = tk.StringVar(value=DEFAULT_EXT)
        self.double_seek_var = tk.BooleanVar(value=True)

        self.configure_styles()
        self.create_widgets()
        self.after(150, self.poll_queue)

    def configure_styles(self) -> None:
        self.option_add("*Font", "{Segoe UI} 10")
        style = ttk.Style(self)

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        colors = CORPORATE_COLORS

        style.configure("TFrame", background=colors["bg"])
        style.configure("Surface.TFrame", background=colors["surface"])
        style.configure("Header.TFrame", background=colors["primary"])
        style.configure("Actions.TFrame", background=colors["bg"])

        style.configure(
            "TLabel",
            background=colors["bg"],
            foreground=colors["text"],
            font="{Segoe UI} 10",
        )
        style.configure(
            "HeaderTitle.TLabel",
            background=colors["primary"],
            foreground="#FFFFFF",
            font="{Segoe UI} 18 bold",
        )
        style.configure(
            "HeaderSubtitle.TLabel",
            background=colors["primary"],
            foreground="#D9E8F5",
            font="{Segoe UI} 10",
        )
        style.configure(
            "SectionHint.TLabel",
            background=colors["surface"],
            foreground=colors["muted"],
            font="{Segoe UI} 9",
        )
        style.configure(
            "Status.TLabel",
            background=colors["surface"],
            foreground=colors["text"],
            font="{Segoe UI} 10 bold",
        )
        style.configure(
            "Eta.TLabel",
            background=colors["surface"],
            foreground=colors["muted"],
            font="{Segoe UI} 9",
        )

        style.configure(
            "Corporate.TLabelframe",
            background=colors["surface"],
            bordercolor=colors["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Corporate.TLabelframe.Label",
            background=colors["surface"],
            foreground=colors["primary"],
            font="{Segoe UI} 10 bold",
        )

        style.configure(
            "TEntry",
            fieldbackground="#FFFFFF",
            foreground=colors["text"],
            insertcolor=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            padding=6,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#FFFFFF",
            background="#FFFFFF",
            foreground=colors["text"],
            bordercolor=colors["border"],
            arrowcolor=colors["primary"],
            padding=5,
        )
        style.configure(
            "TSpinbox",
            fieldbackground="#FFFFFF",
            foreground=colors["text"],
            bordercolor=colors["border"],
            arrowsize=13,
            padding=5,
        )
        style.configure(
            "TCheckbutton",
            background=colors["surface"],
            foreground=colors["text"],
            font="{Segoe UI} 10",
        )

        style.configure(
            "Primary.TButton",
            background=colors["primary"],
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
            padding=(16, 9),
            font="{Segoe UI} 10 bold",
        )
        style.map(
            "Primary.TButton",
            background=[("active", colors["primary_dark"]), ("disabled", colors["disabled"])],
            foreground=[("disabled", "#F4F6F8")],
        )

        style.configure(
            "Secondary.TButton",
            background=colors["surface_alt"],
            foreground=colors["primary"],
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
            font="{Segoe UI} 10 bold",
        )
        style.map(
            "Secondary.TButton",
            background=[("active", colors["border"]), ("disabled", colors["surface_alt"])],
            foreground=[("disabled", colors["disabled"])],
        )

        style.configure(
            "Danger.TButton",
            background=colors["danger"],
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
            font="{Segoe UI} 10 bold",
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#812D2D"), ("disabled", colors["disabled"])],
            foreground=[("disabled", "#F4F6F8")],
        )

        style.configure(
            "Corporate.Horizontal.TProgressbar",
            background=colors["accent"],
            troughcolor=colors["surface_alt"],
            bordercolor=colors["surface_alt"],
            lightcolor=colors["accent"],
            darkcolor=colors["accent"],
            thickness=14,
        )

    def create_widgets(self) -> None:
        root = ttk.Frame(self, padding=18, style="TFrame")
        root.pack(fill="both", expand=True)

        self.create_header(root)
        self.create_path_section(root)
        self.create_config_section(root)
        self.create_action_section(root)
        self.create_progress_section(root)
        self.create_log_section(root)
        self.create_footer(root)

    def create_header(self, parent: ttk.Frame) -> None:
        colors = CORPORATE_COLORS
        header = ttk.Frame(parent, padding=(18, 16), style="Header.TFrame")
        header.pack(fill="x", pady=(0, 14))

        logo = tk.Label(
            header,
            text="MC",
            bg=colors["accent"],
            fg="#FFFFFF",
            font="{Segoe UI} 16 bold",
            width=4,
            height=2,
            bd=0,
        )
        logo.pack(side="left", padx=(0, 14))

        text_box = ttk.Frame(header, style="Header.TFrame")
        text_box.pack(side="left", fill="x", expand=True)

        ttk.Label(
            text_box,
            text="MarkerCut Corporate Exporter",
            style="HeaderTitle.TLabel",
        ).pack(anchor="w")

        ttk.Label(
            text_box,
            text="Exportación de clips por marcadores",
            style="HeaderSubtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        right_box = ttk.Frame(header, style="Header.TFrame")
        right_box.pack(side="right", padx=(12, 0))

        badge = tk.Label(
            right_box,
            text="PRODUCTION READY",
            bg=colors["primary_dark"],
            fg="#FFFFFF",
            font="{Segoe UI} 9 bold",
            padx=12,
            pady=7,
            bd=0,
        )
        badge.pack(anchor="e")

        owner = tk.Label(
            right_box,
            text=f"Desarrollado por {DEVELOPER_NAME}",
            bg=colors["primary"],
            fg="#D9E8F5",
            font="{Segoe UI} 9",
            bd=0,
        )
        owner.pack(anchor="e", pady=(8, 0))

        about_btn = tk.Button(
            right_box,
            text="Acerca de",
            command=self.show_about_dialog,
            bg=colors["accent"],
            fg="#FFFFFF",
            activebackground=colors["primary_dark"],
            activeforeground="#FFFFFF",
            font="{Segoe UI} 9 bold",
            padx=10,
            pady=4,
            bd=0,
            relief="flat",
            cursor="hand2",
        )
        about_btn.pack(anchor="e", pady=(8, 0))

    def create_path_section(self, parent: ttk.Frame) -> None:
        paths = ttk.LabelFrame(parent, text="Origen y destino", style="Corporate.TLabelframe", padding=(14, 10))
        paths.pack(fill="x", pady=(0, 12))

        ttk.Label(
            paths,
            text="Selecciona el CSV de marcadores, el video maestro y la carpeta donde se generarán los clips.",
            style="SectionHint.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))

        self.add_path_row(paths, "CSV de marcadores", self.csv_var, self.select_csv, 1)
        self.add_path_row(paths, "Video maestro", self.video_var, self.select_video, 2)
        self.add_path_row(paths, "Carpeta de salida", self.output_var, self.select_output, 3)

    def create_config_section(self, parent: ttk.Frame) -> None:
        config = ttk.LabelFrame(parent, text="Parámetros de exportación", style="Corporate.TLabelframe", padding=(14, 10))
        config.pack(fill="x", pady=(0, 12))

        ttk.Label(
            config,
            text="Define rendimiento, calidad, nombres de archivo y precisión de corte.",
            style="SectionHint.TLabel",
        ).grid(row=0, column=0, columnspan=6, sticky="w", padx=4, pady=(0, 8))

        ttk.Label(config, text="FPS").grid(row=1, column=0, sticky="w", padx=6, pady=7)
        ttk.Entry(config, textvariable=self.fps_var, width=13).grid(row=1, column=1, sticky="ew", padx=6, pady=7)

        ttk.Label(config, text="Motor de codificación").grid(row=1, column=2, sticky="w", padx=6, pady=7)
        ttk.Combobox(
            config,
            textvariable=self.backend_var,
            values=["ultra", "auto", "nvenc", "qsv", "x264", "hibrido"],
            state="readonly",
            width=14,
        ).grid(row=1, column=3, sticky="ew", padx=6, pady=7)

        ttk.Label(config, text="Calidad CRF/CQ").grid(row=1, column=4, sticky="w", padx=6, pady=7)
        ttk.Spinbox(config, from_=15, to=30, textvariable=self.quality_var, width=9).grid(row=1, column=5, sticky="ew", padx=6, pady=7)

        ttk.Label(config, text="Procesos paralelos").grid(row=2, column=0, sticky="w", padx=6, pady=7)
        ttk.Spinbox(config, from_=1, to=16, textvariable=self.jobs_var, width=9).grid(row=2, column=1, sticky="ew", padx=6, pady=7)

        ttk.Label(config, text="Prefijo de archivo").grid(row=2, column=2, sticky="w", padx=6, pady=7)
        ttk.Entry(config, textvariable=self.prefix_var, width=16).grid(row=2, column=3, sticky="ew", padx=6, pady=7)

        ttk.Label(config, text="Formato de salida").grid(row=2, column=4, sticky="w", padx=6, pady=7)
        ttk.Combobox(
            config,
            textvariable=self.ext_var,
            values=["mp4", "mov", "m4v"],
            state="readonly",
            width=9,
        ).grid(row=2, column=5, sticky="ew", padx=6, pady=7)

        ttk.Checkbutton(
            config,
            text="Usar doble seek para cortes más precisos",
            variable=self.double_seek_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(8, 2))

        for col in range(6):
            config.columnconfigure(col, weight=1 if col in {1, 3, 5} else 0)

    def create_action_section(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent, style="Actions.TFrame")
        actions.pack(fill="x", pady=(0, 12))

        self.start_btn = ttk.Button(
            actions,
            text="Ejecutar exportación",
            command=self.start_export,
            style="Primary.TButton",
        )
        self.start_btn.pack(side="left")

        self.cancel_btn = ttk.Button(
            actions,
            text="Detener proceso",
            command=self.cancel_export,
            state="disabled",
            style="Danger.TButton",
        )
        self.cancel_btn.pack(side="left", padx=8)

        self.open_btn = ttk.Button(
            actions,
            text="Abrir destino",
            command=self.open_output_folder,
            style="Secondary.TButton",
        )
        self.open_btn.pack(side="left")

        self.new_btn = ttk.Button(
            actions,
            text="Nueva exportación",
            command=self.prepare_new_export,
            style="Secondary.TButton",
        )
        self.new_btn.pack(side="left", padx=8)

    def create_progress_section(self, parent: ttk.Frame) -> None:
        progress_box = ttk.LabelFrame(parent, text="Estado operativo", style="Corporate.TLabelframe", padding=(14, 10))
        progress_box.pack(fill="x", pady=(0, 12))

        self.status_var = tk.StringVar(value="Listo para iniciar.")
        ttk.Label(progress_box, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w", padx=4, pady=(0, 8))

        self.progress = ttk.Progressbar(
            progress_box,
            mode="determinate",
            maximum=100,
            style="Corporate.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", padx=4, pady=(0, 6))

        self.eta_var = tk.StringVar(value="Tiempo restante estimado: -- | Transcurrido: -- | Avance: 0.0%")
        ttk.Label(progress_box, textvariable=self.eta_var, style="Eta.TLabel").pack(anchor="w", padx=4, pady=(0, 2))

    def create_log_section(self, parent: ttk.Frame) -> None:
        colors = CORPORATE_COLORS
        log_box = ttk.LabelFrame(parent, text="Bitácora de ejecución", style="Corporate.TLabelframe", padding=(14, 10))
        log_box.pack(fill="both", expand=True)

        ttk.Label(
            log_box,
            text="Registro técnico de eventos, backends utilizados, archivos generados y errores de FFmpeg.",
            style="SectionHint.TLabel",
        ).pack(anchor="w", padx=4, pady=(0, 8))

        log_frame = ttk.Frame(log_box, style="Surface.TFrame")
        log_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.log_text = tk.Text(
            log_frame,
            height=14,
            wrap="word",
            bg=colors["surface"],
            fg=colors["text"],
            insertbackground=colors["text"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=10,
            font=("Consolas", 9),
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

    def add_path_row(self, parent: ttk.LabelFrame, label: str, var: tk.StringVar, command, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=6)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(parent, text="Explorar", command=command, style="Secondary.TButton").grid(row=row, column=2, sticky="e", padx=4, pady=6)
        parent.columnconfigure(1, weight=1)

    def create_footer(self, parent: ttk.Frame) -> None:
        colors = CORPORATE_COLORS
        footer = ttk.Frame(parent, style="TFrame")
        footer.pack(fill="x", pady=(10, 0))

        left = tk.Label(
            footer,
            text=f"{APP_COPYRIGHT} · Desarrollado por {DEVELOPER_NAME}",
            bg=colors["bg"],
            fg=colors["muted"],
            font="{Segoe UI} 9",
            bd=0,
        )
        left.pack(side="left")

        repo = tk.Label(
            footer,
            text="Repositorio oficial",
            bg=colors["bg"],
            fg=colors["accent"],
            font="{Segoe UI} 9 underline",
            cursor="hand2",
            bd=0,
        )
        repo.pack(side="right")
        repo.bind("<Button-1>", lambda _event: self.open_url(PROJECT_REPOSITORY))

        github = tk.Label(
            footer,
            text="Perfil GitHub",
            bg=colors["bg"],
            fg=colors["accent"],
            font="{Segoe UI} 9 underline",
            cursor="hand2",
            bd=0,
        )
        github.pack(side="right", padx=(0, 16))
        github.bind("<Button-1>", lambda _event: self.open_url(DEVELOPER_GITHUB))

    def open_url(self, url: str) -> None:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            messagebox.showwarning("No se pudo abrir el enlace", url)

    def show_about_dialog(self) -> None:
        colors = CORPORATE_COLORS
        dialog = tk.Toplevel(self)
        dialog.title("Acerca de MarkerCut")
        dialog.configure(bg=colors["surface"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=colors["surface"], padx=24, pady=22)
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text=APP_TITLE,
            bg=colors["surface"],
            fg=colors["primary"],
            font="{Segoe UI} 16 bold",
        ).pack(anchor="w")

        tk.Label(
            container,
            text="Exportador de clips por marcadores",
            bg=colors["surface"],
            fg=colors["text"],
            font="{Segoe UI} 10",
        ).pack(anchor="w", pady=(6, 14))

        tk.Label(
            container,
            text=f"Desarrollado por {DEVELOPER_NAME}",
            bg=colors["surface"],
            fg=colors["text"],
            font="{Segoe UI} 10 bold",
        ).pack(anchor="w")

        dev_link = tk.Label(
            container,
            text=DEVELOPER_GITHUB,
            bg=colors["surface"],
            fg=colors["accent"],
            font="{Segoe UI} 10 underline",
            cursor="hand2",
        )
        dev_link.pack(anchor="w", pady=(4, 10))
        dev_link.bind("<Button-1>", lambda _event: self.open_url(DEVELOPER_GITHUB))

        tk.Label(
            container,
            text="Repositorio oficial",
            bg=colors["surface"],
            fg=colors["text"],
            font="{Segoe UI} 10 bold",
        ).pack(anchor="w")

        repo_link = tk.Label(
            container,
            text=PROJECT_REPOSITORY,
            bg=colors["surface"],
            fg=colors["accent"],
            font="{Segoe UI} 10 underline",
            cursor="hand2",
        )
        repo_link.pack(anchor="w", pady=(4, 16))
        repo_link.bind("<Button-1>", lambda _event: self.open_url(PROJECT_REPOSITORY))

        tk.Button(
            container,
            text="Cerrar",
            command=dialog.destroy,
            bg=colors["primary"],
            fg="#FFFFFF",
            activebackground=colors["primary_dark"],
            activeforeground="#FFFFFF",
            font="{Segoe UI} 10 bold",
            padx=18,
            pady=7,
            bd=0,
            relief="flat",
            cursor="hand2",
        ).pack(anchor="e")

        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() // 2) - (width // 2)
        y = self.winfo_rooty() + (self.winfo_height() // 2) - (height // 2)
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    def select_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleccionar CSV de marcadores",
            filetypes=[("CSV", "*.csv"), ("Todos los archivos", "*.*")],
        )
        if path:
            self.csv_var.set(path)

    def select_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleccionar video maestro",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.m4v *.avi"),
                ("Todos los archivos", "*.*"),
            ],
        )
        if path:
            self.video_var.set(path)

    def select_output(self) -> None:
        path = filedialog.askdirectory(title="Seleccionar carpeta de salida")
        if path:
            self.output_var.set(path)

    def validate_inputs(self) -> bool:
        csv_path = Path(self.csv_var.get().strip())
        video_path = Path(self.video_var.get().strip())
        output_dir = self.output_var.get().strip()

        if not ffmpeg_available():
            messagebox.showerror("Dependencia no encontrada", "FFmpeg no está disponible en el PATH del sistema.")
            return False

        if not csv_path.exists():
            messagebox.showerror("CSV no válido", "Selecciona un archivo CSV existente.")
            return False

        if not video_path.exists():
            messagebox.showerror("Video no válido", "Selecciona un video maestro existente.")
            return False

        if not output_dir:
            messagebox.showerror("Destino no definido", "Selecciona una carpeta de salida.")
            return False

        try:
            quality = int(self.quality_var.get())
            if not 15 <= quality <= 30:
                raise ValueError()
        except Exception:
            messagebox.showerror("Calidad fuera de rango", "La calidad debe estar entre 15 y 30.")
            return False

        try:
            jobs = int(self.jobs_var.get())
            if not 1 <= jobs <= 16:
                raise ValueError()
        except Exception:
            messagebox.showerror("Paralelismo fuera de rango", "Los procesos paralelos deben estar entre 1 y 16.")
            return False

        return True

    def parse_fps_setting(self, video_path: str) -> float:
        raw = self.fps_var.get().strip().lower()

        if raw in {"", "auto", "automatico", "automático"}:
            fps = detect_fps(video_path)
            if fps <= 0:
                raise ValueError("No fue posible detectar el FPS. Ingresa un valor manual, por ejemplo 29.97.")
            return fps

        fps = float(raw.replace(",", "."))
        if fps <= 0:
            raise ValueError("El FPS debe ser mayor que 0.")

        return normalize_fps(fps)

    def prepare_new_export(self, clear_log: bool = True) -> None:
        """Deja la interfaz lista para una exportación nueva sin cerrar la aplicación."""
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo(
                "Exportación activa",
                "Hay una exportación en curso. Detén el proceso antes de preparar una nueva.",
            )
            return

        self.stop_event.clear()

        with self.processes_lock:
            self.processes.clear()

        # Descarta eventos antiguos que hayan quedado en cola después de terminar o cancelar.
        self.progress_queue = queue.Queue()

        self.worker_thread = None
        self.segment_progress.clear()
        self.total_segments = 0
        self.completed_segments = 0
        self.error_segments = 0
        self.final_state_received = False
        self.last_output_folder = ""
        self.export_started_at = None
        self.last_percent = 0.0

        self.progress["value"] = 0
        self.status_var.set("Listo para iniciar una nueva exportación.")
        self.eta_var.set("Tiempo restante estimado: -- | Transcurrido: -- | Avance: 0.0%")

        if clear_log:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

        self.start_btn.configure(state="normal", text="Ejecutar exportación")
        self.cancel_btn.configure(state="disabled")
        self.new_btn.configure(state="normal")

    def set_ready_for_next_export(self) -> None:
        """Libera controles y estado interno al terminar, dejando visible el resumen final."""
        self.stop_event.clear()

        with self.processes_lock:
            self.processes.clear()

        self.final_state_received = True
        self.worker_thread = None
        self.start_btn.configure(state="normal", text="Ejecutar nueva exportación")
        self.cancel_btn.configure(state="disabled")
        self.new_btn.configure(state="normal")

    def start_export(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return

        if not self.validate_inputs():
            return

        csv_path = self.csv_var.get().strip()
        video_path = self.video_var.get().strip()
        output_dir = self.output_var.get().strip()

        try:
            fps = self.parse_fps_setting(video_path)
        except Exception as exc:
            messagebox.showerror("FPS no válido", str(exc))
            return

        self.stop_event.clear()
        self.progress_queue = queue.Queue()
        self.segment_progress.clear()
        self.total_segments = 0
        self.completed_segments = 0
        self.error_segments = 0
        self.final_state_received = False
        self.last_output_folder = output_dir
        self.export_started_at = time.monotonic()
        self.last_percent = 0.0
        self.progress["value"] = 0
        self.status_var.set("Inicializando exportación...")
        self.eta_var.set("Tiempo restante estimado: calculando... | Transcurrido: 00:00 | Avance: 0.0%")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.start_btn.configure(state="disabled", text="Exportación en curso")
        self.cancel_btn.configure(state="normal")
        self.new_btn.configure(state="disabled")

        self.worker_thread = threading.Thread(
            target=self.worker_entry,
            args=(csv_path, video_path, output_dir, fps),
            daemon=True,
        )
        self.worker_thread.start()

    def worker_entry(self, csv_path: str, video_path: str, output_dir: str, fps: float) -> None:
        try:
            export_all_segments(
                csv_path=csv_path,
                input_video=video_path,
                output_dir=output_dir,
                requested_backend=self.backend_var.get(),
                fps=fps,
                quality=int(self.quality_var.get()),
                parallel_jobs=int(self.jobs_var.get()),
                prefix=self.prefix_var.get().strip() or DEFAULT_PREFIX,
                ext=self.ext_var.get().strip() or DEFAULT_EXT,
                use_double_seek=bool(self.double_seek_var.get()),
                progress_queue=self.progress_queue,
                stop_event=self.stop_event,
                processes=self.processes,
                processes_lock=self.processes_lock,
            )
        except Exception as exc:
            self.progress_queue.put(("fatal_error", str(exc)))

    def cancel_export(self) -> None:
        self.stop_event.set()
        self.status_var.set("Deteniendo procesos activos...")
        self.eta_var.set("Tiempo restante estimado: detenido por el usuario")
        self.new_btn.configure(state="disabled")

        with self.processes_lock:
            running = list(self.processes.values())

        for proc in running:
            terminate_process(proc)

    def open_output_folder(self) -> None:
        path = self.output_var.get().strip()
        if not path:
            return

        folder = Path(path)
        if not folder.exists():
            messagebox.showwarning("Destino no encontrado", "La carpeta de salida todavía no existe.")
            return

        if os.name == "nt":
            os.startfile(folder)
        elif shutil.which("open"):
            subprocess.Popen(["open", str(folder)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(folder)])

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def update_overall_progress(self) -> None:
        if self.total_segments <= 0:
            self.progress["value"] = 0
            self.last_percent = 0.0
            self.eta_var.set("Tiempo restante estimado: -- | Transcurrido: -- | Avance: 0.0%")
            return

        total = sum(self.segment_progress.get(i, 0.0) for i in range(self.total_segments))
        overall_fraction = max(0.0, min(1.0, total / self.total_segments))
        percent = overall_fraction * 100
        self.last_percent = percent
        self.progress["value"] = percent

        if self.export_started_at is None:
            self.export_started_at = time.monotonic()

        elapsed = max(0.0, time.monotonic() - self.export_started_at)

        if overall_fraction >= 0.999:
            remaining_text = "00:00"
        elif overall_fraction >= 0.005 and elapsed >= 2.0:
            estimated_total = elapsed / overall_fraction
            remaining = max(0.0, estimated_total - elapsed)
            remaining_text = format_duration_compact(remaining)
        else:
            remaining_text = "calculando..."

        elapsed_text = format_duration_compact(elapsed)
        self.status_var.set(f"Avance general: {percent:.1f}% | Falta aprox.: {remaining_text}")
        self.eta_var.set(
            f"Tiempo restante estimado: {remaining_text} | "
            f"Transcurrido: {elapsed_text} | "
            f"Avance: {percent:.1f}%"
        )

    def force_finish_if_all_segments_closed(self) -> None:
        """Cierra la UI aunque el evento final se retrase, siempre que no queden procesos FFmpeg activos."""
        if self.final_state_received or self.total_segments <= 0:
            return

        accounted = self.completed_segments + self.error_segments
        all_progress_closed = (
            len(self.segment_progress) == self.total_segments
            and all(value >= 0.999 for value in self.segment_progress.values())
        )

        with self.processes_lock:
            running_processes = [proc for proc in self.processes.values() if proc.poll() is None]

        if accounted >= self.total_segments and all_progress_closed and not running_processes:
            self.progress["value"] = 100
            elapsed = format_duration_compact(time.monotonic() - self.export_started_at) if self.export_started_at else "--"
            self.status_var.set(
                f"Proceso finalizado. Correctos: {self.completed_segments} | "
                f"Errores: {self.error_segments} | Listo para nueva exportación."
            )
            self.eta_var.set(f"Tiempo restante estimado: 00:00 | Transcurrido: {elapsed} | Avance: 100.0%")
            if self.last_output_folder:
                self.log(f"Destino de archivos: {self.last_output_folder}")
            self.log("Cierre confirmado por verificación de segmentos. Sistema listo para iniciar una nueva exportación.")
            self.set_ready_for_next_export()

    def poll_queue(self) -> None:
        try:
            while True:
                event = self.progress_queue.get_nowait()
                kind = event[0]

                if kind == "log":
                    self.log(event[1])

                elif kind == "segments_ready":
                    self.total_segments = int(event[1])
                    self.segment_progress = {i: 0.0 for i in range(self.total_segments)}
                    self.export_started_at = time.monotonic()
                    self.last_percent = 0.0
                    self.status_var.set(f"Segmentos preparados: {self.total_segments}")
                    self.eta_var.set("Tiempo restante estimado: calculando... | Transcurrido: 00:00 | Avance: 0.0%")

                elif kind == "backend":
                    self.log(f"Pool confirmado: {event[1]}")

                elif kind == "segment_progress":
                    _, idx, fraction = event
                    self.segment_progress[int(idx)] = float(fraction)
                    self.update_overall_progress()

                elif kind == "segment_done":
                    _, idx, filename, completed = event
                    self.completed_segments = max(self.completed_segments, int(completed))
                    self.segment_progress[int(idx)] = 1.0
                    self.log(f"Exportación completada: {filename}")
                    self.status_var.set(f"Completados: {self.completed_segments}/{self.total_segments}")
                    self.update_overall_progress()

                elif kind == "segment_error":
                    _, idx, msg = event
                    self.error_segments += 1
                    self.segment_progress[int(idx)] = 1.0
                    self.log(msg)
                    self.update_overall_progress()

                elif kind == "finished":
                    _, completed, errors, folder = event
                    self.completed_segments = int(completed)
                    self.error_segments = int(errors)
                    self.last_output_folder = str(folder)
                    self.progress["value"] = 100
                    elapsed = format_duration_compact(time.monotonic() - self.export_started_at) if self.export_started_at else "--"
                    self.status_var.set(
                        f"Proceso finalizado. Correctos: {completed} | Errores: {errors} | Listo para nueva exportación."
                    )
                    self.eta_var.set(f"Tiempo restante estimado: 00:00 | Transcurrido: {elapsed} | Avance: 100.0%")
                    self.log(f"Destino de archivos: {folder}")
                    self.log("Sistema listo para iniciar una nueva exportación.")
                    self.set_ready_for_next_export()

                elif kind == "cancelled":
                    _, completed, errors = event
                    self.completed_segments = int(completed)
                    self.error_segments = int(errors)
                    elapsed = format_duration_compact(time.monotonic() - self.export_started_at) if self.export_started_at else "--"
                    self.status_var.set(
                        f"Proceso detenido. Correctos: {completed} | Errores: {errors} | Listo para nueva exportación."
                    )
                    self.eta_var.set(f"Tiempo restante estimado: detenido | Transcurrido: {elapsed} | Avance: {self.last_percent:.1f}%")
                    self.log("Sistema listo para iniciar una nueva exportación.")
                    self.set_ready_for_next_export()

                elif kind == "fatal_error":
                    self.status_var.set("Error crítico. Listo para corregir datos e iniciar una nueva exportación.")
                    self.eta_var.set(f"Tiempo restante estimado: no disponible | Avance: {self.last_percent:.1f}%")
                    self.log(f"ERROR: {event[1]}")
                    self.log("Sistema liberado. Corrige la configuración y ejecuta una nueva exportación.")
                    messagebox.showerror("Error", event[1])
                    self.set_ready_for_next_export()

        except queue.Empty:
            pass

        self.force_finish_if_all_segments_closed()
        self.after(150, self.poll_queue)

def main() -> None:
    app = MarkerExporterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
