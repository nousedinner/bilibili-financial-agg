"""B站 API 客户端：请求签名、独立会话、错误分类与有限退避。"""

import asyncio
import hashlib
import json as _json
from http.cookies import SimpleCookie, CookieError
import os
import subprocess
import time
import urllib.parse
from datetime import datetime
from typing import Callable, Optional

import httpx
from curl_cffi.requests import AsyncSession as CurlSession

from src.config import get_config, get_env
from src.lifecycle import WorkflowError, failure_reason
from src.request_policy import TransportRouter, active_transport, request_policy

# The selected transport profile does not guarantee acceptance by the upstream.
_curl_session = TransportRouter()


class BiliAPIError(Exception):
    """B站 API 返回错误码时抛出（code != 0）。"""

    def __init__(self, code: int, message: str, result: dict | None = None, retry_after: float = 0):
        super().__init__(f"BiliAPIError(code={code}, msg={message})")
        self.code = code
        self.message = message
        self.result = result or {}
        self.reason = failure_reason(self)
        self.retry_after = retry_after


# ------------------------------------------------------------------
# curl 统一请求（含超时回收）
# ------------------------------------------------------------------

async def _run_curl(args: list[str], timeout: float = 20, sep_body_status: bool = False) -> tuple[bytes, int]:
    """统一发送请求；访问拦截保持为显式错误，不能解释成无字幕。"""
    # 从 curl 参数中解析出 url / headers / cookies
    url = ""
    headers = dict(_HEADERS)
    cookies: dict[str, str] = {}
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("-w", "--write-out"):
            i += 2; continue
        if a in ("-sS", "--compressed", "-s"):
            i += 1; continue
        if a == "--connect-timeout" or a == "--max-time":
            i += 2; continue
        if a == "-H" and i + 1 < len(args):
            val = args[i + 1]
            k, _, v = val.partition(":")
            headers[k.strip()] = v.strip()
            i += 2; continue
        if a in ("-b", "--cookie") and i + 1 < len(args):
            for part in args[i + 1].split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
            i += 2; continue
        if not a.startswith("-"):
            url = a
        i += 1

    try:
        await request_policy().before(url)
        resp = await _curl_session.get(
            url, headers=headers, cookies=cookies,
            timeout=timeout, allow_redirects=not bool(cookies),
        )
        if not 200 <= resp.status_code < 300:
            request_policy().observe(url, resp.status_code)
            raise BiliAPIError(resp.status_code, f"HTTP {resp.status_code}")
        return resp.content, resp.status_code
    except BiliAPIError:
        raise
    except WorkflowError as exc:
        raise BiliAPIError(-352 if exc.reason == "risk_blocked" else 412,
                           "Upstream cooldown active", retry_after=exc.delay) from None
    except Exception as e:
        raise BiliAPIError(-1, f"Transport failed ({type(e).__name__})") from None


def _bili_curl_args(url: str, sessdata: str = "", cookie_extra: str = "", sep: bool = False) -> list[str]:
    """构建 B站 API curl 命令行参数。"""
    cmd = ["curl", "-sS", "--compressed", "--connect-timeout", "10", "--max-time", "20"]
    for key, value in _HEADERS.items():
        cmd += ["-H", f"{key}: {value}"]
    if sep:
        cmd += ["-w", "\n%{http_code}"]
    parts = []
    if sessdata:
        parts.append(f"SESSDATA={sessdata}")
    if cookie_extra:
        parts.append(cookie_extra)
    if parts:
        cmd += ["-b", "; ".join(parts)]
    cmd.append(url)
    return cmd


# WBI mixin key lookup table (B站混淆表)
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_HEADERS = {
    # Let curl_cffi use the User-Agent matching its selected transport profile.
    "Referer": "https://www.bilibili.com/",
}


