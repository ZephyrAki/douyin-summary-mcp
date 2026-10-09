"""Narrow Douyin public-video adapter; no browser or login-state harvesting."""
from __future__ import annotations

import asyncio
import math
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from .errors import MCPError
from .urlguard import Resolver, default_resolve_ips, _ip_is_global, _domain_matches
from .vendor.f2.abogus import ABogus, BrowserFingerprintGenerator

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0')
WEB_DOMAINS = ('douyin.com', 'iesdouyin.com')
MEDIA_DOMAINS = ('douyinvod.com', 'douyin.com', 'iesdouyin.com', 'bytecdn.cn', 'bytevcloud.com')
# Exact partner hosts observed in authenticated native Douyin detail responses.
PARTNER_MEDIA_HOSTS = frozenset({'v3-dy-o.zjcdn.com', 'v5-dy-ov-experiment.zjcdn.com'})
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_DETAIL_BYTES = 8 * 1024 * 1024
REQUEST_DEADLINE_SECONDS = 60


def parse_detail(payload: dict, video_id: str) -> dict:
    code = payload.get('status_code', 0)
    if code in (8, 9):
        raise MCPError('DOUYIN_LOGIN_REQUIRED', '该作品需要专用抖音 Cookie。')
    if code != 0:
        raise MCPError('DOUYIN_RISK_CONTROL', '抖音拒绝了详情请求或触发风控。')
    item = payload.get('aweme_detail')
    if not isinstance(item, dict) or str(item.get('aweme_id')) != video_id:
        raise MCPError('DOUYIN_VIDEO_UNAVAILABLE', '作品已删除、私密或无法公开访问。')
    if item.get('images') or item.get('aweme_type') in (68, 150):
        raise MCPError('DOUYIN_VIDEO_ONLY', '首版仅支持单个视频，不支持图文轮播。')
    video = item.get('video') or {}
    urls = (video.get('play_addr') or {}).get('url_list') or []
    if not urls:
        for variant in video.get('bit_rate') or []:
            urls = (variant.get('play_addr') or {}).get('url_list') or []
            if urls:
                break
    # Never inspect music.play_url: it can contain only background music.
    urls = [url for url in urls if isinstance(url, str)]
    duration_ms = video.get('duration', item.get('duration'))
    if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
        raise MCPError('INVALID_VIDEO_DURATION', '抖音详情没有有效的视频时长。')
    if not urls:
        raise MCPError('DOUYIN_VIDEO_UNAVAILABLE', '详情没有可用的完整视频地址。')
    return {'platform':'douyin', 'video_id':video_id, 'part':'1',
            'title':str(item.get('desc') or ''),
            'author':str((item.get('author') or {}).get('nickname') or ''),
            'published_at':item.get('create_time'),
            'duration_seconds':math.ceil(duration_ms / 1000), 'video_urls':urls}


