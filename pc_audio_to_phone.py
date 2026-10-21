#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把电脑正在播放的声音，低延迟转发到手机浏览器播放。

链路：
    WASAPI 回环采集 -> float32 -> int16 裸 PCM
      -> WebSocket(TCP_NODELAY) -> 手机浏览器 Web Audio 手动排程

刻意不做编码：局域网带宽足够，省掉 Opus 编解码和一层额外缓冲。
抖动缓冲与时钟漂移修正放在网页端，目标缓冲可现场调到 15ms。

依赖：
    pip install soundcard numpy websockets

用法：
    python pc_audio_to_phone.py                 # 采集默认扬声器的回环
    python pc_audio_to_phone.py --list-devices  # 查看音频设备
    python pc_audio_to_phone.py --device 耳机    # 指定输出设备（名称模糊匹配）
    python pc_audio_to_phone.py --synthetic     # 不采集，用测试音验证链路
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import re
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus

try:
    import numpy as np
except ImportError:
    raise SystemExit("缺少 numpy，请先执行：pip install numpy")

try:
    import soundcard as sc
except ImportError:
    raise SystemExit("缺少 soundcard，请先执行：pip install soundcard")

try:
    from websockets.asyncio.server import serve
except ImportError:
    raise SystemExit("缺少 websockets，请先执行：pip install websockets")


DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_PORT = 8720
HERE = __file__.rsplit("\\", 1)[0] if "\\" in __file__ else __file__.rsplit("/", 1)[0]


# --------------------------------------------------------------------------- #
# Windows 调度优化：把采集抖动压到最低
# --------------------------------------------------------------------------- #


def boost_process():
    """提高进程优先级、缩短系统定时器粒度、关闭效率模式限速。"""
    done = []
    if sys.platform != "win32":
        return done
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
        done.append("timer 1ms")
    except Exception:
        pass
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00000080)
        done.append("HIGH_PRIORITY_CLASS")

        class _POWER(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        state = _POWER(1, 0x1, 0x0)
        kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)
        )
        done.append("power-throttling off")
    except Exception:
        pass
    return done


def boost_current_thread():
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 15)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


def list_devices():
    print("播放设备（扬声器）：")
    default = None
    try:
        default = sc.default_speaker()
    except Exception as exc:
        print("   读取失败：%s" % exc)
    for spk in sc.all_speakers():
        mark = "   <- 默认" if default is not None and spk.name == default.name else ""
        print("   %s  (%dch)%s" % (spk.name, spk.channels, mark))
    print("")
    print("可用于采集的回环设备：")
    for mic in sc.all_microphones(include_loopback=True):
        tag = "loopback" if getattr(mic, "isloopback", False) else "input"
        print("   [%s] %s" % (tag, mic.name))


def find_loopback_mic(device):
    mics = sc.all_microphones(include_loopback=True)
    loops = [m for m in mics if getattr(m, "isloopback", False)]
    if device:
        key = device.lower()
        for group in (loops, mics):
            for mic in group:
                if key in mic.name.lower():
                    return mic
        raise SystemExit("找不到名称包含 %r 的设备，用 --list-devices 查看。" % device)
    try:
        speaker = sc.default_speaker()
    except Exception as exc:
        raise SystemExit("读取默认播放设备失败：%s" % exc)
    for mic in loops:
        if mic.name == speaker.name:
            return mic
    if loops:
        print("提示：默认扬声器没有对应回环设备，改用 %s" % loops[0].name)
        return loops[0]
    raise SystemExit("没有找到任何回环(loopback)设备，无法采集系统声音。")


