"""USB NMC 双向请求/应答：校验、超时重传、重复请求去重。无网络依赖。"""
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict

MAX_FRAME = 128 * 1024


def encode_frame(message):
    payload = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    frame = json.dumps({'data': payload, 'sha256': digest}, ensure_ascii=False).encode('utf-8') + b'\n'
    if len(frame) > MAX_FRAME:
        raise ValueError('串口报文过大，文件必须分块传送')
    return frame


def decode_frame(line):
    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
        raise ValueError('不完整或过大的串口帧')
    envelope = json.loads(line.decode('utf-8'))
    if (not isinstance(envelope, dict) or not isinstance(envelope.get('data'), str)
            or not isinstance(envelope.get('sha256'), str)):
        raise ValueError('串口帧的数据或校验字段无效')
    payload = envelope['data'].encode('utf-8')
    if hashlib.sha256(payload).hexdigest() != envelope['sha256']:
        raise ValueError('串口报文校验失败')
    result = json.loads(payload)
    if not isinstance(result, dict) or not isinstance(result.get('id'), str):
        raise ValueError('缺少请求编号')
    return result


class SerialConnection:
    def __init__(self, port, baudrate=115200, opener=None):
        self.port, self.baudrate = port, baudrate
        self.opener, self.device = opener, None
        self.pending = bytearray()
        self.lock = threading.Lock()

    def open(self):
        if self.device is None:
            if self.opener is None:
                import serial
                self.opener = serial.Serial
            self.device = self.opener(self.port, self.baudrate, timeout=.1, write_timeout=3)
        return self.device

    def close(self):
        device = self.device
        self.device = None
        self.pending.clear()
        if device is not None:
            try:
                device.close()
            except OSError:
                # 拔线后的旧句柄可能连关闭也失败；丢弃旧帧，下一次请求重新打开。
                pass

    def send(self, message):
        data = encode_frame(message)
        stream = self.open()
        # 插拔发生在上一帧中间时，先发送换行使对端重新找到帧边界。
        framed = b'\n' + data
        if stream.write(framed) != len(framed):
            raise OSError('串口发送不完整')
        # write已有3秒超时。pyserial的Windows flush会无限等发送队列清空，
        # USB异常时会卡住重试；是否到达对端由带编号的应答确认，不再调用flush。

    def receive(self, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b'\n' in self.pending:
                line, _, tail = self.pending.partition(b'\n')
                self.pending = bytearray(tail)
                try:
                    return decode_frame(bytes(line) + b'\n')
                except (ValueError, KeyError, TypeError, UnicodeError):
                    continue
            block = self.open().read(4096)
            self.pending.extend(block)
            if len(self.pending) > MAX_FRAME:
                self.pending.clear()
        raise TimeoutError('等待USB串口应答超时')

    def request(self, command, data=None, timeout=4, attempts=4):
        request = {'id': uuid.uuid4().hex, 'kind': 'request', 'command': command, 'data': data or {}}
        with self.lock:
            for attempt in range(attempts):
                try:
                    self.send(request)
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        reply = self.receive(max(.1, deadline-time.monotonic()))
                        if reply.get('kind') == 'response' and reply['id'] == request['id']:
                            if not reply.get('ok'):
                                raise RuntimeError(reply.get('error', '主机执行失败'))
                            return reply.get('data')
                    raise TimeoutError('未收到本次USB请求的应答')
                except (OSError, TimeoutError):
                    self.close()
                    if attempt + 1 == attempts:
                        raise
                    time.sleep(.2)


class RequestServer:
    def __init__(self, connection, handler):
        self.connection, self.handler = connection, handler
        self.cache = OrderedDict()

    def process(self, request):
        if request.get('kind') != 'request':
            return None
        token = request['id']
        if token in self.cache:
            return self.cache[token]
        try:
            data = self.handler(request['command'], request.get('data', {}))
            reply = {'id': token, 'kind': 'response', 'ok': True, 'data': data}
            # 清单或异常窗口记录可能过大；返回明确错误，不能在send时把主机服务打退出。
            encode_frame(reply)
        except Exception as exc:
            reply = {'id': token, 'kind': 'response', 'ok': False,
                     'error': f'{type(exc).__name__}: {exc}'[:4000]}
        self.cache[token] = reply
        while len(self.cache) > 2048:
            self.cache.popitem(last=False)
        return reply

    def serve(self, stop):
        while not stop.is_set():
            try:
                request = self.connection.receive(.5)
                reply = self.process(request)
                if reply:
                    self.connection.send(reply)
            except TimeoutError:
                continue
            except OSError:
                self.connection.close()
                stop.wait(.5)
