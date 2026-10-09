import httpx
import pytest
import asyncio

from douyin_summary_mcp.errors import MCPError

VIDEO_ID = '7412345678901234567'


@pytest.mark.asyncio
async def test_detail_total_deadline_closes_slow_stream(monkeypatch):
    from douyin_summary_mcp import douyin
    class SlowStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'{'
            await asyncio.Event().wait()
        async def aclose(self):
            self.closed = True
    stream = SlowStream()
    monkeypatch.setattr(douyin,'REQUEST_DEADLINE_SECONDS',0.01)
    client = douyin.DouyinClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,stream=stream)),resolver=public_dns)
    try:
        with pytest.raises(MCPError) as caught:
            await client.detail(VIDEO_ID)
        assert caught.value.code == 'DOUYIN_REQUEST_TIMEOUT'
        assert stream.closed
    finally:
        await client.close()


async def public_dns(host):
    return ['1.1.1.1']


def detail(**changes):
    value = {'aweme_id':VIDEO_ID,'aweme_type':0,'desc':'教程','create_time':1700000000,
        'author':{'nickname':'作者'},'video':{'duration':12340,
        'play_addr':{'url_list':['https://v3.douyinvod.com/full.mp4']}},
        'music':{'play_url':{'url_list':['https://music.douyinvod.com/music.mp3']}}}
    value.update(changes)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize('link', [f'https://www.douyin.com/video/{VIDEO_ID}',
    f'https://www.douyin.com/?modal_id={VIDEO_ID}',
    '复制此分享 https://v.douyin.com/abcDEF/ 打开抖音'])
async def test_normalize_all_links(link):
    from douyin_summary_mcp.douyin import DouyinClient
    def handler(request):
        return (httpx.Response(302,headers={'location':f'https://www.douyin.com/video/{VIDEO_ID}'})
                if request.url.host == 'v.douyin.com' else httpx.Response(200))
    client = DouyinClient(transport=httpx.MockTransport(handler), resolver=public_dns)
    try:
        assert await client.video_id(link) == VIDEO_ID
    finally:
        await client.close()


def test_full_video_selection_and_millisecond_duration():
    from douyin_summary_mcp.douyin import parse_detail
    result = parse_detail({'status_code':0,'aweme_detail':detail()}, VIDEO_ID)
    assert result['duration_seconds'] == 13
    assert result['video_urls'] == ['https://v3.douyinvod.com/full.mp4']
    assert 'music' not in str(result['video_urls'])


@pytest.mark.parametrize('payload,code', [
    ({'status_code':8},'DOUYIN_LOGIN_REQUIRED'),
    ({'status_code':0,'aweme_detail':None},'DOUYIN_VIDEO_UNAVAILABLE'),
    ({'status_code':4},'DOUYIN_RISK_CONTROL'),
    ({'status_code':0,'aweme_detail':detail(aweme_type=68)},'DOUYIN_VIDEO_ONLY'),
])
def test_platform_errors(payload,code):
    from douyin_summary_mcp.douyin import parse_detail
    with pytest.raises(MCPError) as caught:
        parse_detail(payload,VIDEO_ID)
    assert caught.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['http://www.douyin.com/video/123',
    'https://127.0.0.1/video/123', 'https://www.douyin.com.evil.com/video/123',
    'https://user:pass@www.douyin.com/video/123', 'https://www.douyin.com:444/video/123'])
async def test_unsafe_share_redirect_rejected(target):
    from douyin_summary_mcp.douyin import DouyinClient
    client = DouyinClient(transport=httpx.MockTransport(lambda req:httpx.Response(302,headers={'location':target})),resolver=public_dns)
    try:
        with pytest.raises(MCPError) as caught:
            await client.video_id('https://v.douyin.com/test/')
        assert caught.value.code == 'DOUYIN_URL_REJECTED'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cdn_private_dns_and_cookie_scope(tmp_path):
    from douyin_summary_mcp.douyin import DouyinClient
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200,content=b'video')
    async def private(host):
        return ['127.0.0.1']
    client = DouyinClient(cookie='sessionid=private',transport=httpx.MockTransport(handler),resolver=private)
    try:
        with pytest.raises(MCPError):
            await client.download(['https://v3.douyinvod.com/file.mp4'],tmp_path/'test.mp4')
        assert requests == []
        client.resolver = public_dns
        await client.download(['https://v3.douyinvod.com/file.mp4'],tmp_path/'test.mp4')
        assert 'cookie' not in requests[-1].headers
        assert 'uifid' not in requests[-1].headers
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('host',['v3-dy-o.zjcdn.com','v5-dy-ov-experiment.zjcdn.com'])
async def test_observed_partner_hosts_keep_media_security(host,tmp_path):
    from douyin_summary_mcp.douyin import DouyinClient
    calls=[]
    client=DouyinClient(cookie='sessionid=private; UIFID=private',
        transport=httpx.MockTransport(lambda req:(calls.append(req) or httpx.Response(200,content=b'video'))),
        resolver=public_dns)
    try:
        await client.download(['https://'+host+'/full.mp4'],tmp_path/'video.mp4')
        assert 'cookie' not in calls[-1].headers and 'uifid' not in calls[-1].headers
        for rejected in ['https://evil.zjcdn.com/full.mp4','https://evil.'+host+'/full.mp4',
                         'https://'+host+'.evil.com/full.mp4','http://'+host+'/full.mp4']:
            with pytest.raises(MCPError):await client.check_url(rejected,media=True)
        async def private_dns(_host):return ['169.254.169.254']
        client.resolver=private_dns
        with pytest.raises(MCPError):await client.check_url('https://'+host+'/full.mp4',media=True)
        assert len(calls)==1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_f2_gateway_headers_are_only_sent_to_douyin_web():
    from douyin_summary_mcp.douyin import DouyinClient
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'status_code':0,'aweme_detail':detail()})
    client=DouyinClient(cookie='UIFID_TEMP=dedicated-device; sessionid=private',
        transport=httpx.MockTransport(handler),resolver=public_dns)
    try:
        await client.detail(VIDEO_ID)
        assert calls[-1].headers['x-tt-argus']=='1'
        assert calls[-1].headers['uifid']=='dedicated-device'
        response=await client.open('https://www.iesdouyin.com/share/video/'+VIDEO_ID)
        await response.aclose()
        assert 'uifid' not in calls[-1].headers
        assert 'cookie' not in calls[-1].headers
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dangerous_cdn_redirect_is_not_retried(tmp_path):
    from douyin_summary_mcp.douyin import DouyinClient
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={'location':'https://169.254.169.254/latest/'})
    client = DouyinClient(transport=httpx.MockTransport(handler),resolver=public_dns)
    try:
        with pytest.raises(MCPError) as caught:
            await client.download(['https://v3.douyinvod.com/full.mp4','https://v4.douyinvod.com/full.mp4'],tmp_path/'video.mp4')
        assert caught.value.code == 'DOUYIN_URL_REJECTED'
        assert len(calls) == 1
        assert not (tmp_path/'video.mp4').exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_download_size_limit_removes_partial_file(tmp_path,monkeypatch):
    from douyin_summary_mcp import douyin
    monkeypatch.setattr(douyin,'MAX_VIDEO_BYTES',4)
    client = douyin.DouyinClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'12345')),resolver=public_dns)
    try:
        with pytest.raises(MCPError) as caught:
            await client.download(['https://v3.douyinvod.com/full.mp4'],tmp_path/'video.mp4')
        assert caught.value.code == 'MEDIA_TOO_LARGE'
        assert not (tmp_path/'video.mp4').exists()
    finally:
        await client.close()
