"""JK808 V13和NTP校时，沿用已验证的Linux接收端协议实现。"""
import ctypes
from ctypes import wintypes
import os, math, re, socket, struct, time
from datetime import datetime, timezone
V9_IDS=(0x0483,0x5757)

class ProtocolError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status

def calculate_crc16(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc

def convert_temperature_words(words, cfg):
    values, statuses = {}, {}
    for channel, raw in words.items():
        value = raw / 10.0
        if cfg["unit"] == "F":
            value = (value - 32) / 1.8
        elif cfg["unit"] == "K":
            value -= 273.15
        if channel not in cfg["active_channels"]:
            status = "disabled"
        elif raw in cfg["invalid_raw_values"]:
            status = "invalid_code"
        elif not cfg["valid_min_c"] <= value <= cfg["valid_max_c"]:
            status = "out_of_range"
        else:
            status = "ok"
        values[channel] = round(value, 4) if status == "ok" else None
        statuses[channel] = status
    return values, statuses

def jk808_response_version(cfg):
    # Preserve V9 semantics for existing 1.3.0 configurations. New templates use V13.
    version = cfg.get('hid_data_format', 'v9')
    if version not in ('v9', 'v13'):
        raise ValueError('hid_data_format 必须是 v9 或 v13；新包按所提供厂家软件使用 v13。')
    return version

def jk808_invalid_temperature_word(cfg):
    return 32767 if jk808_response_version(cfg) == 'v13' else -1

def convert_jk808_temperature_words(words, cfg):
    conversion = dict(cfg)
    conversion['invalid_raw_values'] = list(set(cfg['invalid_raw_values']) | {jk808_invalid_temperature_word(cfg)})
    return convert_temperature_words(words, conversion)

def validate_jk808_settings(cfg, require_path=True):
    jk808_response_version(cfg)
    if cfg['channel_count'] != 8:
        raise ValueError('JK808 专用适配器要求 8 通道。')
    path = cfg.get('hid_path_hex', '')
    if require_path and not path:
        raise ValueError('请先选首次部署 A 识别 JK808 USB 设备，不需要填写 COM 口。')
    if path:
        try:
            if not bytes.fromhex(path):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('hid_path_hex 不是有效设备路径，请重新执行首次部署 A。') from None

def build_jk808_read_request():
    body = bytes.fromhex('01 03 00 00')
    return b'\0' + (body + calculate_crc16(body).to_bytes(2, 'big')).ljust(64, b'\0')

class JK808ResponseDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.words = {}

    def feed(self, payload):
        self.buffer.extend(payload)
        while self.buffer:
            if self.buffer[0] == 0:
                # Padding only between complete frames, never inside a frame.
                del self.buffer[0]
                continue
            if len(self.buffer) < 6:
                break
            if self.buffer[:2] != b'\x01\x03':
                raise ProtocolError('header_error', '不是 USB V9/V13 温度响应，请保留原始报文核对固件。')
            start = int.from_bytes(self.buffer[2:4], 'big')
            count = int.from_bytes(self.buffer[4:6], 'big')
            if not 1 <= count <= 8 or start + count > 8:
                raise ProtocolError('channel_error', f'错误的零起始通道范围：{start}, {count}')
            length = 8 + count * 2
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            if calculate_crc16(frame[:-2]) != int.from_bytes(frame[-2:], 'big'):
                raise ProtocolError('crc_error', 'USB V9/V13 响应 CRC 校验失败')
            for offset in range(count):
                channel = start + offset + 1
                if channel in self.words:
                    raise ProtocolError('duplicate_channel', f'同轮重复通道 {channel}')
                self.words[channel] = int.from_bytes(frame[6+2*offset:8+2*offset], 'big', signed=True)
            del self.buffer[:length]
        return self.words

class JK808Thermometer:
    def __init__(self, cfg, trace):
        self.cfg, self.trace, self.device = cfg, trace, None

    def open(self):
        import hid
        validate_jk808_settings(self.cfg)
        path = bytes.fromhex(self.cfg['hid_path_hex'])
        matches = [x for x in hid.enumerate(*V9_IDS) if x['path'] == path]
        if len(matches) != 1:
            raise ProtocolError('device_missing', '指定 JK808 USB 设备未找到；检查数据线，换 USB 口后重做首次部署 A。')
        dev = hid.device()
        try:
            dev.open_path(path)
        except Exception:
            dev.close()
            raise
        self.device = dev

    def close(self):
        device, self.device = self.device, None
        if device is not None:
            device.close()

    def read(self):
        if self.device is None:
            self.open()
        # Bounded drain: a device streaming unsolicited data is not this profile.
        for _ in range(16):
            old = bytes(self.device.read(65, 1))
            if not old:
                break
            self.trace('discard_before_request', old)
        else:
            raise ProtocolError('unexpected_stream', '设备持续主动发送数据，与当前协议不符。')
        request = build_jk808_read_request()
        self.trace('tx_hid', request)
        if self.device.write(request) != len(request):
            raise ProtocolError('write_error', 'USB 读取请求未完整发送')
        decoder = JK808ResponseDecoder()
        deadline = time.monotonic() + self.cfg['timeout_s']
        while time.monotonic() < deadline:
            ms = max(1, min(50, int((deadline-time.monotonic())*1000)))
            raw = bytes(self.device.read(65, ms))
            if not raw:
                continue
            self.trace('rx_hid', raw)
            if len(raw) == 65:
                if raw[0] != 0:
                    raise ProtocolError('report_id_error', '不是 Report ID 0 的 HID 响应')
                raw = raw[1:]
            words = decoder.feed(raw)
            if len(words) == 8 and not decoder.buffer:
                values, statuses = convert_jk808_temperature_words(words, self.cfg)
                return values, statuses, words
        raise ProtocolError('timeout', f'USB V9/V13 超时，收到 {len(decoder.words)}/8 路；可能为另一固件。')

def utc_system_time_fields(epoch):
    if not math.isfinite(epoch) or not 946684800 <= epoch <= 4102444800:
        raise ValueError('拒绝设置无效的 UTC 时间')
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return (dt.year, dt.month, (dt.weekday()+1)%7, dt.day,
            dt.hour, dt.minute, dt.second, dt.microsecond//1000)

def set_windows_system_time(epoch):
    if os.name != 'nt':
        raise RuntimeError('自动修改接收电脑时间仅支持 Windows')
    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [(name, wintypes.WORD) for name in
                    ('year','month','weekday','day','hour','minute','second','milliseconds')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetSystemTime.argtypes = [ctypes.POINTER(SYSTEMTIME)]
    kernel.SetSystemTime.restype = wintypes.BOOL
    value = SYSTEMTIME(*utc_system_time_fields(epoch))
    # SetSystemTime 接收 UTC，并自行启用/还原调用者已有的系统时间权限。
    if not kernel.SetSystemTime(ctypes.byref(value)):
        code = ctypes.get_last_error()
        if code in (5, 1314):
            raise PermissionError('Windows 不允许修改时间。请关闭程序，右键“开始测试.cmd” → 以管理员身份运行，再选原菜单。')
        raise OSError(code, 'Windows 校时失败：'+ctypes.FormatError(code))

def decode_ntp_reply(packet, request_token, round_trip_s, received_monotonic):
    """SNTP 回复检查与换算。使用单调时钟测往返，不依赖电脑原来的错误日期。

    参考 RFC 5905。接收时刻估计 = 服务器发送时间 + 网络往返的一半。
    服务器处理耗时从往返中扣除；记录误差估计，不声称零误差。
    """
    if len(packet)<48:
        raise ValueError('NTP 回复不足 48 字节')
    leap, version, mode = packet[0]>>6, (packet[0]>>3)&7, packet[0]&7
    if leap==3 or version not in (3,4) or mode!=4 or not 1<=packet[1]<=15:
        raise ValueError('NTP 服务器未同步、限流，或回复类型无效')
    if packet[24:32]!=request_token:
        raise ValueError('NTP 回复不属于本次请求')
    def unix_seconds(raw):
        seconds, fraction = struct.unpack('!II',raw)
        if seconds==0 and fraction==0:
            raise ValueError('NTP 时间戳为零')
        epoch = seconds-2208988800+fraction/(2**32)
        if epoch<946684800:  # NTP 秒字段在 2036 年回绕，覆盖 2000—2100 年。
            epoch += 2**32
        utc_system_time_fields(epoch)
        return epoch
    received = unix_seconds(packet[32:40])
    transmitted = unix_seconds(packet[40:48])
    processing = transmitted-received
    if not 0<=round_trip_s<=1 or processing<0 or processing>round_trip_s+.01:
        raise ValueError('NTP 回复耗时或时间顺序异常')
    root_delay = struct.unpack('!i',packet[4:8])[0]/65536
    dispersion = struct.unpack('!I',packet[8:12])[0]/65536
    uncertainty = max(0,round_trip_s-processing)/2+max(0,root_delay)/2+dispersion
    if uncertainty>.5:
        raise ValueError('NTP 误差估计超过 0.5 秒')
    return {'epoch_s':transmitted+max(0,round_trip_s-processing)/2,
            'received_monotonic_s':received_monotonic,'rtt_s':round_trip_s,
            'uncertainty_s':uncertainty,'stratum':packet[1]}

def request_ntp_time(server):
    """Windows 接收电脑发起 UDP 123 请求。联网主机名可在唯一 config.json 中替换。"""
    token = os.urandom(8)
    request = bytes([0x23])+bytes(39)+token
    # 连接 UDP socket 会限制回复来源；随机 origin token 防止旧回复混入。
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect((server,123))
        begin = time.monotonic()
        client.send(request)
        packet = client.recv(512)
        end = time.monotonic()
    result = decode_ntp_reply(packet,token,end-begin,end)
    result['server'] = server
    return result

def standard_time_now(reference):
    """标准时间用 monotonic 推进；修改 Windows 墙钟不会使这个基准跳变。"""
    return reference['epoch_s']+time.monotonic()-reference['received_monotonic_s']

def obtain_internet_standard_time(configuration):
    servers = configuration.get('clock_sync',{}).get('ntp_servers',['ntp.aliyun.com','ntp1.aliyun.com'])
    if not isinstance(servers,list) or not 1<=len(servers)<=4 or any(
        not isinstance(server,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',server) for server in servers):
        raise ValueError('clock_sync.ntp_servers 应为 1 到 4 个 NTP 域名或 IPv4 地址')
    errors = []
    for server in servers:
        samples = []
        for attempt in range(3):
            try:
                samples.append(request_ntp_time(server))
            except (OSError,ValueError) as error:
                errors.append(server+': '+str(error))
            if attempt<2:
                time.sleep(1)
        if len(samples)>=2:
            estimates = [standard_time_now(sample) for sample in samples]
            if max(estimates)-min(estimates)>.25:
                errors.append(server+': 多次回复相差超过 0.25 秒')
                continue
            best = dict(min(samples,key=lambda sample:sample['uncertainty_s']))
            best['accepted_samples'] = len(samples)
            best['source_failures'] = errors
            return best
    raise RuntimeError('没有取得可靠的联网标准时间，未开始测试。请让接收电脑放行 NTP UDP 123，'
                       '或在 config.json 的 clock_sync.ntp_servers 填公司 NTP 地址。'+ '；'.join(errors))
