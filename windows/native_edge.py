"""常用 Edge 的独占测试窗口。准备期通过 UIA 操作，测试期只读窗口遥测。

不使用独立 user-data-dir、调试端口或共享浏览器进程的退出码。
仅关闭本模块新建且带所有权标记的窗口，不关闭用户原有页面。
"""
from __future__ import annotations

import base64
import ctypes
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid


def _origin(url):
    parsed = urlsplit(url)
    return f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme != 'file' else 'file://'


def _window_property(hwnd, name):
    from ctypes import wintypes
    fn = ctypes.windll.user32.GetPropW
    fn.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    fn.restype = wintypes.HANDLE
    return fn(hwnd, name)


def _mark_window(hwnd, name):
    from ctypes import wintypes
    fn = ctypes.windll.user32.SetPropW
    fn.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
    fn.restype = wintypes.BOOL
    if not fn(hwnd, name, 1):
        raise RuntimeError('无法标记本轮Edge窗口所有权')


def _read_title(hwnd):
    """pywin32部分版本GetWindowText最多511字；遥测必须读完整标题。"""
    from ctypes import wintypes
    length = ctypes.windll.user32.GetWindowTextLengthW
    length.argtypes, length.restype = (wintypes.HWND,), ctypes.c_int
    read = ctypes.windll.user32.GetWindowTextW
    read.argtypes, read.restype = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int
    buffer = ctypes.create_unicode_buffer(min(32768, max(1024, length(hwnd)+512)))
    read(hwnd, buffer, len(buffer))
    return buffer.value


def _process_path(hwnd):
    import win32api
    import win32process
    from ctypes import wintypes
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    handle = win32api.OpenProcess(0x1000, False, pid)
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        fn = ctypes.windll.kernel32.QueryFullProcessImageNameW
        fn.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        fn.restype = wintypes.BOOL
        if not fn(wintypes.HANDLE(int(handle)), 0, buffer, ctypes.byref(size)):
            raise OSError('QueryFullProcessImageNameW未返回进程路径')
        return buffer.value
    finally:
        handle.Close()


def build_launch_args(executable, url, profile_directory=''):
    args = [str(executable), '--new-window']
    if profile_directory:
        if not re.fullmatch(r'[\w .-]+', profile_directory) or profile_directory in ('.', '..'):
            raise ValueError('Edge用户配置目录名无效')
        args.append('--profile-directory=' + profile_directory)
    args.append(url)
    return args


def _pinned_edge():
    """仅读取任务栏快捷方式的 Edge 路径及 profile-directory，不执行快捷方式。"""
    import win32com.client
    folder = Path(os.environ.get('APPDATA', '')) / 'Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar'
    found = []
    shell = win32com.client.Dispatch('WScript.Shell')
    for link in folder.glob('*.lnk'):
        item = shell.CreateShortcut(str(link))
        target = Path(item.TargetPath)
        if target.name.lower() != 'msedge.exe' or not target.is_file():
            continue
        # 多个固定的 Edge 配置不能猜选；回到 Edge 正常 last_used。
        match = re.search(r'--profile-directory(?:=|\s+)(?:"([^"]+)"|([^\s]+))', item.Arguments or '')
        found.append((str(target), (match.group(1) or match.group(2)) if match else ''))
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else (None, '')


def _same_desktop_user():
    import win32api
    import win32con
    import win32process
    import win32security
    shell = ctypes.windll.user32.GetShellWindow()
    if not shell:
        return None
    _, pid = win32process.GetWindowThreadProcessId(shell)
    process = win32api.OpenProcess(0x1000, False, pid)
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        mine = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            theirs = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            own = win32security.GetTokenInformation(mine, win32security.TokenUser)[0]
            return win32security.ConvertSidToStringSid(theirs) == win32security.ConvertSidToStringSid(own)
        finally:
            token.Close()
            mine.Close()
    finally:
        process.Close()


