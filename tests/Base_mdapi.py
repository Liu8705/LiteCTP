# -*- coding: utf-8 -*-
"""LiteCTP 行情基类，适用于 Python 3.10；导入时不建立连接。

用法：
    from Base_mdapi import CMdSpiBase

    class MyMd(CMdSpiBase):
        def on_tick(self, tick):
            # tick 是完整的 LiteCTP.CThostFtdcDepthMarketDataField 结构体。
            # 每条收到的 CTP 行情快照都会到这里，不按时间合并。
            # 在这里快速处理，或交给自己的队列；避免耗时计算和磁盘写入。
            pass

    md = MyMd(front, broker_id, user_id, password, instruments=[instrument])
    try:
        md.start()
        md.wait_ready(timeout=20)
        # 主程序保持运行，处理自己的其他任务。
    finally:
        md.close()

说明：
* 配置由构造参数传入，不使用命令行参数解析。
* ready 表示登录和当前目标订阅列表已确认，不代表收到新鲜行情。
* subscribe/unsubscribe 可在登录前调用，也可动态调整目标列表。
  基类串行发送订阅操作；结果通过 ready、status() 和 on_error() 查询。
* API 重连后自动重新登录，并恢复当前目标列表；登录或订阅请求失败
  时通知 on_error，不无限重试。管理线程可调用 login()/subscribe() 重试。
* 所有钩子同步运行在触发它的线程，行情钩子通常在 CTP 回调线程。
  在钩子中调用 close()/wait_ready()/start() 会报错，避免阻塞回调线程。
* 本项目 ctp.i 已将回调结构体复制为 Python 管理的对象。异步保存 tick
  依赖这一包装约定；其他包装应在回调内复制字段后再异步使用。
* 此基类不合成 K 线、不执行策略、不下单、不自动写行情文件。
"""

import logging
import math
import os
import tempfile
import threading
import time
from pathlib import Path

import LiteCTP as ctp

__all__ = ["CMdSpiBase"]