class DouyinClient:
    def __init__(self, cookie: str | None = None, *, transport=None, resolver: Resolver = default_resolve_ips):
        self.cookie = cookie
        self.resolver = resolver
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=False, transport=transport,
                                       headers={'User-Agent':UA})

    async def close(self):
        await self.client.aclose()

    async def check_url(self, url: str, *, media: bool = False):
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or '').lower()
            domains = MEDIA_DOMAINS if media else WEB_DOMAINS
            allowed_host = any(_domain_matches(host,d) for d in domains) or (media and host in PARTNER_MEDIA_HOSTS)
            if (parsed.scheme != 'https' or parsed.username or parsed.password or
                    parsed.port not in (None,443) or not allowed_host):
                raise ValueError
        except ValueError:
            raise MCPError('DOUYIN_URL_REJECTED', '抖音地址未通过 HTTPS/主机/端口校验。') from None
        try:
            addresses = await self.resolver(host)
        except Exception:
            raise MCPError('DOUYIN_DNS_FAILED', '抖音域名解析失败。') from None
        if not addresses or not all(_ip_is_global(str(ip)) for ip in addresses):
            raise MCPError('DOUYIN_URL_REJECTED', '抖音域名解析到非公网地址。')

    async def open(self, url: str, *, media: bool = False) -> httpx.Response:
        for _ in range(6):
            await self.check_url(url, media=media)
            host = (urlparse(url).hostname or '').lower()
            headers = {'Referer':'https://www.douyin.com/'}
            if not media and _domain_matches(host,'douyin.com'):
                # Pinned F2 GatewayHeaderManager: the native gateway requires this header.
                headers['x-tt-argus'] = '1'
                if self.cookie:
                    headers['Cookie'] = self.cookie
                    cookies = dict(pair.strip().split('=',1) for pair in self.cookie.split(';') if '=' in pair)
                    if uifid := cookies.get('UIFID') or cookies.get('UIFID_TEMP'):
                        headers['uifid'] = uifid
            self.client.cookies.clear()
            request = self.client.build_request('GET',url,headers=headers)
            # Do not allow a web Set-Cookie to leak to media or another platform host.
            self.client.cookies.clear()
            response = await self.client.send(request,stream=True,follow_redirects=False)
            self.client.cookies.clear()
            if response.status_code in (301,302,303,307,308):
                location = response.headers.get('location')
                await response.aclose()
                if not location:
                    raise MCPError('DOUYIN_URL_REJECTED','跳转缺少目标地址。')
                url = urljoin(url,location)
                continue
            return response
        raise MCPError('DOUYIN_URL_REJECTED','抖音跳转次数超过限制。')

    @staticmethod
    def _id(url: str) -> str | None:
        parsed = urlparse(url)
        match = re.search(r'/(?:video|share/video)/(\d{8,24})(?:/|$)',parsed.path)
        candidate = match[1] if match else (parse_qs(parsed.query).get('modal_id') or [None])[0]
        return candidate if candidate and re.fullmatch(r'\d{8,24}',candidate) else None

    async def video_id(self, share_link: str) -> str:
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                return await self._video_id(share_link)
        except TimeoutError:
            raise MCPError('DOUYIN_REQUEST_TIMEOUT','抖音链接解析超过总时限。') from None

    async def _video_id(self, share_link: str) -> str:
        if len(share_link) > 8192:
            raise MCPError('DOUYIN_INVALID_LINK','分享内容过长。')
        urls = re.findall(r'https?://[^\s<>"\u3000]+',share_link)
        if len(urls) != 1:
            raise MCPError('DOUYIN_INVALID_LINK','请提供包含一个抖音视频链接的分享内容。')
        url = urls[0].rstrip('，。；！）)')
        await self.check_url(url)
        if video_id := self._id(url):
            return video_id
        response = await self.open(url)
        try:
            response.raise_for_status()
            video_id = self._id(str(response.url))
            if not video_id:
                raise MCPError('DOUYIN_INVALID_LINK','链接未指向单个抖音视频。')
            return video_id
        finally:
            await response.aclose()

    async def detail(self, video_id: str) -> dict:
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                return await self._detail(video_id)
        except TimeoutError:
            raise MCPError('DOUYIN_REQUEST_TIMEOUT','抖音详情请求超过总时限。') from None

    async def _detail(self, video_id: str) -> dict:
        if not re.fullmatch(r'\d{8,24}',video_id):
            raise MCPError('DOUYIN_INVALID_LINK','视频 ID 无效。')
        # Native detail parameters follow the pinned F2 BaseRequestModel.
        params = dict(device_platform='webapp',aid='6383',channel='channel_pc_web',
            pc_client_type='1',publish_video_strategy_type='2',pc_libra_divert='Windows',
            version_code='290100',version_name='29.1.0',cookie_enabled='true',
            screen_width='1920',screen_height='1080',browser_language='zh-CN',
            browser_platform='Win32',browser_name='Edge',browser_version='130.0.0.0',
            browser_online='true',engine_name='Blink',engine_version='130.0.0.0',
            os_name='Windows',os_version='10',cpu_core_num='12',device_memory='8',
            platform='PC',downlink='10',effective_type='4g',round_trip_time='100',aweme_id=video_id)
        if self.cookie:
            for pair in self.cookie.split(';'):
                name, _, value = pair.strip().partition('=')
                if name == 'msToken':
                    params['msToken'] = value
        query = urlencode(params)
        signer = ABogus(fp=BrowserFingerprintGenerator.generate_fingerprint('Edge'),user_agent=UA)
        signature = signer.generate_abogus(query)[1]
        url = 'https://www.douyin.com/aweme/v1/web/aweme/detail/?' + query + '&' + urlencode({'a_bogus':signature})
        response = await self.open(url)
        try:
            if response.status_code in (401,403):
                raise MCPError('DOUYIN_LOGIN_REQUIRED','详情访问被拒绝，可能需要专用 Cookie。')
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_DETAIL_BYTES:
                    raise MCPError('DOUYIN_DETAIL_TOO_LARGE','详情响应超过限制。')
            try:
                payload = __import__('json').loads(data)
            except ValueError:
                raise MCPError('DOUYIN_RISK_CONTROL','详情返回验证页面或空响应。') from None
            return parse_detail(payload,video_id)
        finally:
            await response.aclose()

    async def download(self, urls: list[str], target: Path) -> Path:
        try:
            async with asyncio.timeout(600):
                for url in urls:
                    response = await self.open(url,media=True)
                    try:
                        if response.status_code in (403,404,410,500,502,503,504):
                            continue
                        response.raise_for_status()
                        if int(response.headers.get('content-length','0')) > MAX_VIDEO_BYTES:
                            raise MCPError('MEDIA_TOO_LARGE','视频超过 512 MiB 限制。')
                        total = 0
                        with target.open('wb') as file:
                            os.chmod(target,0o600)
                            async for chunk in response.aiter_bytes(256*1024):
                                total += len(chunk)
                                if total > MAX_VIDEO_BYTES:
                                    raise MCPError('MEDIA_TOO_LARGE','视频超过 512 MiB 限制。')
                                file.write(chunk)
                        if total:
                            return target
                    finally:
                        await response.aclose()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        target.unlink(missing_ok=True)
        raise MCPError('DOUYIN_DOWNLOAD_FAILED','完整视频下载失败。')