class Capture:
    """后台线程：回环音频 -> int16 裸 PCM -> 回调。"""

    def __init__(self, mic, rate, channels, synthetic=False):
        self.mic = mic
        self.rate = rate
        self.channels = channels
        self.synthetic = synthetic
        self.stop = threading.Event()
        self.started = threading.Event()
        self.blocks = 0
        self.frames = 0
        self.block_history = []
        self.rms = 0.0
        self.error = None
        self._loop = None
        self._publish = None

    def start(self, loop, publish):
        self._loop = loop
        self._publish = publish
        thread = threading.Thread(target=self._guarded_run, name="capture", daemon=True)
        thread.start()
        return thread

    def _guarded_run(self):
        boost_current_thread()
        try:
            self.run()
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.started.set()

    def run(self):
        if self.synthetic:
            self._synthetic_loop()
            return
        with self.mic.recorder(samplerate=self.rate, channels=self.channels) as recorder:
            self.started.set()
            while not self.stop.is_set():
                # numframes=None：驱动给出多少就立刻发多少，不额外攒数据
                block = recorder.record(numframes=None)
                if block is None or len(block) == 0:
                    continue
                self._emit(block)

    def _synthetic_loop(self):
        frames = self.rate // 100
        step = 2.0 * np.pi * 440.0 / self.rate
        index = 0
        self.started.set()
        start = time.perf_counter()
        while not self.stop.is_set():
            t = (np.arange(frames, dtype=np.float64) + index) * step
            wave = 0.05 * np.sin(t)
            index += frames
            self._emit(np.stack([wave, wave], axis=1).astype(np.float32))
            target = start + index / self.rate
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    def _emit(self, block):
        frames = len(block)
        if frames == 0:
            return
        self.blocks += 1
        self.frames += frames
        if len(self.block_history) < 200:
            self.block_history.append(frames)
        else:
            self.block_history[self.blocks % 200] = frames
        self.rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
        pcm = np.clip(block, -1.0, 1.0) * 32767.0
        payload = pcm.astype("<i2").tobytes()
        loop = self._loop
        if loop is not None and self._publish is not None:
            try:
                loop.call_soon_threadsafe(self._publish, payload)
            except RuntimeError:
                pass


class Client:
    __slots__ = ("ws", "out", "dropped", "sent", "peer")

    def __init__(self, ws, peer):
        self.ws = ws
        self.peer = peer
        self.out = asyncio.Queue(maxsize=2)
        self.dropped = 0
        self.sent = 0


class Hub:
    def __init__(self):
        self.clients = set()
        self.dropped = 0

    def publish(self, payload):
        for client in list(self.clients):
            out = client.out
            if out.full():
                try:
                    out.get_nowait()
                    client.dropped += 1
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                out.put_nowait(payload)
            except asyncio.QueueFull:
                pass


HUB = Hub()


async def sender(client):
    while True:
        payload = await client.out.get()
        await client.ws.send(payload)
        client.sent += 1