class BiliClient:
    """B站 API 客户端，自带WBI签名和反限速。"""

    def __init__(self, sessdata: str = "", *, credential_version: int = 1, cookie_extra: str = "", auth_observer=None):
        self._sessdata = sessdata
        self.credential_version = credential_version
        self._cookie_extra = cookie_extra
        self._auth_observer = auth_observer
        self._transport = CurlSession(impersonate="chrome")
        self._transport_token = None
        self._client = httpx.AsyncClient(
            headers={**_HEADERS},
            timeout=30,
            follow_redirects=True,
        )
        self._img_key: str = ""
        self._sub_key: str = ""
        self._wbi_ts: float = 0  # last refresh timestamp
        self._buvid3: str = ""
        self._subtitle_cache = {}

    async def __aenter__(self):
        self._transport_token = active_transport.set(self._transport)
        return self

    async def __aexit__(self, *args):
        try:
            await self.close()
        finally:
            if self._transport_token is not None:
                active_transport.reset(self._transport_token)

    async def close(self):
        await self._client.aclose()
        await self._transport.close()

    def _args(self, url, *, authenticated=True, extra=""):
        cookies = "; ".join(value for value in (self._cookie_extra if authenticated else "", extra) if value)
        return _bili_curl_args(url, self._sessdata if authenticated else "", cookie_extra=cookies)

    async def _observe(self, state, reason=None):
        if self._auth_observer is not None:
            await self._auth_observer(self.credential_version, state, reason)

    def _login_cookie_values(self):
        values = {}
        for cookie in self._transport.cookies.jar:
            domain = cookie.domain.lstrip('.').lower()
            if domain == 'bilibili.com' or domain.endswith('.bilibili.com'):
                if cookie.name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
                    values[cookie.name] = cookie.value
        return values

    async def resolve_login_callback(self, callback_url, refresh_token=''):
        """Exchange an official one-use ticket and preserve raw Set-Cookie values."""
        def checked(url):
            try:
                parsed = urllib.parse.urlsplit(url)
                safe = (parsed.scheme == 'https' and parsed.hostname in {
                    'passport.bilibili.com', 'account.bilibili.com', 'passport.biligame.com'}
                    and parsed.port in (None, 443) and parsed.username is None
                    and parsed.password is None and not parsed.fragment)
            except (TypeError, ValueError):
                safe = False
            if not safe:
                raise BiliAPIError(502, 'Untrusted login callback')
            return parsed

        parsed = checked(callback_url)
        query = urllib.parse.parse_qs(parsed.query)
        values = self._login_cookie_values()
        for name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
            if not values.get(name) and query.get(name):
                values[name] = query[name][0]
        if all(values.get(name) for name in ('SESSDATA', 'bili_jct', 'DedeUserID')):
            return {'sessdata': values['SESSDATA'], 'bili_jct': values['bili_jct'],
                'dedeuserid': values['DedeUserID'], 'refresh_token': refresh_token}
        current = callback_url
        for _ in range(5):
            parsed = checked(current)
            if parsed.path != '/x/passport-login/web/crossDomain':
                raise BiliAPIError(502, 'Unsupported login exchange')
            from src.lifecycle import owner_context
            ticket = owner_context.get()
            if ticket is not None:
                await ticket.check()
            try:
                await request_policy().before(current)
            except WorkflowError as exc:
                raise BiliAPIError(-352, 'Login exchange cooldown active', retry_after=exc.delay) from None
            try:
                response = await self._transport.get(current, headers=dict(_HEADERS),
                    timeout=15, allow_redirects=False)
            except Exception:
                raise BiliAPIError(-1, 'Login exchange transport unavailable') from None
            if response.status_code not in (200, 301, 302, 303, 307, 308):
                request_policy().observe(current, response.status_code)
                raise BiliAPIError(response.status_code, 'Login exchange temporarily unavailable')
            values.update(self._login_cookie_values())
            # crossDomain may issue cookies for another official SSO domain.
            # Read this already-validated response without altering raw values;
            # final nav/UID verification remains mandatory before activation.
            lines = response.headers.get_list('set-cookie') if hasattr(response.headers, 'get_list') else [response.headers.get('set-cookie', '')]
            for line in lines:
                parsed_cookies = SimpleCookie()
                try:
                    parsed_cookies.load(line)
                except CookieError:
                    continue
                for name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
                    if name in parsed_cookies:
                        values[name] = parsed_cookies[name].value
            if all(values.get(name) for name in ('SESSDATA', 'bili_jct', 'DedeUserID')):
                return {'sessdata': values['SESSDATA'], 'bili_jct': values['bili_jct'],
                    'dedeuserid': values['DedeUserID'], 'refresh_token': refresh_token}
            location = response.headers.get('location')
            if response.status_code == 200 or not location:
                break
            current = urllib.parse.urljoin(current, location)
        raise BiliAPIError(502, 'Login exchange returned no complete credential')

    # ------------------------------------------------------------------
    # WBI 签名
    # ------------------------------------------------------------------

    async def _refresh_wbi_keys(self):
        """从B站nav接口获取最新的img_key和sub_key。使用 curl 获取。"""
        now = time.time()
        if self._img_key and (now - self._wbi_ts) < 600:  # cache 10min
            return

        url = "https://api.bilibili.com/x/web-interface/nav"
        try:
            body, _ = await _run_curl(_bili_curl_args(url, self._sessdata), timeout=10)
            data = _json.loads(body)
        except Exception as e:
            print(f"[bili] WBI keys curl error: {e}")
            return
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

        # Extract keys from URLs: .../xxx{key}.jpg
        self._img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
        self._sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""
        self._wbi_ts = now

    def _get_mixin_key(self, raw: str) -> str:
        """Apply the mixin key enc table to produce the 32-char signing key."""
        if len(raw) != 64:
            raise BiliAPIError(-1, "WBI signing keys unavailable")
        return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]

    async def sign_params(self, params: dict) -> dict:
        """Add wbi signature (wts + w_rid) to params dict."""
        await self._refresh_wbi_keys()

        wbi_key = self._get_mixin_key(self._img_key + self._sub_key)
        params["wts"] = int(time.time())

        # Sort params, filter unsafe chars, encode
        filtered = {
            k: "".join(c for c in str(v) if c not in "!'()*")
            for k, v in sorted(params.items())
        }
        query = urllib.parse.urlencode(filtered)
        w_rid = hashlib.md5((query + wbi_key).encode()).hexdigest()

        filtered["w_rid"] = w_rid
        return filtered

    # ------------------------------------------------------------------
    # 通用请求（带退避）
    # ------------------------------------------------------------------

    async def _get(
        self,
        url: str,
        params: dict = None,
        signed: bool = True,
        consume_retry_attempt: Optional[Callable[[], bool]] = None,
    ) -> dict:
        if signed and params is not None:
            params = await self.sign_params(dict(params))
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        backoff = get_config().get("limits", {}).get("backoff_base_seconds", 10)
        for attempt in range(4):
            try:
                body, _ = await _run_curl(self._args(url,
                    extra=f"buvid3={self._buvid3}" if self._buvid3 else ""), timeout=25)
                result = _json.loads(body)
                if not isinstance(result, dict) or "code" not in result:
                    raise BiliAPIError(-1, "Malformed API response")
                code = result["code"]
                if code == 0:
                    return result
                request_policy().observe(url, code)
                if code == -101:
                    await self._observe("invalid", "auth_required")
                raise BiliAPIError(code, str(result.get("message", "API failure")))
            except BiliAPIError as exc:
                if exc.code not in (-1, 408, 500, 502, 503, 504) or attempt == 3:
                    raise
                if consume_retry_attempt is not None and not consume_retry_attempt():
                    raise BiliAPIError(-1, "Request retry budget exhausted") from exc
            except (TimeoutError, OSError, ValueError) as exc:
                if attempt == 3:
                    raise BiliAPIError(-1, f"Request failed: {type(exc).__name__}") from exc
                if consume_retry_attempt is not None and not consume_retry_attempt():
                    raise BiliAPIError(-1, "Request retry budget exhausted") from exc
            await asyncio.sleep(backoff * 2 ** attempt)
        raise BiliAPIError(-1, "Request retries exhausted")

    # ------------------------------------------------------------------
    # 视频列表
    # ------------------------------------------------------------------

    async def get_video_list(
        self,
        mid: int,
        page: int = 1,
        page_size: int = 30,
        consume_retry_attempt: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """获取用户视频列表 (WBI signed)。

        Returns: {"list": {"vlist": [...]}, "page": {...}}
        """
        params = {
            "mid": mid,
            "ps": page_size,
            "pn": page,
            "order": "pubdate",
            "keyword": "",
            "tid": 0,
        }
        result = await self._get(
            "https://api.bilibili.com/x/space/wbi/arc/search",
            params=params,
            signed=True,
            consume_retry_attempt=consume_retry_attempt,
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise BiliAPIError(-1, "Missing API data")
        if not isinstance(data.get("list"), dict) or not isinstance(data["list"].get("vlist"), list):
            raise BiliAPIError(-1, "Malformed video list")
        return data

    # ------------------------------------------------------------------
    # 动态列表
    # ------------------------------------------------------------------

    async def get_dynamics(self, mid: int, offset: str = "") -> dict:
        await self._ensure_buvid3()
        result = await self._get("https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space",
            {"host_mid": mid, "offset": offset, "timezone_offset": "-480"}, signed=False)
        data = result.get("data")
        if not isinstance(data, dict):
            raise BiliAPIError(-1, "Missing dynamic feed data")
        return data

    async def _ensure_buvid3(self):
        """获取buvid3设备标识cookie。使用 curl 获取。"""
        if self._buvid3:
            return
        try:
            body, _ = await _run_curl(
                _bili_curl_args("https://api.bilibili.com/x/frontend/finger/spi"),
                timeout=10,
            )
            data = _json.loads(body).get("data", {})
            self._buvid3 = data.get("b_3", "")
        except Exception:
            self._buvid3 = ""

    # ------------------------------------------------------------------
    # 字幕获取
    # ------------------------------------------------------------------

    async def get_subtitle_url(self, bvid: str, cid: int) -> Optional[str]:
        """Return an AI Chinese track; propagate auth/risk/transport failures."""
        for sub in await self._subtitle_tracks(bvid, cid):
            if sub.get("lan") == "ai-zh":
                return self._subtitle_url(sub)
        return None

    async def get_cc_subtitle(self, bvid: str, cid: int) -> Optional[str]:
        """Human and AI tracks share one authenticated metadata request."""
        for sub in await self._subtitle_tracks(bvid, cid):
            if sub.get("lan", "").startswith("zh"):
                return self._subtitle_url(sub)
        return None

    async def _subtitle_tracks(self, bvid, cid):
        key = (bvid, cid)
        if key not in self._subtitle_cache:
            result = await self._get("https://api.bilibili.com/x/player/wbi/v2",
                                     {"bvid": bvid, "cid": cid}, signed=True)
            data = result.get("data")
            if not isinstance(data, dict):
                raise BiliAPIError(-1, "Malformed subtitle metadata")
            if data.get("need_login_subtitle") is True:
                # This means access requires authentication, not proof of session expiry.
                raise BiliAPIError(-101, "Subtitle requires authentication")
            tracks = data.get("subtitle", {}).get("subtitles")
            if not isinstance(tracks, list):
                raise BiliAPIError(-1, "Malformed subtitle tracks")
            self._subtitle_cache[key] = tracks
        return self._subtitle_cache[key]

    @staticmethod
    def _subtitle_url(sub):
        url = urllib.parse.urljoin("https://www.bilibili.com", str(sub.get("subtitle_url") or ""))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or not any(
            parsed.hostname == host or parsed.hostname.endswith("." + host)
            for host in ("hdslb.com", "bilibili.com", "bilivideo.com")
        ) or parsed.username or parsed.password:
            raise BiliAPIError(-1, "Invalid subtitle URL")
        return url

    # ------------------------------------------------------------------
    # 视频详情
    # ------------------------------------------------------------------

    async def get_video_info(self, bvid: str) -> dict:
        """获取视频详情（cid、时长）— 使用 pagelist 端点绕过 view 的412。"""
        url = f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}"
        try:
            body, _ = await _run_curl(_bili_curl_args(url), timeout=10)
            data = _json.loads(body)
        except BiliAPIError:
            raise
        except Exception as e:
            raise BiliAPIError(-1, f"pagelist request failed: {e}")
        if data.get("code") != 0:
            request_policy().observe(url, data.get("code", -1))
            raise BiliAPIError(data.get("code", -1), data.get("message", "pagelist error"))
        pages = data.get("data", [])
        if not pages:
            raise BiliAPIError(-1, f"No pages for {bvid}")
        first = pages[0]
        # 返回兼容格式：cid + pages 列表（含 duration）
        return {
            "cid": first.get("cid"),
            "duration": first.get("duration", 0),
            "pages": pages,
        }

    async def get_video_aid(self, bvid: str) -> int:
        """获取视频的aid（用于评论接口）。使用view端点。"""
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        try:
            body, _ = await _run_curl(_bili_curl_args(url), timeout=10)
            data = _json.loads(body)
            if data.get("code") == 0:
                return data.get("data", {}).get("aid", 0) or 0
            request_policy().observe(url, data.get("code", -1))
            raise BiliAPIError(data.get("code", -1), data.get("message", "view error"))
        except BiliAPIError:
            raise
        except Exception as e:
            print(f"[bilibili] get_video_aid({bvid}) failed: {e}")
        return 0

    # ------------------------------------------------------------------
    # 音频下载（ASR用）
    # ------------------------------------------------------------------

    async def get_audio_url(self, bvid: str, cid: int) -> Optional[str]:
        """获取音频流URL。"""
        params = {"bvid": bvid, "cid": cid, "fnval": 16}
        result = await self._get(
            "https://api.bilibili.com/x/player/playurl",
            params=params,
            signed=False,
        )
        dash = result.get("data", {}).get("dash", {})
        audio_list = dash.get("audio", [])
        if audio_list:
            return audio_list[0].get("baseUrl") or audio_list[0].get("base_url")
        return None

    async def download_audio(self, url: str) -> str:
        import tempfile
        limit = int(get_config().get("asr", {}).get("max_audio_bytes", 256 * 1024 * 1024))
        # #5: 返回临时文件路径而非bytes，避免全量驻留内存
        r = await _curl_session.get(url, headers=_HEADERS, timeout=120, stream=True)
        tmp_path = None
        transferred = False
        try:
            r.raise_for_status()
            fd, tmp_path = tempfile.mkstemp(suffix=".audio")
            downloaded = 0
            with os.fdopen(fd, "wb") as f:
                async for chunk in r.aiter_content(chunk_size=64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > limit:
                        raise ValueError(f"Audio exceeds configured download limit ({limit} bytes)")
                    f.write(chunk)
            await r.aclose()
            transferred = True
            return tmp_path
        finally:
            try:
                if not transferred:
                    await r.aclose()
            finally:
                if tmp_path and not transferred:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------

    async def get_comments(self, oid: int, oid_type: int = 1, count: int = 20) -> dict:
        """获取评论列表。

        oid_type: 1=视频, 17=动态, 11=图文
        Returns {"replies": list, "total": int}.
        """
        params = {
            "oid": oid,
            "type": oid_type,
            "sort": 2,  # 按热度
            "pn": 1,
            "ps": min(count, 20),
        }
        result = await self._get(
            "https://api.bilibili.com/x/v2/reply",
            params=params,
            signed=False,
        )
        data = result.get("data", {})
        replies = data.get("replies", []) or []
        total = data.get("page", {}).get("count", len(replies))
        return {"replies": replies[:count], "total": total}

    # ------------------------------------------------------------------
    # 弹幕
    # ------------------------------------------------------------------

    async def get_danmaku(self, cid: int, max_count: int = 2000) -> dict:
        import random
        import xml.etree.ElementTree as ET
        body, _ = await _run_curl(_bili_curl_args(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"), timeout=25)
        entries = [node.text or "" for node in ET.fromstring(body).findall("d")]
        total = len(entries)
        sample = random.sample(entries, max_count) if total > max_count else entries
        return {"items": sample, "total": total}

    # ------------------------------------------------------------------
    # Cookie验证
    # ------------------------------------------------------------------

    async def validate_sessdata(self) -> dict:
        """验证SESSDATA是否有效。使用 curl 获取。

        Returns: {"valid": bool, "username": str, "expire_date": str}
        """
        if not self._sessdata:
            return {"valid": False, "state": "invalid", "reason": "missing_credential", "username": "", "expire_date": ""}

        try:
            body, _ = await _run_curl(
                self._args("https://api.bilibili.com/x/web-interface/nav"),
                timeout=10,
            )
            result = _json.loads(body)
            data = result.get("data") or {}
            code = result.get("code")
        except Exception as e:
            return {"valid": None, "state": "unknown", "reason": failure_reason(e), "username": "", "expire_date": ""}
        if code == -101 or (code == 0 and data.get("isLogin") is False):
            state, is_login = "invalid", False
        elif code == 0 and data.get("isLogin") is True:
            state, is_login = "valid", True
        else:
            return {"valid": None, "state": "unknown", "reason": "unexpected_response", "username": "", "expire_date": ""}
        uname = data.get("uname", "")

        # Parse expire from SESSDATA timestamp
        # SESSDATA format: xxx,timestamp,xxx
        expire_date = ""
        try:
            parts = urllib.parse.unquote(self._sessdata).split(",")
            if len(parts) >= 2:
                ts = int(parts[1])
                expire_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            pass

        return {"valid": is_login, "state": state, "reason": None if is_login else "auth_required", "username": uname, "expire_date": expire_date}

    # ------------------------------------------------------------------
    # 用户搜索
    # ------------------------------------------------------------------

    async def search_user(self, keyword: str) -> Optional[dict]:
        """通过用户名搜索B站用户，返回第一个匹配结果。

        使用 wbi/search/all/v2 接口（比 search/type 更稳定，不易被412）。
        Returns: {"mid": int, "name": "", "sign": ""} 或 None
        """
        params = {
            "keyword": keyword,
        }
        result = await self._get(
            "https://api.bilibili.com/x/web-interface/wbi/search/all/v2",
            params=params,
            signed=True,
        )

        if result.get("code") != 0:
            print(f"[bili] search_user failed: {result}")
            return None

        # 从 result 列表中找 bili_user 类型的结果
        for r in result.get("data", {}).get("result", []):
            if r.get("result_type") == "bili_user":
                users = r.get("data", [])
                if users:
                    user = users[0]
                    return {
                        "mid": user.get("mid"),
                        "name": user.get("uname", ""),
                        "sign": user.get("usign", ""),
                    }
        return None

    async def get_followings(self, mid: int, ps: int = 50) -> list:
        ps = max(1, min(ps, 50))
        items, seen = [], set()
        for page in range(1, 501):
            result = await self._get("https://api.bilibili.com/x/relation/followings",
                {"vmid": mid, "pn": page, "ps": ps, "order": "desc"}, signed=False)
            data = result.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise BiliAPIError(-1, "Incomplete followings response")
            batch = data["list"]
            fresh = [item for item in batch if item.get("mid") not in seen]
            if batch and not fresh:
                raise BiliAPIError(-1, "Followings pagination repeated")
            items.extend(fresh)
            seen.update(item["mid"] for item in fresh)
            total = data.get("total")
            if total is not None and len(items) >= total:
                return items
            if len(batch) < ps:
                if total is not None and len(items) < total:
                    raise BiliAPIError(-1, "Followings response was truncated")
                return items
        raise BiliAPIError(-1, "Followings pagination limit exceeded")