class CMdSpiBase(ctp.CThostFtdcMdSpi):
    """子类重写 on_tick/on_ready/on_disconnect/on_error，保留 CTP 原始回调。"""

    def __init__(self, front, broker_id, user_id, password, instruments=None,
                 flow_dir=None, logger=None):
        super().__init__()
        if not isinstance(front, str) or not front.startswith("tcp://"):
            raise ValueError("front 必须是 tcp://IP:端口 格式的行情前置地址。")
        if not all(isinstance(value, str) and value.strip()
                   for value in (broker_id, user_id, password)):
            raise ValueError("broker_id、user_id、password 必须是非空字符串。")
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self.flow_dir = flow_dir
        self.logger = logger or logging.getLogger(__name__)
        self.api = None
        self._condition = threading.Condition()
        self._lifecycle_lock = threading.Lock()
        self._hook_local = threading.local()
        self._started = self._closing = self._closed = False
        self._connected = self._loggedin = self._ready = False
        self._trading_day = ""
        self._last_tick_ns = None
        self._last_error = self._failure = None
        self._desired = set(self._normalize(instruments or []))
        self._confirmed = set()
        self._operation = None
        self._request_id = 0
        self._login_request_id = None
        self._login_pending = False
        self._api_calls = 0
        self._temporary_dir = None

    @staticmethod
    def _normalize(instruments):
        if isinstance(instruments, str):
            instruments = [instruments]
        instruments = list(instruments)
        if not all(isinstance(item, str) and item.strip() for item in instruments):
            raise ValueError("合约代码必须是非空字符串。")
        return list(dict.fromkeys(item.strip() for item in instruments))

    @property
    def connected(self):
        with self._condition:
            return self._connected

    @property
    def loggedin(self):
        with self._condition:
            return self._loggedin

    @property
    def ready(self):
        with self._condition:
            return self._ready

    @property
    def trading_day(self):
        with self._condition:
            return self._trading_day

    @property
    def last_error(self):
        """最近的错误 (source, code, message)，恢复后仍保留，便于诊断。"""
        with self._condition:
            return self._last_error

    def status(self):
        """返回状态副本；最后接收时间为 perf_counter_ns，不是行情交易时间。"""
        with self._condition:
            return {
                "connected": self._connected, "loggedin": self._loggedin,
                "ready": self._ready, "trading_day": self._trading_day,
                "instruments": tuple(sorted(self._desired)),
                "confirmed": tuple(sorted(self._confirmed)),
                "last_tick_ns": self._last_tick_ns,
                "last_error": self._last_error, "closed": self._closed,
            }

    def _require_management_thread(self):
        if getattr(self._hook_local, "active", False):
            raise RuntimeError("请在管理线程调用 start()/close()/wait_ready()，不要在回调钩子中调用。")

    def start(self):
        """启动一次；关闭后需要创建新实例。"""
        self._require_management_thread()
        with self._lifecycle_lock:
            with self._condition:
                if self._started or self._closed:
                    raise RuntimeError("此实例已启动或关闭，请创建新实例。")
                self._started = True
            try:
                if self.flow_dir is None:
                    self._temporary_dir = tempfile.TemporaryDirectory(prefix="litectp-md-")
                    directory = Path(self._temporary_dir.name)
                else:
                    directory = Path(self.flow_dir).expanduser().resolve()
                    directory.mkdir(parents=True, exist_ok=True)
                api = ctp.CThostFtdcMdApi.CreateFtdcMdApi(str(directory) + os.sep)
                if api is None:
                    raise RuntimeError("创建行情 API 失败。")
                with self._condition:
                    self.api = api
                api.RegisterSpi(self)
                api.RegisterFront(self.front)
                api.Init()
            except Exception as exc:
                self._report_error("start", None, str(exc))
                raise

    def _call_api(self, method, *args):
        # 只在取引用时加状态锁，不持锁调用 SDK 或子类钩子。
        with self._condition:
            if self._closing or self._closed or self.api is None:
                return None
            api = self.api
            self._api_calls += 1
        try:
            return getattr(api, method)(*args)
        except Exception as exc:
            self._report_error(method, None, str(exc))
            return None
        finally:
            with self._condition:
                self._api_calls -= 1
                self._condition.notify_all()

    def _check_request(self, source, ret):
        if ret == 0:
            return True
        if ret is not None:
            reasons = {-1: "网络连接失败", -2: "未处理请求过多", -3: "请求发送过于频繁"}
            self._report_error(source, ret, reasons.get(ret, "未知请求返回码"))
        return False

    def login(self):
        """连接建立后发起登录；已登录或有登录请求在途时不会重复发送。"""
        with self._condition:
            if not self._connected or self._closing or self._closed:
                raise RuntimeError("行情服务器尚未连接，不能登录。")
            if self._loggedin or self._login_pending:
                return
            self._failure = None
            self._request_id += 1
            request_id = self._request_id
            self._login_request_id = request_id
            self._login_pending = True
        request = ctp.CThostFtdcReqUserLoginField()
        request.BrokerID = self.broker_id
        request.UserID = self.user_id
        request.Password = self.password
        ret = self._call_api("ReqUserLogin", request, request_id)
        if not self._check_request("ReqUserLogin", ret):
            with self._condition:
                if self._login_request_id == request_id:
                    self._login_pending = False

    def subscribe(self, instruments):
        """添加目标合约；登录前暂存，登录后发起订阅。"""
        instruments = self._normalize(instruments)
        with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("此实例已关闭。")
            changed = not set(instruments).issubset(self._desired)
            self._desired.update(instruments)
            if changed or self._failure is not None:
                self._ready = False
            self._failure = None
        self._reconcile()

    def unsubscribe(self, instruments):
        """移除目标合约；重连后不会重新订阅这些合约。"""
        instruments = self._normalize(instruments)
        with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("此实例已关闭。")
            changed = bool(set(instruments) & self._desired)
            self._desired.difference_update(instruments)
            if changed or self._failure is not None:
                self._ready = False
            self._failure = None
        self._reconcile()

    def _reconcile(self):
        # SubscribeMarketData 没有调用方的 request_id，串行操作便于对应回报。
        notify_ready = False
        with self._condition:
            if (not self._loggedin or self._closing or self._closed
                    or self._failure is not None or self._operation is not None):
                return
            remove = self._confirmed - self._desired
            add = self._desired - self._confirmed
            if not remove and not add:
                notify_ready = not self._ready
                self._ready = True
                self._condition.notify_all()
                operation = None
            else:
                self._ready = False
                kind = "unsubscribe" if remove else "subscribe"
                operation = {"kind": kind, "instruments": set(remove or add),
                             "seen": set(), "error": None}
                self._operation = operation
        if notify_ready:
            self._dispatch("on_ready")
        if operation is not None:
            method = "UnSubscribeMarketData" if operation["kind"] == "unsubscribe" else "SubscribeMarketData"
            ret = self._call_api(method, sorted(operation["instruments"]))
            if not self._check_request(method, ret):
                with self._condition:
                    if self._operation is operation:
                        self._operation = None

    def wait_ready(self, timeout=20):
        """等待登录和目标订阅全部确认，超时或发生错误时抛出异常。"""
        self._require_management_thread()
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是有限正数。")
        deadline = time.monotonic() + timeout
        with self._condition:
            if not self._started:
                raise RuntimeError("请先调用 start()。")
            while True:
                if self._closing or self._closed:
                    raise RuntimeError("行情连接已关闭。")
                if self._failure is not None:
                    source, code, message = self._failure
                    raise RuntimeError(f"{source} 失败，code={code}：{message}")
                if self._ready:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待行情登录或订阅确认超时。")
                self._condition.wait(min(remaining, 0.2))

    @staticmethod
    def _response_error(info):
        return (info.ErrorID, info.ErrorMsg) if info is not None else (0, "")

    def OnFrontConnected(self):
        with self._condition:
            if self._closing or self._closed:
                return
            self._connected = True
            self._loggedin = self._ready = self._login_pending = False
            self._trading_day = ""
            self._last_tick_ns = None
            self._confirmed.clear()
            self._operation = None
            self._failure = None
        self.login()

    def OnFrontDisconnected(self, nReason):
        with self._condition:
            if self._closing or self._closed:
                return
            self._connected = self._loggedin = self._ready = self._login_pending = False
            self._login_request_id = None
            self._trading_day = ""
            self._last_tick_ns = None
            self._confirmed.clear()
            self._operation = None
            self._failure = None
            self._condition.notify_all()
        self._dispatch("on_disconnect", nReason)

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        error_id, message = self._response_error(pRspInfo)
        with self._condition:
            if (self._closing or self._closed or not self._connected
                    or nRequestID != self._login_request_id):
                return
            if error_id or bIsLast:
                self._login_pending = False
            if not error_id and bIsLast and pRspUserLogin is not None:
                self._loggedin = True
                self._trading_day = pRspUserLogin.TradingDay
        if error_id:
            self._report_error("OnRspUserLogin", error_id, message)
        elif bIsLast:
            if pRspUserLogin is None:
                self._report_error("OnRspUserLogin", None, "登录回报缺少登录信息。")
            else:
                self._reconcile()

    def _subscription_response(self, kind, instrument_info, error_info, is_last):
        error_id, message = self._response_error(error_info)
        instrument = instrument_info.InstrumentID if instrument_info is not None else ""
        with self._condition:
            operation = self._operation
            if (self._closing or self._closed or not self._loggedin
                    or operation is None or operation["kind"] != kind):
                return
            if instrument and instrument not in operation["instruments"]:
                return
            if instrument:
                operation["seen"].add(instrument)
            if error_id:
                operation["error"] = (error_id, f"{instrument or '未知合约'}：{message}")
            elif instrument:
                if kind == "subscribe":
                    self._confirmed.add(instrument)
                else:
                    self._confirmed.discard(instrument)
            complete = operation["instruments"].issubset(operation["seen"])
            if not complete and not is_last:
                return
            error = operation["error"]
            if not complete and error is None:
                missing = sorted(operation["instruments"] - operation["seen"])
                error = (None, "回报结束，但部分合约未确认：" + ", ".join(missing))
            if error is not None:
                source = "OnRspSubMarketData" if kind == "subscribe" else "OnRspUnSubMarketData"
                self._last_error = self._failure = (source, *error)
                self._ready = False
            self._operation = None
        if error is not None:
            source = "OnRspSubMarketData" if kind == "subscribe" else "OnRspUnSubMarketData"
            self._report_error(source, *error)
        else:
            self._reconcile()

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        self._subscription_response("subscribe", pSpecificInstrument, pRspInfo, bIsLast)

    def OnRspUnSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        self._subscription_response("unsubscribe", pSpecificInstrument, pRspInfo, bIsLast)

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        error_id, message = self._response_error(pRspInfo)
        if error_id:
            with self._condition:
                if self._closing or self._closed:
                    return
                self._last_error = self._failure = (
                    "OnRspError", error_id, f"request_id={nRequestID}：{message}")
                self._ready = False
                if bIsLast:
                    if nRequestID == self._login_request_id:
                        self._login_pending = False
                    self._operation = None
            self._report_error("OnRspError", error_id, f"request_id={nRequestID}：{message}")

    def OnRtnDepthMarketData(self, pDepthMarketData):
        if pDepthMarketData is None:
            return
        received_ns = time.perf_counter_ns()
        with self._condition:
            if self._closing or self._closed or not self._loggedin:
                return
            self._last_tick_ns = received_ns
        self._dispatch("on_tick", pDepthMarketData)

    def _report_error(self, source, code, message):
        with self._condition:
            if self._closing or self._closed:
                return
            self._last_error = self._failure = (source, code, message)
            self._ready = False
            self._condition.notify_all()
        self._dispatch("on_error", source, code, message)

    def _dispatch(self, hook, *args):
        previous = getattr(self._hook_local, "active", False)
        self._hook_local.active = True
        try:
            getattr(self, hook)(*args)
        except Exception as exc:
            self.logger.exception("行情钩子 %s 执行失败", hook)
            if hook == "on_error":
                with self._condition:
                    self._last_error = self._failure = (hook, None, str(exc))
                    self._ready = False
                    self._condition.notify_all()
            else:
                self._report_error(hook, None, str(exc))
        finally:
            self._hook_local.active = previous

    def pyError(self, type, value, traceback):
        """接收 ctp.i 的 SWIG director 异常通知，避免只打印后继续假装就绪。"""
        self.logger.error("CTP 行情回调发生异常", exc_info=(type, value, traceback))
        self._report_error("CTP callback", None, str(value))

    def close(self):
        """由管理线程释放连接；不在回调中 Release，不持状态锁等待 SDK。"""
        self._require_management_thread()
        with self._lifecycle_lock:
            with self._condition:
                if self._closed:
                    return
                self._closing = True
                self._ready = False
                self._condition.notify_all()
                while self._api_calls:
                    self._condition.wait(0.2)
                api = self.api
            released = api is None
            try:
                if api is not None:
                    try:
                        api.RegisterSpi(None)
                    finally:
                        api.Release()
                        released = True
            finally:
                with self._condition:
                    self._connected = self._loggedin = self._ready = False
                    self._login_pending = False
                    self._last_tick_ns = None
                    self._confirmed.clear()
                    self._operation = None
                    if released:
                        self.api = None
                        self._closed = True
                    self._condition.notify_all()
                if released and self._temporary_dir is not None:
                    self._temporary_dir.cleanup()
                    self._temporary_dir = None

    def on_tick(self, tick):
        """子类接收每条行情；基类不打印、不合并、不自动缓存。"""
        pass

    def on_ready(self):
        """初次登录及订阅完成，或重连/调整订阅后重新就绪。"""
        pass

    def on_disconnect(self, reason):
        """子类可通知行情处理模块存在断线区间。"""
        pass

    def on_error(self, source, code, message):
        """默认只记录错误；子类可实现自己的告警与恢复策略。"""
        self.logger.error("行情错误：source=%s code=%s message=%s", source, code, message)