async def ws_handler(ws, config):
    peer = "?"
    try:
        addr = ws.remote_address
        if addr:
            peer = "%s:%s" % (addr[0], addr[1])
    except Exception:
        pass

    # 关掉 Nagle：小包立即发出，不被合并
    try:
        transport = getattr(ws, "transport", None)
        if transport is None:
            transport = getattr(getattr(ws, "protocol", None), "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

    client = Client(ws, peer)
    task = None
    try:
        await ws.send(json.dumps(config, ensure_ascii=False))
        HUB.clients.add(client)
        print("[+] 手机已连接：%s" % peer)
        task = asyncio.create_task(sender(client))
        async for message in ws:
            if not isinstance(message, str):
                continue
            try:
                data = json.loads(message)
            except ValueError:
                continue
            if data.get("t") == "ping":
                await ws.send(json.dumps({"t": "pong", "ts": data.get("ts")}))
    except Exception as exc:
        print("[-] 连接异常 %s：%s" % (peer, exc))
    finally:
        if task is not None:
            task.cancel()
        HUB.clients.discard(client)
        print("[-] 手机断开：%s（丢块 %d）" % (peer, client.dropped))


def http_handler(conn, request):
    path = request.path
    if path in ("/", "/index.html"):
        response = conn.respond(HTTPStatus.OK, load_page())
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-store"
        return response
    if path == "/favicon.ico":
        return conn.respond(HTTPStatus.NO_CONTENT, "")
    if path == "/ws":
        return None  # 交给 websockets 完成握手升级
    return conn.respond(HTTPStatus.NOT_FOUND, "not found")


async def stats_loop(capture):
    warned = False
    while True:
        await asyncio.sleep(5)
        if capture.error:
            print("[!] 采集已停止：%s" % capture.error)
            return
        history = capture.block_history
        median = sorted(history)[len(history) // 2] if history else 0
        block_ms = median / capture.rate * 1000.0
        dbfs = 20.0 * np.log10(capture.rms) if capture.rms > 1e-9 else -120.0
        sent = sum(c.sent for c in HUB.clients)
        print(
            "[状态] 采集块 %.1fms | 电平 %.1fdBFS | 手机 %d 台 | 本机丢块 %d | 已发送 %d"
            % (block_ms, dbfs, len(HUB.clients), HUB.dropped, sent)
        )
        if not capture.synthetic and not warned and block_ms > 20 and dbfs < -90:
            warned = True
            print("[提示] 当前没有音频在播放，驱动在补 40ms 静音块（正常）；"
                  "开始播放声音后包长会自动回到 10ms。")


def probe_local_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ips.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    if not ips:
        try:
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, errors="ignore", timeout=6
            ).stdout
            ips.extend(re.findall(r"IPv4[^:]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", out))
        except Exception:
            pass
    result = []
    for ip in ips:
        if ip.startswith(("127.", "169.254.")) or ip == "0.0.0.0":
            continue
        if ip not in result:
            result.append(ip)
    return result


def print_banner(port, device_name, rate, channels, boosted, synthetic):
    ips = probe_local_ips()
    print("")
    print("=" * 64)
    print(" 电脑音频 -> 手机   已启动")
    print("=" * 64)
    print(" 采集设备：%s" % device_name)
    print(" 音频格式：%d Hz / %d ch / 16bit 裸 PCM（无编解码）" % (rate, channels))
    print(" 音源    ：%s" % ("测试音(--synthetic)" if synthetic else "系统回环(loopback)"))
    print(" 调度优化：%s" % (", ".join(boosted) if boosted else "未启用"))
    print("")
    print(" 手机浏览器打开下面任意地址（需与电脑同一网络）：")
    if ips:
        for ip in ips:
            print("     http://%s:%d/" % (ip, port))
    else:
        print("     http://<本机IP>:%d/   （未识别到网卡，用 ipconfig 查看）" % port)
    print("")
    print(" 想要最低延迟：用手机的「USB 网络共享」，再打开上面新出现的地址")
    print("（通常是 192.168.42.x / 192.168.137.x 之类，USB 比 WiFi 抖动小很多）")
    print("=" * 64)
    try:
        import qrcode  # 可选：pip install qrcode
    except ImportError:
        return
    if not ips:
        return
    print("")
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data("http://%s:%d/" % (ips[0], port))
        qr.print_ascii(invert=True)
    except Exception:
        pass


async def run_server(args):
    mic = None
    device_name = "测试音发生器"
    channels = args.channels
    if not args.synthetic:
        mic = find_loopback_mic(args.device)
        device_name = mic.name
        if getattr(mic, "channels", channels) < channels:
            channels = max(1, mic.channels)

    capture = Capture(mic, args.rate, channels, args.synthetic)
    config = {
        "type": "config",
        "rate": args.rate,
        "channels": channels,
        "device": device_name,
        "engine": "synthetic" if args.synthetic else "wasapi-loopback+rawpcm",
    }

    loop = asyncio.get_running_loop()
    capture.start(loop, HUB.publish)

    boosted = [] if args.no_boost else boost_process()
    print_banner(args.port, device_name, args.rate, channels, boosted, args.synthetic)

    asyncio.create_task(stats_loop(capture))

    async with serve(
        lambda ws: ws_handler(ws, config),
        args.host,
        args.port,
        process_request=http_handler,
        compression=None,      # 音频不压缩
        ping_interval=None,    # 改用网页端的 JSON ping 测 RTT
        max_queue=4,
        max_size=1 << 20,
    ):
        if not capture.started.wait(timeout=3.0):
            print("[!] 采集线程启动超时")
        if capture.error:
            print("[!] 采集失败：%s" % capture.error)
        try:
            await asyncio.Future()
        finally:
            capture.stop.set()


def load_page():
    path = HERE + "\\index.html"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        return "<h1>读取 index.html 失败</h1><p>%s</p><p>请保持 index.html 与本脚本在同一目录。</p>" % exc


def build_parser():
    parser = argparse.ArgumentParser(description="把电脑音频低延迟转发到手机浏览器播放")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8720）")
    parser.add_argument("--device", help="输出设备名，模糊匹配（如 '耳机' 'Realtek'）")
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE, help="采样率（默认 48000）")
    parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS, help="声道数（默认 2）")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    parser.add_argument("--synthetic", action="store_true", help="不采集音频，发测试音")
    parser.add_argument("--no-boost", action="store_true", help="不做 Windows 调度优化")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass
    if args.list_devices:
        list_devices()
        return
    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        print("")
        print("已退出。")


if __name__ == "__main__":
    main()
