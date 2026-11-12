"""换机工具定位：临时文件及系统API桩，不访问真实硬件、注册表或KnownFolders。"""
import copy
from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import portable_tools as tools
import environment_setup as setup
import windows_temperature_test as app
import 集中联调 as combined


class VideoLocationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.drive=self.root.drive
        self.desktop=self.root/'已重定向 OneDrive/桌面'
        self.videos=self.root/'实际视频'
        self.documents=self.root/'实际文档'
        for directory in (self.desktop,self.videos,self.documents):directory.mkdir(parents=True)
        self.roots=[self.desktop,self.videos,self.documents]

    def video(self,root,name='原本视频.mp4'):
        result=root/name;result.parent.mkdir(parents=True,exist_ok=True);result.write_bytes(b'unit-video-only')
        return result

    def test_existing_system_disk_path_is_kept_without_searching_or_mutation(self):
        path=self.video(self.videos)
        local={'file':str(path),'operator_value':42};before=copy.deepcopy(local)
        with patch.object(tools,'video_search_roots',side_effect=AssertionError('must not search')):
            result=tools.rebind_local_video(local,drive=self.drive)
        self.assertFalse(result['rebound']);self.assertEqual(local,before)

    def test_known_folders_returns_actual_redirected_user_locations(self):
        found=dict(zip(tools.KNOWN_FOLDERS.values(),self.roots))
        with patch.object(tools,'known_folder',side_effect=lambda identifier:found[identifier]), \
             patch.object(tools,'system_drive',return_value=self.drive):
            roots,errors=tools.video_search_roots()
        self.assertEqual(roots[:3],self.roots);self.assertEqual(errors,[])
        self.assertEqual(roots[3],Path(self.drive+'/JK808_Windows/media'))

    def test_unique_same_named_file_on_redirected_desktop_is_rebound(self):
        expected=self.video(self.desktop/'测试素材')
        self.video(self.videos,'其他视频.mp4')
        local={'file':str(self.root/'旧账户/原本视频.mp4'),'ui':{'kept':True}}
        result=tools.rebind_local_video(local,roots=self.roots,drive=self.drive)
        self.assertTrue(result['rebound']);self.assertEqual(Path(local['file']),expected.resolve())
        self.assertEqual(local['ui'],{'kept':True})
        self.assertEqual(result['filename'],'原本视频.mp4')

    def test_multiple_exact_names_are_listed_and_original_configuration_is_preserved(self):
        first=self.video(self.desktop);second=self.video(self.videos)
        local={'file':str(self.root/'失效/原本视频.mp4')};before=copy.deepcopy(local)
        with self.assertRaises(RuntimeError) as raised:
            tools.rebind_local_video(local,roots=self.roots,drive=self.drive)
        text=str(raised.exception)
        self.assertIn(str(first),text);self.assertIn(str(second),text);self.assertIn('多个',text)
        self.assertEqual(local,before)

    def test_missing_name_does_not_pick_an_unrelated_video(self):
        self.video(self.desktop,'其他视频.mp4');local={'file':str(self.root/'失效/原本视频.mp4')}
        with self.assertRaisesRegex(RuntimeError,'没有找到同名文件'):
            tools.rebind_local_video(local,roots=self.roots,drive=self.drive)

    def test_non_system_roots_are_never_scanned(self):
        usb=self.root/'模拟U盘';usb.mkdir();self.video(usb)
        result=tools.find_named_video('原本视频.mp4',[usb],drive='Z:')
        self.assertEqual(result['candidates'],[]);self.assertEqual(result['searched_roots'],[])
        self.assertEqual(result['excluded_non_system_roots'],[str(usb.resolve())])

    def test_search_is_bounded_in_depth_and_does_not_walk_entire_folders(self):
        self.video(self.desktop/'一级/二级/三级')
        result=tools.find_named_video('原本视频.mp4',[self.desktop],drive=self.drive,max_depth=2)
        self.assertEqual(result['candidates'],[])

    def test_scan_limit_prevents_claiming_a_unique_candidate_from_incomplete_search(self):
        self.video(self.desktop)
        self.video(self.desktop,'第二个.mp4')
        result=tools.find_named_video('原本视频.mp4',[self.desktop],drive=self.drive,max_entries=1)
        self.assertTrue(result['scan_errors'])

    def test_known_folder_failure_is_reported_and_does_not_rebind_from_partial_results(self):
        self.video(self.desktop);local={'file':str(self.root/'失效/原本视频.mp4')};before=copy.deepcopy(local)
        with patch.object(tools,'video_search_roots',return_value=([self.desktop],['Videos：KnownFolder失败'])):
            with self.assertRaisesRegex(RuntimeError,'未完整检查'):
                tools.rebind_local_video(local,drive=self.drive)
        self.assertEqual(local,before)

    def test_unfilled_original_filename_requires_an_explicit_path(self):
        for value in ('','C:/'):
            with self.subTest(value=value),self.assertRaisesRegex(RuntimeError,'不能猜选'):
                tools.rebind_local_video({'file':value},roots=self.roots,drive=self.drive)

    def test_known_folder_api_returns_unicode_redirected_path_and_frees_buffer(self):
        actual=self.desktop
        def query(guid,flags,token,out):
            out._obj.value=str(actual)
            return 0
        query_api=Mock(side_effect=query);free=Mock()
        shell=SimpleNamespace(SHGetKnownFolderPath=query_api)
        ole=SimpleNamespace(CoTaskMemFree=free)
        with patch.object(tools.ctypes,'WinDLL',side_effect=lambda name,**_:shell if name=='shell32' else ole):
            found=tools.known_folder(tools.KNOWN_FOLDERS['Desktop'])
        self.assertEqual(found,actual);query_api.assert_called_once();free.assert_called_once()

    def test_known_folder_api_failure_does_not_fall_back_to_a_guessed_user(self):
        query=Mock(return_value=-2147024894)
        with patch.object(tools.ctypes,'WinDLL',return_value=SimpleNamespace(SHGetKnownFolderPath=query)):
            with self.assertRaisesRegex(OSError,'KnownFolder查询失败'):
                tools.known_folder(tools.KNOWN_FOLDERS['Desktop'])


class BitLocationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)

    def executable(self,name):
        result=self.root/name/'bit.exe';result.parent.mkdir();result.write_bytes(b'unit fixture, never executable')
        return result

    def test_existing_supported_executable_is_preserved_without_registry_or_search(self):
        path=self.executable('current');bit={'executable':str(path),'configuration':'manual.bitcfg'}
        before=copy.deepcopy(bit)
        with patch.object(tools,'bit_installation_candidates',side_effect=AssertionError('must not search')):
            result=tools.rebind_bit_executable(bit,lambda _: [9,0,1003,0])
        self.assertFalse(result['rebound']);self.assertEqual(bit,before)

    def test_existing_other_version_is_reported_without_replacing_or_installing_it(self):
        path=self.executable('current');bit={'executable':str(path)};before=copy.deepcopy(bit)
        with patch.object(tools,'bit_installation_candidates') as search:
            with self.assertRaisesRegex(RuntimeError,'不是已适配'):
                tools.rebind_bit_executable(bit,lambda _: [10,2,1,0])
        search.assert_not_called();self.assertEqual(bit,before)

    def test_one_supported_installed_candidate_rebinds_missing_old_path(self):
        old=self.executable('version10');supported=self.executable('known9')
        bit={'executable':str(self.root/'missing/bit.exe'),'operator_value':42}
        with patch.object(tools,'bit_installation_candidates',return_value=[old,supported]):
            result=tools.rebind_bit_executable(bit,lambda p:[9,0,1003,0] if p==supported else [10,0,1,0])
        self.assertTrue(result['rebound']);self.assertEqual(bit['executable'],str(supported))
        self.assertEqual(bit['operator_value'],42);self.assertEqual(len(result['checked']),2)

    def test_two_supported_installations_are_not_guessed(self):
        paths=[self.executable('A'),self.executable('B')];bit={'executable':'missing.exe'}
        with patch.object(tools,'bit_installation_candidates',return_value=paths):
            with self.assertRaises(RuntimeError) as raised:
                tools.rebind_bit_executable(bit,lambda _: [9,0,1003,0])
        self.assertIn('不唯一',str(raised.exception))
        for path in paths:self.assertIn(str(path),str(raised.exception))
        self.assertEqual(bit,{'executable':'missing.exe'})

    def test_no_executable_or_only_unreadable_versions_never_become_verified(self):
        path=self.executable('unreadable');bit={'executable':'missing.exe'}
        with patch.object(tools,'bit_installation_candidates',return_value=[path]),self.assertRaises(RuntimeError):
            tools.rebind_bit_executable(bit,Mock(side_effect=OSError('version unreadable')))
        self.assertEqual(bit,{'executable':'missing.exe'})

    def test_candidate_collection_is_limited_to_standard_install_roots_and_registry(self):
        with patch.dict(tools.os.environ,{'ProgramFiles':str(self.root/'pf64'),'ProgramFiles(x86)':str(self.root/'pf32')}), \
             patch.object(tools,'registered_bit_candidates',return_value=[self.root/'registered/bit.exe']):
            paths=tools.bit_installation_candidates()
        self.assertEqual(len(paths),7)
        self.assertIn((self.root/'registered/bit.exe').resolve(),paths)
        self.assertTrue(all(p.name=='bit.exe' for p in paths))

    def test_registry_only_reads_app_path_and_burnintest_install_location(self):
        class Key:
            def __init__(self,name):self.name=name
            def __enter__(self):return self
            def __exit__(self,*_):pass
        def open_key(parent,name,*_):
            if isinstance(parent,Key):return Key(name)
            return Key('app' if name.endswith('bit.exe') else 'uninstall')
        def query(key,name):
            values={('app',''):str(self.root/'registered/bit.exe'),
                    ('other','DisplayName'):'Other Program',
                    ('bit','DisplayName'):'BurnInTest Professional',
                    ('bit','InstallLocation'):str(self.root/'installation'),
                    ('empty','DisplayName'):'BurnInTest Professional',('empty','InstallLocation'):''}
            return values[(key.name,name)],1
        registry=SimpleNamespace(HKEY_LOCAL_MACHINE=1,HKEY_CURRENT_USER=2,KEY_READ=1,
            KEY_WOW64_64KEY=2,KEY_WOW64_32KEY=4,OpenKey=Mock(side_effect=open_key),
            QueryValueEx=Mock(side_effect=query),EnumKey=Mock(side_effect=lambda key,index:
                ['other','bit','empty'][index] if index<3 else (_ for _ in ()).throw(OSError('no more'))))
        with patch.dict(sys.modules,{'winreg':registry}):
            found=tools.registered_bit_candidates()
        self.assertEqual(set(found),{self.root/'registered/bit.exe',self.root/'installation/bit.exe'})
        self.assertFalse(any(p==Path('bit.exe') for p in found))