def inspect_native_edge():
    """只读检查：不启动浏览器、不修改用户设置、不复制登录资料。"""
    record = {'passed': False, 'executable': '', 'profile_directory': '', 'source': ''}
    try:
        if os.name != 'nt':
            raise RuntimeError('常用Edge窗口控制需要Windows测试主机')
        same = _same_desktop_user()
        record['same_desktop_user'] = same
        if same is False:
            raise RuntimeError('当前程序与桌面登录用户不同，请用桌面当前账号运行，避免打开另一个账号的Edge')
        if same is None:
            raise RuntimeError('无法确认当前交互桌面用户，请在已登录且解锁的测试主机桌面运行')
        try:
            executable, profile = _pinned_edge()
        except Exception:
            executable, profile = None, ''
        record['source'] = 'taskbar_shortcut' if executable else 'installed_edge'
        if not executable:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe') as key:
                        candidate = str(winreg.QueryValueEx(key, '')[0]).strip('"')
                    if Path(candidate).is_file():
                        executable = candidate
                        break
                except OSError:
                    pass
        if not executable:
            for name in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
                candidate = Path(os.environ.get(name, '')) / 'Microsoft/Edge/Application/msedge.exe'
                if candidate.is_file():
                    executable = str(candidate)
                    break
        if not executable:
            raise RuntimeError('没有找到本机已安装的Microsoft Edge')
        if not profile:
            state = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/Edge/User Data/Local State'
            if state.is_file():
                try:
                    profile = json.loads(state.read_text('utf-8')).get('profile', {}).get('last_used', '')
                except (OSError, ValueError):
                    pass
        build_launch_args(executable, 'about:blank', profile)
        if profile and not (Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/Edge/User Data' / profile).is_dir():
            raise RuntimeError('常用Edge用户配置目录不存在，请先从任务栏正常打开Edge后重试；程序不会新建空配置')
        record.update(passed=True, executable=executable, profile_directory=profile)
    except Exception as exc:
        record['error'] = str(exc)
    return record


def decode_telemetry(title, nonce, allowed_origin, now=None):
    """标题只接受本轮随机标识的、新鲜的同源JSON数据；缺测不能当作通过。"""
    def missing(reason):
        return {'observation_available': False, 'observation_error': reason}
    prefix = 'JK808_' + nonce + ':'
    if not title.startswith(prefix):
        return missing('页面遥测尚未就绪或页面正在重新加载')
    try:
        encoded = title[len(prefix):].split(' - ', 1)[0].strip()
        if len(encoded) > 16000:
            raise ValueError('遥测长度异常')
        data = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(data, dict):
            raise ValueError('遥测载荷不是对象')
        if data.get('nonce') != nonce or data.get('origin') != allowed_origin:
            raise ValueError('遥测不属于本轮页面')
        if type(data.get('seq')) is not int or data['seq'] < 1:
            raise ValueError('遥测序号无效')
        stamp = data['epoch']
        age = (time.time() if now is None else now) - stamp
        if isinstance(stamp, bool) or not math.isfinite(stamp) or not -2 <= age <= 5:
            raise ValueError('页面遥测已过期')
        sample = data.get('sample')
        if sample is None:
            return missing('页面尚无可见视频') | {'page_state': data.get('page_state', {})}
        if not isinstance(sample, dict):
            raise ValueError('视频遥测不是对象')
        fields = ('position', 'readyState', 'width', 'height', 'playback_rate',
                  'frame_age_s', 'frame_span_s', 'observed_fps')
        for field in fields:
            value = sample.get(field)
            # 尚未收到首帧时允许明确的null帧龄，其余数字均须有效。
            if field == 'frame_age_s' and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('视频遥测数值无效：' + field)
        if not isinstance(sample.get('paused'), bool) or not isinstance(sample.get('ended'), bool):
            raise ValueError('播放状态缺失')
        if type(sample['readyState']) is not int or not 0 <= sample['readyState'] <= 4:
            raise ValueError('视频readyState无效')
        full = sample.get('fullscreen')
        if not isinstance(full, dict) or any(type(full.get(key)) is not bool for key in
                                            ('active', 'contains_video', 'fills_viewport')):
            raise ValueError('视频全屏状态缺失')
        sample.update(observation_available=True, bridge_epoch=stamp, bridge_seq=data['seq'],
                      page_state=data.get('page_state', {}))
        return sample
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return missing('页面遥测无法校验：' + str(exc))


# 一次注入后持续采样；正式测试只读标题，不移动鼠标或退出视频全屏。
# 只使用网页公开DOM和视频API，不改播放器私有配置、不缓存/替换视频流。
BRIDGE_SCRIPT = r'''(()=>{
 const nonce=__NONCE__,key='__jk808Native';
 if(window[key]?.nonce===nonce)return;
 if(window[key]?.timer)clearInterval(window[key].timer);
 const s={nonce,seq:0,timer:null,originalTitle:document.title};window[key]=s;
 const visible=e=>{const r=e.getBoundingClientRect(),c=getComputedStyle(e);
   return e.isConnected!==false&&r.width>20&&r.height>20&&c.visibility!=='hidden'&&c.display!=='none'
     &&(c.opacity==null||c.opacity===''||Number(c.opacity)>0);};
 const needsLogin=location.protocol!=='file:'&&/(^|\.)bilibili\.com$/i.test(new URL(location.origin).hostname);
 const login=()=>{
   const overlay=Array.from(document.querySelectorAll('.bili-mini-login-wp,.bili-mini-content-wp,.bili-mini-mask')).some(visible);
   if(!needsLogin)return {confirmed:!overlay,visible:overlay,source:'not_required'};
   let confirmed=null,source='unknown';
   /* 仅读网站自身已加载的登录布尔值，不调用账号接口、不读取或导出账号字段。 */
   const user=window.__BiliUser__,cache=user?.cache;
   if(cache?.code===0&&typeof cache.data?.isLogin==='boolean'){
     confirmed=cache.data.isLogin;source='page_user_cache';
   }else if(cache?.code===-101){confirmed=false;source='page_user_cache';}
   if(!user&&typeof window.UserStatus?.userInfo?.isLogin==='boolean'){
     confirmed=window.UserStatus.userInfo.isLogin;source='page_user_status';
   }
   if(overlay){confirmed=false;source='login_overlay';}
   return {confirmed,visible:overlay,source};
 };
 const allowed=()=>{const a=login();return a.confirmed===true&&!a.visible;};
 const active=()=>{
   let candidates=Array.from(document.querySelectorAll('video')).map((v,i)=>{
     const r=v.getBoundingClientRect();return {v,i,r,area:r.width*r.height};}).filter(x=>visible(x.v));
   const full=document.fullscreenElement;
   const inside=candidates.filter(x=>full&&(full===x.v||full.contains(x.v)));
   if(inside.length)candidates=inside;
   candidates.sort((a,b)=>b.area-a.area||b.i-a.i);
   if(!candidates.length)return undefined;
   const main=candidates[0],r=main.r;
   const x=Math.max(0,Math.min(innerWidth-1,(r.left||0)+r.width*.5));
   const y=Math.max(0,Math.min(innerHeight-1,(r.top||0)+r.height*.4));
   for(const element of document.elementsFromPoint(x,y)){
     const front=candidates.find(item=>item.v===element&&item.area>=main.area*.8);
     if(front)return front.v;
   }
   return main.v;
 };
 const quality=()=>{const m=document.querySelector('[role="button"][aria-label="清晰度"]');
   if(!m)return {selected:null,label:'',choices:[]};
   return {selected:m.querySelector('li.bpx-state-active')?.getAttribute('data-value')||null,
     label:m.querySelector('.bpx-player-ctrl-quality-result')?.textContent.trim()||'',
     choices:Array.from(m.querySelectorAll('li[data-value]'),e=>({value:e.getAttribute('data-value'),label:e.textContent.trim()}))};};
 const bind=v=>{if(v.__jk808NativeFrames)return v.__jk808NativeFrames;
   const f={frames:[],at:0,size:''};v.__jk808NativeFrames=f;
   const reset=()=>{f.frames=[];f.at=0;};v.addEventListener('seeking',reset);v.addEventListener('emptied',reset);
   if(v.requestVideoFrameCallback){const cb=(t,m)=>{
     if(!v.isConnected){if(v.__jk808NativeFrames===f)delete v.__jk808NativeFrames;return;}
     if(v.__jk808NativeFrames!==f)return;
     const size=v.videoWidth+'x'+v.videoHeight,last=f.frames.at(-1);
     if(size!==f.size||(last&&(m.mediaTime<=last.t||m.presentedFrames<=last.n)))reset();
     f.size=size;f.at=performance.now();f.frames.push({t:m.mediaTime,n:m.presentedFrames});
     if(f.frames.length>180)f.frames.shift();v.requestVideoFrameCallback(cb);};v.requestVideoFrameCallback(cb);}
   return f;};
 const tick=()=>{try{const v=active(),auth=login();let sample=null;
   if(auth.confirmed!==true||auth.visible){
     document.getElementById('jk808-native-controls')?.remove();
     if(document.fullscreenElement&&document.exitFullscreen){const p=document.exitFullscreen();if(p?.catch)p.catch(()=>{});}
   }
   if(v){const f=bind(v),a=f.frames[0],b=f.frames.at(-1),full=document.fullscreenElement;
     const r=full?.getBoundingClientRect();sample={position:v.currentTime,paused:v.paused,ended:v.ended,
       readyState:v.readyState,width:v.videoWidth,height:v.videoHeight,playback_rate:v.playbackRate,
       frame_age_s:f.at?(performance.now()-f.at)/1000:null,frame_span_s:a&&b?b.t-a.t:0,
       observed_fps:a&&b&&b.t>a.t?(b.n-a.n)/(b.t-a.t):0,quality:quality(),
       fullscreen:{active:!!full,contains_video:!!full&&(full===v||full.contains(v)),
         fills_viewport:!!r&&r.width>=innerWidth-4&&r.height>=innerHeight-4}};}
   const payload={nonce,seq:++s.seq,epoch:Date.now()/1000,origin:location.protocol==='file:'?'file://':location.origin,
     sample,page_state:{has_video:!!v,ready:document.readyState,
       login_visible:auth.visible,login_confirmed:auth.confirmed,login_source:auth.source,
       error:!!v?.error||Array.from(document.querySelectorAll('.error-container,.error-wrap,.error-panel,#error-page,.bpx-player-state-play-err,.bpx-player-state-error')).some(visible)}};
   document.title='JK808_'+nonce+':'+btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
 }catch(e){s.error=String(e);}};
 /* 可访问的真实按钮获得可信用户激活，仅在准备阶段按需创建。 */
 s.controls=()=>{let bar=document.getElementById('jk808-native-controls');if(bar)bar.remove();
   if(!allowed())return false;
   bar=document.createElement('div');bar.id='jk808-native-controls';
   bar.style.cssText='position:fixed;left:8px;top:8px;z-index:2147483647;background:white;color:black;padding:8px';
   for(const [label,action]of [['JK808播放',()=>{s.play();}],
      ['JK808视频全屏',()=>{if(!allowed())return;const v=active();if(!v)return;const box=v.closest('.bpx-player-container')||v;
        const p=box.requestFullscreen();if(p?.catch)p.catch(()=>{});bar.remove();}]]){
     const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-label',label);
     b.onclick=action;bar.appendChild(b);}
   document.body.appendChild(bar);return true;};
 s.play=()=>{if(!allowed())return false;const v=active();if(v){v.playbackRate=1;if(v.paused&&!v.ended)v.play().catch(()=>{});}};
 s.quality=(force=false)=>{if(!allowed())return false;const menu=document.querySelector('[role="button"][aria-label="清晰度"]');
   if(!menu)return false;const q=quality(),v=active();
   if(!force&&v?.videoWidth>=1920&&v.videoHeight>=1080&&q.selected&&q.selected!=='0'&&/1080|1440|2160|2K|4K|8K/i.test(q.label))return true;
   const option=menu.querySelector('li[data-value="80"]');if(!option)return false;
   option.click();return true;};
 tick();s.timer=setInterval(tick,500);
})()'''


class NativeEdgeSession:
    def __init__(self, cfg, stop=None, status=None):
        self.cfg, self.stop, self.status = cfg, stop, status
        self.settings = cfg.get('online_video', {})
        self.url = self.settings.get('url', '')
        self.allowed_origin = _origin(self.url)
        host = (urlsplit(self.url).hostname or '').lower()
        self._requires_login = host == 'bilibili.com' or host.endswith('.bilibili.com')
        self._playback_maximized = False
        self.nonce = uuid.uuid4().hex
        self.property = 'JK808-Owned-' + self.nonce
        self.hwnd = None
        self._last_seq = None
        self._seq_at = 0
        self.marker_url = ''
        self._marker_server = None
        self._marker_thread = None
        self.record = {'browser_control': 'native_edge', 'observations': [], 'page_attempts': []}

    def _check(self):
        if self.stop is not None and self.stop.is_set():
            raise InterruptedError('测试人员取消')

    def _wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check()
            time.sleep(min(.1, max(0, end-time.monotonic())))

    def wait_for_timeout(self, ms):
        self._wait(ms/1000)

    def _message(self, text, waiting=False):
        if self.status is not None:
            self.status.update(message=text, phase='常用Edge播放准备', waiting_for_user=waiting)

    @staticmethod
    def _windows():
        """枚举候选窗口。进程查询失败不能把已经打开的窗口当作不存在。"""
        import win32gui
        result = set()
        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd).startswith('Chrome_WidgetWin_'):
                result.add(hwnd)
        win32gui.EnumWindows(collect, None)
        return result

    def _probe_marker(self, hwnd):
        """仅读新窗口地址栏，确认为本轮唯一标识后才能导航或关闭。"""
        from pywinauto import Desktop
        result = {'hwnd': hwnd, 'marker_matches': False}
        self._check()
        try:
            executable = _process_path(hwnd)
            result['process_name'] = Path(executable).name
            if result['process_name'].lower() != 'msedge.exe':
                return result
        except Exception as exc:
            # 精确的随机地址仍能证明归属，但不能把查询异常静默丢掉。
            result['process_query_error'] = type(exc).__name__ + ': ' + str(exc)
        try:
            self._check()
            title = _read_title(hwnd)
            result['title'] = title[:240]
            if title.startswith('JK808-OWN-' + self.nonce + ' - ') or title == 'JK808-OWN-' + self.nonce:
                result.update(marker_matches=True, evidence='exact_nonce_title')
                return result
            window = Desktop(backend='uia').window(handle=hwnd)
            edits = window.descendants(control_type='Edit')
            self._check()
            result['edit_count'] = len(edits)
            for edit in edits:
                self._check()
                try:
                    value = edit.get_value()
                except Exception:
                    try:
                        value = edit.iface_value.CurrentValue
                    except Exception:
                        continue
                if self.marker_url and isinstance(value, str) and value.strip() in (self.marker_url, self.marker_url.removeprefix('http://')):
                    result.update(marker_matches=True, evidence='exact_address_value')
                    return result
        except InterruptedError:
            raise
        except Exception as exc:
            result['address_read_error'] = type(exc).__name__ + ': ' + str(exc)
        return result

    def _start_marker(self):
        """内存中的本机确认页，无文件路径；只监听127.0.0.1，认领后立即停止。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        route = '/jk808/' + self.nonce
        body = ('<!doctype html><meta charset="utf-8"><title>JK808-OWN-' + self.nonce +
                '</title><p>JK808 video preparation</p>').encode('utf-8')
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path != route:
                    handler.send_error(404)
                    return
                handler.send_response(200)
                handler.send_header('Content-Type', 'text/html; charset=utf-8')
                handler.send_header('Content-Length', str(len(body)))
                handler.send_header('Cache-Control', 'no-store')
                handler.send_header('Content-Security-Policy', "default-src 'none'")
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(handler, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        self._marker_server = server
        self.marker_url = f'http://127.0.0.1:{server.server_port}' + route
        try:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
            self._marker_thread = thread
            thread.start()
        except BaseException:
            self._marker_server = self._marker_thread = None
            server.server_close()
            raise

    def _stop_marker(self):
        server = self._marker_server
        thread = self._marker_thread
        if server is not None:
            self._marker_server = self._marker_thread = None
            try:
                # 没有进入serve_forever的服务不能调用shutdown，否则会等待不存在的线程。
                if thread is not None and thread.is_alive():
                    server.shutdown()
            finally:
                server.server_close()

    def _owns_window(self):
        import win32gui
        return bool(self.hwnd and win32gui.IsWindow(self.hwnd)
                    and _window_property(self.hwnd, self.property) == 1)

    def is_open(self):
        return self._owns_window()

    def _window(self):
        from pywinauto import Desktop
        if not self._owns_window():
            raise RuntimeError('本轮Edge测试窗口已关闭或所有权不匹配')
        return Desktop(backend='uia').window(handle=self.hwnd)

    def _address(self, text):
        from pywinauto.keyboard import send_keys
        window = self._window()
        window.set_focus()
        send_keys('^l')
        self._wait(.15)
        focused = [edit for edit in window.descendants(control_type='Edit') if edit.has_keyboard_focus()]
        if len(focused) != 1:
            raise RuntimeError('未能定位本轮Edge的地址栏')
        focused[0].set_edit_text(text)  # ValuePattern保留javascript:，不用剪贴板或模拟输入JS字符。
        self._check()
        send_keys('{ENTER}')

    def _inject(self, code):
        self._address('javascript:void(' + code.replace('\r', ' ').replace('\n', ' ') + ')')

    def _install_bridge(self):
        self._inject(BRIDGE_SCRIPT.replace('__NONCE__', json.dumps(self.nonce)))

    def _click(self, name):
        controls = self._window().descendants(control_type='Button')
        matches = [c for c in controls if c.element_info.name == name and c.is_visible()]
        if len(matches) != 1:
            raise RuntimeError('未能唯一定位本轮视频按钮：' + name)
        self._check()
        matches[0].click_input()  # 全屏API需要真正的用户激活，不能仅invoke。

    def _login_pending(self, sample):
        state = sample.get('page_state') or {}
        return bool(state.get('login_visible') or
                    (self._requires_login and state.get('login_confirmed') is not True))

    def _restore_for_login(self):
        """仅恢复本轮窗口；扫码等待不抢焦点、不反复注入或调整窗口。"""
        import win32gui
        if not self._owns_window():
            raise RuntimeError('本轮Edge窗口所有权不匹配，不能恢复登录窗口')
        if win32gui.GetWindowPlacement(self.hwnd)[1] in (2, 3, 6, 7):
            win32gui.ShowWindow(self.hwnd, 1)  # SW_SHOWNORMAL
        self._playback_maximized = False

    def _maximize_for_playback(self):
        """调用方已核对刚读取的登录状态，仅放大本轮测试窗口。"""
        import win32gui
        self._check()
        if not self._owns_window():
            raise RuntimeError('本轮Edge窗口所有权不匹配，不能最大化')
        win32gui.ShowWindow(self.hwnd, 3)  # SW_MAXIMIZE，只能在登录后调用。
        self._playback_maximized = True

    def open(self):
        import win32gui
        if urlsplit(self.url).scheme not in ('https', 'http', 'file'):
            raise RuntimeError('在线视频地址必须是有效的HTTP/HTTPS页面')
        checked = inspect_native_edge()
        self.record['browser'] = checked
        if not checked['passed']:
            raise RuntimeError(checked.get('error', '常用Edge检查失败'))
        before = self._windows()
        self.record['initial_window_handles'] = sorted(before)
        # Edge某些版本会将命令行about:blank#标识改成新标签页，故使用无文件确认页。
        self._start_marker()
        args = build_launch_args(checked['executable'], self.marker_url,
                                 checked.get('profile_directory', ''))
        self._check()
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.record['launch'] = {'arguments': args, 'pid': process.pid}
        self.record['window_scans'] = []
        self._message('正在用常用Edge打开本轮窗口并核对地址，尚未开始测试计时')
        # 常用Edge复用进程，Popen进程退出并不代表窗口退出。
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            self._check()
            new = self._windows()-before
            probes = []
            for h in sorted(new):
                self._check()
                probes.append(self._probe_marker(h))
                self._check()
            scans = self.record['window_scans']
            if probes and (not scans or scans[-1]['candidates'] != probes):
                scans.append({'epoch': time.time(), 'candidates': probes})
                del scans[:-20]
            matches = [item['hwnd'] for item in probes if item.get('marker_matches')]
            if len(matches) > 1:
                raise RuntimeError('出现多个带本轮标识的Edge窗口，无法安全确定，请重试')
            if len(matches) == 1:
                self.hwnd = matches[0]
                _mark_window(self.hwnd, self.property)
                win32gui.ShowWindow(self.hwnd, 1)  # 登录前强制普通窗口，不继承上次最大化。
                self.record['window'] = self.hwnd
                self.record['launch']['exit_code'] = process.poll()
                self._stop_marker()
                return
            self._wait(.5)
        self.record['launch']['exit_code'] = process.poll()
        if self.record['window_scans']:
            raise RuntimeError('Edge已出现新窗口，但未能核对本轮地址；详见常用Edge启动检查.json的window_scans')
        raise RuntimeError('未发现本轮Edge新窗口；详见常用Edge启动检查.json的启动进程记录')

    def sample(self):
        import win32gui
        if not self._owns_window():
            raise RuntimeError('本轮Edge测试窗口已关闭')
        sample = decode_telemetry(_read_title(self.hwnd), self.nonce, self.allowed_origin)
        seq = sample.get('bridge_seq')
        now = time.monotonic()
        if seq is not None:
            if seq != self._last_seq:
                self._last_seq, self._seq_at = seq, now
            elif now-self._seq_at > 5:
                return {'observation_available': False, 'observation_error': '页面遥测停止更新'}
        return sample

    def select_quality(self):
        """在视频全屏内点击实际清晰度菜单，不打开地址栏、不重置计时。"""
        def current():
            sample = self.sample()
            if self._login_pending(sample):
                self._restore_for_login()
                raise RuntimeError('等待B站登录确认，未操作清晰度菜单')
            if not sample.get('observation_available') or (sample.get('page_state') or {}).get('error'):
                raise RuntimeError('页面观测不可用，未操作清晰度菜单')
            return sample
        sample = current()
        choices = (sample.get('quality') or {}).get('choices', [])
        choice = next((x for x in choices if x.get('value') == '80'), None)
        if not choice:
            raise RuntimeError('页面未提供1080P清晰度选项')
        self._click('清晰度')
        current()  # 菜单点击也可能引出扫码遮罩，不能继续点选项。
        # B站选项可表现为ListItem而非Button。
        window = self._window()
        matches = [c for c in window.descendants() if c.element_info.name.strip() == choice['label'].strip()
                   and c.is_visible() and c.element_info.control_type in ('ListItem', 'MenuItem', 'Button')]
        if len(matches) != 1:
            raise RuntimeError('无法唯一定位1080P选项，保留当前播放并等待下一次检查')
        self._check()
        current()
        matches[0].click_input()

    def prepare(self):
        retries = max(5, min(20, int(self.settings.get('page_retry_count', 5))))
        def install_bridge():
            self._check()
            try:
                self._install_bridge()
                self._check()
                return True
            except InterruptedError:
                raise
            except Exception as exc:
                self.record['last_bridge_error'] = str(exc)
                return False

        def wait_for_login(sample):
            """有页面观测时，未知/游客/遮罩都等待；不把预览或关弹窗当登录。"""
            state = sample.get('page_state') or {}
            if (state.get('login_visible') or
                    (('page_state' in sample or sample.get('observation_available')) and
                     not state.get('error') and self._login_pending(sample))):
                self._restore_for_login()
                self._message('等待B站登录确认，请在普通窗口完成扫码登录；确认后自动设置画质、播放并全屏，可按Ctrl+C取消', True)
                self._wait(1)
                return True
            return False

        def load_page():
            # 初次打开和稍后出现的错误页共享同一预算，不能无限刷新或误当扫码。
            attempts = self.record['page_attempts']
            while len(attempts) < retries+1:
                entry = {'attempt': len(attempts)+1, 'bridge_ready': False}
                attempts.append(entry)
                self._message(f'常用Edge正在打开视频页面（{entry["attempt"]}/{retries+1}），尚未开始测试计时')
                self._check()
                try:
                    self._address(self.url)
                except InterruptedError:
                    raise
                except Exception as exc:
                    entry['navigation_error'] = str(exc)
                    self._wait(1)
                    continue
                deadline = time.monotonic()+20
                last_install = 0
                while time.monotonic() < deadline:
                    self._check()
                    sample = self.sample()
                    state = sample.get('page_state') or {}
                    if (sample.get('observation_available') or state) and (
                            not state.get('error') or state.get('login_visible')):
                        entry['bridge_ready'] = True
                        return
                    if state.get('error'):
                        entry['page_error'] = True
                    elif time.monotonic()-last_install > 3:
                        install_bridge()
                        last_install = time.monotonic()
                    self._wait(.5)
            raise RuntimeError('页面打开和至少5次重新加载均未成功，或浏览器策略阻止页面遥测；未开始测试计时')

        load_page()

        stable_since = None
        controls_at = 0
        quality_at = 0
        speed_at = 0
        missing_at = None
        previous = None
        invalid_since = None
        from windows_temperature_test import online_video_issues
        while True:
            self._check()
            sample = self.sample()
            now = time.monotonic()
            if wait_for_login(sample):
                stable_since = invalid_since = previous = None
                continue
            if (sample.get('page_state') or {}).get('error'):
                self._message('在线视频页面出现错误，正在按本轮剩余次数重新打开，尚未开始计时')
                load_page()
                stable_since = invalid_since = missing_at = previous = None
                controls_at = quality_at = speed_at = 0
                continue
            if not sample.get('observation_available'):
                stable_since = None
                invalid_since = None
                self._message('等待B站页面/扫码登录；不限定等待时间，可按Ctrl+C取消', True)
                if missing_at is None:
                    missing_at = now
                # 页面登录跳转后重新安装；已安装但没有video时不不断抢焦点。
                if 'page_state' not in sample and now-missing_at >= 3:
                    install_bridge()
                    missing_at = now
                self._wait(1)
                continue
            missing_at = None
            if sample['ended']:
                raise RuntimeError('在线视频已经结束，请选择剩余时长足够的测试视频')
            # 登录、画质、正常倍速和实际播放已就绪才放大；打开网页时绝不放大。
            if (not self._playback_maximized and not sample['paused'] and sample['readyState'] >= 3
                    and sample['width'] >= 1920 and sample['height'] >= 1080
                    and sample.get('playback_rate', 1) == 1):
                self._maximize_for_playback()
                self._playback_maximized = True
                self._wait(.2)
                sample = self.sample()
                if wait_for_login(sample):
                    stable_since = invalid_since = previous = None
                    continue
                if not sample.get('observation_available') or (sample.get('page_state') or {}).get('error'):
                    continue
            issues = online_video_issues(sample, previous, require_fullscreen=True)
            previous = sample['position']
            if not issues:
                invalid_since = None
                stable_since = now if stable_since is None else stable_since
                self._message('常用Edge视频已播放并全屏，正在确认至少1080P、30帧持续稳定2秒')
                if now-stable_since >= 2:
                    return sample
            else:
                stable_since = None
                self._message('常用Edge播放准备：' + '；'.join(issues), sample['paused'] or sample['readyState'] < 3)
                if sample['paused'] or sample['readyState'] < 3:
                    invalid_since = None  # 扫码和缓冲不设人不在场的超时。
                elif invalid_since is None:
                    invalid_since = now
                elif now-invalid_since > 40:
                    raise RuntimeError('在线视频未达到测试要求，尚未开始计时：'+'；'.join(issues))
                if sample.get('playback_rate', 1) != 1 and now-speed_at >= 8:
                    speed_at = now
                    try:
                        self._inject('window.__jk808Native.play()')
                        self._wait(.5)
                        sample = self.sample()
                        if wait_for_login(sample):
                            stable_since = invalid_since = previous = None
                            continue
                        if (not sample.get('observation_available') or
                                (sample.get('page_state') or {}).get('error')):
                            continue
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self.record['last_speed_error'] = str(exc)
                if now-quality_at >= 15 and (sample['width'] < 1920 or sample['height'] < 1080):
                    quality_at = now
                    try:
                        self._inject('window.__jk808Native.quality(true)')
                        self._wait(.5)
                        sample = self.sample()
                        # 请求画质可能弹出扫码框或退出全屏，必须按更新后的状态操作。
                        if wait_for_login(sample):
                            stable_since = invalid_since = previous = None
                            continue
                        if (not sample.get('observation_available') or
                                (sample.get('page_state') or {}).get('error')):
                            continue
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self.record['last_quality_error'] = str(exc)
                if now-controls_at >= 8 and (sample['paused'] or not sample.get('fullscreen', {}).get('active')):
                    controls_at = now
                    try:
                        self._inject('window.__jk808Native.controls()')
                        self._wait(.3)
                        sample = self.sample()
                        if wait_for_login(sample):
                            stable_since = invalid_since = previous = None
                            continue
                        if not sample.get('observation_available') or (sample.get('page_state') or {}).get('error'):
                            continue
                        if sample['paused']:
                            self._click('JK808播放')
                            self._wait(.5)
                            sample = self.sample()
                            if wait_for_login(sample):
                                stable_since = invalid_since = previous = None
                                continue
                            if not sample.get('observation_available') or (sample.get('page_state') or {}).get('error'):
                                continue
                        if (not sample['paused'] and sample['readyState'] >= 3
                                and sample['width'] >= 1920 and sample['height'] >= 1080
                                and sample.get('playback_rate', 1) == 1):
                            if not self._playback_maximized:
                                self._maximize_for_playback()
                                self._playback_maximized = True
                                self._wait(.2)
                                sample = self.sample()
                            if wait_for_login(sample):
                                stable_since = invalid_since = previous = None
                                continue
                            if (sample.get('observation_available') and
                                    not (sample.get('page_state') or {}).get('error') and
                                    not sample.get('fullscreen', {}).get('active')):
                                self._click('JK808视频全屏')
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self.record['last_control_error'] = str(exc)
            self.record['observations'].append({'epoch': time.time(), 'sample': sample})
            self.record['observations'] = self.record['observations'][-120:]
            self._wait(1)

    def close(self):
        import win32gui
        errors = []
        try:
            self._stop_marker()
        except Exception as exc:
            self.record['marker_stop_error'] = str(exc)
            errors.append('窗口确认服务停止失败：'+str(exc))
        try:
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                if not self._owns_window():
                    raise RuntimeError('Edge窗口所有权已变化，拒绝关闭用户其他窗口')
                win32gui.PostMessage(self.hwnd, 0x0010, 0, 0)
                deadline = time.monotonic()+8
                while win32gui.IsWindow(self.hwnd) and _window_property(self.hwnd, self.property) == 1:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('本轮Edge窗口未能关闭，请处理窗口提示后重试')
                    time.sleep(.1)
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise RuntimeError('；'.join(errors))

    def save_record(self, error=None):
        folder = self.cfg.get('_diagnostic_folder')
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            if error:
                self.record['error'] = str(error)
            (target/'常用Edge启动检查.json').write_text(json.dumps(self.record, ensure_ascii=False, indent=2), 'utf-8')


def start_online_video(cfg, stop=None, status=None):
    session = NativeEdgeSession(cfg, stop=stop, status=status)
    try:
        session.open()
        last = session.prepare()
        session.record['passed'] = True
        session.save_record()
        return {'adapter': 'native_edge', 'page': session, 'session': session,
                'last': last, 'quality_attempts': {}}
    except BaseException as exc:
        try:
            session.close()
        except Exception as cleanup:
            from windows_temperature_test import WorkloadCleanupError
            raise WorkloadCleanupError(str(exc)+'；常用Edge窗口收尾失败：'+str(cleanup)) from exc
        finally:
            try:
                session.save_record(error=exc)
            except Exception:
                pass
        raise
    finally:
        if status is not None:
            status['waiting_for_user'] = False
