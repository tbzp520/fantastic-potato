"""用纯Node假DOM运行发布脚本；不启动浏览器，不连接网络或测试硬件。"""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


FAKE_DOM = r'''
global.window=global;
global.innerWidth=1920;global.innerHeight=1080;
let now=1000,tick=null,optionClicks=0,fullscreenCalls=0,exitFullscreenCalls=0;
const nodes={};
global.__BiliUser__=Object.hasOwn(CONFIG,'auth')?CONFIG.auth:{cache:{code:0,data:{isLogin:true}}};
global.UserStatus=CONFIG.userStatus;
global.performance={now:()=>now};
global.location={protocol:CONFIG.protocol??'https:',origin:CONFIG.origin??'https://www.bilibili.com'};
global.setInterval=f=>{tick=f;return 1;};
global.clearInterval=()=>{};
global.btoa=s=>Buffer.from(s,'binary').toString('base64');
global.HTMLVideoElement=class {};
function element(spec={}) {
  return {style:{},opacity:spec.opacity??'1',display:spec.display??'block',
    visibility:spec.visibility??'visible',isConnected:spec.connected??true,
    getBoundingClientRect:()=>({left:spec.left??0,top:spec.top??0,
      width:spec.rectWidth??1920,height:spec.rectHeight??1080}),
    remove:function(){if(this.id)delete nodes[this.id];},contains:()=>false};
}
const videos=(CONFIG.videos??[]).map((spec,i)=>{
 const v=Object.assign(new HTMLVideoElement(),element(spec),{
   id:spec.id??('video'+i),paused:spec.paused??false,ended:spec.ended??false,readyState:4,
   currentTime:spec.position??(i+1),videoWidth:spec.width??1920,videoHeight:spec.height??1080,
   playbackRate:spec.playbackRate??1,error:spec.error??null,_requests:0,_callback:null,_playCalls:0,_pauseCalls:0,
   addEventListener:()=>{},play:()=>{v._playCalls++;v.paused=false;return Promise.resolve();},
   pause:()=>{v._pauseCalls++;v.paused=true;},closest:()=>null,
   requestFullscreen:()=>{fullscreenCalls++;document.fullscreenElement={contains:x=>x===v,
     getBoundingClientRect:()=>({width:1920,height:1080})};return Promise.resolve();},
   requestVideoFrameCallback:f=>{v._callback=f;v._requests++;}
 });
 if(spec.noRvfc)delete v.requestVideoFrameCallback;
 return v;
});
const errors=(CONFIG.errors??[]).map(spec=>element({rectWidth:300,rectHeight:100,...spec}));
const logins=(CONFIG.logins??[]).map(spec=>element({rectWidth:300,rectHeight:100,...spec}));
global.getComputedStyle=e=>({display:e.display??'block',visibility:e.visibility??'visible',opacity:e.opacity??'1'});
const q=CONFIG.quality??{selected:'80',label:'1080P'};
const selected={getAttribute:()=>q.selected,textContent:q.label};
const option={getAttribute:()=> '80',textContent:'1080P 高清',click:()=>{optionClicks++;}};
const menu={
 querySelector:s=>s==='li.bpx-state-active'?selected:
   s==='.bpx-player-ctrl-quality-result'?{textContent:q.label}:s.includes('data-value')?option:null,
 querySelectorAll:()=>[option],click:()=>{}
};
const fullIds=CONFIG.fullscreenIds??[];
const full=fullIds.length?{contains:v=>fullIds.includes(v.id),
 getBoundingClientRect:()=>({width:1920,height:1080})}:null;
global.document={
 title:'Offline bridge fixture',readyState:'complete',fullscreenElement:full,
 querySelectorAll:s=>s==='video'?videos:s.includes('bili-mini')?logins:s.includes('error')?errors:[],
 querySelector:s=>s.includes('清晰度')?menu:s.includes('error')?(errors[0]??null):null,
 elementsFromPoint:()=>{
   const order=CONFIG.front??videos.map(v=>v.id).reverse();
   return order.map(id=>videos.find(v=>v.id===id)).filter(Boolean);
 },
 getElementById:id=>nodes[id]??null,
 createElement:()=>({style:{},children:[],setAttribute:()=>{},appendChild:function(n){this.children.push(n);},
   remove:function(){if(this.id)delete nodes[this.id];}}),
 body:{appendChild:n=>{if(n.id)nodes[n.id]=n;}},
 exitFullscreen:()=>{exitFullscreenCalls++;document.fullscreenElement=null;return Promise.resolve();}
};
'''


class NativeEdgeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bundled = ROOT/'runtime/Lib/site-packages/playwright/driver/node.exe'
        cls.node = str(bundled) if bundled.is_file() else shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('需要包内Node或PATH上的Node运行纯DOM回归')
        tree = ast.parse((ROOT/'native_edge.py').read_text('utf-8-sig'))
        cls.bridge = next(ast.literal_eval(n.value) for n in tree.body
                          if isinstance(n, ast.Assign) and any(
                              isinstance(t, ast.Name) and t.id == 'BRIDGE_SCRIPT' for t in n.targets))

    def run_dom(self, configuration, actions=''):
        nonce = 'offline-bridge-nonce'
        code = 'const CONFIG='+json.dumps(configuration)+';\n'+FAKE_DOM
        code += self.bridge.replace('__NONCE__', json.dumps(nonce))+';\n'+actions
        code += r'''
const prefix='JK808_offline-bridge-nonce:';
if(!document.title.startsWith(prefix))throw Error('Bridge did not publish: '+window.__jk808Native?.error);
const payload=JSON.parse(Buffer.from(document.title.slice(prefix.length),'base64').toString('utf8'));
console.log(JSON.stringify({payload,optionClicks,requests:videos.map(v=>v._requests),
  playCalls:videos.map(v=>v._playCalls),pauseCalls:videos.map(v=>v._pauseCalls),
  retained:videos.map(v=>!!v.__jk808NativeFrames),fullscreenCalls,exitFullscreenCalls,
  controlsPresent:!!document.getElementById('jk808-native-controls'),bridgeError:window.__jk808Native?.error??null}));
'''
        result = subprocess.run([self.node, '-e', code], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', timeout=8,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
        data = json.loads(result.stdout)
        self.assertIsNone(data['bridgeError'])
        return data

    def test_transparent_old_video_is_not_the_observed_stream(self):
        data = self.run_dom({'videos':[
            {'id':'old','opacity':'0','width':1280,'height':720,'position':1,'paused':True},
            {'id':'new','width':1920,'height':1080,'position':99}], 'front':['new','old']})
        sample = data['payload']['sample']
        self.assertEqual(sample['position'], 99)
        self.assertEqual((sample['width'], sample['height']), (1920,1080))
        self.assertIs(sample['paused'], False)

    def test_same_size_front_video_wins_over_old_dom_order(self):
        data = self.run_dom({'videos':[{'id':'old','position':1},{'id':'new','position':99}],
                             'front':['new','old']})
        self.assertEqual(data['payload']['sample']['position'], 99)

    def test_fullscreen_video_wins_over_a_larger_background_video(self):
        data = self.run_dom({'videos':[
            {'id':'old','position':1,'rectWidth':1920,'rectHeight':1080},
            {'id':'new','position':99,'rectWidth':1280,'rectHeight':720}],
            'fullscreenIds':['new'], 'front':['old','new']})
        sample = data['payload']['sample']
        self.assertEqual(sample['position'], 99)
        self.assertIs(sample['fullscreen']['contains_video'], True)

    def test_selected_1080_label_does_not_hide_actual_720_stream(self):
        data = self.run_dom({'videos':[{'width':1280,'height':720}],
                             'quality':{'selected':'80','label':'1080P'}},
                            'window.__jk808Native.quality();')
        self.assertEqual(data['optionClicks'], 1)

    def test_actual_1080_or_higher_does_not_get_downgraded(self):
        for width,height,label,selected in ((1920,1080,'1080P','80'),(3840,2160,'4K','120')):
            with self.subTest(label=label):
                data = self.run_dom({'videos':[{'width':width,'height':height}],
                                     'quality':{'selected':selected,'label':label}},
                                    'window.__jk808Native.quality();')
                self.assertEqual(data['optionClicks'], 0)

    def test_explicit_quality_repair_can_reselect_even_if_label_is_already_1080(self):
        data = self.run_dom({'videos':[{}]}, 'window.__jk808Native.quality(true);')
        self.assertEqual(data['optionClicks'], 1)

    def test_detached_and_reattached_video_registers_frame_callback_again(self):
        actions = '''const v=videos[0];v.isConnected=false;
v._callback(1000,{mediaTime:1,presentedFrames:1});
v.isConnected=true;v.currentTime=2;tick();'''
        data = self.run_dom({'videos':[{}]}, actions)
        self.assertEqual(data['requests'][0], 2)
        self.assertEqual(data['payload']['sample']['observed_fps'], 0)

    def test_hidden_error_template_does_not_mark_page_failed(self):
        for style in ({'display':'none'},{'visibility':'hidden'},{'opacity':'0'}):
            with self.subTest(style=style):
                data = self.run_dom({'videos':[], 'errors':[style]})
                self.assertIs(data['payload']['page_state']['error'], False)

    def test_visible_error_page_is_reported(self):
        data = self.run_dom({'videos':[], 'errors':[{}]})
        self.assertIs(data['payload']['page_state']['error'], True)

    def test_visible_error_overlay_is_reported_even_if_video_still_exists(self):
        data = self.run_dom({'videos':[{}], 'errors':[{}]})
        self.assertIsNotNone(data['payload']['sample'])
        self.assertIs(data['payload']['page_state']['error'], True)

    def test_media_error_is_reported_without_an_error_dom_node(self):
        data = self.run_dom({'videos':[{'error':{'code':3}}]})
        self.assertIs(data['payload']['page_state']['error'], True)

    def test_missing_frame_callback_api_does_not_fabricate_30fps(self):
        data = self.run_dom({'videos':[{'noRvfc':True}]}, 'now+=6000;videos[0].currentTime+=6;tick();')
        sample = data['payload']['sample']
        self.assertEqual(sample['observed_fps'], 0)
        self.assertEqual(sample['frame_span_s'], 0)
        self.assertIsNone(sample['frame_age_s'])
        self.assertEqual(data['requests'][0], 0)

    def test_measured_fps_retains_a_separate_age_when_callbacks_stop(self):
        actions = '''const v=videos[0];
v._callback(1000,{mediaTime:0,presentedFrames:1});now=2000;
v._callback(2000,{mediaTime:1,presentedFrames:31});now=9000;tick();'''
        sample = self.run_dom({'videos':[{}]}, actions)['payload']['sample']
        self.assertEqual(sample['observed_fps'], 30)
        self.assertEqual(sample['frame_span_s'], 1)
        self.assertEqual(sample['frame_age_s'], 7)

    def test_backward_media_frame_counter_cannot_reuse_old_good_fps(self):
        actions = '''const v=videos[0];
v._callback(1000,{mediaTime:0,presentedFrames:1});now=2000;
v._callback(2000,{mediaTime:1,presentedFrames:31});now=2100;
v._callback(2100,{mediaTime:0.01,presentedFrames:2});tick();'''
        sample = self.run_dom({'videos':[{}]}, actions)['payload']['sample']
        self.assertEqual(sample['observed_fps'], 0)
        self.assertEqual(sample['frame_span_s'], 0)

    def test_already_playing_video_returns_to_normal_speed_without_pause_or_seek(self):
        data = self.run_dom({'videos':[{'paused':False,'playbackRate':2,'position':123.5}]},
                            'window.__jk808Native.play();tick();')
        sample = data['payload']['sample']
        self.assertEqual(sample['playback_rate'], 1)
        self.assertEqual(sample['position'], 123.5)
        self.assertIs(sample['paused'], False)
        self.assertEqual(data['playCalls'], [0])
        self.assertEqual(data['pauseCalls'], [0])

    def test_paused_video_is_requested_to_play_at_normal_speed_without_seek(self):
        data = self.run_dom({'videos':[{'paused':True,'playbackRate':2,'position':42.5}]},
                            'window.__jk808Native.play();tick();')
        sample = data['payload']['sample']
        self.assertEqual(sample['playback_rate'], 1)
        self.assertEqual(sample['position'], 42.5)
        self.assertIs(sample['paused'], False)
        self.assertEqual(data['playCalls'], [1])
        self.assertEqual(data['pauseCalls'], [0])

    def test_ended_video_is_not_restarted_or_rewound_by_play_request(self):
        data = self.run_dom({'videos':[{'paused':True,'ended':True,'playbackRate':2,'position':1200}]},
                            'window.__jk808Native.play();tick();')
        sample = data['payload']['sample']
        self.assertEqual(sample['playback_rate'], 1)
        self.assertEqual(sample['position'], 1200)
        self.assertIs(sample['ended'], True)
        self.assertEqual(data['playCalls'], [0])
        self.assertEqual(data['pauseCalls'], [0])

    def test_login_confirmation_comes_from_the_loaded_page_boolean_only(self):
        for auth, expected in (({'cache':{'code':0,'data':{'isLogin':True}}}, True),
                               ({'cache':{'code':0,'data':{'isLogin':False}}}, False),
                               ({'cache':{'code':-101}}, False),
                               ({'cache':{'code':500}}, None),
                               ({'cache':None}, None),
                               ({'cache':{'code':0,'data':{'isLogin':'true'}}}, None)):
            with self.subTest(auth=auth):
                data = self.run_dom({'auth':auth,'videos':[{}]})
                self.assertIs(data['payload']['page_state']['login_confirmed'], expected)

    def test_unconfirmed_or_guest_cannot_change_quality_play_or_create_controls(self):
        for auth in (None, {}, {'cache':None}, {'cache':{'code':0,'data':{'isLogin':False}}}):
            with self.subTest(auth=auth):
                data = self.run_dom({'auth':auth,'videos':[{'paused':True,'playbackRate':2}]},
                    'window.__jk808Native.quality(true);window.__jk808Native.play();window.__jk808Native.controls();tick();')
                self.assertEqual(data['optionClicks'], 0)
                self.assertEqual(data['playCalls'], [0])
                self.assertEqual(data['payload']['sample']['playback_rate'], 2)
                self.assertFalse(data['controlsPresent'])

    def test_old_user_status_boolean_is_supported_only_when_shared_page_cache_is_absent(self):
        data = self.run_dom({'auth':None,'userStatus':{'userInfo':{'isLogin':True}},'videos':[{}]})
        self.assertIs(data['payload']['page_state']['login_confirmed'], True)
        for auth in ({'cache':None}, {'cache':{'code':-101}}, {'cache':{'code':500}}):
            with self.subTest(auth=auth):
                data = self.run_dom({'auth':auth,'userStatus':{'userInfo':{'isLogin':True}},'videos':[{}]})
                self.assertIsNot(data['payload']['page_state']['login_confirmed'], True)

    def test_visible_login_mask_overrides_good_cached_login_and_playing_preview(self):
        data = self.run_dom({'videos':[{}],'logins':[{}]},
            'window.__jk808Native.quality(true);window.__jk808Native.play();window.__jk808Native.controls();tick();')
        self.assertIs(data['payload']['page_state']['login_confirmed'], False)
        self.assertTrue(data['payload']['page_state']['login_visible'])
        self.assertEqual(data['payload']['page_state']['login_source'], 'login_overlay')
        self.assertEqual(data['optionClicks'], 0)
        self.assertFalse(data['controlsPresent'])

    def test_hiding_login_mask_does_not_confirm_a_guest(self):
        data = self.run_dom({'auth':{'cache':{'code':0,'data':{'isLogin':False}}},'logins':[{}],'videos':[{}]},
            'logins[0].display="none";tick();')
        self.assertFalse(data['payload']['page_state']['login_visible'])
        self.assertIs(data['payload']['page_state']['login_confirmed'], False)

    def test_login_success_and_logout_are_observed_without_calling_account_methods(self):
        actions = '''let gets=0;window.__BiliUser__={cache:{code:0,data:{isLogin:true}},
get:()=>{gets++;throw Error('must not fetch account');},refresh:()=>{gets++;throw Error('must not refresh account');}};
tick();if(gets)throw Error('account method called');'''
        data = self.run_dom({'auth':None,'videos':[{}]}, actions)
        self.assertIs(data['payload']['page_state']['login_confirmed'], True)
        data = self.run_dom({'videos':[{}]}, 'window.__BiliUser__.cache={code:-101};tick();')
        self.assertIs(data['payload']['page_state']['login_confirmed'], False)

    def test_quality_triggering_login_blocks_the_next_play_in_the_same_js_turn(self):
        actions = '''option.click=()=>{optionClicks++;logins.push(element({rectWidth:300,rectHeight:100}));};
window.__jk808Native.quality(true);window.__jk808Native.play();window.__jk808Native.controls();tick();'''
        data = self.run_dom({'videos':[{'paused':True}]}, actions)
        self.assertEqual(data['optionClicks'], 1)
        self.assertEqual(data['playCalls'], [0])
        self.assertFalse(data['controlsPresent'])

    def test_queued_fullscreen_click_rechecks_login_before_requesting_fullscreen(self):
        actions = '''window.__jk808Native.controls();
const button=nodes['jk808-native-controls'].children.find(b=>b.textContent==='JK808视频全屏');
logins.push(element({rectWidth:300,rectHeight:100}));button.onclick();tick();'''
        data = self.run_dom({'videos':[{}]}, actions)
        self.assertEqual(data['fullscreenCalls'], 0)
        self.assertFalse(data['controlsPresent'])

    def test_new_login_mask_exits_existing_fullscreen(self):
        actions = 'logins.push(element({rectWidth:300,rectHeight:100}));tick();'
        data = self.run_dom({'videos':[{'id':'main'}],'fullscreenIds':['main']}, actions)
        self.assertEqual(data['exitFullscreenCalls'], 1)
        self.assertFalse(data['payload']['sample']['fullscreen']['active'])

    def test_non_bilibili_local_fixture_does_not_require_a_bilibili_account(self):
        for protocol,origin in (('http:','http://127.0.0.1:54321'),('file:','null')):
            with self.subTest(protocol=protocol):
                data = self.run_dom({'protocol':protocol,'origin':origin,'auth':None,'videos':[{'paused':True}]},
                    'window.__jk808Native.play();tick();')
                self.assertIs(data['payload']['page_state']['login_confirmed'], True)
                self.assertEqual(data['playCalls'], [1])


if __name__ == '__main__':
    unittest.main()
