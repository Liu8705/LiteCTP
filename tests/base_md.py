#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LiteCTP 行情连接与订阅示例，适用于 Python 3.10。

先填写文件顶部的连接配置，再运行：python md.py
PASSWORD 留空时，密码在启动时输入，也可以通过环境变量 CTP_PASSWORD 提供。
终端每隔 0.5 秒显示各合约最新行情；中间 tick 会被合并，不是逐 tick 记录器。
"""

import getpass
import math
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

import LiteCTP as ctp

class MdSpi(ctp.CThostFtdcMdSpi):
    """登录后订阅指定合约，断线重连后重新登录和订阅。"""

    def __init__(self, front, broker_id, user_id, password, instruments, flow_dir=None):
        super().__init__()
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self.instruments = list(dict.fromkeys(instruments))
        if not self.instruments:
            raise ValueError("至少需要一个合约代码。")
        self.flow_dir = flow_dir
        self.api = None
        self.connected = False
        self.loggedin = False
        self.subscribed = False
        self.ready = threading.Event()
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._messages = queue.SimpleQueue()
        self._latest = {}
        self._pending = {}
        self._confirmed = set()
        self._error = None
        self._request_id = 0
        self._login_request_id = None
        self._temporary_dir = None

    def start(self):
        if self.api is not None or self._closing.is_set():
            raise RuntimeError("请为每次连接创建新的 MdSpi 实例。")
        if self.flow_dir is None:
            self._temporary_dir = tempfile.TemporaryDirectory(prefix="litectp-md-")
            directory = Path(self._temporary_dir.name)
        else:
            directory = Path(self.flow_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
        # 每个 API 实例应使用独立的流文件目录。
        self.api = ctp.CThostFtdcMdApi.CreateFtdcMdApi(str(directory) + os.sep)
        if self.api is None:
            raise RuntimeError("创建行情 API 失败。")
        self.api.RegisterSpi(self)
        self.api.RegisterFront(self.front)
        self.api.Init()
        # API 自行运行回调线程；主线程保持运行即可，不在这里调用 Join()。

    def _message(self, text):
        self._messages.put(text)

    def _fail(self, text):
        with self._lock:
            if self._error is None:
                self._error = text
        self.ready.clear()

    def raise_if_failed(self):
        with self._lock:
            error = self._error
        if error:
            raise RuntimeError(error)

    @staticmethod
    def _response_error(info):
        # CTP 回调中的指针参数可能为空，不能直接访问其字段。
        if info is None:
            return 0, ""
        return info.ErrorID, info.ErrorMsg

    def _check_request(self, name, ret):
        if ret == 0:
            return True
        reasons = {-1: "网络连接失败", -2: "未处理请求过多", -3: "发送请求过于频繁"}
        self._fail(f"{name}提交失败：ret={ret}，{reasons.get(ret, '请查阅 API 返回码')}。")
        return False

    def OnFrontConnected(self):
        if self._closing.is_set():
            return
        self.connected = True
        self.loggedin = self.subscribed = False
        self.ready.clear()
        self._confirmed.clear()
        self._message("行情服务器已连接，正在登录。")
        request = ctp.CThostFtdcReqUserLoginField()
        request.BrokerID = self.broker_id
        request.UserID = self.user_id
        request.Password = self.password
        self._request_id += 1
        self._login_request_id = self._request_id
        self._check_request("登录请求", self.api.ReqUserLogin(request, self._request_id))

    def OnFrontDisconnected(self, nReason):
        if self._closing.is_set():
            return
        self.connected = self.loggedin = self.subscribed = False
        self._login_request_id = None
        self._confirmed.clear()
        self.ready.clear()
        with self._lock:
            self._latest.clear()
            self._pending.clear()
        self._message(f"行情连接断开：reason={nReason}，等待 API 重连。")

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        if self._closing.is_set() or nRequestID != self._login_request_id:
            return
        error_id, error_msg = self._response_error(pRspInfo)
        if error_id:
            self._fail(f"登录失败：ErrorID={error_id}，{error_msg}")
            return
        if not bIsLast or self.loggedin:
            return
        if pRspUserLogin is None:
            self._fail("登录回报缺少登录信息。")
            return
        self.loggedin = True
        self._message(f"登录成功，交易日：{pRspUserLogin.TradingDay}。")
        self._message("正在订阅：" + ", ".join(self.instruments))
        # ctp.i 中的 typemap 自动处理 Python 字符串列表和数量参数。
        self._check_request("行情订阅请求", self.api.SubscribeMarketData(self.instruments))

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        if self._closing.is_set() or not self.loggedin:
            return
        error_id, error_msg = self._response_error(pRspInfo)
        instrument = pSpecificInstrument.InstrumentID if pSpecificInstrument is not None else ""
        if error_id:
            self._fail(f"订阅失败 [{instrument or '未知合约'}]：ErrorID={error_id}，{error_msg}")
            return
        if instrument:
            self._confirmed.add(instrument)
            self._message(f"订阅确认：{instrument}")
        if set(self.instruments).issubset(self._confirmed):
            with self._lock:
                if self._error is not None:
                    return
            self.subscribed = True
            self.ready.set()
        elif bIsLast:
            missing = sorted(set(self.instruments) - self._confirmed)
            self._fail("订阅响应结束，但以下合约未确认：" + ", ".join(missing))

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        if self._closing.is_set():
            return
        error_id, error_msg = self._response_error(pRspInfo)
        if error_id:
            self._fail(f"行情接口错误：request_id={nRequestID}，ErrorID={error_id}，{error_msg}")

    def OnRtnDepthMarketData(self, pDepthMarketData):
        if self._closing.is_set() or pDepthMarketData is None or not self.loggedin:
            return
        data = pDepthMarketData
        # 复制需要的字段，主线程使用普通 Python 数据，不持有回调指针。
        tick = {name: getattr(data, name) for name in (
            "InstrumentID", "ExchangeID", "TradingDay", "ActionDay", "UpdateTime",
            "UpdateMillisec", "LastPrice", "Volume", "OpenInterest", "BidPrice1",
            "BidVolume1", "AskPrice1", "AskVolume1",
        )}
        tick["received_ns"] = time.perf_counter_ns()
        with self._lock:
            self._latest[tick["InstrumentID"]] = tick
            self._pending[tick["InstrumentID"]] = tick

    def get_latest(self, instrument):
        """返回最新行情字典的副本；断线后清空，尚无行情时返回 None。"""
        with self._lock:
            tick = self._latest.get(instrument)
            return dict(tick) if tick is not None else None

    def drain_ticks(self):
        """取走本轮显示的最新行情；同一合约的中间 tick 已合并。"""
        with self._lock:
            ticks = list(self._pending.values())
            self._pending.clear()
        return [dict(tick) for tick in ticks]

    def print_messages(self):
        while True:
            try:
                message = self._messages.get_nowait()
            except queue.Empty:
                break
            print(message, flush=True)

    def wait_ready(self, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            self.print_messages()
            self.raise_if_failed()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("连接、登录或订阅超时，请检查行情地址、账号和合约代码。")
            if self.ready.wait(min(remaining, 0.2)):
                self.raise_if_failed()
                return

    def close(self):
        """由主线程调用；重复关闭不会再次释放 API。"""
        if self._closing.is_set():
            return
        self._closing.set()
        self.ready.clear()
        api, self.api = self.api, None
        try:
            if api is not None:
                try:
                    api.RegisterSpi(None)
                finally:
                    api.Release()
        finally:
            self.connected = self.loggedin = self.subscribed = False
            with self._lock:
                self._latest.clear()
                self._pending.clear()
        if self._temporary_dir is not None:
            self._temporary_dir.cleanup()
            self._temporary_dir = None


def format_price(value):
    if not math.isfinite(value) or value == sys.float_info.max:
        return "-"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def print_tick(tick):
    print(
        f"{tick['InstrumentID']} {tick['UpdateTime']}.{tick['UpdateMillisec']:03d} "
        f"最新={format_price(tick['LastPrice'])} "
        f"买一={format_price(tick['BidPrice1'])}({tick['BidVolume1']}) "
        f"卖一={format_price(tick['AskPrice1'])}({tick['AskVolume1']}) "
        f"累计成交量={tick['Volume']} 持仓量={format_price(tick['OpenInterest'])}",
        flush=True,
    )

# ===== 在这里填写你的连接信息，不需要命令行参数 =====
FRONT = ""             # 行情前置地址，例如 "tcp://行情服务器IP:端口"
BROKER_ID = ""         # 经纪商代码，使用字符串
USER_ID = ""           # 登录账号，使用字符串
PASSWORD = ""          # 留空则启动时输入；也可以在此填写密码
INSTRUMENTS = []        # 合约代码列表，例如 ["合约代码1", "合约代码2"]
TIMEOUT = 20            # 连接、登录和订阅的启动超时，单位：秒
PRINT_INTERVAL = 0.5    # 行情显示间隔，单位：秒；同一合约的中间 tick 会合并
FLOW_DIR = None         # 默认使用临时目录；也可以指定独立的流文件目录
def main():
    spi = None
    stop = threading.Event()
    try:
        if not isinstance(FRONT, str) or not FRONT.startswith("tcp://"):
            raise ValueError("请填写文件顶部的 FRONT，格式为 tcp://IP:端口，使用行情前置地址。")
        if not all(isinstance(value, str) and value.strip() for value in (BROKER_ID, USER_ID)):
            raise ValueError("请填写文件顶部的 BROKER_ID 和 USER_ID，二者都应为字符串。")
        if (not isinstance(INSTRUMENTS, (list, tuple)) or not INSTRUMENTS
                or not all(isinstance(item, str) and item.strip() for item in INSTRUMENTS)):
            raise ValueError('请填写 INSTRUMENTS，例如 ["合约代码1", "合约代码2"]。')
        if not isinstance(TIMEOUT, (int, float)) or not math.isfinite(TIMEOUT) or TIMEOUT <= 0:
            raise ValueError("TIMEOUT 必须是有限正数。")
        if (not isinstance(PRINT_INTERVAL, (int, float))
                or not math.isfinite(PRINT_INTERVAL) or PRINT_INTERVAL < 0.01):
            raise ValueError("PRINT_INTERVAL 必须是至少 0.01 的有限数。")
        password = PASSWORD or os.environ.get("CTP_PASSWORD") or getpass.getpass("请输入行情登录密码：")
        if not password:
            raise ValueError("密码不能为空。")
        spi = MdSpi(FRONT, BROKER_ID, USER_ID, password, INSTRUMENTS, FLOW_DIR)
        spi.start()
        spi.wait_ready(TIMEOUT)
        spi.print_messages()
        print("订阅已确认，等待行情；按 Ctrl+C 退出。", flush=True)
        next_display = time.monotonic()
        while not stop.wait(min(PRINT_INTERVAL, 0.2)):
            spi.print_messages()
            spi.raise_if_failed()
            now = time.monotonic()
            if now >= next_display:
                for tick in spi.drain_ticks():
                    print_tick(tick)
                next_display = now + PRINT_INTERVAL
    except KeyboardInterrupt:
        print("\n正在退出……", flush=True)
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if spi is not None:
            spi.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
