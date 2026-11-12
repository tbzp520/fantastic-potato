"""在联网测试前检查浏览器驱动；不打开网页、不改系统安全策略。"""
import hashlib
import json
from pathlib import Path
import subprocess

BROWSER_DRIVER_SHA256 = 'ba4e6d110e8c1592a1ecd390f6b05f3da124b13871a5be62b341a07a853c6c32'

class BrowserDriverError(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__(details['error'])


def browser_driver_path():
    from playwright._impl._driver import compute_driver_executable
    return Path(compute_driver_executable()[0]).resolve()


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inspect_browser_driver(root, *, run_probe=True):
    root = Path(root).resolve()
    result = {'passed': False, 'file': '', 'sha256': '', 'expected_sha256': '',
              'reason': '', 'error': ''}
    try:
        driver = browser_driver_path()
        result['file'] = str(driver)
        if not driver.is_relative_to(root):
            raise ValueError('浏览器驱动位于当前程序包以外，请核对 PLAYWRIGHT_NODEJS_PATH：'+str(driver))
        expected = {}
        for line in (root/'FILES.sha256').read_text('utf-8-sig').splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                expected[parts[1].lstrip('*').replace('\\', '/')] = parts[0].lower()
        result['expected_sha256'] = expected.get(driver.relative_to(root).as_posix(), '')
        if result['expected_sha256'] != BROWSER_DRIVER_SHA256:
            raise ValueError('本包没有浏览器驱动校验值，请完整解压新版包：'+str(driver))
        result['sha256'] = file_sha256(driver)
        if result['sha256'] != result['expected_sha256']:
            result['reason'] = 'checksum_mismatch'
            result['error'] = '浏览器驱动与原包不一致，尚未执行该文件：'+str(driver)+'。请完整重新解压本包。'
            return result
        if run_probe:
            completed = subprocess.run([str(driver), '--version'], capture_output=True,
                encoding='utf-8', errors='replace', timeout=12,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result.update(exit_code=completed.returncode, stdout=completed.stdout.strip(), stderr=completed.stderr.strip())
            if completed.returncode:
                result['reason'] = 'launch_failed'
                result['error'] = '浏览器驱动启动自检失败：'+str(driver)+'；退出码 '+str(completed.returncode)+'；'+completed.stderr.strip()
                return result
        result['passed'] = True
    except OSError as exc:
        code = getattr(exc, 'winerror', None)
        result.update(winerror=code, reason='application_control' if code == 4556 else 'launch_error')
        result['error'] = ('Windows 应用控制拦截了浏览器驱动：' if code == 4556 else '浏览器驱动无法加载：')+result['file']+'；'+str(exc)
        if code == 4556:
            result['error'] += '。文件校验一致时，请将部署记录中的浏览器组件检查.json交公司IT核对。'
    except subprocess.TimeoutExpired:
        result.update(reason='timeout', error='浏览器驱动 --version 自检超时：'+result['file'])
    except (ValueError, ImportError) as exc:
        result.update(reason='invalid_package', error=str(exc))
    return result


def require_browser_driver(root, record=None):
    result = inspect_browser_driver(root)
    if record is not None:
        try:
            record = Path(record)
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        except OSError as exc:
            result['record_error'] = str(exc)
    if not result['passed']:
        raise BrowserDriverError(result)
    return result