class HostPreparationTests(unittest.TestCase):
    def configuration(self):
        cfg=json.loads((ROOT/'config.example.json').read_text('utf-8-sig'))
        cfg['report'].update(project='人工项目',batch='人工批次',test_version='人工版本')
        cfg['hwinfo']['usb']={'volume_serial':'EXISTING-USB'}
        return cfg

    def test_platform_and_all_missing_tools_are_collected_before_any_load(self):
        cfg=self.configuration();before=copy.deepcopy(cfg)
        def platform(updated):
            updated['report'].update(platform='新电脑',cpu='新CPU',os='新系统')
            return {'platform':'新电脑','cpu':'新CPU','os':'新系统','bios':'NEWBIOS'}
        with ExitStack() as stack:
            stack.enter_context(patch('sys.stdout',io.StringIO()))
            stack.enter_context(patch('serial.tools.list_ports.comports',return_value=[]))
            stack.enter_context(patch.object(app,'refresh_test_platform',side_effect=platform,create=True))
            stack.enter_context(patch('portable_tools.rebind_local_video',side_effect=RuntimeError('同名素材未找到')))
            stack.enter_context(patch('portable_tools.rebind_bit_executable',side_effect=RuntimeError('没有BIT9')))
            stack.enter_context(patch.object(app,'resolve_hwinfo_executable',side_effect=RuntimeError('USB未找到')))
            stack.enter_context(patch.object(app,'refresh_system_disk',side_effect=RuntimeError('盘身份读取失败')))
            stack.enter_context(patch.object(app,'network_snapshot',return_value=[]))
            save=stack.enter_context(patch.object(app,'save_setup_config'))
            loads=stack.enter_context(patch.object(app,'execute_stage',side_effect=AssertionError('不得施加负载')))
            network=stack.enter_context(patch.object(app,'apply_network_state',side_effect=AssertionError('不得改网卡')))
            updated,issues=combined.prepare_configuration(cfg,'host')
        for label in ('USB互连线','本地视频','BIT配置','U盘HWiNFO','系统盘身份','在线视频网卡'):
            self.assertTrue(any(value.startswith(label+'：') for value in issues),label)
        self.assertEqual(updated['report']['platform'],'新电脑')
        self.assertEqual(updated['hwinfo']['usb'],before['hwinfo']['usb'])
        for key in ('project','batch','test_version'):self.assertEqual(updated['report'][key],before['report'][key])
        self.assertEqual(cfg,before)
        save.assert_called_once();self.assertEqual(save.call_args.args[2]['issues'],issues)
        self.assertEqual(save.call_args.args[2]['checks']['电脑平台身份']['bios'],'NEWBIOS')
        loads.assert_not_called();network.assert_not_called()

    def test_existing_com_binding_is_case_insensitive_and_ambiguity_still_refuses(self):
        self.assertEqual(setup.select_ftdi_port(' com8 ',['COM8','COM9']),'COM8')
        self.assertEqual(setup.select_ftdi_port('old',['COM8','com8']),'COM8')
        with self.assertRaisesRegex(RuntimeError,'唯一FTDI'):
            setup.select_ftdi_port('old',['COM8','COM9'])

    def test_real_config_save_keeps_original_backup_before_rebinding(self):
        cfg=self.configuration()
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            root=Path(directory);desktop=root/'重定向桌面';desktop.mkdir()
            video=desktop/'same.mp4';video.write_bytes(b'test-only-video')
            bit=root/'bit.exe';bit.write_bytes(b'test-only-executable')
            cfg['local_video']['file']=str(root/'old-user/same.mp4')
            cfg['burnintest']['executable']=str(bit)
            config_path=root/'config.json';original=json.dumps(cfg,ensure_ascii=False).encode('utf-8')
            config_path.write_bytes(original)
            stack.enter_context(patch.object(app,'ROOT',root));stack.enter_context(patch.object(app,'CONFIG',config_path))
            stack.enter_context(patch('sys.stdout',io.StringIO()))
            stack.enter_context(patch('serial.tools.list_ports.comports',return_value=[SimpleNamespace(device='COM8',vid=0x0403)]))
            stack.enter_context(patch.object(app,'refresh_test_platform',return_value={'bios':'TEST'},create=True))
            stack.enter_context(patch.object(tools,'video_search_roots',return_value=([desktop],[])))
            stack.enter_context(patch.object(app,'bit_program_version',return_value=[9,0,1003,0]))
            stack.enter_context(patch('接入并验证BIT.confirmed_bit_configuration',side_effect=copy.deepcopy))
            stack.enter_context(patch.object(app,'resolve_hwinfo_executable',return_value=root/'HWiNFO32.exe'))
            stack.enter_context(patch.object(app,'refresh_system_disk'))
            stack.enter_context(patch.object(app,'network_snapshot',return_value=[{'InterfaceGuid':cfg['network']['online_adapter_guid']}]))
            updated,issues=combined.prepare_configuration(cfg,'host')
            self.assertEqual(issues,[])
            backups=list((root/'部署记录').rglob('修改前配置.json'))
            self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),original)
            saved=json.loads(config_path.read_text('utf-8-sig'))
            self.assertEqual(saved['local_video']['file'],str(video.resolve()))
            self.assertEqual(updated['serial']['host_port'],'COM8')
            for key in ('project','batch','test_version'):self.assertEqual(saved['report'][key],cfg['report'][key])


if __name__=='__main__':unittest.main()
