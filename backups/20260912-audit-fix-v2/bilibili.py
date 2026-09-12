"""B站 API 客户端 — WBI签名、视频列表、动态列表、反限速。"""

import asyncio
import hashlib
import json as _json
import subprocess
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import httpx
from curl_cffi.requests import AsyncSession as CurlSession

from src.config import get_config, get_env

# curl_cffi 会话复用，伪装 Chrome TLS 指纹绕过 412
_curl_session = CurlSession(impersonate="chrome")


class BiliAPIError(Exception):
    """B站 API 返回错误码时抛出（code != 0）。"""

    def __init__(self, code: int, message: str, result: dict | None = None):
        super().__init__(f"BiliAPIError(code={code}, msg={message})")
        self.code = code
        self.message = message
        self.result = result or {}


# ------------------------------------------------------------------
# curl 统一请求（含超时回收）
# ------------------------------------------------------------------

async def _run_curl(args: list[str], timeout: float = 20, sep_body_status: bool = False) -> tuple[bytes, int]:
    """用 curl_cffi 伪装 Chrome TLS 指纹发送请求，绕过 B 站 412 风控。"""
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
        resp = await _curl_session.get(
            url, headers=headers, cookies=cookies,
            timeout=timeout, allow_redirects=True,
        )
        if not 200 <= resp.status_code < 300:
            raise BiliAPIError(resp.status_code, f"HTTP {resp.status_code}")
        return resp.content, resp.status_code
    except BiliAPIError:
        raise
    except Exception as e:
        raise BiliAPIError(-1, f"curl_cffi request failed: {e}") from e


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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}


class BiliClient:
    """B站 API 客户端，自带WBI签名和反限速。"""

    def __init__(self, sessdata: str = ""):
        self._sessdata = sessdata
        self._client = httpx.AsyncClient(
            headers={**_HEADERS},
            timeout=30,
            follow_redirects=True,
        )
        self._img_key: str = ""
        self._sub_key: str = ""
        self._wbi_ts: float = 0  # last refresh timestamp
        self._buvid3: str = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self._client.aclose()

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

    async def _get(self, url: str, params: dict = None, signed: bool = True) -> dict:
        if signed and params is not None:
            params = await self.sign_params(dict(params))
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        backoff = get_config().get("limits", {}).get("backoff_base_seconds", 10)
        for attempt in range(4):
            try:
                body, _ = await _run_curl(_bili_curl_args(url, self._sessdata,
                    cookie_extra=f"buvid3={self._buvid3}" if self._buvid3 else ""), timeout=25)
                result = _json.loads(body)
                if not isinstance(result, dict) or "code" not in result:
                    raise BiliAPIError(-1, "Malformed API response")
                code = result["code"]
                if code == 0:
                    return result
                raise BiliAPIError(code, str(result.get("message", "API failure")))
            except BiliAPIError as exc:
                if exc.code not in (-1, -412, -352, 412, 429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except (TimeoutError, OSError, ValueError) as exc:
                if attempt == 3:
                    raise BiliAPIError(-1, f"Request failed: {type(exc).__name__}") from exc
            await asyncio.sleep(backoff * 2 ** attempt)
        raise BiliAPIError(-1, "Request retries exhausted")

    # ------------------------------------------------------------------
    # 视频列表
    # ------------------------------------------------------------------

    async def get_video_list(self, mid: int, page: int = 1, page_size: int = 30) -> dict:
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
        """获取AI中文字幕URL（需SESSDATA + WBI签名）。使用 curl 获取。

        Returns subtitle URL or None.
        """
        if not self._sessdata:
            return None

        # 需要WBI签名
        params = {"bvid": bvid, "cid": cid}
        signed_params = await self.sign_params(params)
        query = urllib.parse.urlencode(signed_params)
        url = f"https://api.bilibili.com/x/player/wbi/v2?{query}"
        
        try:
            body, _ = await _run_curl(
                _bili_curl_args(url, self._sessdata), timeout=10,
            )
            data = _json.loads(body).get("data", {})
        except Exception as e:
            print(f"[bili] AI字幕 curl error {bvid}: {e}")
            return None
        subtitles = data.get("subtitle", {}).get("subtitles", [])

        # Prefer ai-zh, then any Chinese
        for sub in subtitles:
            if sub.get("lan") == "ai-zh":
                return urllib.parse.urljoin("https://www.bilibili.com", sub["subtitle_url"])
        for sub in subtitles:
            if sub.get("lan", "").startswith("zh"):
                return urllib.parse.urljoin("https://www.bilibili.com", sub["subtitle_url"])
        return None

    async def get_cc_subtitle(self, bvid: str, cid: int) -> Optional[str]:
        """获取CC字幕URL（不需要cookie）。使用 curl 获取。

        Returns subtitle URL or None.
        """
        url = f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
        try:
            body, _ = await _run_curl(_bili_curl_args(url), timeout=10)
            data = _json.loads(body).get("data", {})
        except Exception as e:
            print(f"[bili] CC字幕 curl error {bvid}: {e}")
            return None
        subtitles = data.get("subtitle", {}).get("subtitles", [])

        for sub in subtitles:
            if sub.get("lan", "").startswith("zh"):
                return urllib.parse.urljoin("https://www.bilibili.com", sub["subtitle_url"])
        return None

    # ------------------------------------------------------------------
    # 视频详情
    # ------------------------------------------------------------------

    async def get_video_info(self, bvid: str) -> dict:
        """获取视频详情（cid、时长）— 使用 pagelist 端点绕过 view 的412。"""
        url = f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}"
        try:
            body, _ = await _run_curl(_bili_curl_args(url), timeout=10)
            data = _json.loads(body)
        except Exception as e:
            raise BiliAPIError(-1, f"pagelist request failed: {e}")
        if data.get("code") != 0:
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

    async def download_audio(self, url: str) -> bytes:
        limit = int(get_config().get("asr", {}).get("max_audio_bytes", 256 * 1024 * 1024))
        # Issue #7: 流式下载——传输过程中检查大小，避免大文件全部读入内存
        r = await _curl_session.get(url, headers=_HEADERS, timeout=120, stream=True)
        r.raise_for_status()
        chunks = []
        downloaded = 0
        try:
            async for chunk in r.aiter_content(chunk_size=64 * 1024):
                downloaded += len(chunk)
                if downloaded > limit:
                    raise ValueError(f"Audio exceeds configured download limit ({limit} bytes)")
                chunks.append(chunk)
        finally:
            await r.aclose()
        return b"".join(chunks)

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
            return {"valid": False, "username": "", "expire_date": ""}

        try:
            body, _ = await _run_curl(
                _bili_curl_args(
                    "https://api.bilibili.com/x/web-interface/nav",
                    self._sessdata,
                ),
                timeout=10,
            )
            data = _json.loads(body).get("data", {})
        except Exception as e:
            print(f"[bili] validate_sessdata curl error: {e}")
            return {"valid": False, "username": "", "expire_date": ""}
        is_login = data.get("isLogin", False)
        uname = data.get("uname", "")

        # Parse expire from SESSDATA timestamp
        # SESSDATA format: xxx,timestamp,xxx
        expire_date = ""
        try:
            parts = self._sessdata.split(",")
            if len(parts) >= 2:
                ts = int(parts[1])
                expire_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            pass

        return {"valid": is_login, "username": uname, "expire_date": expire_date}

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
